"""Operational hooks: python -m app.ops COMMAND

Used by host-side scripts (backup.sh) and the centralpay management command
to record operational events in the database. These are append-only
operational records — never financial mutations: no command here changes an
amount, fabricates a verification, alters a reference id, or deletes events.

Commands:
  backup-event {success|failure} [--size TEXT] [--file-name TEXT]
                                 [--retention-days N] [--detail TEXT]
  test-alert
  review list | show ORDER_ID | acknowledge ORDER_ID --note TEXT
  review resolve ORDER_ID --resolution VALUE --note TEXT
  review resend ORDER_ID --confirm-idempotent-bot --yes   (idempotent mode only)
  db-check [--repair-sequences]   read-only integrity checks (restore verification)
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.adminbot.alerts import configure_alert_creation, create_alert
from app.audit import record_event
from app.config import Settings
from app.db import create_session_factory
from app.logging_setup import configure_logging
from app.models import Payment, PaymentStatus

# Non-financial operational resolution states only.
ALLOWED_RESOLUTIONS = (
    "confirmed_by_bot_operator",
    "duplicate_notification_confirmed_safe",
    "bot_not_credited",
    "refund_required",
    "false_positive",
    "configuration_fixed",
)


def _cmd_backup_event(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    configure_alert_creation(settings)
    session_factory = create_session_factory(settings.database_url)
    event_type = "backup_succeeded" if args.outcome == "success" else "backup_failed"
    data: dict[str, object] = {}
    if args.size:
        data["size"] = args.size[:64]
    if args.file_name:
        # Base name only: full paths are unnecessary disclosure.
        data["file_name"] = args.file_name.rsplit("/", 1)[-1][:128]
    if args.retention_days:
        data["retention_days"] = args.retention_days
    if args.detail:
        data["detail"] = args.detail[:200]
    with session_factory() as db:
        record_event(
            db,
            payment_id=None,
            event_type=event_type,
            level="info" if args.outcome == "success" else "error",
            data=data,
        )
        db.commit()
    print(f"recorded {event_type}")
    return 0


def _cmd_test_alert(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    if not settings.admin_bot_enabled:
        print("admin bot is disabled (ADMIN_BOT_ENABLED=false)", file=sys.stderr)
        return 1
    session_factory = create_session_factory(settings.database_url)
    with session_factory() as db:
        alert = create_alert(
            db,
            alert_type="admin_test_alert",
            severity="info",
            payload={"detail": "پیام آزمایشی — این فقط یک تست است / test message"},
        )
        db.commit()
        print(f"test alert queued (id={alert.id}); delivery within the poll interval")
    return 0


# --- manual review operations (host CLI; never through Telegram) -----------


def _review_summary(payment: Payment) -> dict[str, object]:
    return {
        "bot_order_id": payment.bot_order_id,
        "gateway_order_id": payment.gateway_order_id,
        "amount": payment.amount,
        "status": payment.status,
        "gateway_verified": payment.gateway_verified_at is not None,
        "reason": payment.bot_notify_reason or payment.last_error,
        "attempts": payment.bot_notify_attempts,
        "reference_id": payment.reference_id,
        "manual_review_at": (
            payment.manual_review_at.isoformat() if payment.manual_review_at else None
        ),
        "acknowledged_at": (
            payment.review_acknowledged_at.isoformat()
            if payment.review_acknowledged_at
            else None
        ),
        "resolved_at": (
            payment.review_resolved_at.isoformat() if payment.review_resolved_at else None
        ),
        "resolution": payment.review_resolution,
    }


def _load_review_payment(db: Session, order_id: str) -> Payment | None:
    payment = db.execute(
        select(Payment).where(Payment.bot_order_id == order_id).with_for_update()
    ).scalar_one_or_none()
    if payment is None and order_id.isdigit():
        payment = db.execute(
            select(Payment)
            .where(Payment.gateway_order_id == int(order_id))
            .with_for_update()
        ).scalar_one_or_none()
    return payment


_SEQUENCE_TABLES = ("payments", "payment_events", "admin_alerts", "worker_heartbeats")


def _cmd_db_check(args: argparse.Namespace) -> int:
    """Database integrity checks used after a restore (and on demand).

    Read-only by default. --repair-sequences advances any PostgreSQL
    sequence that fell behind its table maximum (safe: setval to MAX(id),
    schema-qualified names taken from pg_get_serial_sequence itself).
    Never touches financial data.
    """
    settings = Settings()
    configure_logging(settings)
    session_factory = create_session_factory(settings.database_url)
    failures: list[str] = []
    report: dict[str, object] = {}

    with session_factory() as db:
        from app.models import PaymentEvent

        try:
            revision = db.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
        except Exception:
            # Clear the aborted transaction so the remaining checks run.
            db.rollback()
            revision = None
        report["alembic_revision"] = revision
        if revision is None:
            failures.append("alembic_version_missing")

        def dup_count(column: Any) -> int:
            sub = (
                select(column)
                .where(column.is_not(None))
                .group_by(column)
                .having(func.count() > 1)
                .subquery()
            )
            return int(db.execute(select(func.count()).select_from(sub)).scalar_one())

        checks: dict[str, int] = {
            "invalid_payment_status": int(
                db.execute(
                    select(func.count(Payment.id)).where(
                        Payment.status.not_in([status.value for status in PaymentStatus])
                    )
                ).scalar_one()
            ),
            "duplicate_bot_order_id": dup_count(Payment.bot_order_id),
            "duplicate_gateway_order_id": dup_count(Payment.gateway_order_id),
            "duplicate_reference_id": dup_count(Payment.reference_id),
            "orphan_payment_events": int(
                db.execute(
                    select(func.count(PaymentEvent.id)).where(
                        PaymentEvent.payment_id.is_not(None),
                        PaymentEvent.payment_id.not_in(select(Payment.id)),
                    )
                ).scalar_one()
            ),
            "claims_on_non_pending_payments": int(
                db.execute(
                    select(func.count(Payment.id)).where(
                        Payment.notification_claimed_at.is_not(None),
                        Payment.status != PaymentStatus.BOT_NOTIFY_PENDING.value,
                    )
                ).scalar_one()
            ),
        }
        report["checks"] = checks
        failures.extend(name for name, value in checks.items() if value != 0)

        sequences: dict[str, dict[str, object]] = {}
        if db.get_bind().dialect.name == "postgresql":
            repaired: list[str] = []
            for table in _SEQUENCE_TABLES:
                seq_name = db.execute(
                    text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table}
                ).scalar_one_or_none()
                if seq_name is None:
                    continue
                max_id = int(
                    db.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")).scalar_one()
                )
                # seq_name comes from PostgreSQL itself (schema-qualified),
                # never from user input.
                last_value, is_called = db.execute(
                    text(f"SELECT last_value, is_called FROM {seq_name}")
                ).one()
                behind = max_id > 0 and (
                    int(last_value) < max_id or (int(last_value) == max_id and not is_called)
                )
                sequences[table] = {
                    "sequence": seq_name,
                    "max_id": max_id,
                    "last_value": int(last_value),
                    "behind": behind,
                }
                if behind and args.repair_sequences:
                    db.execute(text(f"SELECT setval('{seq_name}', {max_id})"))
                    repaired.append(table)
                elif behind:
                    failures.append(f"sequence_behind:{table}")
            if repaired:
                db.commit()
                report["repaired_sequences"] = repaired
        report["sequences"] = sequences

    report["status"] = "ok" if not failures else "failed"
    report["failures"] = failures
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if not failures else 1


def _cmd_review(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    configure_alert_creation(settings)
    session_factory = create_session_factory(settings.database_url)

    with session_factory() as db:
        if args.review_command == "list":
            payments = db.execute(
                select(Payment)
                .where(Payment.status == PaymentStatus.MANUAL_REVIEW.value)
                .order_by(Payment.manual_review_at.asc().nulls_first())
            ).scalars()
            shown = 0
            for row in payments:
                if row.review_resolved_at is not None and not args.all:
                    continue
                print(json.dumps(_review_summary(row), ensure_ascii=False))
                shown += 1
            if shown == 0:
                print("no unresolved manual-review payments" if not args.all else "none")
            return 0

        payment = _load_review_payment(db, args.order_id)
        if payment is None:
            print(f"payment not found: {args.order_id}", file=sys.stderr)
            return 1

        if args.review_command == "show":
            db.rollback()
            print(json.dumps(_review_summary(payment), ensure_ascii=False, indent=2))
            return 0

        if payment.status != PaymentStatus.MANUAL_REVIEW.value:
            print(
                f"payment is not in manual_review (status={payment.status})",
                file=sys.stderr,
            )
            db.rollback()
            return 1

        now = datetime.now(UTC)
        if args.review_command == "acknowledge":
            payment.review_acknowledged_at = now
            record_event(
                db,
                payment_id=payment.id,
                event_type="manual_review_acknowledged",
                data={"note": args.note[:500], "operator": "host-cli"},
            )
            db.commit()
            print(f"acknowledged {payment.bot_order_id}")
            return 0

        if args.review_command == "resolve":
            # Operational resolution only: financial fields (amount,
            # reference_id, verification facts, status history) are never
            # modified, and no customer balance is ever touched from here.
            payment.review_acknowledged_at = payment.review_acknowledged_at or now
            payment.review_resolved_at = now
            payment.review_resolution = args.resolution
            record_event(
                db,
                payment_id=payment.id,
                event_type="manual_review_resolved",
                data={
                    "resolution": args.resolution,
                    "note": args.note[:500],
                    "operator": "host-cli",
                },
            )
            db.commit()
            print(f"resolved {payment.bot_order_id}: {args.resolution}")
            return 0

        if args.review_command == "resend":
            if settings.bot_notify_retry_mode != "idempotent":
                print(
                    "resend refused: BOT_NOTIFY_RETRY_MODE is not 'idempotent'. "
                    "In safe mode ambiguous deliveries must be resolved manually.",
                    file=sys.stderr,
                )
                db.rollback()
                return 1
            if payment.gateway_verified_at is None:
                print(
                    "resend refused: payment was never gateway-verified.",
                    file=sys.stderr,
                )
                db.rollback()
                return 1
            payment.status = PaymentStatus.BOT_NOTIFY_PENDING.value
            payment.next_retry_at = now
            payment.notification_claimed_at = None
            payment.notification_claimed_by = None
            record_event(
                db,
                payment_id=payment.id,
                event_type="manual_review_resend_requested",
                level="warning",
                data={
                    "operator": "host-cli",
                    "previous_reason": payment.bot_notify_reason,
                    "retry_mode": settings.bot_notify_retry_mode,
                },
            )
            db.commit()
            print(f"requeued {payment.bot_order_id} for bot notification")
            return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ops", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup-event", help="record a backup outcome")
    backup.add_argument("outcome", choices=["success", "failure"])
    backup.add_argument("--size", default="")
    backup.add_argument("--file-name", default="")
    backup.add_argument("--retention-days", type=int, default=0)
    backup.add_argument("--detail", default="")
    sub.add_parser("test-alert", help="queue a clearly marked test alert")

    db_check = sub.add_parser("db-check", help="database integrity checks (restore verification)")
    db_check.add_argument(
        "--repair-sequences",
        action="store_true",
        help="advance sequences that fell behind their table maxima",
    )

    review = sub.add_parser("review", help="manual-review operations (host only)")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_list = review_sub.add_parser("list")
    review_list.add_argument("--all", action="store_true", help="include resolved")
    review_show = review_sub.add_parser("show")
    review_show.add_argument("order_id")
    review_ack = review_sub.add_parser("acknowledge")
    review_ack.add_argument("order_id")
    review_ack.add_argument("--note", required=True)
    review_resolve = review_sub.add_parser("resolve")
    review_resolve.add_argument("order_id")
    review_resolve.add_argument("--resolution", required=True, choices=ALLOWED_RESOLUTIONS)
    review_resolve.add_argument("--note", required=True)
    review_resend = review_sub.add_parser("resend")
    review_resend.add_argument("order_id")
    review_resend.add_argument("--confirm-idempotent-bot", action="store_true")
    review_resend.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "backup-event":
        return _cmd_backup_event(args)
    if args.command == "db-check":
        return _cmd_db_check(args)
    if args.command == "review":
        if args.review_command == "resend" and not (
            args.confirm_idempotent_bot and args.yes
        ):
            print(
                "resend requires --confirm-idempotent-bot AND --yes "
                "(only after the bot developer confirmed duplicate delivery is idempotent)",
                file=sys.stderr,
            )
            return 1
        if args.review_command in ("acknowledge", "resolve") and not args.note.strip():
            print("a non-empty --note is required", file=sys.stderr)
            return 1
        return _cmd_review(args)
    return _cmd_test_alert(args)


if __name__ == "__main__":
    sys.exit(main())
