"""PostgreSQL integration coverage for `centralpay recover-aged-out ORDER_ID`
(app.services.aged_out_recovery / operator recovery of an aged-out
link_created payment).

Only real PostgreSQL proves the concurrency guarantees this command depends
on: a genuinely BLOCKING ``SELECT ... FOR UPDATE`` row lock held across the
gateway call (scenarios C and D below), and the real ``ck_payments_delivery_
requires_verification`` CHECK constraint / BIGINT columns the eligibility
gate and duplicate-downstream-safety argument rely on -- SQLite's single
shared connection cannot simulate a REAL blocking wait between two
concurrent transactions (see tests/test_aged_out_recovery.py for the fast,
deterministic SQLite-backed proofs of the same contract via injected race
timing).

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import concurrent.futures
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.centralpay import CentralPayClient
from app.cli import main as cli_main
from app.models import Base, Payment, PaymentEvent, PaymentStatus
from app.services.verification import verify_and_settle
from tests.conftest import CentralPayStub, get_payment, verify_ok_response

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


def _make_status_payment(
    pg_session_factory,
    *,
    bot_order_id: str,
    gateway_order_id: int,
    status: str,
    gateway_verified_at: datetime | None = None,
    amount: int = 5000,
    age_seconds: float = 60,
) -> Payment:
    with pg_session_factory() as db:
        payment = Payment(
            bot_order_id=bot_order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=1,
            amount=amount,
            payable_amount=amount,
            status=status,
            gateway_verified_at=gateway_verified_at,
            callback_token_issued_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment


def _make_aged_out_payment(
    pg_session_factory, settings, *, bot_order_id: str, gateway_order_id: int, amount: int = 5000
) -> Payment:
    return _make_status_payment(
        pg_session_factory,
        bot_order_id=bot_order_id,
        gateway_order_id=gateway_order_id,
        status=PaymentStatus.LINK_CREATED.value,
        gateway_verified_at=None,
        amount=amount,
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )


def _events(pg_session_factory, payment_id: int) -> list[PaymentEvent]:
    with pg_session_factory() as db:
        return list(
            db.execute(
                select(PaymentEvent)
                .where(PaymentEvent.payment_id == payment_id)
                .order_by(PaymentEvent.id)
            ).scalars()
        )


# --- A: preview -- zero HTTP, zero writes, zero PaymentEvent changes, no lock


def test_preview_zero_http_zero_writes_zero_events_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        pg_session_factory, settings, bot_order_id="pg-rec-preview", gateway_order_id=900001
    )
    before_updated_at = payment.updated_at
    events_before = len(_events(pg_session_factory, payment.id))

    assert cli_main(["recover-aged-out", "pg-rec-preview"]) == 0
    out = capsys.readouterr().out
    assert "eligible:                yes" in out
    assert "PREVIEW ONLY" in out

    assert len(stub.verify_requests) == 0
    assert len(_events(pg_session_factory, payment.id)) == events_before
    refetched = get_payment(pg_session_factory, "pg-rec-preview")
    assert refetched.updated_at == before_updated_at
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None


def test_preview_never_takes_a_row_lock_pg(cli_env, settings, pg_session_factory, capsys):
    """Real proof preview takes no lock: a background thread holds a REAL
    FOR UPDATE lock on the row for the whole test; the preview command must
    still return immediately instead of blocking behind it."""
    payment = _make_aged_out_payment(
        pg_session_factory, settings, bot_order_id="pg-rec-preview-nolock", gateway_order_id=900002
    )

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_lock():
        with pg_session_factory() as db:
            db.execute(
                select(Payment).where(Payment.id == payment.id).with_for_update()
            ).scalar_one()
            holder_ready.set()
            release_holder.wait(timeout=10)
            db.commit()

    holder_thread = threading.Thread(target=hold_lock)
    holder_thread.start()
    try:
        assert holder_ready.wait(timeout=10)  # the background thread genuinely holds the row
        started = time.monotonic()
        assert cli_main(["recover-aged-out", "pg-rec-preview-nolock"]) == 0
        elapsed = time.monotonic() - started
        assert elapsed < 2.0  # never blocked behind the held lock
    finally:
        release_holder.set()
        holder_thread.join(timeout=10)
    assert not holder_thread.is_alive()
    capsys.readouterr()


# --- B: normal eligible aged-out recovery -----------------------------------


def test_normal_eligible_recovery_one_verify_call_canonical_settlement_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        pg_session_factory, settings, bot_order_id="pg-rec-normal", gateway_order_id=900003
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount, user_id=payment.gateway_user_id, reference_id="REF-pg-rec-normal"
    )

    assert cli_main(["recover-aged-out", "pg-rec-normal", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: verified ---" in out

    assert len(stub.verify_requests) == 1
    refetched = get_payment(pg_session_factory, "pg-rec-normal")
    assert refetched.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert refetched.gateway_verified_at is not None
    assert refetched.reference_id == "REF-pg-rec-normal"

    event_types = [e.event_type for e in _events(pg_session_factory, payment.id)]
    assert "aged_out_recovery_requested" in event_types
    assert "aged_out_recovery_verified" in event_types
    assert "gateway_payment_verified" in event_types  # the ONE canonical settlement path
    assert "bot_notification_queued" in event_types  # normal notification queueing, once


# --- C: a settlement lands between the CLI's lookup and its row lock -------


def test_confirm_reloads_under_lock_after_concurrent_settlement_lands_first_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    """Required scenario C: a settlement (simulating a browser callback or
    a reconciliation attempt) lands AFTER the CLI's non-locking ORDER_ID
    lookup but BEFORE it acquires the recovery row lock. The reload under
    that lock must see it and refuse, making zero gateway calls of its
    own -- injected deterministically at the exact point the CLI's lookup
    returns, so the test is not flaky, but the settlement and the CLI's
    reload run as real, separate PostgreSQL transactions."""
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        pg_session_factory,
        settings,
        bot_order_id="pg-rec-race-settle-first",
        gateway_order_id=900010,
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount, user_id=payment.gateway_user_id, reference_id="REF-race-settle-first"
    )

    import app.cli as cli_module

    original_find_payment = cli_module._find_payment
    raced = {"done": False}

    def racing_find_payment(db, order_id):
        found = original_find_payment(db, order_id)
        if found is not None and not raced["done"]:
            raced["done"] = True
            # A real, separate PostgreSQL transaction settling the payment
            # (simulating the browser callback or a reconciliation attempt)
            # in the window between the CLI's lookup and its recovery lock.
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

    assert cli_main(["recover-aged-out", "pg-rec-race-settle-first", "--confirm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "refused"
    assert payload["refusal_reason"] == "already_gateway_verified"
    assert len(stub.verify_requests) == 1  # only the racing settlement's call -- zero from recovery

    refetched = get_payment(pg_session_factory, "pg-rec-race-settle-first")
    # The race's settlement stands.
    assert refetched.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert refetched.gateway_verified_at is not None
    assert refetched.reference_id == "REF-race-settle-first"


# --- D: two simultaneous recoveries -----------------------------------------


def test_two_simultaneous_recoveries_only_one_settles_no_double_settlement_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    """Required scenario D: two operators run `--confirm` on the SAME
    payment at the same time. Only one may issue the gateway verification;
    the other must block on the REAL row lock, then reload, see the final
    (already-settled) state, and refuse -- never a double settlement, never
    two verify calls."""
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        pg_session_factory,
        settings,
        bot_order_id="pg-rec-two-simultaneous",
        gateway_order_id=900020,
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount, user_id=payment.gateway_user_id, reference_id="REF-two-simultaneous"
    )
    # Widen the window the eventual winner holds the row lock across its
    # own verify call, so the loser's FOR UPDATE genuinely has to wait on a
    # real PostgreSQL lock rather than the two calls happening not to overlap.
    stub.verify_delay_seconds = 0.3

    def run() -> int:
        return cli_main(["recover-aged-out", "pg-rec-two-simultaneous", "--confirm", "--json"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        second = pool.submit(run)
        results = [first.result(timeout=30), second.result(timeout=30)]

    capsys.readouterr()
    assert results == [0, 0]
    assert len(stub.verify_requests) == 1  # only ONE thread issued the gateway verification

    refetched = get_payment(pg_session_factory, "pg-rec-two-simultaneous")
    assert refetched.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert refetched.gateway_verified_at is not None
    assert refetched.reference_id == "REF-two-simultaneous"

    event_types = [e.event_type for e in _events(pg_session_factory, payment.id)]
    assert event_types.count("aged_out_recovery_verified") == 1
    assert event_types.count("aged_out_recovery_refused") == 1  # the loser refuses
    assert event_types.count("bot_notification_queued") == 1  # never double-queued
    assert event_types.count("gateway_payment_verified") == 1  # settled exactly once


# --- E: manual_review -- zero HTTP -------------------------------------------


def test_manual_review_refuses_zero_http_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    _make_status_payment(
        pg_session_factory,
        bot_order_id="pg-rec-manual-review",
        gateway_order_id=900030,
        status=PaymentStatus.MANUAL_REVIEW.value,
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )

    assert cli_main(["recover-aged-out", "pg-rec-manual-review", "--confirm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refusal_reason"] == "manual_review_owned"
    assert len(stub.verify_requests) == 0


# --- F: not-aged-out -- zero HTTP --------------------------------------------


def test_not_aged_out_refuses_zero_http_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    _make_status_payment(
        pg_session_factory,
        bot_order_id="pg-rec-not-aged-out",
        gateway_order_id=900031,
        status=PaymentStatus.LINK_CREATED.value,
        age_seconds=60,
    )

    assert cli_main(["recover-aged-out", "pg-rec-not-aged-out", "--confirm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refusal_reason"] == "not_aged_out"
    assert len(stub.verify_requests) == 0


# --- G: already verified, nullable/anomalous timestamp -- zero HTTP --------


def test_already_verified_status_with_null_timestamp_refuses_zero_http_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    """A gateway_verified-status row with gateway_verified_at still NULL is
    database-valid (no CHECK constraint requires the timestamp for that
    status) -- must still refuse via VERIFIED_STATUSES semantics, not be
    treated as aged-out-eligible just because the timestamp is absent."""
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    _make_status_payment(
        pg_session_factory,
        bot_order_id="pg-rec-verified-null-ts",
        gateway_order_id=900032,
        status=PaymentStatus.GATEWAY_VERIFIED.value,
        gateway_verified_at=None,
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )

    assert cli_main(["recover-aged-out", "pg-rec-verified-null-ts", "--confirm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refusal_reason"] == "already_gateway_verified"
    assert len(stub.verify_requests) == 0


# --- H: gateway not paid -----------------------------------------------------


def test_gateway_not_paid_remains_unverified_no_reconciliation_reinsertion_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        pg_session_factory, settings, bot_order_id="pg-rec-not-paid", gateway_order_id=900040
    )
    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid yet"})

    assert cli_main(["recover-aged-out", "pg-rec-not-paid", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: gateway_not_paid ---" in out

    assert len(stub.verify_requests) == 1
    refetched = get_payment(pg_session_factory, "pg-rec-not-paid")
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None
    assert refetched.reconciliation_next_at is None  # never re-inserted into polling
    assert refetched.reconciliation_attempts == 0

    events = _events(pg_session_factory, payment.id)
    event_types = [e.event_type for e in events]
    assert "aged_out_recovery_not_paid" in event_types
    assert "bot_notification_queued" not in event_types
    not_paid_event = next(e for e in events if e.event_type == "centralpay_verify_not_paid")
    assert not_paid_event.data is not None
    assert not_paid_event.data["source"] == "aged_out_recovery"


# --- I: financial mismatch -> canonical manual_review -----------------------


def test_amount_mismatch_moves_to_manual_review_no_notification_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        pg_session_factory, settings, bot_order_id="pg-rec-mismatch", gateway_order_id=900050
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount + 1, user_id=payment.gateway_user_id, reference_id="REF-pg-mismatch"
    )

    assert cli_main(["recover-aged-out", "pg-rec-mismatch", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: manual_review ---" in out

    refetched = get_payment(pg_session_factory, "pg-rec-mismatch")
    assert refetched.status == PaymentStatus.MANUAL_REVIEW.value
    assert refetched.gateway_verified_at is None

    event_types = [e.event_type for e in _events(pg_session_factory, payment.id)]
    assert "aged_out_recovery_manual_review" in event_types
    assert "verify_payable_amount_mismatch" in event_types
    assert "bot_notification_queued" not in event_types


# --- J: transport/protocol failure -- no fake success, no auto-retry -------


def test_transport_failure_no_fake_success_no_automatic_retry_pg(
    cli_env, settings, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        pg_session_factory, settings, bot_order_id="pg-rec-transport", gateway_order_id=900060
    )
    stub.verify_result = httpx.ConnectError("boom")

    assert cli_main(["recover-aged-out", "pg-rec-transport", "--confirm"]) == 1
    out = capsys.readouterr().out
    assert "--- --confirm: transport_failed ---" in out

    assert len(stub.verify_requests) == 1  # exactly one attempt -- no automatic retry
    refetched = get_payment(pg_session_factory, "pg-rec-transport")
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None

    event_types = [e.event_type for e in _events(pg_session_factory, payment.id)]
    assert "aged_out_recovery_transport_failed" in event_types
    assert "bot_notification_queued" not in event_types


# --- K: ambiguous ORDER_ID -- refuses before lock/HTTP ----------------------


def test_ambiguous_order_id_refuses_before_lock_or_http_pg(
    cli_env, pg_session_factory, monkeypatch, capsys
):
    stub = CentralPayStub()
    _patch_centralpay_client(monkeypatch, stub)
    with pg_session_factory() as db:
        db.add(
            Payment(
                bot_order_id="900070",
                gateway_order_id=900071,
                gateway_user_id=1,
                amount=4000,
                payable_amount=4000,
                status=PaymentStatus.LINK_CREATED.value,
            )
        )
        db.add(
            Payment(
                bot_order_id="pg-rec-ambiguous-other",
                gateway_order_id=900070,
                gateway_user_id=1,
                amount=4000,
                payable_amount=4000,
                status=PaymentStatus.LINK_CREATED.value,
            )
        )
        db.commit()

    assert cli_main(["recover-aged-out", "900070", "--confirm"]) == 1
    out = capsys.readouterr().out
    assert "ambiguous_order_id" in out
    assert len(stub.verify_requests) == 0
