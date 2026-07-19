"""Admin-alert claim ownership on real PostgreSQL.

The defect under test: record_delivery_result locked only by alert id and
unconditionally cleared the claim and wrote state, so a stale worker
whose claim had been released (and re-claimed by a successor) could
overwrite the successor's active claim, cancel its retry, or forge
delivery history. Results are now persisted only while the row still
carries the same worker id AND attempt number, verified under FOR UPDATE.

Deterministic: injected clocks, no sleeps (the only waits are on real
database row locks and thread joins).
"""

import asyncio
import logging
import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from app.adminbot.alerts import (
    ALERT_RETRY_DELAYS_SECONDS,
    ClaimedAlert,
    claim_due_alerts,
    create_alert,
    record_delivery_result,
    release_stale_alert_claims,
)
from app.models import AdminAlert, AlertStatus, Base, PaymentEvent
from tests.conftest import FakeAlertSender

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
SENTINEL = "HN7RD4QSW2"  # planted in payloads/errors; must never leak


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
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
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    Base.metadata.create_all(pg_engine)
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


def _new_alert(session_factory, payload=None) -> int:
    with session_factory() as db:
        alert = create_alert(
            db, alert_type="admin_test_alert", payload=payload, now=T0
        )
        db.commit()
        return alert.id


def _claim(session_factory, worker_id: str, now=T0) -> ClaimedAlert:
    with session_factory() as db:
        [claimed] = claim_due_alerts(db, worker_id=worker_id, now=now)
    return claimed


def _make_claim_stale(session_factory, settings, alert_id: int) -> None:
    stale_at = T0 - timedelta(
        seconds=settings.admin_bot_alert_claim_timeout_seconds + 1
    )
    with session_factory() as db:
        db.execute(
            update(AdminAlert).where(AdminAlert.id == alert_id).values(claimed_at=stale_at)
        )
        db.commit()


def _release_and_reclaim(session_factory, settings, alert_id: int, successor: str):
    _make_claim_stale(session_factory, settings, alert_id)
    with session_factory() as db:
        assert release_stale_alert_claims(db, settings, now=T0) == 1
    return _claim(session_factory, successor, now=T0)


def _record(session_factory, settings, claimed, *, delivered=1, retryable=False,
            error_code=None, now=None) -> str:
    with session_factory() as db:
        return record_delivery_result(
            db,
            settings,
            claimed,
            delivered_count=delivered,
            retryable=retryable,
            retry_after_seconds=None,
            error_code=error_code,
            now=now or T0,
            jitter=lambda: 1.0,
        )


def _alert_row(session_factory, alert_id: int) -> AdminAlert:
    with session_factory() as db:
        return db.get(AdminAlert, alert_id)


def _event_types(session_factory) -> list[str]:
    with session_factory() as db:
        return list(db.execute(select(PaymentEvent.event_type)).scalars())


# --- matching claims behave exactly as before --------------------------------


def test_matching_claim_delivered(pg_session_factory, admin_settings):
    alert_id = _new_alert(pg_session_factory)
    claimed = _claim(pg_session_factory, "bot-a")
    assert claimed.worker_id == "bot-a" and claimed.attempts == 1

    assert _record(pg_session_factory, admin_settings, claimed, delivered=2) == "delivered"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.DELIVERED.value
    assert row.claimed_at is None and row.claimed_by is None
    assert row.delivered_at == T0
    assert _event_types(pg_session_factory).count("admin_alert_delivered") == 1


def test_matching_claim_retry_backoff_unchanged(pg_session_factory, admin_settings):
    alert_id = _new_alert(pg_session_factory)
    claimed = _claim(pg_session_factory, "bot-a")
    status = _record(
        pg_session_factory, admin_settings, claimed,
        delivered=0, retryable=True, error_code="telegram_http_500",
    )
    assert status == "retry_scheduled"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.RETRY_SCHEDULED.value
    assert row.claimed_at is None and row.claimed_by is None
    # Existing backoff semantics: first-attempt delay, jitter pinned to 1.0.
    assert row.next_retry_at == T0 + timedelta(seconds=ALERT_RETRY_DELAYS_SECONDS[0])
    assert row.last_error_code == "telegram_http_500"


