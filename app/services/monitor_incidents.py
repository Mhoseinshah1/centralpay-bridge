"""Durable incident lifecycle for app.monitor's checks.

Persists MonitorIncident rows so open/closed state survives a container
restart, and reuses the existing admin-alert outbox
(app.adminbot.alerts.create_alert) for delivery — this module never talks
to Telegram itself, and never even requires the admin bot's alert-creation
policy to be configured in this process (create_alert has no such
requirement; only app.adminbot.alerts.on_audit_event's automatic
audit-event routing does).

Alerting happens ONLY on a state TRANSITION — healthy -> unhealthy,
unhealthy -> healthy, or a severity escalation — never on every polling
cycle that finds the same already-open incident still unhealthy. That is
the anti-spam guarantee the monitoring roadmap requires; it is enforced
here structurally (a cycle that changes nothing takes the
TRANSITION_UNCHANGED_* path and never calls create_alert), not by relying
on create_alert's own best-effort time-window dedup.

Concurrency: at most one OPEN row can exist per check_key — see the partial
unique index on app.models.MonitorIncident. Opening a new incident is an
optimistic INSERT that treats an IntegrityError as "a racing monitor
instance already opened it" and falls back to updating that row instead of
crashing or creating a duplicate — the same idiom app.services.payments
already uses for bot_order_id races.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adminbot.alerts import create_alert
from app.config import Settings
from app.models import MonitorIncident, MonitorIncidentStatus
from app.services.monitor_checks import STATUS_OK, CheckResult

logger = logging.getLogger("app.services.monitor_incidents")

TRANSITION_OPENED = "opened"
TRANSITION_CLOSED = "closed"
TRANSITION_ESCALATED = "escalated"
TRANSITION_UNCHANGED_UNHEALTHY = "unchanged_unhealthy"
TRANSITION_UNCHANGED_HEALTHY = "unchanged_healthy"
# An already-open incident that had never been alerted for its CURRENT
# severity -- either because alerting was disabled when it opened, or it
# escalated further while alerting was disabled (see the escalation
# branch's last_alerted_at reset below) -- got its catch-up alert now that
# alerting is available. Distinct from TRANSITION_OPENED (a brand-new row).
TRANSITION_ALERT_CAUGHT_UP = "alert_caught_up"

_SEVERITY_RANK = {"warning": 1, "critical": 2}


@dataclass(frozen=True)
class IncidentTransition:
    check_key: str
    transition: str
    incident_id: int | None


# Every check key maps to exactly one of the admin bot's per-category alert
# toggles (ADMIN_BOT_*_ALERTS) so an operator who wants manual-review pings
# but not routine health noise (or vice versa) can say so -- the same
# categories app.adminbot.alerts._map_event already applies to non-monitor
# events. worker_heartbeat's key carries a ":{worker_name}" suffix, so it
# is matched by prefix rather than equality.
_BACKUP_CHECK_KEYS = frozenset({"backup"})
_MANUAL_REVIEW_CHECK_KEYS = frozenset({"manual_review"})
_ERROR_CHECK_KEYS = frozenset(
    {"notification_backlog", "gateway_failure_burst", "bot_failure_burst"}
)


def _alert_category(check_key: str) -> str:
    if check_key in _BACKUP_CHECK_KEYS:
        return "backup"
    if check_key in _MANUAL_REVIEW_CHECK_KEYS:
        return "manual_review"
    if check_key in _ERROR_CHECK_KEYS:
        return "error"
    # public_ready, database, worker_heartbeat:*, reconciliation,
    # disk_space, db_integrity: all report on core service health.
    return "health"


def _alerts_enabled(settings: Settings, check_key: str) -> bool:
    # An incident is always persisted regardless of this flag (`/monitor`
    # and `centralpay monitor incidents` stay accurate even with Telegram
    # off); this only decides whether a row is ALSO queued for delivery,
    # mirroring app.adminbot.alerts.configure_alert_creation's own gate so
    # a fully-disabled admin bot never accumulates permanently-undelivered
    # outbox rows.
    if not (settings.admin_bot_enabled and settings.admin_bot_alerts_enabled):
        return False
    category = _alert_category(check_key)
    if category == "backup":
        return settings.admin_bot_backup_alerts
    if category == "manual_review":
        return settings.admin_bot_manual_review_alerts
    if category == "error":
        return settings.admin_bot_error_alerts
    return settings.admin_bot_health_alerts


def _alert_payload(result: CheckResult) -> dict[str, object]:
    payload: dict[str, object] = {"check": result.key, "detail": result.reason}
    if "count" in result.details:
        payload["count"] = result.details["count"]
    return payload


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _open_incident(db: Session, check_key: str) -> MonitorIncident | None:
    return db.execute(
        select(MonitorIncident)
        .where(
            MonitorIncident.check_key == check_key,
            MonitorIncident.status == MonitorIncidentStatus.OPEN.value,
        )
        .with_for_update()
    ).scalar_one_or_none()


def record_check_result(
    db: Session, settings: Settings, result: CheckResult, *, now: datetime
) -> IncidentTransition:
    """Apply one check's current status to its incident state, alerting
    only on an actual transition. Commits internally (one incident per
    call, in its own short transaction) — never left pending for the
    caller."""
    if result.status == STATUS_OK:
        return _handle_healthy(db, settings, result, now=now)
    return _handle_unhealthy(db, settings, result, now=now)


def _handle_healthy(
    db: Session, settings: Settings, result: CheckResult, *, now: datetime
) -> IncidentTransition:
    incident = _open_incident(db, result.key)
    if incident is None:
        db.rollback()  # nothing to change; release the read
        return IncidentTransition(result.key, TRANSITION_UNCHANGED_HEALTHY, None)
    incident.status = MonitorIncidentStatus.RESOLVED.value
    incident.resolved_at = now
    incident.last_seen_at = now
    if _alerts_enabled(settings, result.key):
        incident.last_alerted_at = now
        db.flush()
        duration_seconds = int((now - _as_utc(incident.opened_at)).total_seconds())
        create_alert(
            db,
            alert_type="monitor_incident_resolved",
            severity="info",
            deduplication_key=f"monitor:{result.key}:{incident.id}:resolved",
            payload={
                "check": result.key,
                "detail": f"recovered after {max(duration_seconds, 0)}s",
            },
            now=now,
        )
    db.commit()
    return IncidentTransition(result.key, TRANSITION_CLOSED, incident.id)


def _handle_unhealthy(
    db: Session, settings: Settings, result: CheckResult, *, now: datetime
) -> IncidentTransition:
    incident = _open_incident(db, result.key)
    if incident is None:
        candidate = MonitorIncident(
            check_key=result.key,
            severity=result.status,
            status=MonitorIncidentStatus.OPEN.value,
            opened_at=now,
            last_seen_at=now,
            details=result.details,
        )
        db.add(candidate)
        try:
            db.flush()
        except IntegrityError:
            # A racing monitor instance already opened it first (the
            # partial unique index rejected this insert). Fall back to
            # updating ITS row below -- never a duplicate incident/alert.
            db.rollback()
            incident = _open_incident(db, result.key)
            if incident is None:
                # Vanishingly unlikely (the winner would have to resolve
                # it again inside this same window) -- no-op rather than
                # risk fabricating a duplicate alert.
                return IncidentTransition(result.key, TRANSITION_UNCHANGED_UNHEALTHY, None)
        else:
            if _alerts_enabled(settings, result.key):
                candidate.last_alerted_at = now
                db.flush()
                create_alert(
                    db,
                    alert_type="monitor_incident_opened",
                    severity=result.status,
                    deduplication_key=f"monitor:{result.key}:{candidate.id}",
                    payload=_alert_payload(result),
                    now=now,
                )
            db.commit()
            return IncidentTransition(result.key, TRANSITION_OPENED, candidate.id)

    # An incident is already open (found directly above, or reached after
    # losing the create race).
    incident.last_seen_at = now
    incident.details = result.details
    if (
        result.status != incident.severity
        and _SEVERITY_RANK[result.status] > _SEVERITY_RANK[incident.severity]
    ):
        incident.severity = result.status
        if _alerts_enabled(settings, result.key):
            incident.last_alerted_at = now
            db.flush()
            create_alert(
                db,
                alert_type="monitor_incident_escalated",
                severity=result.status,
                # Includes `now`, unlike the opened/resolved keys below: the
                # SAME incident.id can escalate more than once over its
                # lifetime (warning -> critical -> warning -> critical
                # again), and each such escalation is a genuinely NEW,
                # alert-worthy transition -- a key without a
                # time-varying component would let create_alert's own
                # 30-minute dedup window incorrectly SUPPRESS a real
                # second escalation as if it were a duplicate of the
                # first. A literal duplicate call for the very same
                # transition (identical `now`) still collapses correctly.
                deduplication_key=(
                    f"monitor:{result.key}:{incident.id}:escalated:"
                    f"{result.status}:{now.isoformat()}"
                ),
                payload=_alert_payload(result),
                now=now,
            )
        else:
            # Alerting is unavailable right now (admin bot/category
            # disabled), but the severity just got WORSE than whatever
            # last_alerted_at reflects (an earlier open/escalation, or
            # nothing at all). Clear it so this incident reads as
            # never-alerted-for-its-current-state -- the catch-up branch
            # below then sends exactly one fresh alert at the new,
            # correct severity the moment alerting resumes, instead of
            # staying silent forever because SOME alert was sent once,
            # long ago, for a since-superseded lower severity.
            incident.last_alerted_at = None
        db.commit()
        return IncidentTransition(result.key, TRANSITION_ESCALATED, incident.id)
    if result.status != incident.severity:
        # A de-escalation that is still unhealthy (critical -> warning):
        # update severity silently, never a duplicate alert.
        incident.severity = result.status

    if incident.last_alerted_at is None and _alerts_enabled(settings, result.key):
        # This incident has never actually been queued for delivery at its
        # CURRENT severity -- either it opened while alerting was disabled
        # (the loser of an open-race whose winner ran with alerting
        # disabled counts too), or it escalated further while disabled
        # AFTER an earlier severity's alert already went out (see the
        # escalation branch above, which resets last_alerted_at to None
        # for exactly this reason). Now that alerting is available, catch
        # up with one alert at the incident's CURRENT severity -- otherwise
        # an admin enabling the admin bot after `monitor enable` (the exact
        # order MONITORING.md documents) would never hear about an
        # incident that opened or worsened before that point.
        incident.last_alerted_at = now
        db.flush()
        create_alert(
            db,
            alert_type="monitor_incident_opened",
            severity=incident.severity,
            # Time-varying (like the escalation key above), never just
            # f"monitor:{result.key}:{incident.id}" -- that exact key may
            # already belong to a REAL, already-delivered alert (the
            # incident's original opening alert, if this catch-up follows
            # a since-cleared escalation rather than a from-birth one),
            # and colliding with it would get this genuinely new alert
            # silently marked "suppressed" by create_alert's own
            # dedup-window check instead of actually queued.
            deduplication_key=(
                f"monitor:{result.key}:{incident.id}:catchup:"
                f"{incident.severity}:{now.isoformat()}"
            ),
            payload=_alert_payload(result),
            now=now,
        )
        db.commit()
        return IncidentTransition(result.key, TRANSITION_ALERT_CAUGHT_UP, incident.id)

    db.commit()
    return IncidentTransition(result.key, TRANSITION_UNCHANGED_UNHEALTHY, incident.id)


def list_incidents(
    db: Session, *, include_resolved: bool = False, limit: int = 50
) -> list[MonitorIncident]:
    """Read-only incident history/audit view for `centralpay monitor
    incidents` — never re-runs a check, just reads persisted state."""
    stmt = select(MonitorIncident).order_by(MonitorIncident.opened_at.desc()).limit(limit)
    if not include_resolved:
        stmt = stmt.where(MonitorIncident.status == MonitorIncidentStatus.OPEN.value)
    return list(db.execute(stmt).scalars())
