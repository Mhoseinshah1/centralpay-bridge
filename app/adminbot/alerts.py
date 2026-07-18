"""Alert outbox: creation (in payment transactions) and delivery (admin bot).

Creation side: `configure_alert_creation(settings)` is called at API/worker
startup. After that, `on_audit_event` — invoked from `app.audit.record_event`
inside the caller's transaction — maps alertable audit events to outbox rows.
Any failure here is swallowed: alert creation must never break a financial
transaction, and Telegram is never contacted from this side.

Delivery side: the admin-bot service claims due rows (SKIP LOCKED), sends
via Telegram outside any transaction, and records the classified result in a
new transaction, with bounded retries and stale-claim recovery.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.adminbot.format import alert_message
from app.adminbot.telegram import AlertSender
from app.config import Settings
from app.models import AdminAlert, AlertStatus
from app.services.notification import JitterFn, NowFn, default_jitter, utcnow

logger = logging.getLogger("app.adminbot.alerts")

# Bounded backoff for Telegram delivery (seconds). Attempts beyond the table
# reuse the final delay until the attempt limit is reached.
ALERT_RETRY_DELAYS_SECONDS: tuple[int, ...] = (30, 60, 120, 300, 900, 1800)

_MISMATCH_REASONS = frozenset(
    {"verify_amount_mismatch", "verify_user_id_mismatch", "verify_missing_reference_id"}
)


@dataclass(frozen=True)
class AlertCreationPolicy:
    alerts_enabled: bool
    payment_success_alerts: bool
    error_alerts: bool
    manual_review_alerts: bool
    backup_alerts: bool
    dedup_minutes: int


_policy: AlertCreationPolicy | None = None


def configure_alert_creation(settings: Settings) -> None:
    """Enable alert-row creation. Called by API/worker/admin-bot startup."""
    global _policy
    if not settings.admin_bot_enabled or not settings.admin_bot_alerts_enabled:
        _policy = None
        return
    _policy = AlertCreationPolicy(
        alerts_enabled=True,
        payment_success_alerts=settings.admin_bot_payment_success_alerts,
        error_alerts=settings.admin_bot_error_alerts,
        manual_review_alerts=settings.admin_bot_manual_review_alerts,
        backup_alerts=settings.admin_bot_backup_alerts,
        dedup_minutes=settings.admin_bot_alert_dedup_minutes,
    )


def reset_alert_creation() -> None:
    global _policy
    _policy = None


def create_alert(
    db: Session,
    *,
    alert_type: str,
    severity: str = "info",
    payment_id: int | None = None,
    deduplication_key: str | None = None,
    payload: dict[str, Any] | None = None,
    dedup_minutes: int | None = None,
    now: datetime | None = None,
) -> AdminAlert:
    """Insert an alert row into the caller's transaction (no commit here).

    When a deduplication key matches a recent alert, the row is stored with
    status=suppressed so storms stay visible without being sent.
    """
    now = now or utcnow()
    status = AlertStatus.PENDING.value
    if deduplication_key is not None:
        window = dedup_minutes if dedup_minutes is not None else (
            _policy.dedup_minutes if _policy else 30
        )
        cutoff = now - timedelta(minutes=window)
        duplicate = db.execute(
            select(AdminAlert.id)
            .where(
                AdminAlert.deduplication_key == deduplication_key,
                AdminAlert.created_at >= cutoff,
                AdminAlert.status != AlertStatus.SUPPRESSED.value,
            )
            .limit(1)
        ).first()
        if duplicate is not None:
            status = AlertStatus.SUPPRESSED.value
    alert = AdminAlert(
        alert_type=alert_type,
        severity=severity,
        payment_id=payment_id,
        deduplication_key=deduplication_key,
        payload=payload,
        status=status,
        next_retry_at=now if status == AlertStatus.PENDING.value else None,
        created_at=now,
    )
    db.add(alert)
    db.flush()
    from app.audit import record_event  # local import to avoid cycle

    record_event(
        db,
        payment_id=payment_id,
        event_type="admin_alert_created",
        data={"alert_id": alert.id, "alert_type": alert_type, "status": status},
    )
    logger.info(
        "admin_alert_queued",
        extra={
            "alert_id": alert.id,
            "alert_type": alert_type,
            "severity": severity,
            "payment_id": payment_id,
            "status": status,
        },
    )
    return alert


def on_audit_event(
    db: Session,
    *,
    event_type: str,
    level: str,
    payment_id: int | None,
    data: dict[str, Any] | None,
) -> None:
    """Map an audit event to an alert row. Never raises."""
    policy = _policy
    if policy is None:
        return
    try:
        _map_event(db, policy, event_type=event_type, payment_id=payment_id, data=data or {})
    except Exception:
        # Alert creation is best-effort; the financial transaction proceeds.
        logger.exception("admin_alert_creation_failed", extra={"event_type": event_type})


def _map_event(
    db: Session,
    policy: AlertCreationPolicy,
    *,
    event_type: str,
    payment_id: int | None,
    data: dict[str, Any],
) -> None:
    if event_type in ("gateway_payment_verified", "bot_notification_accepted"):
        if policy.payment_success_alerts:
            alert_type = (
                "gateway_payment_verified"
                if event_type == "gateway_payment_verified"
                else "bot_notify_accepted"
            )
            create_alert(
                db,
                alert_type=alert_type,
                severity="info",
                payment_id=payment_id,
                payload=data,
            )
    elif event_type in _MISMATCH_REASONS:
        # Financial-integrity alerts: always created, never deduplicated.
        create_alert(
            db, alert_type=event_type, severity="critical", payment_id=payment_id, payload=data
        )
    elif event_type == "manual_review_required":
        if not policy.manual_review_alerts:
            return
        reason = str(data.get("reason", ""))
        if reason in _MISMATCH_REASONS:
            return  # already alerted from the mismatch event itself
        alert_type = (
            reason
            if reason in ("retry_limit_reached", "bot_timeout_ambiguous")
            else "manual_review_required"
        )
        create_alert(
            db, alert_type=alert_type, severity="critical", payment_id=payment_id, payload=data
        )
    elif event_type in ("centralpay_getlink_failed", "centralpay_verify_failed"):
        if policy.error_alerts:
            create_alert(
                db,
                alert_type=event_type,
                severity="warning",
                payment_id=payment_id,
                deduplication_key=f"{event_type}:{payment_id}",
                payload={k: v for k, v in data.items() if k != "reason"},
            )
    elif event_type == "notification_recovered_after_restart":
        if policy.error_alerts:
            create_alert(
                db,
                alert_type="notification_recovered_after_restart",
                severity="warning",
                payment_id=payment_id,
                deduplication_key=f"worker_recovery:{payment_id}",
                payload=data,
            )
    elif event_type in ("backup_succeeded", "backup_failed") and policy.backup_alerts:
        create_alert(
            db,
            alert_type=event_type,
            severity="info" if event_type == "backup_succeeded" else "error",
            payload=data,
        )


# --------------------------------------------------------------------------
# Delivery (admin-bot service side)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimedAlert:
    alert_id: int
    alert_type: str
    severity: str
    payment_id: int | None
    payload: dict[str, Any] | None
    attempts: int


def alert_retry_delay_seconds(
    attempts_completed: int, retry_after_seconds: int | None, jitter: JitterFn
) -> float:
    index = min(max(attempts_completed, 1), len(ALERT_RETRY_DELAYS_SECONDS)) - 1
    delay = ALERT_RETRY_DELAYS_SECONDS[index] * jitter()
    if retry_after_seconds is not None and retry_after_seconds > delay:
        delay = float(retry_after_seconds)
    return delay


def release_stale_alert_claims(db: Session, settings: Settings, *, now: datetime) -> int:
    """Return alerts stuck in `sending` (crashed instance) to pending.

    Re-sending an operational alert is safe; the worst case is a duplicate
    Telegram message, never a financial effect.
    """
    cutoff = now - timedelta(seconds=settings.admin_bot_alert_claim_timeout_seconds)
    alerts = (
        db.execute(
            select(AdminAlert)
            .where(
                AdminAlert.status == AlertStatus.SENDING.value,
                AdminAlert.claimed_at.is_not(None),
                AdminAlert.claimed_at <= cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not alerts:
        db.rollback()
        return 0
    for alert in alerts:
        alert.status = AlertStatus.PENDING.value
        alert.claimed_at = None
        alert.claimed_by = None
        alert.next_retry_at = now
        logger.warning(
            "admin_alert_stale_claim_released",
            extra={"alert_id": alert.id, "alert_type": alert.alert_type},
        )
    db.commit()
    return len(alerts)


def claim_due_alerts(
    db: Session, *, worker_id: str, now: datetime, limit: int = 10
) -> list[ClaimedAlert]:
    alerts = (
        db.execute(
            select(AdminAlert)
            .where(
                AdminAlert.status.in_(
                    [AlertStatus.PENDING.value, AlertStatus.RETRY_SCHEDULED.value]
                ),
                or_(AdminAlert.next_retry_at.is_(None), AdminAlert.next_retry_at <= now),
                AdminAlert.claimed_at.is_(None),
            )
            .order_by(AdminAlert.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not alerts:
        db.rollback()
        return []
    claimed: list[ClaimedAlert] = []
    for alert in alerts:
        alert.status = AlertStatus.SENDING.value
        alert.claimed_at = now
        alert.claimed_by = worker_id
        alert.attempts += 1
        claimed.append(
            ClaimedAlert(
                alert_id=alert.id,
                alert_type=alert.alert_type,
                severity=alert.severity,
                payment_id=alert.payment_id,
                payload=alert.payload,
                attempts=alert.attempts,
            )
        )
    db.commit()
    return claimed


def record_delivery_result(
    db: Session,
    settings: Settings,
    claimed: ClaimedAlert,
    *,
    delivered_count: int,
    retryable: bool,
    retry_after_seconds: int | None,
    error_code: str | None,
    now: datetime,
    jitter: JitterFn = default_jitter,
) -> str:
    """Record the aggregated outcome of one delivery attempt. Returns status."""
    alert = db.execute(
        select(AdminAlert).where(AdminAlert.id == claimed.alert_id).with_for_update()
    ).scalar_one()
    alert.claimed_at = None
    alert.claimed_by = None
    alert.last_error_code = error_code
    from app.audit import record_event  # local import to avoid cycle

    if delivered_count > 0:
        alert.status = AlertStatus.DELIVERED.value
        alert.delivered_at = now
        alert.next_retry_at = None
        record_event(
            db,
            payment_id=alert.payment_id,
            event_type="admin_alert_delivered",
            data={
                "alert_id": alert.id,
                "alert_type": alert.alert_type,
                "recipients": delivered_count,
                "attempt": claimed.attempts,
            },
        )
        logger.info(
            "admin_alert_delivered",
            extra={
                "alert_id": alert.id,
                "alert_type": alert.alert_type,
                "recipients": delivered_count,
                "attempt": claimed.attempts,
            },
        )
    elif retryable and claimed.attempts < settings.admin_bot_alert_max_attempts:
        delay = alert_retry_delay_seconds(claimed.attempts, retry_after_seconds, jitter)
        alert.status = AlertStatus.RETRY_SCHEDULED.value
        alert.next_retry_at = now + timedelta(seconds=delay)
        logger.warning(
            "admin_alert_retry_scheduled",
            extra={
                "alert_id": alert.id,
                "alert_type": alert.alert_type,
                "attempt": claimed.attempts,
                "error_code": error_code,
                "next_retry_at": alert.next_retry_at.isoformat(),
            },
        )
    else:
        alert.status = AlertStatus.FAILED.value
        alert.next_retry_at = None
        record_event(
            db,
            payment_id=alert.payment_id,
            event_type="admin_alert_failed",
            level="error",
            data={
                "alert_id": alert.id,
                "alert_type": alert.alert_type,
                "attempt": claimed.attempts,
                "error_code": error_code,
            },
        )
        logger.error(
            "admin_alert_permanent_failure",
            extra={
                "alert_id": alert.id,
                "alert_type": alert.alert_type,
                "attempt": claimed.attempts,
                "error_code": error_code,
            },
        )
    status = alert.status
    db.commit()
    return status


async def deliver_claimed_alert(
    session_factory: sessionmaker[Session],
    sender: AlertSender,
    settings: Settings,
    admin_ids: tuple[int, ...],
    claimed: ClaimedAlert,
    *,
    now_fn: NowFn = utcnow,
    jitter: JitterFn = default_jitter,
) -> str:
    """Send one claimed alert to every administrator; record the result."""
    started = time.perf_counter()

    def _format() -> list[str]:
        with session_factory() as db:
            return alert_message(db, settings, claimed)

    messages = await asyncio.to_thread(_format)

    delivered = 0
    retryable = False
    retry_after: int | None = None
    error_code: str | None = None
    for chat_id in admin_ids:
        recipient_ok = True
        for chunk in messages:
            outcome = await sender.send(chat_id, chunk)
            if not outcome.ok:
                recipient_ok = False
                error_code = error_code or outcome.error_code
                if outcome.retryable:
                    retryable = True
                    if outcome.retry_after_seconds is not None:
                        retry_after = max(retry_after or 0, outcome.retry_after_seconds)
                break
        if recipient_ok:
            delivered += 1
    if delivered and error_code is not None:
        error_code = f"partial:{error_code}"

    duration_ms = round((time.perf_counter() - started) * 1000, 1)

    def _record() -> str:
        with session_factory() as db:
            return record_delivery_result(
                db,
                settings,
                claimed,
                delivered_count=delivered,
                retryable=retryable,
                retry_after_seconds=retry_after,
                error_code=error_code,
                now=now_fn(),
                jitter=jitter,
            )

    status = await asyncio.to_thread(_record)
    logger.info(
        "admin_alert_attempt_completed",
        extra={
            "alert_id": claimed.alert_id,
            "alert_type": claimed.alert_type,
            "status": status,
            "duration_ms": duration_ms,
        },
    )
    return status


async def alert_delivery_pass(
    session_factory: sessionmaker[Session],
    sender: AlertSender,
    settings: Settings,
    admin_ids: tuple[int, ...],
    *,
    worker_id: str | None = None,
    now_fn: NowFn = utcnow,
    jitter: JitterFn = default_jitter,
) -> int:
    """One polling pass: recover stale claims, then deliver due alerts."""
    instance = worker_id or f"adminbot-{uuid.uuid4().hex[:8]}"

    def _release() -> int:
        with session_factory() as db:
            return release_stale_alert_claims(db, settings, now=now_fn())

    await asyncio.to_thread(_release)

    def _claim() -> list[ClaimedAlert]:
        with session_factory() as db:
            return claim_due_alerts(db, worker_id=instance, now=now_fn())

    claimed = await asyncio.to_thread(_claim)
    for item in claimed:
        await deliver_claimed_alert(
            session_factory, sender, settings, admin_ids, item, now_fn=now_fn, jitter=jitter
        )
    return len(claimed)