def test_matching_claim_permanent_failure(pg_session_factory, admin_settings):
    alert_id = _new_alert(pg_session_factory)
    claimed = _claim(pg_session_factory, "bot-a")
    status = _record(
        pg_session_factory, admin_settings, claimed,
        delivered=0, retryable=False, error_code="telegram_forbidden",
    )
    assert status == "failed"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.FAILED.value
    assert _event_types(pg_session_factory).count("admin_alert_failed") == 1


# --- stale results are discarded without touching the successor --------------


def test_stale_worker_cannot_overwrite_successor_claim(
    pg_session_factory, admin_settings, caplog
):
    alert_id = _new_alert(pg_session_factory, payload={"note": SENTINEL})
    stale_claim = _claim(pg_session_factory, "bot-a")
    successor = _release_and_reclaim(pg_session_factory, admin_settings, alert_id, "bot-b")
    assert successor.attempts == 2

    with caplog.at_level(logging.DEBUG, logger="app.adminbot.alerts"):
        status = _record(
            pg_session_factory, admin_settings, stale_claim,
            delivered=3, error_code=f"late-{SENTINEL}",
        )
    assert status == "discarded"

    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.SENDING.value  # successor claim intact
    assert row.claimed_by == "bot-b"
    assert row.attempts == 2
    assert row.claimed_at == T0  # Worker B's claim time
    assert row.next_retry_at == T0  # set by the stale release; unchanged since
    assert row.delivered_at is None
    assert row.last_error_code is None  # the stale error code was never stored

    events = _event_types(pg_session_factory)
    assert events.count("admin_alert_result_discarded") == 1
    assert "admin_alert_delivered" not in events
    assert "admin_alert_failed" not in events

    # Logging/audit safety: fixed name + safe metadata only.
    discard_logs = [
        r
        for r in caplog.records
        if r.getMessage() == "admin_alert_result_discarded"
        and r.name == "app.adminbot.alerts"  # app.audit logs the event too
    ]
    assert len(discard_logs) == 1
    assert discard_logs[0].stale_worker_id == "bot-a"
    assert discard_logs[0].stale_attempt == 1
    assert SENTINEL not in caplog.text  # payload/error text never logged
    with pg_session_factory() as db:
        [event] = db.execute(
            select(PaymentEvent).where(
                PaymentEvent.event_type == "admin_alert_result_discarded"
            )
        ).scalars()
        assert set(event.data) == {
            "alert_id", "stale_attempt", "stale_worker_id",
            "observed_status", "observed_attempts",
        }
        assert SENTINEL not in repr(event.data)


def test_successor_still_completes_after_discarded_stale_result(
    pg_session_factory, admin_settings
):
    alert_id = _new_alert(pg_session_factory)
    stale_claim = _claim(pg_session_factory, "bot-a")
    successor = _release_and_reclaim(pg_session_factory, admin_settings, alert_id, "bot-b")
    assert _record(pg_session_factory, admin_settings, stale_claim) == "discarded"

    # Worker B's own result applies normally.
    assert _record(pg_session_factory, admin_settings, successor, delivered=1) == "delivered"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.DELIVERED.value
    assert row.delivered_at == T0
    events = _event_types(pg_session_factory)
    assert events.count("admin_alert_delivered") == 1  # exactly one terminal event
    assert events.count("admin_alert_failed") == 0
    assert events.count("admin_alert_result_discarded") == 1


def test_same_worker_id_reuse_is_protected_by_attempt_number(
    pg_session_factory, admin_settings
):
    """A fixed/configured worker id re-claims its own stale alert: the old
    attempt-1 claim must still be discarded — identity alone is not
    ownership."""
    alert_id = _new_alert(pg_session_factory)
    first_claim = _claim(pg_session_factory, "adminbot-fixed")
    second_claim = _release_and_reclaim(
        pg_session_factory, admin_settings, alert_id, "adminbot-fixed"
    )
    assert second_claim.worker_id == first_claim.worker_id  # same identity
    assert second_claim.attempts == 2

    assert _record(pg_session_factory, admin_settings, first_claim) == "discarded"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.SENDING.value
    assert row.attempts == 2  # attempt-2 claim untouched
    # The live attempt still completes.
    assert _record(pg_session_factory, admin_settings, second_claim) == "delivered"


