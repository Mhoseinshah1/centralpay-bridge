"""Unit tests for app.services.monitor_incidents (SQLite; no network).

Covers the anti-spam/durability contract: an incident opens exactly once,
the same still-unhealthy condition never re-alerts, an escalation alerts
once, a recovery alerts exactly once, a check that is healthy the entire
time never creates anything, and alert payloads never leak details beyond
the fixed safe allowlist.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import AdminAlert, MonitorIncident, MonitorIncidentStatus
from app.services.monitor_checks import CheckResult
from app.services.monitor_incidents import (
    TRANSITION_ALERT_CAUGHT_UP,
    TRANSITION_CLOSED,
    TRANSITION_ESCALATED,
    TRANSITION_OPENED,
    TRANSITION_UNCHANGED_HEALTHY,
    TRANSITION_UNCHANGED_UNHEALTHY,
    record_check_result,
)

CHECK_KEY = "disk_space"


def _incidents(session_factory) -> list[MonitorIncident]:
    with session_factory() as db:
        return list(db.execute(select(MonitorIncident)).scalars())


def _alerts(session_factory, alert_type: str | None = None) -> list[AdminAlert]:
    with session_factory() as db:
        stmt = select(AdminAlert)
        if alert_type is not None:
            stmt = stmt.where(AdminAlert.alert_type == alert_type)
        return list(db.execute(stmt).scalars())


def _critical(details: dict[str, object] | None = None) -> CheckResult:
    return CheckResult(CHECK_KEY, "critical", "low_disk_space", details or {"free_percent": 2.0})


def _warning(details: dict[str, object] | None = None) -> CheckResult:
    return CheckResult(CHECK_KEY, "warning", "low_disk_space", details or {"free_percent": 12.0})


def _healthy() -> CheckResult:
    return CheckResult(CHECK_KEY, "ok", "healthy", {"free_percent": 80.0})


def test_incident_opened_once(session_factory, admin_settings):
    now = datetime.now(UTC)
    with session_factory() as db:
        transition = record_check_result(db, admin_settings, _critical(), now=now)
    assert transition.transition == TRANSITION_OPENED

    incidents = _incidents(session_factory)
    assert len(incidents) == 1
    assert incidents[0].check_key == CHECK_KEY
    assert incidents[0].status == MonitorIncidentStatus.OPEN.value
    assert incidents[0].severity == "critical"

    alerts = _alerts(session_factory, "monitor_incident_opened")
    assert len(alerts) == 1


def test_same_incident_next_cycle_does_not_duplicate_alert(session_factory, admin_settings):
    now = datetime.now(UTC)
    with session_factory() as db:
        first = record_check_result(db, admin_settings, _critical(), now=now)
    with session_factory() as db:
        second = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(seconds=60)
        )
    assert first.transition == TRANSITION_OPENED
    assert second.transition == TRANSITION_UNCHANGED_UNHEALTHY
    assert second.incident_id == first.incident_id

    incidents = _incidents(session_factory)
    assert len(incidents) == 1  # never a duplicate row
    assert incidents[0].status == MonitorIncidentStatus.OPEN.value

    assert len(_alerts(session_factory, "monitor_incident_opened")) == 1
    assert len(_alerts(session_factory)) == 1  # nothing else was ever queued


def test_incident_severity_escalation(session_factory, admin_settings):
    now = datetime.now(UTC)
    with session_factory() as db:
        opened = record_check_result(db, admin_settings, _warning(), now=now)
    with session_factory() as db:
        escalated = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(seconds=60)
        )
    assert opened.transition == TRANSITION_OPENED
    assert escalated.transition == TRANSITION_ESCALATED
    assert escalated.incident_id == opened.incident_id

    incidents = _incidents(session_factory)
    assert len(incidents) == 1
    assert incidents[0].severity == "critical"

    assert len(_alerts(session_factory, "monitor_incident_opened")) == 1
    assert len(_alerts(session_factory, "monitor_incident_escalated")) == 1


def test_deescalation_still_unhealthy_never_alerts(session_factory, admin_settings):
    """critical -> warning (still unhealthy): severity updates silently,
    no new alert -- only a WORSENING condition is an escalation event."""
    now = datetime.now(UTC)
    with session_factory() as db:
        record_check_result(db, admin_settings, _critical(), now=now)
    with session_factory() as db:
        result = record_check_result(
            db, admin_settings, _warning(), now=now + timedelta(seconds=60)
        )
    assert result.transition == TRANSITION_UNCHANGED_UNHEALTHY
    incidents = _incidents(session_factory)
    assert incidents[0].severity == "warning"
    assert len(_alerts(session_factory)) == 1  # only the original "opened" alert


def test_recovery_closes_incident(session_factory, admin_settings):
    now = datetime.now(UTC)
    with session_factory() as db:
        record_check_result(db, admin_settings, _critical(), now=now)
    with session_factory() as db:
        closed = record_check_result(
            db, admin_settings, _healthy(), now=now + timedelta(minutes=5)
        )
    assert closed.transition == TRANSITION_CLOSED

    incidents = _incidents(session_factory)
    assert len(incidents) == 1
    assert incidents[0].status == MonitorIncidentStatus.RESOLVED.value
    assert incidents[0].resolved_at is not None


def test_recovery_alert_emitted_exactly_once(session_factory, admin_settings):
    now = datetime.now(UTC)
    with session_factory() as db:
        record_check_result(db, admin_settings, _critical(), now=now)
    with session_factory() as db:
        first_recovery = record_check_result(
            db, admin_settings, _healthy(), now=now + timedelta(minutes=5)
        )
    with session_factory() as db:
        second_healthy = record_check_result(
            db, admin_settings, _healthy(), now=now + timedelta(minutes=10)
        )
    assert first_recovery.transition == TRANSITION_CLOSED
    assert second_healthy.transition == TRANSITION_UNCHANGED_HEALTHY
    assert len(_alerts(session_factory, "monitor_incident_resolved")) == 1


def test_healthy_cycles_never_produce_persistent_spam(session_factory, admin_settings):
    now = datetime.now(UTC)
    for i in range(5):
        with session_factory() as db:
            result = record_check_result(
                db, admin_settings, _healthy(), now=now + timedelta(seconds=60 * i)
            )
        assert result.transition == TRANSITION_UNCHANGED_HEALTHY
    assert _incidents(session_factory) == []
    assert _alerts(session_factory) == []


def test_incident_reopens_as_a_new_episode_after_recovery(session_factory, admin_settings):
    """A check that flaps open -> recovered -> open again gets a SECOND,
    independent incident row (history preserved), never resurrects the
    resolved one, and alerts again on the new episode."""
    now = datetime.now(UTC)
    with session_factory() as db:
        first_open = record_check_result(db, admin_settings, _critical(), now=now)
    with session_factory() as db:
        record_check_result(db, admin_settings, _healthy(), now=now + timedelta(minutes=1))
    with session_factory() as db:
        second_open = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(minutes=2)
        )
    assert second_open.transition == TRANSITION_OPENED
    assert second_open.incident_id != first_open.incident_id

    incidents = _incidents(session_factory)
    assert len(incidents) == 2
    assert {i.status for i in incidents} == {
        MonitorIncidentStatus.OPEN.value,
        MonitorIncidentStatus.RESOLVED.value,
    }
    assert len(_alerts(session_factory, "monitor_incident_opened")) == 2


def test_alerts_disabled_still_persists_incident_but_never_queues_an_alert(
    session_factory, settings
):
    """admin_bot disabled entirely: incident lifecycle stays fully durable
    (so `centralpay monitor incidents` remains accurate) but no outbox row
    is ever created -- it would only pile up undelivered."""
    disabled = settings.model_copy(update={"admin_bot_enabled": False})
    now = datetime.now(UTC)
    with session_factory() as db:
        opened = record_check_result(db, disabled, _critical(), now=now)
    assert opened.transition == TRANSITION_OPENED
    assert len(_incidents(session_factory)) == 1
    assert _alerts(session_factory) == []


@pytest.mark.parametrize(
    ("toggle", "check_key"),
    [
        ("admin_bot_health_alerts", "disk_space"),
        ("admin_bot_backup_alerts", "backup"),
        ("admin_bot_manual_review_alerts", "manual_review"),
        ("admin_bot_error_alerts", "notification_backlog"),
    ],
)
def test_category_alert_toggle_suppresses_only_its_own_category(
    session_factory, admin_settings, toggle, check_key
):
    """Each ADMIN_BOT_*_ALERTS toggle gates only checks in its own category
    -- the incident still opens and is fully persisted (`/monitor` and
    `centralpay monitor incidents` stay accurate) but no outbox row is
    queued for a category the operator turned off."""
    disabled = admin_settings.model_copy(update={toggle: False})
    now = datetime.now(UTC)
    result = CheckResult(check_key, "critical", "unhealthy", {})
    with session_factory() as db:
        transition = record_check_result(db, disabled, result, now=now)
    assert transition.transition == TRANSITION_OPENED
    assert len(_incidents(session_factory)) == 1
    assert _alerts(session_factory) == []


def test_category_alert_toggle_does_not_suppress_other_categories(
    session_factory, admin_settings
):
    """Turning off manual-review alerts must not silence an unrelated
    health check -- the toggles are independent, not a single global
    switch."""
    disabled = admin_settings.model_copy(update={"admin_bot_manual_review_alerts": False})
    now = datetime.now(UTC)
    with session_factory() as db:
        transition = record_check_result(db, disabled, _critical(), now=now)
    assert transition.transition == TRANSITION_OPENED
    assert len(_alerts(session_factory, "monitor_incident_opened")) == 1


def test_no_open_incident_before_first_failure_is_a_true_no_op(session_factory, admin_settings):
    now = datetime.now(UTC)
    with session_factory() as db:
        result = record_check_result(db, admin_settings, _healthy(), now=now)
    assert result.transition == TRANSITION_UNCHANGED_HEALTHY
    assert result.incident_id is None


def test_alert_payload_never_leaks_beyond_the_safe_allowlist(session_factory, admin_settings):
    """CheckResult.details may carry arbitrary check-specific keys (kept in
    MonitorIncident.details for `centralpay monitor incidents`), but the
    Telegram-bound AdminAlert payload only ever forwards the fixed
    check/detail/count subset -- never raw URLs, error strings, or
    anything else a future check might put in .details."""
    now = datetime.now(UTC)
    dangerous = CheckResult(
        CHECK_KEY,
        "critical",
        "low_disk_space",
        {
            "count": 3,
            "free_percent": 1.0,
            "url": "https://user:s3cr3t-token@internal.example/health",
            "raw_exception": "Traceback: password=hunter2",
        },
    )
    with session_factory() as db:
        record_check_result(db, admin_settings, dangerous, now=now)
    alerts = _alerts(session_factory, "monitor_incident_opened")
    assert len(alerts) == 1
    payload = alerts[0].payload or {}
    assert set(payload) <= {"check", "detail", "count"}
    serialized = str(payload)
    assert "s3cr3t-token" not in serialized
    assert "hunter2" not in serialized
    assert "raw_exception" not in serialized

    # The full details -- including the check-specific url/free_percent --
    # ARE kept on the incident row itself for `centralpay monitor incidents`,
    # which is a host-CLI-only, never-Telegram surface.
    incidents = _incidents(session_factory)
    incident_details = incidents[0].details or {}
    assert incident_details["free_percent"] == 1.0


def test_incident_opened_while_disabled_is_alerted_once_delivery_becomes_available(
    session_factory, settings, admin_settings
):
    """An incident that opens while ADMIN_BOT_ENABLED=false is persisted but
    never queued for delivery; the very next cycle that observes it still
    open, with delivery now available, must send exactly one catch-up
    "opened" alert -- never silently drop the notification just because the
    state transition itself happened earlier (this is the exact sequence
    `centralpay monitor enable` then `centralpay admin-bot enable`
    produces)."""
    now = datetime.now(UTC)
    with session_factory() as db:
        opened = record_check_result(db, settings, _critical(), now=now)
    assert opened.transition == TRANSITION_OPENED
    assert _alerts(session_factory) == []  # nothing queued yet -- alerts disabled

    with session_factory() as db:
        caught_up = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(minutes=1)
        )
    assert caught_up.transition == TRANSITION_ALERT_CAUGHT_UP
    assert caught_up.incident_id == opened.incident_id

    incidents = _incidents(session_factory)
    assert len(incidents) == 1  # same row, never duplicated
    opened_alerts = _alerts(session_factory, "monitor_incident_opened")
    assert len(opened_alerts) == 1

    # And it never re-fires on a further still-unhealthy cycle.
    with session_factory() as db:
        again = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(minutes=2)
        )
    assert again.transition == TRANSITION_UNCHANGED_UNHEALTHY
    assert len(_alerts(session_factory, "monitor_incident_opened")) == 1


def test_escalation_flap_within_dedup_window_alerts_on_every_worsening(
    session_factory, admin_settings
):
    """warning -> critical -> warning -> critical, all within
    create_alert's 30-minute rolling dedup window: each worsening
    transition is a genuinely distinct, alert-worthy event and must not be
    silently swallowed as a "duplicate" of the earlier escalation just
    because they'd otherwise share one dedup key."""
    now = datetime.now(UTC)
    with session_factory() as db:
        record_check_result(db, admin_settings, _warning(), now=now)
    with session_factory() as db:
        first_escalation = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(minutes=1)
        )
    with session_factory() as db:
        record_check_result(db, admin_settings, _warning(), now=now + timedelta(minutes=2))
    with session_factory() as db:
        second_escalation = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(minutes=3)
        )
    assert first_escalation.transition == TRANSITION_ESCALATED
    assert second_escalation.transition == TRANSITION_ESCALATED

    escalation_alerts = _alerts(session_factory, "monitor_incident_escalated")
    assert len(escalation_alerts) == 2  # neither suppressed as a false duplicate


