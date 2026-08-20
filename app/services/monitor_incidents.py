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

_SEVERITY_RANK = {"warning": 1, "critical": 2}


@dataclass(frozen=True)
class IncidentTransition:
    check_key: str
    transition: str
    incident_id: int | None


def _alerts_enabled(settings: Settings) -> bool:
    # An incident is always persisted regardless of this flag (`/monitor`
    # and `centralpay monitor incidents` stay accurate even with Telegram
    # off); this only decides whether a row is ALSO queued for delivery,
    # mirroring app.adminbot.alerts.configure_alert_creation's own gate so
    # a fully-disabled admin bot never accumulates permanently-undelivered
    # outbox rows.
    return settings.admin_bot_enabled and settings.admin_bot_alerts_enabled


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
    if _alerts_enabled(settings):
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
            if _alerts_enabled(settings):
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
        if _alerts_enabled(settings):
            incident.last_alerted_at = now
            db.flush()
            create_alert(
                db,
                alert_type="monitor_incident_escalated",
                severity=result.status,
                deduplication_key=f"monitor:{result.key}:{incident.id}:escalated:{result.status}",
                payload=_alert_payload(result),
                now=now,
            )
        db.commit()
        return IncidentTransition(result.key, TRANSITION_ESCALATED, incident.id)
    if result.status != incident.severity:
        # A de-escalation that is still unhealthy (critical -> warning):
        # update severity silently, never a duplicate alert.
        incident.severity = result.status
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
