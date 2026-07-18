"""PostgreSQL integration tests: migration, full flow, and row-lock concurrency.

These run only when TEST_DATABASE_URL points at a disposable PostgreSQL
database:

    export TEST_DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/centralpay_test'
    pytest -m postgres
"""

import concurrent.futures
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, Payment, PaymentStatus
from tests.conftest import (
    CentralPayStub,
    build_app,
    create_order,
    event_types,
    get_events,
    get_payment,
    getlink_ok_response,
    valid_callback_path,
    verify_ok_response,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]


def _drop_all(engine) -> None:
    with engine.begin() as connection:
        for table in (
            "admin_alerts",
            "worker_heartbeats",
            "payment_events",
            "payments",
            "fee_policies",
            "alembic_version",
        ):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    _drop_all(engine)
    yield engine
    engine.dispose()


def test_alembic_upgrade_on_empty_database(pg_engine):
    """The quality gate: the migration must bring up an empty PostgreSQL database."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}"

    inspector = inspect(pg_engine)
    tables = set(inspector.get_table_names())
    assert {"payments", "payment_events", "alembic_version"} <= tables

    payment_indexes = {index["name"] for index in inspector.get_indexes("payments")}
    assert "ix_payments_bot_order_id" in payment_indexes
    assert "ix_payments_gateway_order_id" in payment_indexes
    assert "ix_payments_notify_due" in payment_indexes

    # Phase 2 delivery-tracking columns from migration 0002.
    payment_columns = {c["name"] for c in inspector.get_columns("payments")}
    assert {
        "gateway_verified_at",
        "bot_notify_reason",
        "bot_notify_attempts",
        "bot_last_http_status",
        "bot_last_error_code",
        "bot_notify_started_at",
        "bot_notify_accepted_at",
        "next_retry_at",
        "manual_review_at",
        "notification_claimed_at",
        "notification_claimed_by",
    } <= payment_columns

    # JSONB on PostgreSQL, and a second upgrade run is a no-op.
    columns = {c["name"]: c for c in inspector.get_columns("payment_events")}
    assert str(columns["data"]["type"]).upper() == "JSONB"
    rerun = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert rerun.returncode == 0


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


def test_full_payment_flow_on_postgres(settings, pg_app, pg_session_factory):
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-flow", amount=15000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-flow")
        stub.verify_result = verify_ok_response(amount=15000)
        response = client.get(valid_callback_path(stub, payment.gateway_order_id))
        assert response.status_code == 200
        assert 'data-status="bot_pending"' in response.text

    payment = get_payment(pg_session_factory, "pg-flow")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.gateway_verified_at is not None
    assert event_types(get_events(pg_session_factory, payment.id)) == [
        "payment_created",
        "payment_fee_snapshotted",
        "payment_link_created",
        "callback_received",
        "gateway_payment_verified",
        "bot_notification_queued",
    ]


def test_concurrent_callbacks_verify_exactly_once(settings, pg_app, pg_session_factory):
    """Row locking must serialize concurrent callbacks: verify runs once."""
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-race", amount=20000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-race")
        stub.verify_result = verify_ok_response(amount=20000)
        stub.verify_delay_seconds = 0.5

        path = valid_callback_path(stub, payment.gateway_order_id)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.get, path) for _ in range(2)]
            responses = [future.result(timeout=30) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    for response in responses:
        assert 'data-status="bot_pending"' in response.text
    # The gateway verify endpoint was hit exactly once; the second callback
    # took the duplicate path.
    assert len(stub.verify_requests) == 1

    payment = get_payment(pg_session_factory, "pg-race")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    events = event_types(get_events(pg_session_factory, payment.id))
    assert events.count("gateway_payment_verified") == 1
    assert events.count("duplicate_callback_ignored") == 1


def test_concurrent_creates_return_one_link(settings, pg_app, pg_session_factory):
    """Concurrent duplicate creates must serialize to a single payment link."""
    stub = pg_app.state.centralpay_stub
    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [
            pool.submit(create_order, client, settings, order_id="pg-create-race")
            for _ in range(2)
        ]
        responses = [future.result(timeout=30) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    urls = {response.json()["url"] for response in responses}
    assert len(urls) == 1
    assert len(stub.getlink_requests) == 1


def test_concurrent_worker_claims_use_skip_locked(settings, pg_app, pg_session_factory):
    """Two workers racing for one due payment: exactly one claims it."""
    from app.services.notification import claim_next_due, utcnow

    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-claim", amount=9000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-claim")
        stub.verify_result = verify_ok_response(amount=9000)
        assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    barrier = threading.Barrier(2)

    def attempt_claim(worker_id: str):
        session = pg_session_factory()
        try:
            barrier.wait(timeout=10)
            claimed = claim_next_due(session, worker_id=worker_id, now=utcnow())
            # Hold the claim result; no result recording in this test.
            return claimed
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt_claim, f"pg-worker-{i}") for i in range(2)]
        results = [future.result(timeout=30) for future in futures]

    claims = [claim for claim in results if claim is not None]
    # FOR UPDATE SKIP LOCKED: the loser skips instead of double-claiming.
    assert len(claims) == 1
    assert claims[0].payment_id == payment.id
    payment = get_payment(pg_session_factory, "pg-claim")
    assert payment.bot_notify_attempts == 1
    assert payment.notification_claimed_by is not None


def test_concurrent_stale_and_current_token_callbacks(settings, pg_app, pg_session_factory):
    """Callback replay audit: a stale-token callback racing the legitimate
    one must never reach verify. Exactly one verify call, one verified fact,
    one queued notification — regardless of lock acquisition order."""
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        stub.getlink_result = httpx.ConnectError("connection refused")
        assert create_order(client, settings, order_id="pg-stale", amount=7000).status_code == 502
        stub.getlink_result = getlink_ok_response()
        assert create_order(client, settings, order_id="pg-stale", amount=7000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-stale")

        # The first attempt's token was durably superseded by the second
        # link. Re-sign it for the current order id to isolate the token
        # check from the signature check.
        first_url = str(stub.getlink_requests[0]["returnUrl"])
        stale_ct = parse_qs(urlsplit(first_url).query)["ct"][0]
        from app.security import callback_signature

        stale_sig = callback_signature(
            settings.callback_hmac_secret, payment.gateway_order_id, stale_ct
        )
        stale_path = (
            f"/api/centralpay/callback?orderId={payment.gateway_order_id}"
            f"&ct={stale_ct}&sig={stale_sig}"
        )
        valid_path = valid_callback_path(stub, payment.gateway_order_id)
        stub.verify_result = verify_ok_response(amount=7000, reference_id="REF-pg-stale")
        stub.verify_delay_seconds = 0.3

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.get, path) for path in (valid_path, stale_path)]
            responses = [future.result(timeout=30) for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 403]
    # The stale token NEVER reached CentralPay: verify ran exactly once,
    # triggered by the legitimate link.
    assert len(stub.verify_requests) == 1

    payment = get_payment(pg_session_factory, "pg-stale")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    events = event_types(get_events(pg_session_factory, payment.id))
    assert events.count("gateway_payment_verified") == 1
    assert events.count("bot_notification_queued") == 1
    assert "callback_token_invalid" in events


def test_concurrent_replays_after_verification_never_reverify(
    settings, pg_app, pg_session_factory
):
    """At-most-once proof under concurrency: replaying the legitimate signed
    URL after verification returns the final page from every request while
    verify is never called again and the notification is never re-queued."""
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-replay", amount=6000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-replay")
        stub.verify_result = verify_ok_response(amount=6000, reference_id="REF-pg-replay")
        path = valid_callback_path(stub, payment.gateway_order_id)
        assert client.get(path).status_code == 200
        assert len(stub.verify_requests) == 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(client.get, path) for _ in range(4)]
            responses = [future.result(timeout=30) for future in futures]

    for response in responses:
        assert response.status_code == 200
        assert 'data-status="bot_pending"' in response.text
    # Verified exactly once, queued exactly once, delivery not yet attempted.
    assert len(stub.verify_requests) == 1
    payment = get_payment(pg_session_factory, "pg-replay")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.bot_notify_attempts == 0
    events = event_types(get_events(pg_session_factory, payment.id))
    assert events.count("gateway_payment_verified") == 1
    assert events.count("bot_notification_queued") == 1
    assert events.count("duplicate_callback_ignored") == 4


def test_many_identical_concurrent_creates(settings, pg_app, pg_session_factory):
    """Creation audit: 10 identical concurrent requests, released through a
    barrier, must produce exactly one payment row, one gateway order id,
    one getLink call, one payment_link_created event, and one URL."""
    stub = pg_app.state.centralpay_stub
    barrier = threading.Barrier(10)

    def submit(client):
        barrier.wait(timeout=10)
        return create_order(client, settings, order_id="pg-many", amount=12000)

    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool,
    ):
        futures = [pool.submit(submit, client) for _ in range(10)]
        responses = [future.result(timeout=60) for future in futures]

    assert [r.status_code for r in responses] == [200] * 10
    assert len({r.json()["url"] for r in responses}) == 1  # deterministic result
    assert len(stub.getlink_requests) == 1  # gateway called at most once

    with pg_session_factory() as session:
        rows = session.execute(
            select(Payment).where(Payment.bot_order_id == "pg-many")
        ).scalars().all()
    assert len(rows) == 1
    events = event_types(get_events(pg_session_factory, rows[0].id))
    assert events.count("payment_created") == 1
    assert events.count("payment_link_created") == 1


def test_concurrent_conflicting_amounts_single_row(settings, pg_app, pg_session_factory):
    """Same order id with different amounts concurrently: exactly one row,
    one winning amount, one getLink; the loser gets the explicit 409
    amount-mismatch code — never a 500 or a second payment."""
    barrier = threading.Barrier(2)
    stub = pg_app.state.centralpay_stub

    def submit(client, amount):
        barrier.wait(timeout=10)
        return create_order(client, settings, order_id="pg-conflict", amount=amount)

    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(submit, client, amount) for amount in (10000, 20000)]
        responses = [future.result(timeout=60) for future in futures]

    assert sorted(r.status_code for r in responses) == [200, 409]
    rejected = next(r for r in responses if r.status_code == 409)
    assert rejected.json()["error"]["code"] == "duplicate_order_amount_mismatch"
    assert len(stub.getlink_requests) == 1

    with pg_session_factory() as session:
        rows = session.execute(
            select(Payment).where(Payment.bot_order_id == "pg-conflict")
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].amount in (10000, 20000)
    winner = next(r for r in responses if r.status_code == 200)
    assert winner.json()["url"]


def test_concurrent_distinct_orders_get_unique_gateway_ids(
    settings, pg_app, pg_session_factory
):
    """Gateway order id allocation under concurrency: distinct orders always
    receive distinct ids (unique index enforced), one getLink each."""
    barrier = threading.Barrier(10)

    def submit(client, index):
        barrier.wait(timeout=10)
        return create_order(client, settings, order_id=f"pg-uid-{index}", amount=9000)

    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool,
    ):
        futures = [pool.submit(submit, client, i) for i in range(10)]
        responses = [future.result(timeout=60) for future in futures]

    assert [r.status_code for r in responses] == [200] * 10
    stub = pg_app.state.centralpay_stub
    assert len(stub.getlink_requests) == 10
    with pg_session_factory() as session:
        gateway_ids = session.execute(
            select(Payment.gateway_order_id).where(Payment.bot_order_id.like("pg-uid-%"))
        ).scalars().all()
    assert len(gateway_ids) == 10
    assert len(set(gateway_ids)) == 10


def test_four_workers_drain_queue_exactly_once(
    settings, pg_app, pg_session_factory, bot_stub, notifier
):
    """Worker audit: four workers draining a 12-payment queue under real
    SKIP LOCKED must deliver every payment exactly once — no duplicates, no
    losses, no deadlocks — with deterministic per-payment attempt counts."""
    from app.services.notification import run_worker_pass

    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        for i in range(12):
            order_id = f"pg-drain-{i}"
            assert create_order(client, settings, order_id=order_id, amount=5000).status_code == 200
            payment = get_payment(pg_session_factory, order_id)
            stub.verify_result = verify_ok_response(amount=5000, reference_id=f"REF-{order_id}")
            callback = client.get(valid_callback_path(stub, payment.gateway_order_id))
            assert callback.status_code == 200

    barrier = threading.Barrier(4)

    def drain(worker_index):
        barrier.wait(timeout=10)
        session = pg_session_factory()
        try:
            total = 0
            while True:
                result = run_worker_pass(
                    session, notifier, settings, worker_id=f"pg-worker-{worker_index}"
                )
                total += result["processed"]
                if result["processed"] == 0:
                    return total
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(drain, i) for i in range(4)]
        totals = [future.result(timeout=120) for future in futures]

    # Every payment delivered exactly once across all workers.
    assert sum(totals) == 12
    assert len(bot_stub.requests) == 12
    delivered_orders = [str(request["order_id"]) for request in bot_stub.requests]
    assert sorted(delivered_orders) == sorted(f"pg-drain-{i}" for i in range(12))

    with pg_session_factory() as session:
        payments = session.execute(
            select(Payment).where(Payment.bot_order_id.like("pg-drain-%"))
        ).scalars().all()
    assert len(payments) == 12
    for payment in payments:
        assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
        assert payment.bot_notify_attempts == 1  # exactly one attempt each
        assert payment.notification_claimed_at is None  # claims released


def test_race_duplicate_create_against_callback(settings, pg_app, pg_session_factory):
    """Final audit race 2: a duplicate create request racing the verifying
    callback. Whichever wins the row lock, every invariant holds: exactly
    one verify call, one queued notification, and the create response is
    either the existing link (200) or the explicit already-verified 409 —
    never a new link, never a 500."""
    stub = pg_app.state.centralpay_stub
    barrier = threading.Barrier(2)
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-cvc", amount=11000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-cvc")
        stub.verify_result = verify_ok_response(amount=11000, reference_id="REF-pg-cvc")
        stub.verify_delay_seconds = 0.3
        callback_path = valid_callback_path(stub, payment.gateway_order_id)

        def do_create():
            barrier.wait(timeout=10)
            return ("create", create_order(client, settings, order_id="pg-cvc", amount=11000))

        def do_callback():
            barrier.wait(timeout=10)
            return ("callback", client.get(callback_path))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(
                future.result(timeout=60)
                for future in [pool.submit(do_create), pool.submit(do_callback)]
            )

    assert results["callback"].status_code == 200
    create_response = results["create"]
    assert create_response.status_code in (200, 409)
    if create_response.status_code == 200:
        # The pre-existing link was returned; a fresh link was never issued.
        assert create_response.json()["url"]
        assert len(stub.getlink_requests) == 1
    else:
        assert create_response.json()["error"]["code"] == "order_already_verified"

    assert len(stub.verify_requests) == 1
    payment = get_payment(pg_session_factory, "pg-cvc")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.reference_id == "REF-pg-cvc"
    events = event_types(get_events(pg_session_factory, payment.id))
    assert events.count("gateway_payment_verified") == 1
    assert events.count("bot_notification_queued") == 1


def test_race_duplicate_callback_against_worker(
    settings, pg_app, pg_session_factory, bot_stub, notifier
):
    """Final audit race 4: a replayed callback racing the delivering worker.
    Verify runs once total, the bot receives exactly one request, and the
    callback replay returns a stable final page without resetting state."""
    from app.services.notification import run_worker_pass

    stub = pg_app.state.centralpay_stub
    barrier = threading.Barrier(2)
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-cbw", amount=7500).status_code == 200
        payment = get_payment(pg_session_factory, "pg-cbw")
        stub.verify_result = verify_ok_response(amount=7500, reference_id="REF-pg-cbw")
        callback_path = valid_callback_path(stub, payment.gateway_order_id)
        assert client.get(callback_path).status_code == 200  # verified + queued

        def replay_callback():
            barrier.wait(timeout=10)
            return client.get(callback_path)

        def run_worker():
            barrier.wait(timeout=10)
            session = pg_session_factory()
            try:
                return run_worker_pass(session, notifier, settings, worker_id="pg-race-worker")
            finally:
                session.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            callback_future = pool.submit(replay_callback)
            worker_future = pool.submit(run_worker)
            replay = callback_future.result(timeout=60)
            worker_result = worker_future.result(timeout=60)

    assert replay.status_code == 200  # stable final page, whichever state won
    assert worker_result["processed"] == 1
    assert len(stub.verify_requests) == 1  # never re-verified
    assert len(bot_stub.requests) == 1  # delivered exactly once
    payment = get_payment(pg_session_factory, "pg-cbw")
    assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert payment.bot_notify_attempts == 1
    events = event_types(get_events(pg_session_factory, payment.id))
    assert events.count("gateway_payment_verified") == 1
    assert events.count("bot_notification_queued") == 1
    assert events.count("bot_notification_accepted") == 1


def test_race_review_acknowledge_against_callback(
    settings, pg_app, pg_session_factory, monkeypatch
):
    """Final audit race 7: the manual-review CLI acknowledging while a
    callback replays against the same payment. The callback can never reset
    review state; the acknowledgment is durably recorded."""
    import app.ops as ops_module
    from app.ops import main as ops_main

    stub = pg_app.state.centralpay_stub
    barrier = threading.Barrier(2)
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-rvc", amount=9000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-rvc")
        # Amount mismatch routes to manual review without a verified fact.
        stub.verify_result = verify_ok_response(amount=1, reference_id="REF-pg-rvc")
        callback_path = valid_callback_path(stub, payment.gateway_order_id)
        assert client.get(callback_path).status_code == 200
        assert get_payment(pg_session_factory, "pg-rvc").status == PaymentStatus.MANUAL_REVIEW.value

        monkeypatch.setattr(ops_module, "Settings", lambda: settings)
        monkeypatch.setattr(ops_module, "create_session_factory", lambda url: pg_session_factory)
        monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)

        def acknowledge():
            barrier.wait(timeout=10)
            return ops_main(["review", "acknowledge", "pg-rvc", "--note", "race-audit check"])

        def replay_callback():
            barrier.wait(timeout=10)
            return client.get(callback_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            ack_future = pool.submit(acknowledge)
            replay_future = pool.submit(replay_callback)
            ack_code = ack_future.result(timeout=60)
            replay = replay_future.result(timeout=60)

    assert ack_code == 0
    assert replay.status_code == 200
    assert 'data-status="under_review"' in replay.text  # review never reset
    payment = get_payment(pg_session_factory, "pg-rvc")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.review_acknowledged_at is not None  # ack survived the race
    assert payment.gateway_verified_at is None  # never fabricated
    assert len(stub.verify_requests) == 1  # manual review never re-verifies
    events = event_types(get_events(pg_session_factory, payment.id))
    assert "manual_review_acknowledged" in events


# --- dynamic fee: migration backfill, concurrency, db-check ------------------


def _alembic_upgrade(target: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic upgrade {target} failed:\n{result.stdout}\n{result.stderr}"
    )


def test_alembic_stepwise_upgrade(pg_engine):
    """Every revision applies individually in order (the release gate)."""
    for revision in ("0001", "0002", "0003", "0004", "0005", "0006"):
        _alembic_upgrade(revision)
    inspector = inspect(pg_engine)
    assert "fee_policies" in inspector.get_table_names()


def test_migration_0006_backfills_existing_payments(pg_engine):
    """Upgrading a database that already contains payments must backfill
    them as fee-less: payable_amount = amount, zero rate, zero fee, no
    policy reference — their financial meaning is unchanged."""
    _alembic_upgrade("0005")
    with pg_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO payments (bot_order_id, gateway_order_id,"
                " gateway_user_id, amount, status) VALUES"
                " ('legacy-1', 100000000001, 4242, 250000, 'link_created')"
            )
        )
    _alembic_upgrade("head")

    with pg_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT amount, fee_policy_id, fee_rate_bps, fee_amount,"
                " payable_amount FROM payments WHERE bot_order_id = 'legacy-1'"
            )
        ).one()
    assert row.amount == 250000
    assert row.fee_policy_id is None
    assert row.fee_rate_bps == 0
    assert row.fee_amount == 0
    assert row.payable_amount == 250000

    inspector = inspect(pg_engine)
    assert "fee_policies" in inspector.get_table_names()
    payment_checks = {c["name"] for c in inspector.get_check_constraints("payments")}
    assert {
        "ck_payments_fee_rate_bps_range",
        "ck_payments_fee_amount_non_negative",
        "ck_payments_payable_positive",
        "ck_payments_payable_equals_amount_plus_fee",
    } <= payment_checks
    policy_checks = {c["name"] for c in inspector.get_check_constraints("fee_policies")}
    assert {
        "ck_fee_policies_rate_bps_range",
        "ck_fee_policies_note_not_empty",
        "ck_fee_policies_cancellation_consistent",
    } <= policy_checks


def _add_pg_policy(pg_session_factory, rate_bps: int, *, effective_at) -> int:
    from app.models import FeePolicy

    with pg_session_factory() as db:
        policy = FeePolicy(
            rate_bps=rate_bps,
            effective_at=effective_at,
            created_by="test",
            note="pg fee test",
        )
        db.add(policy)
        db.commit()
        return policy.id


def test_concurrent_create_and_fee_change_snapshot_never_mixed(
    settings, pg_app, pg_session_factory
):
    """A fee change racing a payment creation: the snapshot derives from a
    single policy read — entirely the old rate or entirely the new one,
    never a mixture — and getLink is asked for exactly the stored payable."""
    from datetime import UTC, datetime

    from app.models import FeePolicy

    _add_pg_policy(
        pg_session_factory, 1000, effective_at=datetime(2020, 1, 1, tzinfo=UTC)
    )

    barrier = threading.Barrier(2)
    stub = pg_app.state.centralpay_stub

    def submit(client):
        barrier.wait(timeout=10)
        return create_order(client, settings, order_id="pg-fee-race", amount=500_000)

    def change_fee():
        barrier.wait(timeout=10)
        with pg_session_factory() as db:
            db.add(
                FeePolicy(
                    rate_bps=250,
                    effective_at=datetime(2020, 1, 2, tzinfo=UTC),
                    created_by="test",
                    note="pg fee race change",
                )
            )
            db.commit()

    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        create_future = pool.submit(submit, client)
        change_future = pool.submit(change_fee)
        response = create_future.result(timeout=60)
        change_future.result(timeout=60)

    assert response.status_code == 200
    payment = get_payment(pg_session_factory, "pg-fee-race")
    # Whichever policy won the race, the snapshot is internally consistent.
    assert (payment.fee_rate_bps, payment.fee_amount, payment.payable_amount) in {
        (1000, 50_000, 550_000),
        (250, 12_500, 512_500),
    }
    assert payment.amount == 500_000
    assert stub.getlink_requests[-1]["amount"] == payment.payable_amount


def test_concurrent_identical_creates_single_fee_snapshot(
    settings, pg_app, pg_session_factory
):
    """Identical concurrent creates with an active fee: one row, one fee
    snapshot event, one getLink carrying the payable amount."""
    from datetime import UTC, datetime

    _add_pg_policy(
        pg_session_factory, 1000, effective_at=datetime(2020, 1, 1, tzinfo=UTC)
    )
    barrier = threading.Barrier(2)
    stub = pg_app.state.centralpay_stub

    def submit(client):
        barrier.wait(timeout=10)
        return create_order(client, settings, order_id="pg-fee-dup", amount=500_000)

    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(submit, client) for _ in range(2)]
        responses = [future.result(timeout=60) for future in futures]

    assert [r.status_code for r in responses] == [200, 200]
    assert len({r.json()["url"] for r in responses}) == 1
    assert len(stub.getlink_requests) == 1
    assert stub.getlink_requests[0]["amount"] == 550_000

    with pg_session_factory() as session:
        rows = session.execute(
            select(Payment).where(Payment.bot_order_id == "pg-fee-dup")
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].payable_amount == 550_000
    events = event_types(get_events(pg_session_factory, rows[0].id))
    assert events.count("payment_fee_snapshotted") == 1


def test_check_constraints_reject_inconsistent_fee_rows(pg_engine, pg_session_factory):
    """The 0006 CHECK constraints are enforced by PostgreSQL itself: a row
    whose payable does not equal amount + fee cannot exist."""
    from sqlalchemy.exc import IntegrityError

    with (
        pg_engine.begin() as connection,
        pytest.raises(IntegrityError, match="ck_payments_payable"),
    ):
        connection.execute(
            text(
                "INSERT INTO payments (bot_order_id, gateway_order_id,"
                " gateway_user_id, amount, fee_rate_bps, fee_amount,"
                " payable_amount, status) VALUES"
                " ('pg-bad-fee', 100000000002, 4242, 10000, 1000, 1000,"
                " 10500, 'created')"
            )
        )


def test_db_check_detects_policyless_fee_corruption(
    settings, pg_engine, monkeypatch, capsys
):
    """db-check must detect fee corruption that the DB CHECK constraints
    cannot express (a policy-less payment carrying a fee rate), report it,
    exit non-zero, and never alter the financial fields."""
    _alembic_upgrade("head")
    session_factory = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    stub = CentralPayStub()
    application = build_app(settings, session_factory, stub)
    with TestClient(application, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-check", amount=10000).status_code == 200
    application.state.centralpay.close()

    import app.ops as ops_module
    from app.ops import main as ops_main

    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)

    assert ops_main(["db-check"]) == 0  # healthy before corruption
    capsys.readouterr()

    # Corrupt: a fee rate appears on a payment that references no policy.
    # Every DB CHECK still holds (payable == amount + fee_amount), so only
    # db-check can surface this.
    with pg_engine.begin() as connection:
        connection.execute(
            text("UPDATE payments SET fee_rate_bps = 500 WHERE bot_order_id = 'pg-check'")
        )

    assert ops_main(["db-check"]) == 1
    out = capsys.readouterr().out
    assert "policyless_payment_with_fee" in out

    # Read-only: the corruption was reported, not silently "repaired".
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "pg-check")
        ).scalar_one()
    assert payment.fee_rate_bps == 500
    assert payment.fee_amount == 0


def test_concurrent_ensure_initial_creates_exactly_one_policy(
    settings, pg_session_factory, monkeypatch
):
    """Zero-based audit: two installer reruns racing `fee set
    --ensure-initial` are serialized by a transaction-level PostgreSQL
    advisory lock — exactly one initial policy row can ever exist."""
    import app.ops as ops_module
    from app.models import FeePolicy
    from app.ops import main as ops_main

    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: pg_session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)

    barrier = threading.Barrier(2)

    def initialize(rate: str) -> int:
        barrier.wait(timeout=10)
        return ops_main(
            ["fee", "set", rate, "--note", "Initial installation fee",
             "--actor", "installer", "--ensure-initial"]
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize, rate) for rate in ("10", "5")]
        results = [future.result(timeout=60) for future in futures]

    assert results == [0, 0]  # the loser is a clean no-op, not an error
    with pg_session_factory() as db:
        policies = db.execute(select(FeePolicy)).scalars().all()
    assert len(policies) == 1
    assert policies[0].rate_bps in (1000, 500)  # whichever won the lock