def test_escalation_while_disabled_is_caught_up_once_delivery_resumes(
    session_factory, settings, admin_settings
):
    """An incident opens (and is alerted) at warning, then escalates to
    critical while delivery is unavailable -- the escalation itself must
    not be silently lost forever just because SOME alert (for the old,
    lower severity) already went out for this incident. The next cycle
    that observes it still critical, with delivery available again, must
    send exactly one catch-up alert -- and that alert must not collide
    with the original opening alert's deduplication key and get itself
    silently marked suppressed."""
    now = datetime.now(UTC)
    with session_factory() as db:
        opened = record_check_result(db, admin_settings, _warning(), now=now)
    assert opened.transition == TRANSITION_OPENED
    assert len(_alerts(session_factory, "monitor_incident_opened")) == 1

    # Delivery goes away, then the condition worsens to critical.
    with session_factory() as db:
        escalated = record_check_result(db, settings, _critical(), now=now + timedelta(minutes=1))
    assert escalated.transition == TRANSITION_ESCALATED
    assert _alerts(session_factory, "monitor_incident_escalated") == []  # never queued

    # Delivery comes back; the very next cycle must catch up.
    with session_factory() as db:
        caught_up = record_check_result(
            db, admin_settings, _critical(), now=now + timedelta(minutes=2)
        )
    assert caught_up.transition == TRANSITION_ALERT_CAUGHT_UP
    assert caught_up.incident_id == opened.incident_id

    incidents = _incidents(session_factory)
    assert len(incidents) == 1
    assert incidents[0].severity == "critical"

    opened_alerts = _alerts(session_factory, "monitor_incident_opened")
    # The original warning-severity alert, PLUS the critical catch-up --
    # neither suppressed as a false duplicate of the other.
    assert len(opened_alerts) == 2
    assert {a.status for a in opened_alerts} == {"pending"}
