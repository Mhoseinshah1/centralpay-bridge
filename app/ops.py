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
from app.models import FeePolicy, Payment, PaymentStatus

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
        "original_bot_invoice": payment.amount,
        "amount": payment.amount,
        "fee_rate_bps": payment.fee_rate_bps,
        "fee_amount": payment.fee_amount,
        "paid_through_gateway": payment.payable_amount,
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


# Advisory-lock key serializing `fee set --ensure-initial` across processes
# (installer reruns racing each other). Arbitrary but fixed; used only with
# pg_advisory_xact_lock, so it is released automatically at commit/rollback.
FEE_ENSURE_INITIAL_LOCK_KEY = 0x6665_6501  # "fee\x01"


def _cmd_fee(args: argparse.Namespace) -> int:
    """Fee policy operations (host CLI delegates here; no shell SQL).

    Mutations are append-only and permanently audited. Fee changes affect
    NEW payment orders only: existing payments keep their immutable
    snapshot forever.
    """
    from app.services.fees import (
        cancel_policy,
        create_policy,
        format_rate_percent,
        next_scheduled_policy,
        parse_rate_percent,
        select_effective_policy,
    )

    settings = Settings()
    configure_logging(settings)
    session_factory = create_session_factory(settings.database_url)

    with session_factory() as db:
        if args.fee_command == "status":
            active = select_effective_policy(db)
            scheduled = next_scheduled_policy(db)
            if active is None:
                print("Current fee: 0% (no fee policy configured)")
            else:
                print(f"Current fee: {format_rate_percent(active.rate_bps)}")
                print(f"Rate basis points: {active.rate_bps}")
                print(f"Effective since: {active.effective_at.isoformat()}")
                print(f"Policy ID: {active.id}")
            if scheduled is not None:
                print(
                    f"Next scheduled: {format_rate_percent(scheduled.rate_bps)} "
                    f"at {scheduled.effective_at.isoformat()} (policy {scheduled.id})"
                )
            print("Applies to: new payment orders only")
            db.rollback()
            return 0

        if args.fee_command == "history":
            policies = (
                db.execute(select(FeePolicy).order_by(FeePolicy.id.asc())).scalars().all()
            )
            if not policies:
                print("No fee policies recorded.")
            for policy in policies:
                state = "cancelled" if policy.cancelled_at is not None else "active/scheduled"
                print(
                    json.dumps(
                        {
                            "policy_id": policy.id,
                            "rate_bps": policy.rate_bps,
                            "rate": format_rate_percent(policy.rate_bps),
                            "effective_at": policy.effective_at.isoformat(),
                            "created_at": policy.created_at.isoformat()
                            if policy.created_at
                            else None,
                            "created_by": policy.created_by,
                            "note": policy.note,
                            "state": state,
                            "cancelled_at": policy.cancelled_at.isoformat()
                            if policy.cancelled_at
                            else None,
                            "cancelled_by": policy.cancelled_by,
                        },
                        ensure_ascii=False,
                    )
                )
            db.rollback()
            return 0

        actor = args.actor
        try:
            if args.fee_command in ("set", "schedule"):
                rate_bps = parse_rate_percent(args.rate)
                if args.fee_command == "schedule":
                    effective_at = datetime.fromisoformat(args.at)
                    if effective_at.tzinfo is None:
                        raise ValueError(
                            "--at must be an ISO timestamp with an explicit timezone"
                        )
                    if effective_at <= datetime.now(UTC):
                        raise ValueError("--at must be in the future (use 'fee set' for now)")
                    scheduled_flag = True
                else:
                    effective_at = datetime.now(UTC)
                    scheduled_flag = False
                if args.ensure_initial:
                    # "Initial" means the fee_policies table has ZERO rows —
                    # not "no currently effective policy". A future scheduled
                    # policy or cancelled history is an operator decision the
                    # installer must never override with a surprise immediate
                    # policy. Serialize concurrent installer reruns with a
                    # transaction-level advisory lock (PostgreSQL): the loser
                    # waits for the winner's commit, re-counts, and no-ops —
                    # at most one initial policy can ever be created.
                    if db.get_bind().dialect.name == "postgresql":
                        db.execute(
                            text("SELECT pg_advisory_xact_lock(:key)"),
                            {"key": FEE_ENSURE_INITIAL_LOCK_KEY},
                        )
                    existing = db.execute(
                        select(func.count(FeePolicy.id))
                    ).scalar_one()
                    if existing:
                        print(
                            f"Fee policy history already exists ({existing} "
                            "row(s), including any scheduled or cancelled "
                            "policies); --ensure-initial makes no change. "
                            "Use 'centralpay fee set' to change the fee."
                        )
                        db.rollback()
                        return 0
                policy = create_policy(
                    db,
                    rate_bps=rate_bps,
                    effective_at=effective_at,
                    actor=actor,
                    note=args.note,
                    scheduled=scheduled_flag,
                )
                db.commit()
                verb = "scheduled" if scheduled_flag else "set"
                print(
                    f"Fee {verb}: {format_rate_percent(rate_bps)} "
                    f"(policy {policy.id}, effective {effective_at.isoformat()})"
                )
                print("Applies to: new payment orders only")
                return 0

            # cancel
            policy = cancel_policy(
                db, policy_id=args.policy_id, actor=actor, note=args.note
            )
            db.commit()
            print(f"Fee policy {policy.id} cancelled (history preserved).")
            return 0
        except ValueError as exc:
            db.rollback()
            print(f"error: {exc}", file=sys.stderr)
            return 1


