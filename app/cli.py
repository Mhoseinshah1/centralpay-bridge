"""Read-only inspection commands.

Usage:

    python -m app.cli recent [--limit N]
    python -m app.cli payment ORDER_ID
    python -m app.cli retry-queue
    python -m app.cli manual-review
    python -m app.cli stuck [--limit N] [--json]

ORDER_ID may be the original bot order id or the numeric gateway order id.
Output is one JSON object per line, EXCEPT `stuck`, which prints a grouped,
human-readable report by default (pass --json for the one-object-per-line
form instead). These commands never modify data and never print secrets,
redirect URLs, or full card numbers.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import create_session_factory
from app.models import Payment, PaymentEvent, PaymentStatus
from app.services.stuck_payments import StuckCategory, StuckEntry, stuck_payments_overview


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


# --- stuck: categorized, human-readable operator view ----------------------

_STUCK_DISPLAY_LIMIT_DEFAULT = 20
_STUCK_DISPLAY_LIMIT_MAX = 200  # matches app.services.stuck_payments._QUERY_CAP

_STUCK_CATEGORY_EMOJI = {
    StuckCategory.NEEDS_ATTENTION: "🔴",
    StuckCategory.WAITING_GATEWAY: "🟡",
    StuckCategory.EXPIRED: "⚫",
}
_STUCK_CATEGORY_LABEL = {
    StuckCategory.NEEDS_ATTENTION: "Need attention",
    StuckCategory.WAITING_GATEWAY: "Waiting gateway",
    StuckCategory.EXPIRED: "Expired links",
}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _age_anchor(payment: Payment) -> datetime:
    """Same anchor reconciliation itself ages a link from (see
    app.services.reconciliation.link_age_anchor) — required so "Age" can
    never disagree with which category the entry was actually sorted into
    (e.g. an EXPIRED entry always displaying an age past the max-age
    cutoff). Payments that never reached link_created fall back to
    created_at, same as the SQL-side expression."""
    return payment.callback_token_issued_at or payment.created_at


def _humanize_duration(seconds: float) -> str:
    seconds = max(seconds, 0)
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, remaining_minutes = divmod(minutes, 60)
    if remaining_minutes:
        return f"{hours}h {remaining_minutes}m"
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _humanize_ago(value: datetime | None, now: datetime) -> str | None:
    if value is None:
        return None
    return f"{_humanize_duration((now - _as_utc(value)).total_seconds())} ago"


def _stuck_entry_dict(entry: StuckEntry, now: datetime) -> dict[str, Any]:
    payment = entry.payment
    is_link_created = payment.status == PaymentStatus.LINK_CREATED.value
    attempts = payment.reconciliation_attempts if is_link_created else payment.bot_notify_attempts
    next_retry = payment.reconciliation_next_at if is_link_created else payment.next_retry_at
    return {
        "category": entry.category.value,
        "reason": entry.reason,
        "order": payment.bot_order_id,
        "gateway_order_id": payment.gateway_order_id,
        "amount": payment.amount,
        "age_seconds": int((now - _as_utc(_age_anchor(payment))).total_seconds()),
        "status": payment.status,
        "gateway_state": entry.gateway_state,
        "attempts": attempts,
        "last_check_at": _iso(payment.reconciliation_last_at) if is_link_created else None,
        "next_retry_at": _iso(next_retry),
    }


def _stuck_entry_lines(index: int, entry: StuckEntry, now: datetime) -> list[str]:
    payment = entry.payment
    is_link_created = payment.status == PaymentStatus.LINK_CREATED.value
    attempts = payment.reconciliation_attempts if is_link_created else payment.bot_notify_attempts
    next_retry = payment.reconciliation_next_at if is_link_created else payment.next_retry_at

    lines = [
        f"{index}) {_STUCK_CATEGORY_EMOJI[entry.category]} {_STUCK_CATEGORY_LABEL[entry.category]}",
        "",
        "Order:",
        payment.bot_order_id,
        "",
        "Amount:",
        f"{payment.amount:,} تومان",
        "",
        "Age:",
        _humanize_duration((now - _as_utc(_age_anchor(payment))).total_seconds()),
        "",
        "Status:",
        payment.status,
        "",
    ]
    if entry.gateway_state is not None:
        lines += ["Gateway:", entry.gateway_state, ""]
    if attempts:
        lines += ["Attempts:", str(attempts), ""]
    last_check = _humanize_ago(payment.reconciliation_last_at, now) if is_link_created else None
    if last_check:
        lines += ["Last check:", last_check, ""]
    if next_retry is not None:
        lines += ["Next retry:", _as_utc(next_retry).strftime("%H:%M:%S"), ""]
    if entry.category == StuckCategory.NEEDS_ATTENTION:
        lines += ["Reason:", entry.reason, ""]
    elif entry.category == StuckCategory.WAITING_GATEWAY and not attempts:
        lines += ["No action needed", ""]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _cmd_stuck(db: Session, settings: Settings, *, limit: int, as_json: bool) -> int:
    limit = max(1, min(limit, _STUCK_DISPLAY_LIMIT_MAX))
    overview = stuck_payments_overview(db, settings)
    now = datetime.now(UTC)
    ordered = overview.ordered()
    shown = ordered[:limit]

    if as_json:
        _print(
            {
                "type": "summary",
                "needs_attention": overview.total_counts["needs_attention"],
                "waiting_gateway": overview.total_counts["waiting_gateway"],
                "expired": overview.total_counts["expired"],
                "shown": len(shown),
                "total": len(ordered),
            }
        )
        for entry in shown:
            _print({"type": "entry", **_stuck_entry_dict(entry, now)})
        return 0

    print("🚨 Stuck Payments")
    print()
    print("Summary:")
    print("----------------")
    print(f"🔴 Need attention: {overview.total_counts['needs_attention']}")
    print(f"🟡 Waiting gateway: {overview.total_counts['waiting_gateway']}")
    print(f"⚫ Expired links: {overview.total_counts['expired']}")
    print()
    if not shown:
        print()
        print("Nothing needs attention.")
        return 0
    for index, entry in enumerate(shown, start=1):
        print()
        for line in _stuck_entry_lines(index, entry, now):
            print(line)
    remaining = len(ordered) - len(shown)
    if remaining > 0:
        print()
        print(f"... {remaining} more not shown (raise --limit to see more)")
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
    stuck = subparsers.add_parser(
        "stuck", help="categorized view of payments needing operator attention"
    )
    stuck.add_argument(
        "--limit",
        type=int,
        default=_STUCK_DISPLAY_LIMIT_DEFAULT,
        help=f"max entries to display, across all categories combined "
        f"(default {_STUCK_DISPLAY_LIMIT_DEFAULT}, max {_STUCK_DISPLAY_LIMIT_MAX}); "
        "summary counts are always exact",
    )
    stuck.add_argument(
        "--json", action="store_true", dest="as_json", help="one JSON object per line"
    )
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
        if args.command == "manual-review":
            return _cmd_manual_review(db)
        return _cmd_stuck(db, settings, limit=args.limit, as_json=args.as_json)
    except BrokenPipeError:
        # Piping into head/less that exits early is not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
