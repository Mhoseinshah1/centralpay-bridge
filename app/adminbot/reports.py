"""Daily operational report.

The report is queued through the same alert outbox as everything else. A
deduplication key derived from the local report date guarantees the report
is never sent twice for one day, including after restarts.
"""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adminbot import queries
from app.adminbot.alerts import create_alert
from app.audit import record_event
from app.config import Settings
from app.models import AdminAlert

logger = logging.getLogger("app.adminbot.reports")


def report_due(settings: Settings, now_utc: datetime) -> str | None:
    """Return the local report date string when the report is due, else None."""
    if not settings.admin_bot_daily_report_enabled:
        return None
    tz = ZoneInfo(settings.admin_bot_timezone)
    local_now = now_utc.astimezone(tz)
    hour, minute = (int(part) for part in settings.admin_bot_daily_report_time.split(":"))
    if (local_now.hour, local_now.minute) >= (hour, minute):
        return local_now.date().isoformat()
    return None


def maybe_queue_daily_report(
    db: Session, settings: Settings, *, now_utc: datetime | None = None
) -> bool:
    """Queue today's report if due and not already queued. Returns True when
    a new report was queued."""
    now_utc = now_utc or datetime.now(UTC)
    report_date = report_due(settings, now_utc)
    if report_date is None:
        return False
    dedup_key = f"daily_report:{report_date}"
    existing = db.execute(
        select(AdminAlert.id).where(AdminAlert.deduplication_key == dedup_key).limit(1)
    ).first()
    if existing is not None:
        return False
    payload = queries.daily_report_payload(db, report_date=report_date)
    payload["health_summary"] = "در گزارش /status"
    # Dedup by existence check above (unbounded window): the standard
    # windowed dedup does not apply to daily reports.
    create_alert(
        db,
        alert_type="daily_report",
        severity="info",
        deduplication_key=dedup_key,
        payload=payload,
        dedup_minutes=60 * 48,
    )
    record_event(
        db,
        payment_id=None,
        event_type="daily_report_delivered",
        data={"report_date": report_date, "queued": True},
    )
    db.commit()
    logger.info("admin_daily_report_delivered", extra={"report_date": report_date})
    return True
