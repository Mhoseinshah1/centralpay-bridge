"""Read-only inspection commands.

Usage:

    python -m app.cli recent [--limit N]
    python -m app.cli payment ORDER_ID
    python -m app.cli retry-queue
    python -m app.cli manual-review
    python -m app.cli stuck [--limit N] [--json]
    python -m app.cli reconciliation status [--json]
    python -m app.cli reconcile ORDER_ID [--verify [--confirm-aged-out]] [--json]

ORDER_ID may be the original bot order id or the numeric gateway order id.
Output is one JSON object per line, EXCEPT `stuck`, `reconciliation status`,
and `reconcile`, which print a human-readable report by default (pass --json
for a single machine-readable JSON object instead). These commands never
modify data and never print secrets, redirect URLs, or full card numbers.

`reconcile` NEVER writes to the database in either mode. By default it is
LOCAL-ONLY (no network call, no lock, one consistent read). ORDER_ID
resolution refuses rather than guesses: if the numeric string given also
happens to be a DIFFERENT payment's bot_order_id and gateway_order_id, the
command reports `ambiguous_order_id` instead of silently picking one.

`--verify` performs exactly ONE fresh CentralPayClient.verify() call and
reports what settlement WOULD conclude -- it never settles, never invokes
any mutating settlement or callback-processing path (app.services.
verification / app.services.reconciliation), and never claims a
reconciliation slot. This is diagnostic gateway verification with no LOCAL
database mutation; it is NOT known to be read-only on the gateway side --
real CentralPay verify.php verify-after-verify/idempotency behavior has
never been confirmed (release blocker B2, see STAGING_VALIDATION.md) --
so `--verify` is gated behind `settings.centralpay_diagnostic_verify_
enabled` (env `CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED`, default false).
When disabled, `--verify` refuses with `diagnostic_verify_not_enabled`
BEFORE any row lock and BEFORE any HTTP request; `--confirm-aged-out`
cannot bypass this gate. Only enable after the STAGING_VALIDATION.md
procedure confirms real verify-after-verify behavior is safe.

Once enabled, `--verify` acquires the SAME row-lock discipline the
mutating settlement path (app.services.verification) requires its caller to
hold (`SELECT ... FOR UPDATE`), RELOADS the payment and its eligibility
flags under that lock USING A TIMESTAMP TAKEN AFTER THE LOCK IS ACQUIRED
(not before any wait behind a concurrent transaction), and holds the lock
across its own diagnostic gateway call -- closing both the race where a
concurrent callback or reconciliation attempt could settle the payment
between an earlier, non-locking read and this diagnostic call, and the
narrower race where the payment ages out WHILE this command waits for the
lock. `--verify` refuses (without any network call, checked AFTER the row
lock is held and the post-lock timestamp is taken) when the payment is
already locally gateway-verified, is in manual_review, or is aged out
(RECONCILIATION_MAX_AGE_SECONDS or older) -- the last case requires the
explicit `--confirm-aged-out` override, which remains fully read-only. See
app.services.reconcile_inspect.

`reconciliation status` reports the CONFIGURATION OF THE PROCESS IT RUNS IN —
invoke it inside the worker container (the host `centralpay reconciliation
status` command always does this) for the actual effective runtime
configuration; the api container's environment can be stale after a
worker-only redeploy (see scripts/centralpay). The host `centralpay reconcile
...` command routes through the worker container for the same reason: the
reconciliation tier/aged-out/enabled configuration it reports on must never
be read from a possibly-stale api container environment.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.centralpay import CentralPayClient
from app.config import Settings
from app.db import create_session_factory
from app.exceptions import CentralPayConnectionError, CentralPayError
from app.models import Payment, PaymentEvent, PaymentStatus
from app.services.reconcile_inspect import (
    LocalSnapshot,
    VerifyComparison,
    VerifyRefusal,
    build_local_snapshot,
    determine_verify_refusal,
    evaluate_verify_result,
)
from app.services.reconciliation_status import (
    CONFIG_SOURCE_WORKER_CONTAINER,
    ReconciliationStatusSnapshot,
    build_reconciliation_status_snapshot,
)
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


class AmbiguousOrderIdError(Exception):
    """Raised by `_find_payment` when ORDER_ID is a numeric string that
    names TWO DIFFERENT payments at once -- one by bot_order_id, another by
    gateway_order_id. Silently picking either risks inspecting or verifying
    the wrong payment, so callers must refuse instead of guessing."""


def _find_payment(db: Session, order_id: str) -> Payment | None:
    """Look up a payment by bot_order_id, falling back to the numeric
    gateway_order_id -- shared by every command that takes ORDER_ID.

    Raises AmbiguousOrderIdError if ORDER_ID is numeric and matches one
    payment's bot_order_id and a DIFFERENT payment's gateway_order_id."""
    payment = db.execute(
        select(Payment).where(Payment.bot_order_id == order_id)
    ).scalar_one_or_none()
    if order_id.isdigit():
        gateway_payment = db.execute(
            select(Payment).where(Payment.gateway_order_id == int(order_id))
        ).scalar_one_or_none()
        if gateway_payment is not None:
            if payment is None:
                payment = gateway_payment
            elif payment.id != gateway_payment.id:
                raise AmbiguousOrderIdError(order_id)
    return payment


def _cmd_payment(db: Session, order_id: str) -> int:
    try:
        payment = _find_payment(db, order_id)
    except AmbiguousOrderIdError:
        _print({"error": "ambiguous_order_id", "order_id": order_id})
        return 1
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


# --- reconciliation status: shared snapshot, human + json renderers --------


def _fmt_seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}s"


def _fmt_bool_na(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def _reconciliation_status_dict(snapshot: ReconciliationStatusSnapshot) -> dict[str, Any]:
    runtime, config = snapshot.runtime, snapshot.config
    buckets, queue, recent = snapshot.buckets, snapshot.queue, snapshot.recent
    return {
        "generated_at": _iso(snapshot.generated_at),
        "runtime": {
            "enabled": runtime.enabled,
            "config_source": runtime.config_source,
            "heartbeat_present": runtime.heartbeat_present,
            "heartbeat_age_seconds": runtime.heartbeat_age_seconds,
            "heartbeat_fresh": runtime.heartbeat_fresh,
            "last_successful_cycle_at": _iso(runtime.last_successful_cycle_at),
            "last_successful_cycle_age_seconds": runtime.last_successful_cycle_age_seconds,
            "last_error_code": runtime.last_error_code,
        },
        "config": {
            "min_age_seconds": config.min_age_seconds,
            "fast_window_seconds": config.fast_window_seconds,
            "max_age_seconds": config.max_age_seconds,
            "fast_interval_seconds": config.fast_interval_seconds,
            "slow_interval_seconds": config.slow_interval_seconds,
            "scan_interval_seconds": config.scan_interval_seconds,
            "batch_size": config.batch_size,
            "max_attempts": config.max_attempts,
            "slow_tier_reserved_slots": config.slow_tier_reserved_slots,
        },
        "buckets": {
            "total_unverified": buckets.total_unverified,
            "active": buckets.active,
            "expiring": buckets.expiring,
            "aged_out": buckets.aged_out,
        },
        "queue": {
            "active_due": queue.active_due,
            "expiring_due": queue.expiring_due,
            "exhausted_not_aged_out": queue.exhausted_not_aged_out,
            "oldest_active_due_age_seconds": queue.oldest_active_due_age_seconds,
            "oldest_expiring_due_age_seconds": queue.oldest_expiring_due_age_seconds,
            "oldest_due_age_seconds": queue.oldest_due_age_seconds,
        },
        "recent": {
            "window_hours": recent.window_hours,
            "verified": recent.verified,
            "retry_scheduled": recent.retry_scheduled,
            "gateway_not_paid": recent.gateway_not_paid,
            "transport_failed": recent.transport_failed,
            "exhausted": recent.exhausted,
        },
    }


def _print_reconciliation_status_human(snapshot: ReconciliationStatusSnapshot) -> None:
    runtime, config = snapshot.runtime, snapshot.config
    buckets, queue, recent = snapshot.buckets, snapshot.queue, snapshot.recent

    source_label = (
        "running worker container"
        if runtime.config_source == CONFIG_SOURCE_WORKER_CONTAINER
        else "this process's environment (NOT confirmed to be the worker container)"
    )
    if runtime.heartbeat_present:
        heartbeat_line = (
            f"present, age {_fmt_seconds(runtime.heartbeat_age_seconds)}, "
            f"fresh: {_fmt_bool_na(runtime.heartbeat_fresh)}"
        )
    elif not runtime.enabled:
        heartbeat_line = "none recorded (reconciliation disabled)"
    else:
        heartbeat_line = "MISSING — reconciliation is enabled but has never heartbeated"
    last_cycle_line = (
        f"{_iso(runtime.last_successful_cycle_at)} "
        f"({_fmt_seconds(runtime.last_successful_cycle_age_seconds)} ago)"
        if runtime.last_successful_cycle_at is not None
        else "never recorded"
    )

    print("🔄 Reconciliation Status")
    print("========================")
    print(f"(config source: {source_label})")
    print()
    print("Runtime:")
    print(f"  enabled:                  {'yes' if runtime.enabled else 'no'}")
    print(f"  heartbeat:                {heartbeat_line}")
    print(f"  last successful pass:     {last_cycle_line}")
    if runtime.last_error_code:
        print(f"  last pass error:          {runtime.last_error_code}")
    print()
    print("Effective configuration:")
    print(f"  min_age:                  {config.min_age_seconds}s")
    print(f"  active window:            < {config.fast_window_seconds}s")
    print(
        f"  expiring window:          {config.fast_window_seconds}s "
        f"- {config.max_age_seconds}s"
    )
    print(f"  max_age:                  {config.max_age_seconds}s")
    print(f"  fast_interval:            {config.fast_interval_seconds}s")
    print(f"  slow_interval:            {config.slow_interval_seconds}s")
    print(f"  scan_interval:            {config.scan_interval_seconds}s")
    print(f"  batch_size:               {config.batch_size}")
    print(f"  max_attempts:             {config.max_attempts}")
    print(f"  slow_tier_reserved_slots: {config.slow_tier_reserved_slots}")
    print()
    print("Payment buckets (link_created, unverified):")
    print(f"  total:                    {buckets.total_unverified}")
    print(f"  active:                   {buckets.active}")
    print(f"  expiring:                 {buckets.expiring}")
    print(f"  aged_out:                 {buckets.aged_out}")
    print()
    print("Queue health (due now):")
    print(f"  active_due:               {queue.active_due}")
    print(f"  expiring_due:             {queue.expiring_due}")
    print(f"  exhausted (within auto-reconciliation lifetime): {queue.exhausted_not_aged_out}")
    print(f"  oldest active due age:    {_fmt_seconds(queue.oldest_active_due_age_seconds)}")
    print(f"  oldest expiring due age:  {_fmt_seconds(queue.oldest_expiring_due_age_seconds)}")
    print(f"  oldest due age (overall): {_fmt_seconds(queue.oldest_due_age_seconds)}")
    print()
    print(f"Recent activity (last {recent.window_hours}h):")
    print(f"  verified:                 {recent.verified}")
    print(f"  retry_scheduled:          {recent.retry_scheduled}  (normal polling)")
    print(
        f"  gateway_not_paid:         {recent.gateway_not_paid}  "
        "(informational — gateway not yet confirming payment)"
    )
    print(f"  transport_failed:         {recent.transport_failed}  (attention)")
    # NOT the same guarantee as queue.exhausted_not_aged_out: this counts
    # `reconciliation_exhausted` events raised in the window, which does not
    # prove those payments are still not aged-out by now (a payment can be
    # marked exhausted and later age out before this command runs). Render
    # plainly as "exhausted" — never "exhausted_not_aged_out".
    print(f"  exhausted:                {recent.exhausted}  (attention)")


def _cmd_reconciliation_status(db: Session, settings: Settings, *, as_json: bool) -> int:
    snapshot = build_reconciliation_status_snapshot(db, settings)
    if as_json:
        _print(_reconciliation_status_dict(snapshot))
    else:
        _print_reconciliation_status_human(snapshot)
    return 0


# --- reconcile: single-payment inspection, LOCAL-ONLY unless --verify ------
#
# See app.services.reconcile_inspect for the full safety contract. This
# section only renders what that module computes; it never assigns a
# Payment attribute, never creates a PaymentEvent, and never commits.

_NO_LOCAL_CHANGES_LINE = "NO LOCAL CHANGES WERE MADE."

_VERIFY_REFUSAL_MESSAGE = {
    VerifyRefusal.DIAGNOSTIC_VERIFY_DISABLED: (
        "Refusing to verify: diagnostic gateway verification is disabled "
        "(CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED=false). Real CentralPay verify.php "
        "verify-after-verify/idempotency behavior has not been confirmed against "
        "the production gateway (see STAGING_VALIDATION.md); enable only after "
        "that staging validation closes. No gateway call was made."
    ),
    VerifyRefusal.ALREADY_VERIFIED: (
        "Refusing to re-verify: this payment is already locally gateway-verified "
        "(gateway_verified_at is set). No gateway call was made."
    ),
    VerifyRefusal.MANUAL_REVIEW_OWNED: (
        "Refusing to verify: this payment is in manual_review -- an administrator "
        "already owns it. No gateway call was made."
    ),
    VerifyRefusal.AGED_OUT: (
        "Refusing to verify: this payment's link is aged out "
        "(>= RECONCILIATION_MAX_AGE_SECONDS old) and unverified. "
        "Pass --verify --confirm-aged-out to force one diagnostic gateway query anyway. "
        "No gateway call was made."
    ),
}


def _reconcile_local_dict(payment: Payment, local: LocalSnapshot) -> dict[str, Any]:
    return {
        "bot_order_id": payment.bot_order_id,
        "gateway_order_id": payment.gateway_order_id,
        "status": payment.status,
        "gateway_verified": payment.gateway_verified_at is not None,
        "gateway_verified_at": _iso(payment.gateway_verified_at),
        "original_amount": payment.amount,
        "fee_rate_bps": payment.fee_rate_bps,
        "fee_amount": payment.fee_amount,
        "payable_amount": payment.payable_amount,
        "link_age_seconds": local.link_age_seconds,
        "reconciliation": {
            "age_bucket": local.age_bucket,
            "attempts": payment.reconciliation_attempts,
            "last_at": _iso(payment.reconciliation_last_at),
            "next_at": _iso(payment.reconciliation_next_at),
            "last_error_code": payment.reconciliation_last_error_code,
            "enabled": local.reconciliation_enabled,
            "schedule_due": local.schedule_due,
            "auto_reconciliation_due": local.auto_reconciliation_due,
            "aged_out": local.verify_aged_out,
            "attempts_exhausted": local.attempts_exhausted,
        },
    }


def _print_reconcile_local_human(payment: Payment, local: LocalSnapshot) -> None:
    print(f"🔍 Reconcile: {payment.bot_order_id}")
    print("=" * (14 + len(payment.bot_order_id)))
    print(f"  gateway order id:        {payment.gateway_order_id}")
    print(f"  local status:            {payment.status}")
    verified_at = payment.gateway_verified_at
    verified_flag = "yes" if verified_at else "no"
    verified_suffix = f" ({_iso(verified_at)})" if verified_at else ""
    print(f"  gateway_verified:        {verified_flag}{verified_suffix}")
    print(f"  original amount:         {payment.amount:,} تومان")
    print(f"  fee:                     {payment.fee_amount:,} تومان ({payment.fee_rate_bps} bps)")
    print(f"  payable amount:          {payment.payable_amount:,} تومان")
    print(f"  link age:                {_humanize_duration(local.link_age_seconds)}")
    print(f"  reconciliation tier:     {local.age_bucket or 'n/a'}")
    print(f"  reconciliation attempts: {payment.reconciliation_attempts}")
    print(f"  last reconciliation:     {_iso(payment.reconciliation_last_at) or 'never'}")
    print(f"  next reconciliation:     {_iso(payment.reconciliation_next_at) or 'none scheduled'}")
    print(f"  last error code:         {payment.reconciliation_last_error_code or 'none'}")
    print(f"  reconciliation enabled:  {'yes' if local.reconciliation_enabled else 'no'}")
    print(f"  auto-reconciliation due: {'yes' if local.auto_reconciliation_due else 'no'}")
    print(f"  aged out:                {'yes' if local.verify_aged_out else 'no'}")
    print(f"  attempts exhausted:      {'yes' if local.attempts_exhausted else 'no'}")


def _verify_comparison_dict(comparison: VerifyComparison) -> dict[str, Any]:
    return {
        "gateway_success": comparison.gateway_success,
        "assessment": comparison.assessment.value,
        "reason_code": comparison.reason_code,
        "gateway_failure_reason": comparison.gateway_failure_reason,
        "reference_id_present": comparison.reference_id_present,
        "reference_id_valid": comparison.reference_id_valid,
        "reported_reference_id": comparison.reported_reference_id,
        "amount_matches": comparison.amount_matches,
        "expected_payable_amount": comparison.expected_payable_amount,
        "reported_amount": comparison.reported_amount,
        "user_id_matches": comparison.user_id_matches,
        "reference_id_collision": comparison.reference_id_collision,
        "field_errors": list(comparison.field_errors),
    }


def _print_verify_comparison_human(comparison: VerifyComparison) -> None:
    print()
    print("--- --verify: fresh diagnostic gateway check (no LOCAL database mutation) ---")
    print(f"  gateway response:        {comparison.assessment.value}")
    if not comparison.gateway_success:
        print(f"  internal failure reason: {comparison.gateway_failure_reason or 'unknown'}")
    else:
        print(f"  reference_id present:    {'yes' if comparison.reference_id_present else 'no'}")
        print(f"  reference_id valid:      {'yes' if comparison.reference_id_valid else 'no'}")
        if comparison.reported_reference_id is not None:
            print(f"  reported reference_id:   {comparison.reported_reference_id}")
        if comparison.amount_matches is not None:
            print(
                f"  amount matches:          {'yes' if comparison.amount_matches else 'no'} "
                f"(expected payable {comparison.expected_payable_amount:,}, "
                f"gateway reported {comparison.reported_amount:,})"
                if comparison.reported_amount is not None
                else f"  amount matches:          {'yes' if comparison.amount_matches else 'no'}"
            )
        if comparison.user_id_matches is not None:
            # Never the raw ids -- match/mismatch fact only.
            print(f"  user_id matches:         {'yes' if comparison.user_id_matches else 'no'}")
        if comparison.reference_id_collision:
            print("  reference_id collision:  yes (already used by another payment)")
        if comparison.field_errors:
            print(f"  field errors:            {', '.join(comparison.field_errors)}")
    if comparison.reason_code is not None:
        print(f"  reason code:             {comparison.reason_code}")
    print(f"  assessment:              {comparison.assessment.value}")
    print(
        "  NOTE: this is a diagnostic prediction only -- the payment was NOT settled."
    )
    print(f"  {_NO_LOCAL_CHANGES_LINE}")


def _cmd_reconcile(
    db: Session,
    settings: Settings,
    order_id: str,
    *,
    verify: bool,
    confirm_aged_out: bool,
    as_json: bool,
) -> int:
    if confirm_aged_out and not verify:
        print("--confirm-aged-out requires --verify", file=sys.stderr)
        return 1

    # Non-locking lookup, used only to resolve WHICH payment id this order
    # id names -- never as the source of the displayed fields or the
    # eligibility check (see build_local_snapshot calls below, which are
    # the sole source of both).
    try:
        found = _find_payment(db, order_id)
    except AmbiguousOrderIdError:
        _print({"error": "ambiguous_order_id", "order_id": order_id})
        return 1
    if found is None:
        _print({"error": "payment_not_found", "order_id": order_id})
        return 1
    payment_id = found.id

    if not verify:
        # Default inspection: no lock, one consistent read.
        now = datetime.now(UTC)
        snapshot = build_local_snapshot(db, settings, payment_id, now=now)
        if snapshot is None:
            _print({"error": "payment_not_found", "order_id": order_id})
            return 1
        payment, local = snapshot
        if as_json:
            _print({"local": _reconcile_local_dict(payment, local), "verify": None})
        else:
            _print_reconcile_local_human(payment, local)
        return 0

    # --verify is gated behind an explicit, off-by-default configuration
    # flag -- see the module docstring and STAGING_VALIDATION.md. Checked
    # BEFORE any row lock and BEFORE any HTTP request; --confirm-aged-out
    # cannot bypass it.
    if not settings.centralpay_diagnostic_verify_enabled:
        now = datetime.now(UTC)
        snapshot = build_local_snapshot(db, settings, payment_id, now=now)
        if snapshot is None:
            _print({"error": "payment_not_found", "order_id": order_id})
            return 1
        payment, local = snapshot
        if as_json:
            _print(
                {
                    "local": _reconcile_local_dict(payment, local),
                    "verify": {
                        "requested": True,
                        "performed": False,
                        "refused": VerifyRefusal.DIAGNOSTIC_VERIFY_DISABLED.value,
                        "note": _NO_LOCAL_CHANGES_LINE,
                    },
                }
            )
        else:
            _print_reconcile_local_human(payment, local)
            print()
            print("--- --verify: refused ---")
            print(f"  {_VERIFY_REFUSAL_MESSAGE[VerifyRefusal.DIAGNOSTIC_VERIFY_DISABLED]}")
            print(f"  {_NO_LOCAL_CHANGES_LINE}")
        return 0

    # Acquire the SAME row-lock discipline the mutating settlement path
    # (app.services.verification) requires its caller to hold. This minimal
    # lock probe may BLOCK behind a concurrent callback or reconciliation
    # transaction -- only once it returns is the lock actually held.
    locked = db.execute(
        select(Payment.id).where(Payment.id == payment_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        _print({"error": "payment_not_found", "order_id": order_id})
        return 1

    # `now` is captured AFTER the lock above is actually acquired -- never
    # before any wait behind a concurrent transaction -- so the aged-out
    # gate below cannot be evaluated against a stale pre-wait timestamp.
    now = datetime.now(UTC)

    # RELOAD the payment and its eligibility flags under the lock just
    # acquired (a second `FOR UPDATE` on a row this transaction already
    # locks is instant, never a second wait) -- closing the window where a
    # concurrent callback or reconciliation attempt could settle this
    # payment between an earlier, non-locking read and the diagnostic
    # gateway call below. The lock is held across that gateway call and
    # released normally when this command's database transaction ends (no
    # commit is ever made). Zero persistent DB mutations either way.
    locked_snapshot = build_local_snapshot(db, settings, payment_id, now=now, for_update=True)
    if locked_snapshot is None:
        _print({"error": "payment_not_found", "order_id": order_id})
        return 1
    payment, local = locked_snapshot

    refusal = determine_verify_refusal(payment, local, confirm_aged_out=confirm_aged_out)
    if refusal is not None:
        if as_json:
            _print(
                {
                    "local": _reconcile_local_dict(payment, local),
                    "verify": {
                        "requested": True,
                        "performed": False,
                        "refused": refusal.value,
                        "note": _NO_LOCAL_CHANGES_LINE,
                    },
                }
            )
        else:
            _print_reconcile_local_human(payment, local)
            print()
            print("--- --verify: refused ---")
            print(f"  {_VERIFY_REFUSAL_MESSAGE[refusal]}")
            print(f"  {_NO_LOCAL_CHANGES_LINE}")
        return 0

    client = CentralPayClient(
        base_url=settings.centralpay_base_url,
        getlink_api_key=settings.centralpay_getlink_api_key,
        verify_api_key=settings.centralpay_verify_api_key,
        timeout_seconds=settings.centralpay_timeout_seconds,
    )
    try:
        try:
            result = client.verify(order_id=payment.gateway_order_id)
        except CentralPayError as exc:
            # The POST may have already reached the gateway even though no
            # usable result came back -- httpx cannot distinguish "never
            # left this process" from "sent, but the response was lost" for
            # a connection-level failure (a timeout can occur after the
            # request bytes were already written), so that case is reported
            # as genuinely uncertain rather than guessed as not-performed.
            # A non-200 status or an unparseable body both PROVE the
            # request was transmitted and answered -- never "not performed".
            performed: bool | None
            delivery_uncertain: bool
            if isinstance(exc, CentralPayConnectionError):
                performed, delivery_uncertain = None, True
            else:
                performed, delivery_uncertain = True, False
            if as_json:
                _print(
                    {
                        "local": _reconcile_local_dict(payment, local),
                        "verify": {
                            "requested": True,
                            "performed": performed,
                            "delivery_uncertain": delivery_uncertain,
                            "refused": None,
                            "transport_error_code": exc.code,
                            "note": _NO_LOCAL_CHANGES_LINE,
                        },
                    }
                )
            else:
                _print_reconcile_local_human(payment, local)
                print()
                print("--- --verify: gateway call failed (transport/protocol) ---")
                print(f"  error code:              {exc.code}")
                if delivery_uncertain:
                    print(
                        "  delivery uncertain:      the request may or may not have "
                        "reached the gateway"
                    )
                else:
                    print("  request reached gateway: yes (response could not be used)")
                print(f"  {_NO_LOCAL_CHANGES_LINE}")
            return 1
    finally:
        client.close()

    comparison = evaluate_verify_result(db, payment, result)
    if as_json:
        _print(
            {
                "local": _reconcile_local_dict(payment, local),
                "verify": {
                    "requested": True,
                    "performed": True,
                    "refused": None,
                    **_verify_comparison_dict(comparison),
                    "note": _NO_LOCAL_CHANGES_LINE,
                },
            }
        )
    else:
        _print_reconcile_local_human(payment, local)
        _print_verify_comparison_human(comparison)
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
    reconciliation = subparsers.add_parser(
        "reconciliation", help="reconciliation runtime/config/queue status (read-only)"
    )
    reconciliation_sub = reconciliation.add_subparsers(
        dest="reconciliation_command", required=True
    )
    reconciliation_status = reconciliation_sub.add_parser(
        "status",
        help="reconciliation runtime, effective config, payment buckets, and queue health",
    )
    reconciliation_status.add_argument(
        "--json", action="store_true", dest="as_json", help="one JSON object"
    )
    reconcile = subparsers.add_parser(
        "reconcile",
        help="inspect one payment (local-only); --verify performs one diagnostic "
        "gateway check (off by default), never a settlement",
    )
    reconcile.add_argument("order_id")
    reconcile.add_argument(
        "--verify",
        action="store_true",
        help="perform exactly one fresh, diagnostic CentralPay verify.php call and "
        "report what settlement would conclude; never writes to the database; "
        "requires CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED=true",
    )
    reconcile.add_argument(
        "--confirm-aged-out",
        action="store_true",
        dest="confirm_aged_out",
        help="required in addition to --verify to query the gateway for a payment "
        "whose link has aged past RECONCILIATION_MAX_AGE_SECONDS; still fully read-only",
    )
    reconcile.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="single JSON object instead of the report",
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
        if args.command == "reconciliation":
            return _cmd_reconciliation_status(db, settings, as_json=args.as_json)
        if args.command == "reconcile":
            return _cmd_reconcile(
                db,
                settings,
                args.order_id,
                verify=args.verify,
                confirm_aged_out=args.confirm_aged_out,
                as_json=args.as_json,
            )
        return _cmd_stuck(db, settings, limit=args.limit, as_json=args.as_json)
    except BrokenPipeError:
        # Piping into head/less that exits early is not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
