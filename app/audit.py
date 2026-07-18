"""Append-only audit events for every financial state transition.

Events are flushed in the caller's transaction so an event is committed
atomically with the state change it records. Event ``data`` must only ever
contain safe values — no secrets, no full card numbers, no full URLs.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.logging_setup import request_id_var
from app.models import PaymentEvent

logger = logging.getLogger("app.audit")

_LEVELS = {"debug", "info", "warning", "error", "critical"}


def record_event(
    db: Session,
    *,
    payment_id: int | None,
    event_type: str,
    level: str = "info",
    data: dict[str, Any] | None = None,
) -> PaymentEvent:
    if level not in _LEVELS:
        level = "info"
    event = PaymentEvent(
        payment_id=payment_id,
        event_type=event_type,
        level=level,
        request_id=request_id_var.get(),
        data=data,
    )
    db.add(event)
    db.flush()
    logger.log(
        logging.getLevelNamesMapping()[level.upper()],
        event_type,
        extra={"payment_id": payment_id, "audit_data": data},
    )
    # Best-effort admin alert creation in the same transaction. A no-op
    # unless the admin bot is enabled; never raises (see adminbot.alerts).
    from app.adminbot import alerts as admin_alerts

    admin_alerts.on_audit_event(
        db, event_type=event_type, level=level, payment_id=payment_id, data=data
    )
    return event
