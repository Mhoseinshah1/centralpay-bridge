"""Operational hooks: python -m app.ops COMMAND

Used by host-side scripts (backup.sh) and the centralpay management command
to record operational events in the database. These are append-only
operational records — never financial mutations: no command here changes an
amount, fabricates a verification, alters a reference id, or deletes events.

Commands:
  backup-event {success|failure} [--size TEXT] [--file-name TEXT]
                                 [--retention-days N] [--detail TEXT]
  test-alert
  review list [--all] | show ORDER_ID | acknowledge ORDER_ID --note TEXT
  review resolve ORDER_ID --resolution VALUE --note TEXT
  review resolve-many ORDER_ID [ORDER_ID ...] --resolution VALUE --note TEXT [--yes]
      All-or-nothing bulk resolution of an EXPLICIT list of open manual
      reviews. ONLY allowlisted downstream-DELIVERY failures
      (retry_limit_reached / bot_timeout_ambiguous) are eligible;
      financial/verification reviews (bot_notify_reason IS NULL) are refused
      and must be resolved individually with `review resolve`.
      Preview-only without --yes. Never "resolve all", never any gateway or
      downstream-bot request, never a financial mutation.
      See app.services.review_resolution.
  review resend ORDER_ID --confirm-idempotent-bot --yes   (idempotent mode only)
  attention list [--resolved] | show ORDER_ID
  attention resolve ORDER_ID --resolution VALUE --note TEXT --yes
      Durably close a STALE NON-FINANCIAL operator-attention item (e.g. an
      old getlink_failed payment that never obtained a payment link) without
      deleting anything. Preserves the payment row, every payment event, and
      every admin alert; never changes status or any financial fact.
      See app.services.attention.
  notification accept ORDER_ID --note TEXT --yes
      Mark one already-processed, gateway-verified bot notification (stuck
      in bot_notify_pending) as operator-confirmed accepted. Makes NO bot
      or gateway request; see app.services.notification.execute_manual_accept.
  db-check [--repair-sequences]   read-only integrity checks (restore verification)
  db-check --details [--json]     same report, plus a bounded, read-only drill-down
                                   into the rows behind any failed check
                                   (mutually exclusive with --repair-sequences)
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.adminbot import queries
from app.adminbot.alerts import configure_alert_creation, create_alert
from app.audit import record_event
from app.cli import AmbiguousOrderIdError, _find_payment
from app.config import Settings
from app.db import create_session_factory
from app.logging_setup import configure_logging
from app.models import FeePolicy, Payment, PaymentStatus
from app.services import attention as attention_service
from app.services import review_resolution
from app.services.notification import ManualAcceptRefusal, execute_manual_accept
from app.services.stuck_payments import unexpected_status_conditions

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

        if args.fee_command == "ensure-initial":
            # Idempotent initial-policy creation for the installer. Atomic
            # under a transaction-level advisory lock (PostgreSQL): serialize
            # concurrent installer reruns so at most one initial policy is
            # ever created. "Initial" means the fee_policies table has ZERO
            # rows — any scheduled/cancelled history is an operator decision
            # the installer must never override.
            if db.get_bind().dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": FEE_ENSURE_INITIAL_LOCK_KEY},
                )
            existing = db.execute(select(func.count(FeePolicy.id))).scalar_one()
            if existing:
                print(
                    f"Fee policy history already exists ({existing} row(s), "
                    "including any scheduled or cancelled policies); no change. "
                    "Use 'centralpay fee set' to change the fee."
                )
                db.rollback()
                return 0
            # Zero rows: a validated rate MUST be supplied. A missing value
            # never means 0% — that is exactly the CANON-1 defect. Fail
            # closed so the installer cannot silently ship a 0% fee.
            if args.percent is None:
                db.rollback()
                print(
                    "error: no fee policy exists and no initial rate was "
                    "supplied. The installer's recorded initial rate "
                    "(INSTALLER_INITIAL_FEE_PERCENT) is missing, so NO policy "
                    "was created and NO fee is configured. Re-run the "
                    "installer and choose to reconfigure, or set the fee "
                    "explicitly with: centralpay fee set <rate>",
                    file=sys.stderr,
                )
                return 1
            try:
                rate_bps = parse_rate_percent(args.percent)
            except ValueError as exc:
                db.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 1
            policy = create_policy(
                db,
                rate_bps=rate_bps,
                effective_at=datetime.now(UTC),
                actor=args.actor,
                note=args.note,
                scheduled=False,
            )
            db.commit()
            print(
                f"Initial fee policy created: {format_rate_percent(rate_bps)} "
                f"(policy {policy.id})."
            )
            print("Applies to: new payment orders only")
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
    "monitor_incidents",
)


# --- db-check --details: bounded, read-only drill-down ---------------------
#
# Strictly read-only (SELECT only, never `FOR UPDATE`) and strictly local (no
# CentralPay/bot/Telegram request of any kind). Every value printed is either
# a Payment column printed VERBATIM -- a legacy/unrecognized status is never
# reinterpreted or mapped onto a known one -- or a PaymentEvent.data value
# that passed the explicit allowlist below. PaymentEvent.data can carry
# gateway-influenced text or a Telegram identifier for OTHER event types (see
# app.centralpay's "gateway-controlled data policy" and the
# admin_command_received/succeeded/failed events in app.adminbot.commands),
# so nothing outside that allowlist is ever printed here.

_DETAILS_ROW_LIMIT = 20
_DETAILS_EVENT_LIMIT = 50

# Fixed-vocabulary, non-secret PaymentEvent.data keys only. Every key here
# has been verified at every call site to originate from an internal enum, an
# HTTP status code, a bounded fixed-vocabulary reason string, or an existing
# safe Payment column (e.g. `previous_reason` mirrors bot_notify_reason) --
# never raw gateway response text, an operator's free-text note, or a
# Telegram user/chat id.
_SAFE_EVENT_DATA_KEYS = (
    "reason",
    "reason_code",
    "previous_reason",
    "error_code",
    "http_status",
    "attempt",
    "duration_ms",
    "stage",
    "worker_id",
    "payment_status",
    "resolution",
    "field_errors",
    "retry_mode",
    "scheduled",
    "idempotent",
)

_SAFE_SCALAR_TYPES = (str, int, float, bool)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _safe_event_data(data: object) -> dict[str, object]:
    """Extract only the allowlisted, fixed-vocabulary keys from event data.

    Defense in depth beyond the key allowlist itself: any value of an
    unexpected shape (not a plain scalar, or not a list/tuple of plain
    scalars) is dropped rather than printed, and every string is bounded --
    so a future call site that adds an unexpected value under an allowed key
    name still cannot leak arbitrary-length or structured content here.
    """
    if not isinstance(data, dict):
        return {}
    safe: dict[str, object] = {}
    for key in _SAFE_EVENT_DATA_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            safe[key] = value[:200]
        elif isinstance(value, _SAFE_SCALAR_TYPES) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [
                item[:200] if isinstance(item, str) else item
                for item in value
                if isinstance(item, _SAFE_SCALAR_TYPES)
            ]
    return safe


def _payment_audit_trail(db: Session, payment_id: int) -> dict[str, object]:
    from app.models import PaymentEvent

    total = int(
        db.execute(
            select(func.count(PaymentEvent.id)).where(PaymentEvent.payment_id == payment_id)
        ).scalar_one()
    )
    rows = (
        db.execute(
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id)
            .order_by(PaymentEvent.created_at.asc(), PaymentEvent.id.asc())
            .limit(_DETAILS_EVENT_LIMIT)
        )
        .scalars()
        .all()
    )
    events = [
        {
            "event_type": event.event_type,
            "level": event.level,
            "created_at": _iso(event.created_at),
            "request_id": event.request_id,
            "reason_fields": _safe_event_data(event.data),
        }
        for event in rows
    ]
    return {
        "total": total,
        "shown": len(events),
        "truncated": total > len(events),
        "limit": _DETAILS_EVENT_LIMIT,
        "events": events,
    }


def _invalid_payment_status_row(db: Session, payment: Payment) -> dict[str, object]:
    return {
        "id": payment.id,
        "bot_order_id": payment.bot_order_id,
        "gateway_order_id": payment.gateway_order_id,
        # Raw current status, printed exactly as stored -- never
        # reinterpreted or mapped onto a known PaymentStatus value.
        "status": payment.status,
        "gateway_verified": payment.gateway_verified_at is not None,
        "gateway_verified_at": _iso(payment.gateway_verified_at),
        "bot_notify_reason": payment.bot_notify_reason,
        "bot_notify_attempts": payment.bot_notify_attempts,
        "bot_last_http_status": payment.bot_last_http_status,
        "bot_notify_started_at": _iso(payment.bot_notify_started_at),
        "bot_notify_accepted_at": _iso(payment.bot_notify_accepted_at),
        "next_retry_at": _iso(payment.next_retry_at),
        "manual_review_at": _iso(payment.manual_review_at),
        "review_acknowledged_at": _iso(payment.review_acknowledged_at),
        "review_resolved_at": _iso(payment.review_resolved_at),
        "review_resolution": payment.review_resolution,
        "created_at": _iso(payment.created_at),
        "updated_at": _iso(payment.updated_at),
        "audit_events": _payment_audit_trail(db, payment.id),
    }


def _invalid_payment_status_detail(
    db: Session, condition: Any, total: int
) -> dict[str, object]:
    rows = (
        db.execute(
            select(Payment)
            .where(condition)
            .order_by(Payment.id.asc())
            .limit(_DETAILS_ROW_LIMIT)
        )
        .scalars()
        .all()
    )
    shown = [_invalid_payment_status_row(db, row) for row in rows]
    return {
        "total": total,
        "shown": len(shown),
        "truncated": total > len(shown),
        "limit": _DETAILS_ROW_LIMIT,
        "ordering": "id_ascending",
        "rows": shown,
    }


def _build_db_check_details(
    db: Session, checks: dict[str, int], invalid_status_condition: Any
) -> dict[str, object]:
    """Bounded, read-only drill-down for every FAILED check in `checks`.

    Only `invalid_payment_status` has a row-level implementation in this
    release (it is the check this feature was built to inspect). Every other
    failing check gets an explicit `supported: false` marker instead of a
    silent omission, so a caller can never mistake "not implemented yet" for
    "checked and clean".
    """
    details: dict[str, object] = {}
    if checks.get("invalid_payment_status"):
        details["invalid_payment_status"] = _invalid_payment_status_detail(
            db, invalid_status_condition, checks["invalid_payment_status"]
        )
    for name, count in checks.items():
        if name == "invalid_payment_status" or count == 0:
            continue
        details[name] = {
            "supported": False,
            "total": count,
            "note": (
                "row-level detail is not implemented for this check yet; "
                "see 'checks' for the exact failing count"
            ),
        }
    return details


def run_db_checks(
    db: Session, *, repair_sequences: bool = False, details: bool = False
) -> dict[str, object]:
    """Database integrity checks used after a restore, on demand, and by
    app.monitor's db_integrity check -- the single source of this SQL so
    `centralpay db-check` and the monitor can never quietly drift apart.

    Read-only by default. repair_sequences advances any PostgreSQL sequence
    that fell behind its table maximum (safe: setval to MAX(id),
    schema-qualified names taken from pg_get_serial_sequence itself). Never
    touches financial data. Takes an already-open session; never opens or
    closes one itself, so a caller (CLI or monitor) controls the session
    lifecycle and commit/rollback boundary.
    """
    failures: list[str] = []
    report: dict[str, object] = {}

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

    # Reused verbatim (same expression object) by --details below, so the
    # detail rows are guaranteed to match this exact predicate -- never a
    # separately maintained copy that could silently drift from it.
    invalid_status_condition = Payment.status.not_in(
        [status.value for status in PaymentStatus]
    )

    checks: dict[str, int] = {
        "invalid_payment_status": int(
            db.execute(
                select(func.count(Payment.id)).where(invalid_status_condition)
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
            if behind and repair_sequences:
                db.execute(text(f"SELECT setval('{seq_name}', {max_id})"))
                repaired.append(table)
            elif behind:
                failures.append(f"sequence_behind:{table}")
        if repaired:
            db.commit()
            report["repaired_sequences"] = repaired
    report["sequences"] = sequences

    if details:
        report["details"] = _build_db_check_details(db, checks, invalid_status_condition)

    report["status"] = "ok" if not failures else "failed"
    report["failures"] = failures
    return report


def _cmd_db_check(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    session_factory = create_session_factory(settings.database_url)
    with session_factory() as db:
        report = run_db_checks(
            db, repair_sequences=args.repair_sequences, details=args.details
        )
    if args.details and args.details_json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] == "ok" else 1


def _cmd_privacy_audit(args: argparse.Namespace) -> int:
    """Payer-identity isolation report (incident 2026-07). Counts only — never
    a raw Telegram id or card datum. Exit 1 if any hard invariant is violated
    (a gateway userId shared by two payer identities, which would re-share card
    suggestions)."""
    from sqlalchemy import func

    from app.models import CentralPayPayerIdentity

    settings = Settings()
    configure_logging(settings)
    session_factory = create_session_factory(settings.database_url)
    failures: list[str] = []
    report: dict[str, object] = {}

    with session_factory() as db:
        payments_total = db.execute(select(func.count(Payment.id))).scalar_one()
        legacy = db.execute(
            select(func.count(Payment.id)).where(Payment.payer_identity_id.is_(None))
        ).scalar_one()
        isolated = db.execute(
            select(func.count(Payment.id)).where(Payment.payer_identity_id.is_not(None))
        ).scalar_one()
        # 0007-era rows: isolated (mapped) but created before scope tracking
        # (migration 0008), so payer_identity_type stayed NULL — their scope is
        # historical/untyped by design, never guessed.
        untyped_isolated = db.execute(
            select(func.count(Payment.id)).where(
                Payment.payer_identity_id.is_not(None),
                Payment.payer_identity_type.is_(None),
            )
        ).scalar_one()
        mappings = db.execute(
            select(func.count(CentralPayPayerIdentity.id))
        ).scalar_one()
        # Per-scheme mapping counts (labels only — never a gateway_user_id,
        # which under telegram_raw_v1 is the raw Telegram id).
        mappings_by_scheme: dict[str, int] = {
            row[0]: row[1]
            for row in db.execute(
                select(
                    CentralPayPayerIdentity.identity_scheme,
                    func.count(CentralPayPayerIdentity.id),
                ).group_by(CentralPayPayerIdentity.identity_scheme)
            ).all()
        }
        # A gateway userId owned by more than one payer identity is the exact
        # failure this fix prevents; DB uniqueness should keep it at zero.
        dup_rows = db.execute(
            select(CentralPayPayerIdentity.gateway_user_id)
            .group_by(CentralPayPayerIdentity.gateway_user_id)
            .having(func.count(CentralPayPayerIdentity.id) > 1)
        ).all()
        newest_legacy = db.execute(
            select(func.max(Payment.created_at)).where(Payment.payer_identity_id.is_(None))
        ).scalar_one_or_none()

    guard = "active"
    if not settings.payment_creation_enabled:
        guard = "disabled"
    elif not settings.centralpay_payer_id_secret:
        guard = "misconfigured"
        failures.append("payer_id_secret_missing")

    duplicate_count = len(dup_rows)
    if duplicate_count:
        failures.append("duplicate_gateway_user_ids")

    report = {
        "payments_total": payments_total,
        "legacy_shared_payer_payments": legacy,
        "isolated_payer_payments": isolated,
        "untyped_isolated_payments": untyped_isolated,
        "payer_identity_mappings": mappings,
        "payer_identity_mappings_by_scheme": mappings_by_scheme,
        "duplicate_gateway_user_ids": duplicate_count,
        # Post-fix, every new payment is mapped; legacy rows are the only ones
        # without a mapping and are the residual pre-fix exposure surface.
        "payments_missing_payer_mapping": legacy,
        "newest_legacy_payment_at": newest_legacy.isoformat() if newest_legacy else None,
        "payment_creation_guard": guard,
        "status": "ok" if not failures else "attention",
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if not failures else 1


def _cmd_review(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    configure_alert_creation(settings)
    session_factory = create_session_factory(settings.database_url)

    with session_factory() as db:
        if args.review_command == "list":
            # Filtered in SQL via the SHARED predicate every other surface
            # uses (app.adminbot.queries.open_manual_review_conditions) rather
            # than selecting every manual_review row and dropping resolved
            # ones in Python: one definition of "open", and no unbounded read
            # of permanently-accumulating resolved history just to discard it.
            conditions: tuple[Any, ...] = (
                (Payment.status == PaymentStatus.MANUAL_REVIEW.value,)
                if args.all
                else queries.open_manual_review_conditions()
            )
            payments = db.execute(
                select(Payment)
                .where(*conditions)
                .order_by(Payment.manual_review_at.asc().nulls_first())
            ).scalars()
            shown = 0
            for row in payments:
                print(json.dumps(_review_summary(row), ensure_ascii=False))
                shown += 1
            if shown == 0:
                print("no unresolved manual-review payments" if not args.all else "none")
            return 0

        if args.review_command == "resolve-many":
            return _cmd_review_resolve_many(db, args)

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


# --- review resolve-many: all-or-nothing bulk resolution -------------------
#
# All safety logic lives in app.services.review_resolution; this section only
# renders what that service decides. It never assigns a Payment attribute
# itself and never resolves an order id on its own.


def _bulk_row_line(row: review_resolution.BulkReviewRow) -> str:
    verified = (
        "?" if row.gateway_verified is None else ("yes" if row.gateway_verified else "NO")
    )
    detail = (
        f"status={row.status} gateway_verified={verified} "
        f"reason={row.bot_notify_reason or '—'} amount={row.amount}"
        if row.payment_id is not None
        else "—"
    )
    verdict = "OK" if row.refusal is None else f"REFUSED ({row.refusal.value}): {row.message}"
    return f"  {row.order_id}: {verdict}\n      {detail}"


def _print_bulk_report(report: review_resolution.BulkReviewReport) -> None:
    print(f"Bulk manual-review resolution preview (resolution={report.resolution})")
    print(
        "  SCOPE: allowlisted downstream-DELIVERY failures only "
        f"({', '.join(sorted(review_resolution.BULK_ELIGIBLE_DELIVERY_REASONS))}). "
        "Financial/verification reviews must be resolved individually with "
        "`centralpay review resolve`."
    )
    print(f"  orders listed: {len(report.rows)}")
    for row in report.rows:
        print(_bulk_row_line(row))
    if report.set_message is not None:
        print(f"  SET REFUSED ({report.set_refusal}): {report.set_message}")


def _bulk_resolve_warning(count: int, resolution: str) -> str:
    return (
        f"About to resolve {count} manual review(s) with "
        f"resolution={resolution}.\n"
        "  - Only allowlisted downstream-DELIVERY failures are eligible; "
        "financial/verification reviews are refused and must be handled "
        "individually.\n"
        "  - Does NOT contact CentralPay.\n"
        "  - Does NOT contact the selling bot.\n"
        "  - Does NOT credit any customer.\n"
        "  - Does NOT change amounts, fees, verification facts, or "
        "reference ids.\n"
        "  - Does NOT change payment status (rows stay manual_review as "
        "permanent history).\n"
        "  - Records one audited resolution per payment, all-or-nothing.\n"
        "Re-run with --yes to confirm."
    )


def _cmd_review_resolve_many(db: Session, args: argparse.Namespace) -> int:
    order_ids: list[str] = list(args.order_ids)
    if not args.yes:
        report = review_resolution.preview_bulk_resolution(
            db, order_ids=order_ids, resolution=args.resolution
        )
        _print_bulk_report(report)
        if not report.eligible:
            print(
                "refused: every listed order must individually pass the same "
                "safety checks; nothing was resolved.",
                file=sys.stderr,
            )
            return 1
        print(_bulk_resolve_warning(len(report.rows), args.resolution), file=sys.stderr)
        return 1

    result = review_resolution.resolve_reviews(
        db,
        order_ids=order_ids,
        resolution=args.resolution,
        note=args.note,
        actor="host-cli",
        now=datetime.now(UTC),
    )
    if not result.resolved:
        _print_bulk_report(result.report)
        print(
            "refused: nothing was resolved. The batch is all-or-nothing, so a "
            "single ineligible order blocks the whole set.",
            file=sys.stderr,
        )
        return 1
    for row in result.report.rows:
        print(f"resolved {row.bot_order_id}: {args.resolution}")
    print(f"resolved {result.resolved_count} manual review(s)")
    return 0


# --- attention: durable closure of stale NON-FINANCIAL failures ------------
#
# All safety logic lives in app.services.attention; this section only renders
# what that module decides and reuses app.cli's `_find_payment`/
# `AmbiguousOrderIdError` for ORDER_ID resolution. It never assigns a Payment
# attribute itself.


def _attention_summary(snapshot: attention_service.AttentionSnapshot) -> dict[str, object]:
    """Machine-readable attention view. Reports the ORIGINAL financial and
    failure facts verbatim alongside any resolution — never a summary that
    could imply the payment succeeded. `redirect_url` is exposed only as a
    boolean: a full payment redirect URL must never be printed."""
    return {
        "bot_order_id": snapshot.bot_order_id,
        "gateway_order_id": snapshot.gateway_order_id,
        "status": snapshot.status,
        "original_bot_invoice": snapshot.amount,
        "amount": snapshot.amount,
        "fee_rate_bps": snapshot.fee_rate_bps,
        "fee_amount": snapshot.fee_amount,
        "paid_through_gateway": snapshot.payable_amount,
        "gateway_verified": snapshot.gateway_verified,
        "gateway_verified_at": _iso(snapshot.gateway_verified_at),
        "reference_id": snapshot.reference_id,
        "redirect_url_present": snapshot.redirect_url_present,
        "callback_token_issued": snapshot.callback_token_issued,
        "bot_notify_attempts": snapshot.bot_notify_attempts,
        "manual_review_at": _iso(snapshot.manual_review_at),
        "last_error_code": snapshot.last_error_code,
        "created_at": _iso(snapshot.created_at),
        "attention_resolved": snapshot.attention_resolved_at is not None,
        "attention_resolved_at": _iso(snapshot.attention_resolved_at),
        "attention_resolution": snapshot.attention_resolution,
        "attention_resolved_by": snapshot.attention_resolved_by,
        "attention_resolution_note": snapshot.attention_resolution_note,
        "resolvable": snapshot.refusal is None,
        "attention_resolution_superseded": snapshot.attention_resolution_superseded,
        "refusal": snapshot.refusal.value if snapshot.refusal else None,
        "refusal_message": attention_service.snapshot_refusal_message(snapshot),
        "eligible_resolutions": list(snapshot.eligible_resolutions),
    }


def _attention_resolve_warning(order_id: str, resolution: str) -> str:
    return (
        f"About to record an operational attention resolution for {order_id} "
        f"(resolution={resolution}).\n"
        "  - Does NOT contact CentralPay.\n"
        "  - Does NOT contact the selling bot.\n"
        "  - Does NOT credit any customer.\n"
        "  - Does NOT change the payment status (a getlink_failed payment "
        "stays getlink_failed).\n"
        "  - Does NOT change amounts, fees, verification facts, reference "
        "ids, or payer identity.\n"
        "  - Does NOT delete the payment, any payment event, or any admin "
        "alert.\n"
        "  - Removes it from the CURRENT needs-attention worklist only; it "
        "stays fully inspectable.\n"
        "  - Does NOT block a later legitimate settlement: if CentralPay did "
        "create a link this bridge never received and a payer pays it, the "
        "normal callback path still settles the payment and it reappears in "
        "the ordinary delivery surfaces.\n"
        "You are asserting only that THIS BRIDGE never delivered a payment "
        "link for this order and has nothing further to do about it.\n"
        "Re-run with --yes to confirm."
    )


def _cmd_attention(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    configure_alert_creation(settings)
    session_factory = create_session_factory(settings.database_url)

    with session_factory() as db:
        if args.attention_command == "list":
            now = datetime.now(UTC)
            if args.resolved:
                # HISTORICAL view: filter ONLY on "a resolution was recorded".
                # Deliberately NO status filter. A resolved payment can
                # legitimately settle later through a late callback (see
                # app.services.attention), which moves it to a notification or
                # manual-review status while it keeps its resolution columns.
                # Scoping this to RESOLVABLE_STATUSES would drop exactly that
                # case from the history — the most interesting one — and break
                # the durability this feature promises.
                conditions: tuple[Any, ...] = (
                    attention_service.resolved_attention_condition(),
                )
                # NEWEST RESOLUTION FIRST. Ordering history by `created_at`
                # ascending (the open view's ordering) means that once more
                # resolutions exist than `--limit`, an operator only ever sees
                # the oldest payments by CREATION date and can never reach the
                # decisions just made, with no pagination to get there. The
                # question this view answers is "what did we recently close",
                # so it sorts the way `queries.resolved_review_payments`
                # already does. Ties break on descending id for determinism.
                order: tuple[Any, ...] = (
                    Payment.attention_resolved_at.desc(),
                    Payment.id.desc(),
                )
            else:
                # OPEN view: compose the CANONICAL current-attention predicate
                # (grace period and unresolved filter included) rather than a
                # locally re-derived one, then narrow it to the statuses this
                # command can actually act on. Without the shared builder's
                # grace period this listing would show every in-flight payment
                # creation as a stale attention item: create_payment commits
                # the `created` row BEFORE attempting getLink, so a plain read
                # sees it immediately, while `centralpay stuck` and the admin
                # bot deliberately exclude it for
                # UNEXPECTED_STATE_GRACE_SECONDS. That is precisely the
                # cross-surface disagreement the canonical predicate exists to
                # prevent.
                conditions = (
                    *unexpected_status_conditions(now=now),
                    Payment.status.in_(sorted(attention_service.RESOLVABLE_STATUSES)),
                )
                # OLDEST FIRST: a worklist is ordered most-urgent-first, and
                # the longest-unattended item is the most urgent — the same
                # ordering `centralpay stuck` uses for its attention rows.
                order = (Payment.created_at.asc(), Payment.id.asc())
            payments = list(
                db.execute(
                    select(Payment)
                    .where(*conditions)
                    .order_by(*order)
                    .limit(args.limit)
                ).scalars()
            )
            shown = 0
            for payment in payments:
                print(
                    json.dumps(
                        _attention_summary(
                            attention_service.snapshot(
                                payment,
                                now=now,
                                # Same supersession rule the worklist predicate
                                # applies, so this listing and `centralpay
                                # stuck` can never disagree about a row.
                                superseded=attention_service.
                                resolution_superseded_in_db(db, payment),
                            )
                        ),
                        ensure_ascii=False,
                    )
                )
                shown += 1
            if shown == 0:
                print(
                    "no resolved attention items"
                    if args.resolved
                    else "no open attention items in an attention-resolvable state"
                )
            return 0

        try:
            found = _find_payment(db, args.order_id)
        except AmbiguousOrderIdError:
            print(f"ambiguous order id: {args.order_id}", file=sys.stderr)
            db.rollback()
            return 1
        if found is None:
            print(f"payment not found: {args.order_id}", file=sys.stderr)
            db.rollback()
            return 1

        if args.attention_command == "show":
            summary = _attention_summary(
                attention_service.snapshot(
                    found,
                    now=datetime.now(UTC),
                    superseded=attention_service.resolution_superseded_in_db(db, found),
                )
            )
            db.rollback()
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        # resolve: every eligibility decision is re-made under the row lock
        # inside the service, against a freshly reloaded row.
        outcome = attention_service.resolve_attention(
            db,
            payment_id=found.id,
            resolution=args.resolution,
            note=args.note,
            actor=attention_service.ACTOR_HOST_CLI,
            now=datetime.now(UTC),
        )
        if not outcome.resolved:
            print(attention_service.outcome_refusal_message(outcome), file=sys.stderr)
            return 1
        print(
            f"resolved attention for {outcome.bot_order_id}: {outcome.resolution} "
            f"(status unchanged: {outcome.status})"
        )
        return 0


# --- notification accept: manual acceptance of a stuck bot_notify_pending --
#
# See app.services.notification.execute_manual_accept for the full safety
# contract. This section only renders what that function decides and
# reuses app.cli's `_find_payment`/`AmbiguousOrderIdError` for ORDER_ID
# resolution -- it never re-implements order-id lookup and never assigns a
# Payment attribute itself.

_NOTIFICATION_ACCEPT_REFUSAL_MESSAGE = {
    ManualAcceptRefusal.NOT_BOT_NOTIFY_PENDING: (
        "refused: payment is not in bot_notify_pending (status={status})"
    ),
    ManualAcceptRefusal.NOT_GATEWAY_VERIFIED: (
        "refused: payment has no recorded gateway verification "
        "(gateway_verified_at is NULL) even though status=bot_notify_pending"
    ),
}


def _notification_accept_warning(order_id: str) -> str:
    return (
        f"About to mark {order_id}'s bot notification as operator-confirmed "
        "accepted.\n"
        "  - Does NOT contact the bot.\n"
        "  - Does NOT credit the customer.\n"
        "  - Does NOT change gateway verification.\n"
        "  - Does NOT change payment amounts.\n"
        "  - Records an operator-confirmed notification outcome.\n"
        "  - Permanently stops automatic notification retries for this "
        "payment.\n"
        "Re-run with --yes to confirm."
    )


def _cmd_notification(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    configure_alert_creation(settings)
    session_factory = create_session_factory(settings.database_url)

    with session_factory() as db:
        if args.notification_command == "accept":
            try:
                found = _find_payment(db, args.order_id)
            except AmbiguousOrderIdError:
                print(f"ambiguous order id: {args.order_id}", file=sys.stderr)
                db.rollback()
                return 1
            if found is None:
                print(f"payment not found: {args.order_id}", file=sys.stderr)
                db.rollback()
                return 1

            outcome = execute_manual_accept(
                db, payment_id=found.id, note=args.note, now=datetime.now(UTC)
            )
            if outcome is None:
                print(f"payment not found: {args.order_id}", file=sys.stderr)
                return 1
            if not outcome.accepted:
                assert outcome.refusal is not None
                print(
                    _NOTIFICATION_ACCEPT_REFUSAL_MESSAGE[outcome.refusal].format(
                        status=outcome.status
                    ),
                    file=sys.stderr,
                )
                return 1
            print(
                f"accepted {found.bot_order_id}: "
                "bot_notify_pending -> bot_notify_accepted (manual)"
            )
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
    sub.add_parser(
        "privacy-audit",
        help="payer-identity isolation report (counts only; no customer/card data)",
    )

    db_check = sub.add_parser("db-check", help="database integrity checks (restore verification)")
    db_check.add_argument(
        "--repair-sequences",
        action="store_true",
        help="advance sequences that fell behind their table maxima",
    )
    db_check.add_argument(
        "--details",
        action="store_true",
        help="bounded, read-only drill-down into the rows behind any failed check "
        "(no writes, no network calls; mutually exclusive with --repair-sequences)",
    )
    db_check.add_argument(
        "--json",
        action="store_true",
        dest="details_json",
        help="with --details, emit a single compact-JSON line instead of the "
        "indented report (requires --details)",
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
    # Dedicated installer operation: create the initial policy only when the
    # table is empty, requiring an explicit validated rate (never defaults to
    # 0). No-ops when any history exists. Serialized by advisory lock.
    fee_ensure = fee_sub.add_parser("ensure-initial")
    fee_ensure.add_argument("--percent", default=None)
    fee_ensure.add_argument("--note", default="Initial installation fee")
    fee_ensure.add_argument("--actor", default="installer")
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
    review_resolve_many = review_sub.add_parser(
        "resolve-many",
        help="all-or-nothing bulk resolution of an EXPLICIT list of open "
        "DELIVERY-failure manual reviews (preview-only without --yes); "
        "financial/verification reviews are refused and must be resolved "
        "individually with `review resolve`",
    )
    review_resolve_many.add_argument(
        "order_ids",
        nargs="+",
        metavar="ORDER_ID",
        help="every order id to resolve, listed explicitly. There is no "
        "'resolve all' and no filter-based selection.",
    )
    review_resolve_many.add_argument(
        "--resolution", required=True, choices=ALLOWED_RESOLUTIONS
    )
    review_resolve_many.add_argument("--note", required=True)
    review_resolve_many.add_argument("--yes", action="store_true")
    review_resend = review_sub.add_parser("resend")
    review_resend.add_argument("order_id")
    review_resend.add_argument("--confirm-idempotent-bot", action="store_true")
    review_resend.add_argument("--yes", action="store_true")

    attention = sub.add_parser(
        "attention",
        help="operational resolution of stale NON-FINANCIAL failures "
        "(host only; never contacts CentralPay or the selling bot)",
    )
    attention_sub = attention.add_subparsers(dest="attention_command", required=True)
    attention_list = attention_sub.add_parser("list")
    attention_list.add_argument(
        "--resolved",
        action="store_true",
        help="historical view: items an operator has already resolved",
    )
    attention_list.add_argument("--limit", type=int, default=50)
    attention_show = attention_sub.add_parser("show")
    attention_show.add_argument("order_id")
    attention_resolve = attention_sub.add_parser("resolve")
    attention_resolve.add_argument("order_id")
    attention_resolve.add_argument(
        "--resolution",
        required=True,
        choices=sorted(attention_service.ATTENTION_RESOLUTIONS),
    )
    attention_resolve.add_argument("--note", required=True)
    attention_resolve.add_argument("--yes", action="store_true")

    notification = sub.add_parser(
        "notification",
        help="bot notification operator overrides (host only; never contacts the bot)",
    )
    notification_sub = notification.add_subparsers(
        dest="notification_command", required=True
    )
    notification_accept = notification_sub.add_parser("accept")
    notification_accept.add_argument("order_id")
    notification_accept.add_argument("--note", required=True)
    notification_accept.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "backup-event":
        return _cmd_backup_event(args)
    if args.command == "db-check":
        if args.details and args.repair_sequences:
            print(
                "error: --details cannot be combined with --repair-sequences. "
                "--details is a strictly read-only inspection; --repair-sequences "
                "performs a write. Run them separately.",
                file=sys.stderr,
            )
            return 1
        if args.details_json and not args.details:
            print("error: --json requires --details", file=sys.stderr)
            return 1
        return _cmd_db_check(args)
    if args.command == "privacy-audit":
        return _cmd_privacy_audit(args)
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
        if args.review_command in (
            "acknowledge",
            "resolve",
            "resolve-many",
        ) and not args.note.strip():
            print("a non-empty --note is required", file=sys.stderr)
            return 1
        return _cmd_review(args)
    if args.command == "attention":
        if args.attention_command == "resolve":
            if not args.note.strip():
                print("a non-empty --note is required", file=sys.stderr)
                return 1
            if not args.yes:
                print(
                    _attention_resolve_warning(args.order_id, args.resolution),
                    file=sys.stderr,
                )
                return 1
        if args.attention_command == "list" and args.limit <= 0:
            print("--limit must be positive", file=sys.stderr)
            return 1
        return _cmd_attention(args)
    if args.command == "notification":
        if args.notification_command == "accept" and not args.note.strip():
            print("a non-empty --note is required", file=sys.stderr)
            return 1
        if args.notification_command == "accept" and not args.yes:
            print(_notification_accept_warning(args.order_id), file=sys.stderr)
            return 1
        return _cmd_notification(args)
    return _cmd_test_alert(args)


if __name__ == "__main__":
    sys.exit(main())
