"""Read-only inspection commands.

Usage:

    python -m app.cli recent [--limit N]
    python -m app.cli payment ORDER_ID
    python -m app.cli retry-queue
    python -m app.cli manual-review

ORDER_ID may be the original bot order id or the numeric gateway order id.
Output is one JSON object per line. These commands never modify data and
never print secrets, redirect URLs, or full card numbers.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import create_session_factory
from app.models import Payment, PaymentEvent, PaymentStatus


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _payment_summary(payment: Payment) -> dict[str, Any]:
    return {
        "bot_order_id": payment.bot_order_id,
        "gateway_order_id": payment.gateway_order_id,
        "status": payment.status,
        # Unambiguous amounts: the bot's ORIGINAL invoice vs what the payer
        # paid THROUGH THE GATEWAY (original + fee).
        "original_bot_invoice": payment.amount,
        "amount": payment.amount,
        "fee_rate_bps": payment.fee_rate_bps,
        "fee_amount": payment.fee_amount,
        "paid_through_gateway": payment.payable_amount,
        "gateway_verified": payment.gateway_verified_at is not None,
        "gateway_verified_at": _iso(payment.gateway_verified_at),
        "reference_id": payment.reference_id,
        "bot_notify_reason": payment.bot_notify_reason,
        "bot_notify_attempts": payment.bot_notify_attempts,
        "bot_last_http_status": payment.bot_last_http_status,
        "bot_last_error_code": payment.bot_last_error_code,
        "next_retry_at": _iso(payment.next_retry_at),
        "bot_notify_started_at": _iso(payment.bot_notify_started_at),
        "bot_notify_accepted_at": _iso(payment.bot_notify_accepted_at),
        "manual_review_at": _iso(payment.manual_review_at),
        "created_at": _iso(payment.created_at),
        "updated_at": _iso(payment.updated_at),
    }


def _print(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str))


def _cmd_recent(db: Session, limit: int) -> int:
    payments = db.execute(
        select(Payment).order_by(Payment.created_at.desc()).limit(limit)
    ).scalars()
    for payment in payments:
        _print(_payment_summary(payment))
    return 0


def _cmd_payment(db: Session, order_id: str) -> int:
    query = select(Payment).where(Payment.bot_order_id == order_id)
    payment = db.execute(query).scalar_one_or_none()
    if payment is None and order_id.isdigit():
        payment = db.execute(
            select(Payment).where(Payment.gateway_order_id == int(order_id))
        ).scalar_one_or_none()
    if payment is None:
        _print({"error": "payment_not_found", "order_id": order_id})
        return 1
    _print(_payment_summary(payment))
    events = db.execute(
        select(PaymentEvent)
        .where(PaymentEvent.payment_id == payment.id)
        .order_by(PaymentEvent.id)
    ).scalars()
    for event in events:
        _print(
            {
                "event_type": event.event_type,
                "level": event.level,
                "request_id": event.request_id,
                "created_at": _iso(event.created_at),
                "data": event.data,
            }
        )
    return 0


def _cmd_retry_queue(db: Session) -> int:
    payments = db.execute(
        select(Payment)
        .where(Payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value)
        .order_by(Payment.next_retry_at.asc())
    ).scalars()
    for payment in payments:
        _print(_payment_summary(payment))
    return 0


def _cmd_manual_review(db: Session) -> int:
    payments = db.execute(
        select(Payment)
        .where(Payment.status == PaymentStatus.MANUAL_REVIEW.value)
        .order_by(Payment.manual_review_at.asc())
    ).scalars()
    for payment in payments:
        _print(_payment_summary(payment))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    recent = subparsers.add_parser("recent", help="most recent payments")
    recent.add_argument("--limit", type=int, default=20)
    payment = subparsers.add_parser("payment", help="one payment with its audit events")
    payment.add_argument("order_id")
    subparsers.add_parser("retry-queue", help="payments awaiting bot notification")
    subparsers.add_parser("manual-review", help="payments requiring administrator review")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    session_factory = create_session_factory(settings.database_url)
    db = session_factory()
    try:
        if args.command == "recent":
            return _cmd_recent(db, args.limit)
        if args.command == "payment":
            return _cmd_payment(db, args.order_id)
        if args.command == "retry-queue":
            return _cmd_retry_queue(db)
        return _cmd_manual_review(db)
    except BrokenPipeError:
        # Piping into head/less that exits early is not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
