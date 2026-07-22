"""Reconciliation under real PostgreSQL: locking races and migration 0010.

Proves the concurrency guarantees that SQLite cannot (FOR UPDATE SKIP LOCKED
and real row-lock waits): two workers can never settle one payment twice, a
browser callback racing a reconciliation attempt serializes on the row lock
and takes the duplicate path, and the bot notification is queued exactly
once. Also proves the production migration path: revision 0008 (deployed) ->
``alembic upgrade head`` runs 0009 + 0010, is idempotent/recovery-safe, and
the downgrade is non-destructive.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import concurrent.futures
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.centralpay import CentralPayClient
from app.models import Base, Payment, PaymentEvent, PaymentStatus
from app.services.reconciliation import run_reconciliation_pass
from tests.conftest import (
    CentralPayStub,
    build_app,
    create_order,
    valid_callback_path,
    verify_ok_response,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
REPO_ROOT = Path(__file__).resolve().parents[2]

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


def _client_for(settings, stub) -> CentralPayClient:
    return CentralPayClient(
        base_url=settings.centralpay_base_url,
        getlink_api_key=settings.centralpay_getlink_api_key,
        verify_api_key=settings.centralpay_verify_api_key,
        timeout_seconds=settings.centralpay_timeout_seconds,
        transport=httpx.MockTransport(stub.handler),
    )


def _make_stale_link(client, settings, session_factory, *, order_id):
    assert create_order(client, settings, order_id=order_id).status_code == 200
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(
            seconds=settings.reconciliation_min_age_seconds + 60
        )
        db.commit()
        return payment


def _queued_count(session_factory, payment_id) -> int:
    with session_factory() as db:
        return len(
            db.execute(
                select(PaymentEvent).where(
                    PaymentEvent.payment_id == payment_id,
                    PaymentEvent.event_type == "bot_notification_queued",
                )
            ).all()
        )


def test_two_workers_cannot_settle_one_payment_twice(
    settings, pg_app, pg_session_factory
):
    """Both workers race on the SAME due payment; the slow verify keeps the
    winner's row lock held while the loser's SKIP LOCKED selection runs, so
    the loser skips the row entirely: one verify call, one settlement, one
    queued notification."""
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        payment = _make_stale_link(
            client, settings, pg_session_factory, order_id="race-two-workers"
        )
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id="REF-race-1"
    )
    stub.verify_delay_seconds = 0.5  # widen the race window
    barrier = threading.Barrier(2)

    def run(worker_id: str):
        gateway = _client_for(settings, stub)
        try:
            with pg_session_factory() as db:
                barrier.wait(timeout=30)
                return run_reconciliation_pass(
                    db, gateway, settings, worker_id=worker_id
                )
        finally:
            gateway.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        stats = [
            f.result(timeout=60)
            for f in [pool.submit(run, "worker-A"), pool.submit(run, "worker-B")]
        ]

    assert sum(s["verified"] for s in stats) == 1  # exactly one settlement
    assert len(stub.verify_requests) == 1  # the gateway saw ONE verify
    with pg_session_factory() as db:
        settled = db.execute(
            select(Payment).where(Payment.bot_order_id == "race-two-workers")
        ).scalar_one()
    assert settled.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert _queued_count(pg_session_factory, settled.id) == 1


def test_callback_and_reconciliation_race_settles_once(
    settings, pg_app, pg_session_factory
):
    """The real signed browser callback arrives WHILE reconciliation holds the
    payment's row lock across its verify call: the callback waits on the lock
    and then takes the normal duplicate path. One verify, one settlement, one
    queued notification — in either winning order."""
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        payment = _make_stale_link(
            client, settings, pg_session_factory, order_id="race-callback"
        )
        callback_path = valid_callback_path(stub, payment.gateway_order_id)
        stub.verify_result = verify_ok_response(
            amount=10000, user_id=payment.gateway_user_id, reference_id="REF-race-2"
        )
        stub.verify_delay_seconds = 0.5
        barrier = threading.Barrier(2)

        def reconcile():
            gateway = _client_for(settings, stub)
            try:
                with pg_session_factory() as db:
                    barrier.wait(timeout=30)
                    return run_reconciliation_pass(
                        db, gateway, settings, worker_id="race-worker"
                    )
            finally:
                gateway.close()

        def browser_callback():
            barrier.wait(timeout=30)
            return client.get(callback_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            recon_future = pool.submit(reconcile)
            callback_future = pool.submit(browser_callback)
            recon_stats = recon_future.result(timeout=60)
            response = callback_future.result(timeout=60)

    assert response.status_code == 200  # the payer always gets a success page
    assert len(stub.verify_requests) == 1  # verified exactly once, ever
    with pg_session_factory() as db:
        settled = db.execute(
            select(Payment).where(Payment.bot_order_id == "race-callback")
        ).scalar_one()
        events = [
            e.event_type
            for e in db.execute(
                select(PaymentEvent).where(PaymentEvent.payment_id == settled.id)
            ).scalars()
        ]
    assert settled.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert settled.gateway_verified_at is not None
    assert _queued_count(pg_session_factory, settled.id) == 1
    # Whoever lost the race left the duplicate/no-op trail, never a second
    # settlement.
    assert events.count("gateway_payment_verified") == 1
    assert recon_stats["processed"] in (0, 1)


# --- migration 0010 from the deployed production revision ---------------------


def _alembic(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout + result.stderr


def _column_names(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _index_names(engine, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(engine).get_indexes(table)}


def _alembic_version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


_RECON_COLUMNS = {
    "reconciliation_attempts",
    "reconciliation_next_at",
    "reconciliation_last_at",
    "reconciliation_last_error_code",
    "reconciliation_claimed_at",
    "reconciliation_claimed_by",
}


def test_migration_0010_from_production_0008(settings, pg_engine):
    """From the deployed production revision (0008): `alembic upgrade head`
    runs 0009 + 0010; existing link_created rows need no data migration and
    become due automatically; re-upgrade and the non-destructive downgrade
    are safe; and the app reconciles the previously stuck row."""
    _alembic("upgrade", "0008")
    assert _alembic_version(pg_engine) == "0008"
    assert _RECON_COLUMNS.isdisjoint(_column_names(pg_engine, "payments"))

    # A production-shaped stuck payment written by the 0008-era system.
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO payments"
                " (bot_order_id, gateway_order_id, gateway_user_id, amount,"
                "  payable_amount, status, redirect_url, callback_token_issued_at)"
                " VALUES ('stuck-1', 910000000501, 55501234, 10000, 10000,"
                "  'link_created', 'https://gateway.test/pay/stuck-1', :ti)"
            ),
            {"ti": datetime.now(UTC) - timedelta(hours=3)},
        )

    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0010"
    assert _column_names(pg_engine, "payments") >= _RECON_COLUMNS
    assert "ix_payments_reconciliation_due" in _index_names(pg_engine, "payments")
    with pg_engine.connect() as conn:
        attempts, next_at = conn.execute(
            text(
                "SELECT reconciliation_attempts, reconciliation_next_at"
                " FROM payments WHERE bot_order_id = 'stuck-1'"
            )
        ).one()
    assert attempts == 0 and next_at is None  # due immediately, no backfill

    # Recovery safety: re-upgrade over existing schema and the pointer-only
    # downgrade both no-op destructively.
    _alembic("stamp", "0009")
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0010"
    _alembic("downgrade", "0009")
    assert _alembic_version(pg_engine) == "0009"
    assert _column_names(pg_engine, "payments") >= _RECON_COLUMNS  # preserved
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0010"

    # The previously stuck payment now reconciles through the normal path.
    stub = CentralPayStub()
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=55501234, reference_id="REF-stuck-1"
    )
    session_factory = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    gateway = _client_for(settings, stub)
    try:
        with session_factory() as db:
            stats = run_reconciliation_pass(db, gateway, settings, worker_id="mig-worker")
    finally:
        gateway.close()
    assert stats["verified"] == 1
    with pg_engine.connect() as conn:
        status, verified_at = conn.execute(
            text(
                "SELECT status, gateway_verified_at FROM payments"
                " WHERE bot_order_id = 'stuck-1'"
            )
        ).one()
    assert status == "bot_notify_pending"
    assert verified_at is not None