_SEQUENCE_TABLES = (
    "payments",
    "payment_events",
    "admin_alerts",
    "worker_heartbeats",
    "fee_policies",
)


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
            # Fee snapshot integrity. db-check REPORTS corruption; it never
            # recalculates or overwrites historical financial snapshots.
            "invalid_fee_rate": int(
                db.execute(
                    select(func.count(Payment.id)).where(
                        (Payment.fee_rate_bps < 0) | (Payment.fee_rate_bps > 10000)
                    )
                ).scalar_one()
            ),
            "negative_fee_amount": int(
                db.execute(
                    select(func.count(Payment.id)).where(Payment.fee_amount < 0)
                ).scalar_one()
            ),
            "payable_amount_mismatch": int(
                db.execute(
                    select(func.count(Payment.id)).where(
                        Payment.payable_amount != Payment.amount + Payment.fee_amount
                    )
                ).scalar_one()
            ),
            "missing_payable_amount": int(
                db.execute(
                    select(func.count(Payment.id)).where(Payment.payable_amount.is_(None))
                ).scalar_one()
            ),
            "orphan_fee_policy_reference": int(
                db.execute(
                    select(func.count(Payment.id)).where(
                        Payment.fee_policy_id.is_not(None),
                        Payment.fee_policy_id.not_in(select(FeePolicy.id)),
                    )
                ).scalar_one()
            ),
            # Legacy backfill / policy-less payments must be zero-fee.
            "policyless_payment_with_fee": int(
                db.execute(
                    select(func.count(Payment.id)).where(
                        Payment.fee_policy_id.is_(None),
                        (Payment.fee_rate_bps != 0) | (Payment.fee_amount != 0),
                    )
                ).scalar_one()
            ),
            "invalid_fee_policy_rows": int(
                db.execute(
                    select(func.count(FeePolicy.id)).where(
                        (FeePolicy.rate_bps < 0)
                        | (FeePolicy.rate_bps > 10000)
                        | (FeePolicy.note == "")
                        | (
                            FeePolicy.cancelled_at.is_not(None)
                            & (
                                FeePolicy.cancelled_by.is_(None)
                                | FeePolicy.cancellation_note.is_(None)
                            )
                        )
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

    fee = sub.add_parser("fee", help="fee policy operations (append-only, audited)")
    fee_sub = fee.add_subparsers(dest="fee_command", required=True)
    fee_sub.add_parser("status")
    fee_sub.add_parser("history")
    fee_set = fee_sub.add_parser("set")
    fee_set.add_argument("rate")
    fee_set.add_argument("--note", required=True)
    fee_set.add_argument("--actor", default="host-cli")
    fee_set.add_argument(
        "--ensure-initial",
        action="store_true",
        help="create the policy only when none exists (installer; never resets)",
    )
    fee_schedule = fee_sub.add_parser("schedule")
    fee_schedule.add_argument("rate")
    fee_schedule.add_argument("--at", required=True)
    fee_schedule.add_argument("--note", required=True)
    fee_schedule.add_argument("--actor", default="host-cli")
    fee_cancel = fee_sub.add_parser("cancel")
    fee_cancel.add_argument("policy_id", type=int)
    fee_cancel.add_argument("--note", required=True)
    fee_cancel.add_argument("--actor", default="host-cli")

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
    if args.command == "fee":
        if args.fee_command in ("set", "schedule", "cancel") and not args.note.strip():
            print("a non-empty --note is required", file=sys.stderr)
            return 1
        if args.fee_command in ("schedule", "cancel"):
            args.ensure_initial = False
        return _cmd_fee(args)
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