def test_stale_failure_cannot_overwrite_successor_success(
    pg_session_factory, admin_settings
):
    alert_id = _new_alert(pg_session_factory)
    stale_claim = _claim(pg_session_factory, "bot-a")
    successor = _release_and_reclaim(pg_session_factory, admin_settings, alert_id, "bot-b")
    assert _record(pg_session_factory, admin_settings, successor, delivered=1) == "delivered"

    # The old worker reports BOTH failure flavors late; neither applies.
    for retryable in (True, False):
        status = _record(
            pg_session_factory, admin_settings, stale_claim,
            delivered=0, retryable=retryable, error_code="late_failure",
        )
        assert status == "discarded"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.DELIVERED.value  # delivered state intact
    assert row.delivered_at == T0
    assert row.last_error_code is None
    events = _event_types(pg_session_factory)
    assert events.count("admin_alert_delivered") == 1
    assert events.count("admin_alert_failed") == 0
    assert "admin_alert_retry_scheduled" not in events  # no retry re-added


def test_stale_success_cannot_overwrite_successor_retry(
    pg_session_factory, admin_settings
):
    alert_id = _new_alert(pg_session_factory)
    stale_claim = _claim(pg_session_factory, "bot-a")
    successor = _release_and_reclaim(pg_session_factory, admin_settings, alert_id, "bot-b")
    status = _record(
        pg_session_factory, admin_settings, successor,
        delivered=0, retryable=True, error_code="telegram_http_500",
    )
    assert status == "retry_scheduled"
    scheduled_retry = _alert_row(pg_session_factory, alert_id).next_retry_at

    # The stale worker's late "success" must not cancel the pending retry.
    assert _record(pg_session_factory, admin_settings, stale_claim, delivered=5) == "discarded"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.status == AlertStatus.RETRY_SCHEDULED.value
    assert row.next_retry_at == scheduled_retry
    assert row.delivered_at is None
    assert "admin_alert_delivered" not in _event_types(pg_session_factory)


def test_ownership_check_holds_the_row_lock(pg_session_factory, admin_settings):
    """Two-session TOCTOU proof: the stale worker's record call blocks on
    FOR UPDATE while another session mutates the claim; when the lock is
    granted, the predicate sees the COMMITTED successor claim and
    discards — there is no check-then-write window."""
    alert_id = _new_alert(pg_session_factory)
    stale_claim = _claim(pg_session_factory, "bot-a")

    lock_session = pg_session_factory()
    result: dict[str, str] = {}
    started = threading.Event()

    # Session 1 takes and HOLDS the row lock.
    lock_session.execute(
        select(AdminAlert).where(AdminAlert.id == alert_id).with_for_update()
    )

    def stale_record():
        started.set()
        result["status"] = _record(pg_session_factory, admin_settings, stale_claim)

    thread = threading.Thread(target=stale_record)
    thread.start()
    assert started.wait(timeout=10)

    # While the stale worker is blocked on the lock, hand the claim to a
    # successor and commit (which releases the lock).
    lock_session.execute(
        update(AdminAlert)
        .where(AdminAlert.id == alert_id)
        .values(claimed_by="bot-b", attempts=2, claimed_at=T0)
    )
    lock_session.commit()
    lock_session.close()

    thread.join(timeout=30)
    assert not thread.is_alive()
    assert result["status"] == "discarded"
    row = _alert_row(pg_session_factory, alert_id)
    assert row.claimed_by == "bot-b" and row.attempts == 2  # successor intact


def test_discarded_result_does_not_break_delivery_helpers(
    pg_session_factory, admin_settings
):
    """deliver_claimed_alert returns 'discarded' without raising, so the
    polling pass continues."""
    from app.adminbot.alerts import deliver_claimed_alert

    alert_id = _new_alert(pg_session_factory)
    stale_claim = _claim(pg_session_factory, "bot-a")
    _release_and_reclaim(pg_session_factory, admin_settings, alert_id, "bot-b")

    sender = FakeAlertSender()
    status = asyncio.run(
        deliver_claimed_alert(
            pg_session_factory,
            sender,
            admin_settings,
            (111,),
            stale_claim,
            now_fn=lambda: T0,
            jitter=lambda: 1.0,
        )
    )
    assert status == "discarded"
