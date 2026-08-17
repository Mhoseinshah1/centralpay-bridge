"""PostgreSQL integration coverage for `centralpay reconcile` (scenario 14).

Only real PostgreSQL proves the read-only lookup and reference_id-collision
SELECT queries behave correctly under the database's own types/constraints
(BigInteger order ids, unique reference_id, CHECK constraints) without ever
provoking a write or a DataError -- SQLite is not evidence here (see
tests/integration/test_reference_id_pg.py's module docstring for the same
rationale). Also proves `reconcile` never mutates a row on real PostgreSQL,
matching the SQLite-backed proof in tests/test_cli_reconcile.py.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import concurrent.futures
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from app.centralpay import CentralPayClient
from app.cli import main as cli_main
from app.models import Base, Payment, PaymentStatus
from app.services.verification import verify_and_settle
from tests.conftest import CentralPayStub, build_app, create_order, get_payment, verify_ok_response

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

_TABLES = (
    "admin_alerts",
    "worker_heartbeats",
    "payment_events",
    "payments",
    "centralpay_payer_identities",
    "fee_policies",
    "alembic_version",
)


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    Base.metadata.create_all(pg_engine)
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def pg_app(settings, pg_session_factory):
    stub = CentralPayStub()
    application = build_app(settings, pg_session_factory, stub)
    application.state.centralpay_stub = stub
    yield application
    application.state.centralpay.close()


@pytest.fixture
def cli_env(settings, pg_session_factory, monkeypatch):
    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "Settings", lambda: settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: pg_session_factory)
    return settings


def _patch_centralpay_client(monkeypatch, stub) -> None:
    import app.cli as cli_module

    def factory(*, base_url, getlink_api_key, verify_api_key, timeout_seconds):
        return CentralPayClient(
            base_url=base_url,
            getlink_api_key=getlink_api_key,
            verify_api_key=verify_api_key,
            timeout_seconds=timeout_seconds,
            transport=httpx.MockTransport(stub.handler),
        )

    monkeypatch.setattr(cli_module, "CentralPayClient", factory)


def test_reconcile_lookup_by_bot_order_id_and_numeric_gateway_order_id_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, capsys
):
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(client, settings, order_id="pg-reconcile-lookup", amount=5000)
        assert response.status_code == 200
    payment = get_payment(pg_session_factory, "pg-reconcile-lookup")

    assert cli_main(["reconcile", "pg-reconcile-lookup"]) == 0
    out_by_bot_order = capsys.readouterr().out
    assert "pg-reconcile-lookup" in out_by_bot_order
    assert "link_created" in out_by_bot_order

    assert cli_main(["reconcile", str(payment.gateway_order_id)]) == 0
    out_by_gateway_order = capsys.readouterr().out
    assert "pg-reconcile-lookup" in out_by_gateway_order


def test_reconcile_verify_reference_id_collision_read_only_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with pg_session_factory() as db:
        holder = Payment(
            bot_order_id="pg-collision-holder",
            gateway_order_id=850002,
            gateway_user_id=1,
            amount=5000,
            payable_amount=5000,
            status=PaymentStatus.BOT_NOTIFY_PENDING.value,
            gateway_verified_at=datetime.now(UTC),
            reference_id="REF-PG-COLLIDE",
        )
        db.add(holder)
        db.commit()

    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(
            client, settings, order_id="pg-collision-candidate", amount=6000
        )
        assert response.status_code == 200
    candidate = get_payment(pg_session_factory, "pg-collision-candidate")

    stub.verify_result = verify_ok_response(
        amount=6000, user_id=candidate.gateway_user_id, reference_id="REF-PG-COLLIDE"
    )

    assert cli_main(["reconcile", "pg-collision-candidate", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "reference_id_collision" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    refetched_candidate = get_payment(pg_session_factory, "pg-collision-candidate")
    refetched_holder = get_payment(pg_session_factory, "pg-collision-holder")
    assert refetched_candidate.status == PaymentStatus.LINK_CREATED.value
    assert refetched_candidate.reference_id is None
    assert refetched_candidate.gateway_verified_at is None
    assert refetched_holder.reference_id == "REF-PG-COLLIDE"  # untouched by the collision check
    assert refetched_holder.status == PaymentStatus.BOT_NOTIFY_PENDING.value


def test_reconcile_default_and_verify_zero_mutation_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(client, settings, order_id="pg-zero-write", amount=4000)
        assert response.status_code == 200
    payment = get_payment(pg_session_factory, "pg-zero-write")
    before_updated_at = payment.updated_at

    assert cli_main(["reconcile", "pg-zero-write"]) == 0
    capsys.readouterr()

    stub.verify_result = verify_ok_response(
        amount=4000, user_id=payment.gateway_user_id, reference_id="REF-pg-zero-write"
    )
    assert cli_main(["reconcile", "pg-zero-write", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_VERIFY" in out

    refetched = get_payment(pg_session_factory, "pg-zero-write")
    assert refetched.updated_at == before_updated_at
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None
    assert refetched.reference_id is None
    assert len(stub.verify_requests) == 1


def test_reconcile_aged_out_refused_without_confirm_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with pg_session_factory() as db:
        db.add(
            Payment(
                bot_order_id="pg-aged-out",
                gateway_order_id=850005,
                gateway_user_id=1,
                amount=3000,
                payable_amount=3000,
                status=PaymentStatus.LINK_CREATED.value,
                callback_token_issued_at=datetime.now(UTC)
                - timedelta(seconds=settings.reconciliation_max_age_seconds + 120),
            )
        )
        db.commit()

    assert cli_main(["reconcile", "pg-aged-out", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "aged out" in out
    assert "NO LOCAL CHANGES WERE MADE." in out


# --- PR #59 follow-up: the --verify row-lock race, on real PostgreSQL -------
#
# Required scenario A: a settlement lands AFTER the CLI's initial (non-
# locking) lookup but BEFORE it acquires the --verify row lock -- the CLI's
# reload under lock must see it and refuse, making zero gateway calls of its
# own. Required scenario B: the CLI's row lock is genuinely HELD (blocking a
# real concurrent transaction) across its own diagnostic gateway call, the
# CLI itself never mutates anything, and the blocked settlement proceeds
# normally the instant the CLI's transaction ends.


def test_reconcile_verify_reloads_under_lock_after_concurrent_settlement(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    """Scenario A. The settlement is injected deterministically (via the
    exact point in app.cli._cmd_reconcile where the CLI's non-locking
    lookup returns and before it acquires the verify row lock) rather than
    timed, so the test is not flaky -- but the settlement itself, and the
    CLI's reload, both run as real, separate PostgreSQL transactions."""
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(client, settings, order_id="pg-race-settle-first", amount=5000)
        assert response.status_code == 200
    payment = get_payment(pg_session_factory, "pg-race-settle-first")

    stub.verify_result = verify_ok_response(
        amount=5000, user_id=payment.gateway_user_id, reference_id="REF-pg-race-settle-first"
    )

    import app.cli as cli_module

    original_find_payment = cli_module._find_payment
    raced = {"done": False}

    def racing_find_payment(db, order_id):
        found = original_find_payment(db, order_id)
        if found is not None and not raced["done"]:
            raced["done"] = True
            # A real, separate PostgreSQL transaction settling the payment
            # in the window between the CLI's lookup and its verify lock.
            gateway = CentralPayClient(
                base_url=settings.centralpay_base_url,
                getlink_api_key=settings.centralpay_getlink_api_key,
                verify_api_key=settings.centralpay_verify_api_key,
                timeout_seconds=settings.centralpay_timeout_seconds,
                transport=httpx.MockTransport(stub.handler),
            )
            try:
                with pg_session_factory() as settle_db:
                    locked = settle_db.execute(
                        select(Payment).where(Payment.id == found.id).with_for_update()
                    ).scalar_one()
                    verify_and_settle(
                        settle_db, gateway, locked, settings=settings, source="reconciliation"
                    )
            finally:
                gateway.close()
        return found

    monkeypatch.setattr(cli_module, "_find_payment", racing_find_payment)

    assert cli_main(["reconcile", "pg-race-settle-first", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "already denotes successful gateway verification" in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert len(stub.verify_requests) == 1  # only the racing settlement's call -- zero from the CLI

    refetched = get_payment(pg_session_factory, "pg-race-settle-first")
    # The race's settlement stands.
    assert refetched.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert refetched.gateway_verified_at is not None
    assert refetched.reference_id == "REF-pg-race-settle-first"


def test_reconcile_verify_holds_row_lock_across_diagnostic_call_blocking_concurrent_settlement(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    """Scenario B. A `lock_acquired` signal fires the instant the CLI's own
    ``SELECT ... FOR UPDATE`` reload returns (i.e. the instant it holds the
    lock), so the concurrent settlement thread only attempts its own
    ``FOR UPDATE`` once the CLI provably already holds the row -- proving
    the concurrent transaction genuinely blocks on a REAL PostgreSQL lock
    held across the CLI's diagnostic gateway call, not merely that the two
    happen not to overlap."""
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(client, settings, order_id="pg-race-lock-held", amount=5000)
        assert response.status_code == 200
    payment = get_payment(pg_session_factory, "pg-race-lock-held")

    stub.verify_result = verify_ok_response(
        amount=5000, user_id=payment.gateway_user_id, reference_id="REF-pg-race-lock-held"
    )
    stub.verify_delay_seconds = 0.3  # widen the window the CLI's own call is in flight

    import app.cli as cli_module
    from app.services.reconcile_inspect import build_local_snapshot as original_build_local_snapshot

    lock_acquired = threading.Event()

    def spy_build_local_snapshot(db, settings_arg, payment_id, *, now, for_update=False):
        result = original_build_local_snapshot(
            db, settings_arg, payment_id, now=now, for_update=for_update
        )
        if for_update:
            lock_acquired.set()  # the CLI's FOR UPDATE reload has returned: it holds the lock
        return result

    monkeypatch.setattr(cli_module, "build_local_snapshot", spy_build_local_snapshot)

    def run_cli_verify():
        assert cli_main(["reconcile", "pg-race-lock-held", "--verify"]) == 0

    def run_concurrent_settlement():
        gateway = CentralPayClient(
            base_url=settings.centralpay_base_url,
            getlink_api_key=settings.centralpay_getlink_api_key,
            verify_api_key=settings.centralpay_verify_api_key,
            timeout_seconds=settings.centralpay_timeout_seconds,
            transport=httpx.MockTransport(stub.handler),
        )
        try:
            with pg_session_factory() as db:
                assert lock_acquired.wait(timeout=30)  # the CLI already holds the row lock
                # Blocks here on a REAL PostgreSQL row lock until the CLI's
                # transaction ends (the CLI never commits -- only closing
                # its session releases the lock).
                locked = db.execute(
                    select(Payment)
                    .where(Payment.bot_order_id == "pg-race-lock-held")
                    .with_for_update()
                ).scalar_one()
                verify_and_settle(db, gateway, locked, settings=settings, source="reconciliation")
        finally:
            gateway.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        cli_future = pool.submit(run_cli_verify)
        settle_future = pool.submit(run_concurrent_settlement)
        cli_future.result(timeout=60)
        settle_future.result(timeout=60)

    out = capsys.readouterr().out
    assert "WOULD_VERIFY" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    # Two verify requests reached the gateway: the CLI's own diagnostic
    # call, and the concurrent settlement's call, which could not even
    # start its own row read until the CLI's lock was released.
    assert len(stub.verify_requests) == 2

    refetched = get_payment(pg_session_factory, "pg-race-lock-held")
    # The CLI itself made zero mutations -- the concurrent settlement, which
    # could only proceed once the CLI released the lock, settled normally.
    assert refetched.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert refetched.gateway_verified_at is not None
    assert refetched.reference_id == "REF-pg-race-lock-held"


# --- PR #59 follow-up: consistent single-read snapshot, on real PostgreSQL --


def test_reconcile_snapshot_never_combines_stale_payment_with_newer_classification_pg(
    cli_env, settings, pg_session_factory
):
    """Item 4 regression. build_local_snapshot's single structured SELECT
    must never combine a Payment row read at one moment with classification
    flags (age_bucket, tier due, exhausted) computed at another -- the exact
    failure mode the OLD build_local_snapshot (one SELECT to load the
    Payment ORM object, then several MORE separate SELECTs for the
    tier/due/exhausted booleans) could produce under concurrent writes at
    PostgreSQL READ COMMITTED isolation.

    A background writer continuously flips the row between two states with
    MUTUALLY EXCLUSIVE classifications -- "fresh, unverified link_created"
    (must always report an age_bucket) and "gateway_verified" (must always
    report age_bucket=None and every tier/due/exhausted flag False) -- while
    many concurrent reads snapshot it. Every single read must be internally
    self-consistent between the Payment fields and the classification flags
    it returns, no matter when the writer's commits land relative to it."""
    from app.services.reconcile_inspect import build_local_snapshot

    with pg_session_factory() as db:
        payment = Payment(
            bot_order_id="pg-consistency-race",
            gateway_order_id=970001,
            gateway_user_id=1,
            amount=5000,
            payable_amount=5000,
            status=PaymentStatus.LINK_CREATED.value,
            callback_token_issued_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        payment_id = payment.id

    stop = threading.Event()

    def toggle_state():
        verified = False
        with pg_session_factory() as writer_db:
            while not stop.is_set():
                verified = not verified
                if verified:
                    values = {
                        "status": PaymentStatus.BOT_NOTIFY_PENDING.value,
                        "gateway_verified_at": datetime.now(UTC),
                    }
                else:
                    values = {
                        "status": PaymentStatus.LINK_CREATED.value,
                        "gateway_verified_at": None,
                    }
                writer_db.execute(update(Payment).where(Payment.id == payment_id).values(**values))
                writer_db.commit()

    writer_thread = threading.Thread(target=toggle_state)
    writer_thread.start()
    try:
        for _ in range(200):
            with pg_session_factory() as reader_db:
                snapshot = build_local_snapshot(
                    reader_db, settings, payment_id, now=datetime.now(UTC)
                )
                assert snapshot is not None
                payment, local = snapshot
                if local.is_link_created_unverified:
                    assert payment.status == PaymentStatus.LINK_CREATED.value
                    assert payment.gateway_verified_at is None
                    assert local.age_bucket is not None  # a fresh row always has a bucket
                else:
                    assert payment.status != PaymentStatus.LINK_CREATED.value
                    assert payment.gateway_verified_at is not None
                    assert local.age_bucket is None
                    assert local.active_tier_due is False
                    assert local.expiring_tier_due is False
                    assert local.attempts_exhausted is False
                    assert local.schedule_due is False
                    assert local.auto_reconciliation_due is False
    finally:
        stop.set()
        writer_thread.join(timeout=30)
    assert not writer_thread.is_alive()


# --- final safety follow-up: `now` recomputed AFTER the lock is acquired ----


def test_reconcile_verify_recomputes_aged_out_gate_using_time_after_lock_wait_pg(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    """`now` must be captured AFTER the --verify row lock is actually
    acquired, not before any wait behind a concurrent transaction's hold on
    the SAME row -- otherwise a payment that crosses
    RECONCILIATION_MAX_AGE_SECONDS WHILE the CLI waits for the lock could
    bypass the aged-out gate using a stale, pre-wait timestamp.

    A background thread acquires and holds a REAL PostgreSQL row lock on
    the payment BEFORE the CLI starts, for long enough that the payment
    crosses RECONCILIATION_MAX_AGE_SECONDS purely from elapsed wall-clock
    time while the CLI's own ``SELECT ... FOR UPDATE`` blocks waiting for
    it. The payment is NOT yet aged out at the moment the CLI queues behind
    the lock -- only by the time the lock is actually acquired. Once the
    background thread releases and the CLI's wait ends, the CLI must
    classify the payment as aged out USING THE TIME AT THAT MOMENT and
    refuse, making zero gateway calls."""
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    hold_seconds = 2.5
    with pg_session_factory() as db:
        db.add(
            Payment(
                bot_order_id="pg-post-lock-aged-out",
                gateway_order_id=850010,
                gateway_user_id=1,
                amount=4000,
                payable_amount=4000,
                status=PaymentStatus.LINK_CREATED.value,
                # Just under RECONCILIATION_MAX_AGE_SECONDS when the holder
                # thread starts locking the row -- not aged out yet.
                callback_token_issued_at=datetime.now(UTC)
                - timedelta(seconds=settings.reconciliation_max_age_seconds - 1.0),
            )
        )
        db.commit()

    holder_ready = threading.Event()

    def hold_lock():
        with pg_session_factory() as holder_db:
            holder_db.execute(
                select(Payment)
                .where(Payment.bot_order_id == "pg-post-lock-aged-out")
                .with_for_update()
            ).scalar_one()
            holder_ready.set()
            # Held for longer than the payment's remaining margin to
            # RECONCILIATION_MAX_AGE_SECONDS -- it ages out purely from
            # this real elapsed wait, not from any injected state change.
            time.sleep(hold_seconds)
            holder_db.commit()

    holder_thread = threading.Thread(target=hold_lock)
    holder_thread.start()
    try:
        assert holder_ready.wait(timeout=10)  # the background thread genuinely holds the row
        # Blocks here on the SAME real PostgreSQL row lock until the
        # holder releases -- proving the wait is real, not merely sequenced.
        assert cli_main(["reconcile", "pg-post-lock-aged-out", "--verify"]) == 0
    finally:
        holder_thread.join(timeout=30)
    assert not holder_thread.is_alive()

    out = capsys.readouterr().out
    assert "aged out" in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert len(stub.verify_requests) == 0  # refused before any gateway call

    refetched = get_payment(pg_session_factory, "pg-post-lock-aged-out")
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None
