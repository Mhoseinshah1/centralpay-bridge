"""PostgreSQL concurrency tests for the monitoring incident lifecycle.

Proves the properties SQLite (single-writer, no real row locking under
`with_for_update()`) cannot: two monitor instances racing to open the same
incident produce exactly one open row and exactly one alert; a racing
recovery closes cleanly; a simulated process restart never re-opens an
incident as new; and no monitoring check ever mutates financial Payment
data, even under real Postgres transactional semantics.

Run only when TEST_DATABASE_URL points at a disposable PostgreSQL database:

    export TEST_DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/centralpay_test'
    pytest -m postgres
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.models import AdminAlert, Base, MonitorIncident, MonitorIncidentStatus, Payment
from app.services.monitor_checks import CheckResult, run_all_checks
from app.services.monitor_incidents import (
    TRANSITION_CLOSED,
    TRANSITION_OPENED,
    TRANSITION_UNCHANGED_UNHEALTHY,
    record_check_result,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

_TABLES = (
    "monitor_incidents",
    "admin_alerts",
    "worker_heartbeats",
    "payment_events",
    "payments",
    "fee_policies",
    "centralpay_payer_identities",
    "alembic_version",
)


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


def _incidents(pg_session_factory) -> list[MonitorIncident]:
    with pg_session_factory() as db:
        return list(db.execute(select(MonitorIncident)).scalars())


def _alerts(pg_session_factory, alert_type: str) -> list[AdminAlert]:
    with pg_session_factory() as db:
        return list(
            db.execute(
                select(AdminAlert).where(AdminAlert.alert_type == alert_type)
            ).scalars()
        )


def test_concurrent_open_produces_exactly_one_incident_and_alert(
    pg_session_factory, admin_settings
):
    """N monitor instances all observe the SAME new failure at once. Only
    one may win opening the incident row (the partial unique index);
    everyone else must fall back to the update path -- never a duplicate
    row, never a duplicate admin_alerts row."""
    now = datetime.now(UTC)
    result = CheckResult("disk_space", "critical", "low_disk_space", {"free_percent": 1.0})
    barrier = threading.Barrier(8)

    def worker(_: int) -> str:
        barrier.wait()
        with pg_session_factory() as db:
            return record_check_result(db, admin_settings, result, now=now).transition

    with ThreadPoolExecutor(max_workers=8) as pool:
        transitions = list(pool.map(worker, range(8)))

    assert transitions.count(TRANSITION_OPENED) == 1
    assert all(t in (TRANSITION_OPENED, TRANSITION_UNCHANGED_UNHEALTHY) for t in transitions)

    incidents = _incidents(pg_session_factory)
    assert len(incidents) == 1
    assert incidents[0].status == MonitorIncidentStatus.OPEN.value

    alerts = _alerts(pg_session_factory, "monitor_incident_opened")
    assert len(alerts) == 1


def test_concurrent_recovery_closes_exactly_once(pg_session_factory, admin_settings):
    now = datetime.now(UTC)
    critical = CheckResult("disk_space", "critical", "low_disk_space", {"free_percent": 1.0})
    healthy = CheckResult("disk_space", "ok", "healthy", {"free_percent": 80.0})

    with pg_session_factory() as db:
        opened = record_check_result(db, admin_settings, critical, now=now)
    assert opened.transition == TRANSITION_OPENED

    barrier = threading.Barrier(8)

    def worker(_: int) -> str:
        barrier.wait()
        with pg_session_factory() as db:
            return record_check_result(db, admin_settings, healthy, now=now).transition

    with ThreadPoolExecutor(max_workers=8) as pool:
        transitions = list(pool.map(worker, range(8)))

    assert transitions.count(TRANSITION_CLOSED) == 1

    incidents = _incidents(pg_session_factory)
    assert len(incidents) == 1
    assert incidents[0].status == MonitorIncidentStatus.RESOLVED.value

    resolved_alerts = _alerts(pg_session_factory, "monitor_incident_resolved")
    assert len(resolved_alerts) == 1


def test_restart_simulation_persisted_incident_survives_without_a_new_alert(
    pg_session_factory, admin_settings
):
    """A monitor process opens an incident, then "restarts" (its session is
    closed and discarded, exactly like a container recreate). The next
    process picks up a FRESH session/engine connection and observes the
    same still-failing condition: the persisted row must be recognized as
    already open -- never re-created as a brand-new incident, and never a
    second 'opened' alert."""
    now = datetime.now(UTC)
    result = CheckResult(
        "worker_heartbeat:notification-worker", "critical", "heartbeat_stale", {"age_seconds": 999}
    )

    with pg_session_factory() as first_process_db:
        first = record_check_result(first_process_db, admin_settings, result, now=now)
    assert first.transition == TRANSITION_OPENED
    # Simulate the process/container actually going away.
    del first_process_db

    # A brand new engine + session factory, exactly like a fresh container.
    restarted_engine = create_engine(TEST_DATABASE_URL)
    try:
        restarted_session_factory = sessionmaker(
            bind=restarted_engine, expire_on_commit=False, autoflush=False
        )
        with restarted_session_factory() as db:
            second = record_check_result(db, admin_settings, result, now=now)
    finally:
        restarted_engine.dispose()

    assert second.transition == TRANSITION_UNCHANGED_UNHEALTHY
    assert second.incident_id == first.incident_id

    incidents = _incidents(pg_session_factory)
    assert len(incidents) == 1
    assert incidents[0].status == MonitorIncidentStatus.OPEN.value

    alerts = _alerts(pg_session_factory, "monitor_incident_opened")
    assert len(alerts) == 1  # never a second "new incident" alert after the restart


def test_monitoring_reads_never_mutate_financial_state(pg_session_factory, settings, monkeypatch):
    """A full run_all_checks pass (every check, including db_integrity)
    against a real payment row must leave every financial column
    byte-identical -- proves this on real Postgres, where a stray write
    that SQLite might silently tolerate would show up as a real committed
    change."""
    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, url):
            raise httpx.ConnectError("no route in this test")

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: _FailingClient())

    with pg_session_factory() as db:
        payment = Payment(
            bot_order_id="pg-mon-financial-1",
            gateway_order_id=990001,
            gateway_user_id=42,
            amount=25000,
            fee_rate_bps=250,
            fee_amount=625,
            payable_amount=25625,
            status="bot_notify_pending",
            gateway_verified_at=datetime.now(UTC),
            reference_id="REF-PG-MON-1",
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        payment_id = payment.id
        before = {
            "amount": payment.amount,
            "fee_rate_bps": payment.fee_rate_bps,
            "fee_amount": payment.fee_amount,
            "payable_amount": payment.payable_amount,
            "status": payment.status,
            "reference_id": payment.reference_id,
            "gateway_verified_at": payment.gateway_verified_at,
        }

    monitor_settings = settings.model_copy(update={"reconciliation_enabled": False})
    with pg_session_factory() as db:
        run_all_checks(db, monitor_settings, include_db_integrity=True)
        db.rollback()  # checks never call commit(); this is belt-and-braces

    with pg_session_factory() as db:
        after_payment = db.get(Payment, payment_id)
        after = {
            "amount": after_payment.amount,
            "fee_rate_bps": after_payment.fee_rate_bps,
            "fee_amount": after_payment.fee_amount,
            "payable_amount": after_payment.payable_amount,
            "status": after_payment.status,
            "reference_id": after_payment.reference_id,
            "gateway_verified_at": after_payment.gateway_verified_at,
        }
    assert after == before
