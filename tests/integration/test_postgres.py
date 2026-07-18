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
