"""Read-only database queries for admin bot commands and reports.

Nothing in this module mutates payment data. Values returned here may be
shown to administrators; secrets, redirect URLs, signatures, and untrusted
external text are never selected.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import AdminAlert, Payment, PaymentEvent, WorkerHeartbeat


def _utcnow() -> datetime:
    return datetime.now(UTC)


def count_by_status(db: Session, status: str) -> int:
    return db.execute(
        select(func.count(Payment.id)).where(Payment.status == status)
    ).scalar_one()


def event_count_since(db: Session, event_type: str, *, hours: int = 24) -> int:
    cutoff = _utcnow() - timedelta(hours=hours)
    return db.execute(
        select(func.count(PaymentEvent.id)).where(
            PaymentEvent.event_type == event_type, PaymentEvent.created_at >= cutoff
        )
    ).scalar_one()


def database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def migration_revision(db: Session) -> str:
    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return str(revision) if revision else "unknown"
    except Exception:
        return "unknown"


def recent_payments(db: Session, limit: int) -> list[Payment]:
    return list(
        db.execute(
            select(Payment).order_by(Payment.created_at.desc()).limit(limit)
        ).scalars()
    )


def manual_review_payments(db: Session, limit: int = 20) -> list[Payment]:
    return list(
        db.execute(
            select(Payment)
            .where(Payment.status == "manual_review")
            .order_by(Payment.manual_review_at.asc().nulls_first())
            .limit(limit)
        ).scalars()
    )


@dataclass(frozen=True)
class StuckEntry:
    payment: Payment
    category: str  # exact reason category, never a generic "stuck"


def stuck_payments(
    db: Session,
    *,
    pending_age_minutes: int = 30,
    claim_timeout_seconds: float = 120.0,
    limit: int = 30,
) -> list[StuckEntry]:
    now = _utcnow()
    entries: list[StuckEntry] = []
    for payment in manual_review_payments(db, limit=limit):
        reason = payment.bot_notify_reason or payment.last_error or "manual_review"
        entries.append(StuckEntry(payment, f"manual_review:{reason}"))

    pending_cutoff = now - timedelta(minutes=pending_age_minutes)
    old_pending = db.execute(
        select(Payment)
        .where(Payment.status == "bot_notify_pending", Payment.created_at <= pending_cutoff)
        .order_by(Payment.created_at.asc())
        .limit(limit)
    ).scalars()
    claim_cutoff = now - timedelta(seconds=claim_timeout_seconds)
    for payment in old_pending:
        claimed_at = payment.notification_claimed_at
        if claimed_at is not None:
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=UTC)
            if claimed_at <= claim_cutoff:
                entries.append(StuckEntry(payment, "stale_notification_claim"))
                continue
        entries.append(
            StuckEntry(payment, payment.bot_notify_reason or "bot_notify_pending_old")
        )
    return entries[:limit]


def retry_queue_snapshot(db: Session, *, limit: int = 30) -> dict[str, list[Payment]]:
    now = _utcnow()
    pending = list(
        db.execute(
            select(Payment)
            .where(Payment.status == "bot_notify_pending")
            .order_by(Payment.next_retry_at.asc().nulls_first())
            .limit(limit)
        ).scalars()
    )
    due: list[Payment] = []
    scheduled: list[Payment] = []
    claimed: list[Payment] = []
    for payment in pending:
        if payment.notification_claimed_at is not None:
            claimed.append(payment)
            continue
        retry_at = payment.next_retry_at
        if retry_at is not None and retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        if retry_at is None or retry_at <= now:
            due.append(payment)
        else:
            scheduled.append(payment)
    retry_limit = list(
        db.execute(
            select(Payment)
            .where(
                Payment.status == "manual_review",
                Payment.bot_notify_reason == "retry_limit_reached",
            )
            .order_by(Payment.manual_review_at.desc())
            .limit(10)
        ).scalars()
    )
    return {
        "due": due,
        "scheduled": scheduled,
        "claimed": claimed,
        "retry_limit": retry_limit,
    }


def find_payment(db: Session, identifier: str) -> Payment | None:
    payment = db.execute(
        select(Payment).where(Payment.bot_order_id == identifier)
    ).scalar_one_or_none()
    if payment is None and identifier.isdigit():
        payment = db.execute(
            select(Payment).where(Payment.gateway_order_id == int(identifier))
        ).scalar_one_or_none()
    if payment is None:
        matches = list(
            db.execute(
                select(Payment).where(Payment.reference_id == identifier).limit(2)
            ).scalars()
        )
        if len(matches) == 1:  # reference lookup only when unambiguous
            payment = matches[0]
    return payment


def payment_events(db: Session, payment_id: int, limit: int = 10) -> list[PaymentEvent]:
    return list(
        db.execute(
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.id.desc())
            .limit(limit)
        ).scalars()
    )


def errors_summary(db: Session, *, hours: int = 24) -> dict[str, int]:
    cutoff = _utcnow() - timedelta(hours=hours)
    rows = db.execute(
        select(PaymentEvent.event_type, func.count(PaymentEvent.id))
        .where(
            PaymentEvent.created_at >= cutoff,
            PaymentEvent.event_type.in_(
                [
                    "centralpay_getlink_failed",
                    "centralpay_verify_failed",
                    "verify_amount_mismatch",
                    "verify_user_id_mismatch",
                    "verify_missing_reference_id",
                    "bot_notification_failed",
                    "bot_timeout_ambiguous",
                    "notification_recovered_after_restart",
                    "backup_failed",
                ]
            ),
        )
        .group_by(PaymentEvent.event_type)
    ).tuples().all()
    summary: dict[str, int] = dict(rows)
    signature_alerts = db.execute(
        select(func.count(AdminAlert.id)).where(
            AdminAlert.alert_type == "callback_signature_failures",
            AdminAlert.created_at >= cutoff,
        )
    ).scalar_one()
    if signature_alerts:
        summary["callback_signature_failures"] = signature_alerts
    return summary


def latest_backup_alert(db: Session, alert_type: str) -> AdminAlert | None:
    return db.execute(
        select(AdminAlert)
        .where(AdminAlert.alert_type == alert_type)
        .order_by(AdminAlert.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def alert_queue_stats(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(AdminAlert.status, func.count(AdminAlert.id)).group_by(AdminAlert.status)
    ).tuples().all()
    return dict(rows)


def latest_worker_heartbeat(db: Session, worker_name: str = "notification-worker") -> (
    WorkerHeartbeat | None
):
    return db.execute(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_name == worker_name)
        .order_by(WorkerHeartbeat.last_heartbeat_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def worker_heartbeat_age_seconds(db: Session) -> float | None:
    heartbeat = latest_worker_heartbeat(db)
    if heartbeat is None:
        return None
    beat = heartbeat.last_heartbeat_at
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=UTC)
    return (_utcnow() - beat).total_seconds()


def daily_report_payload(db: Session, *, report_date: str, hours: int = 24) -> dict[str, Any]:
    cutoff = _utcnow() - timedelta(hours=hours)
    verified_amount = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.gateway_verified_at.is_not(None), Payment.gateway_verified_at >= cutoff
        )
    ).scalar_one()
    backup_ok = latest_backup_alert(db, "backup_succeeded")
    backup_failed = latest_backup_alert(db, "backup_failed")
    backup_status = "بدون اطلاعات"
    if backup_ok is not None or backup_failed is not None:
        ok_at = backup_ok.created_at if backup_ok else None
        failed_at = backup_failed.created_at if backup_failed else None
        if ok_at is not None and (failed_at is None or ok_at >= failed_at):
            backup_status = "موفق"
        else:
            backup_status = "ناموفق"
    return {
        "report_date": report_date,
        "backup_status": backup_status,
        "links_created": event_count_since(db, "payment_link_created", hours=hours),
        "gateway_verified": event_count_since(db, "gateway_payment_verified", hours=hours),
        "bot_accepted": event_count_since(db, "bot_notification_accepted", hours=hours),
        "total_verified_toman": int(verified_amount),
        "manual_review": count_by_status(db, "manual_review"),
        "pending_retry": count_by_status(db, "bot_notify_pending"),
        "getlink_failures": event_count_since(db, "centralpay_getlink_failed", hours=hours),
        "verify_failures": event_count_since(db, "centralpay_verify_failed", hours=hours),
        "bot_delivery_failures": event_count_since(db, "bot_notification_failed", hours=hours),
    }
