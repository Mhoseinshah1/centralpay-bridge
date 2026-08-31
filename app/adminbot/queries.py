"""Read-only database queries for admin bot commands and reports.

Nothing in this module mutates payment data. Values returned here may be
shown to administrators; secrets, redirect URLs, signatures, and untrusted
external text are never selected.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session

from app.models import AdminAlert, Payment, PaymentEvent, WorkerHeartbeat
from app.services.payment_lookup import find_payment_by_order_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


def count_by_status(db: Session, status: str) -> int:
    return db.execute(
        select(func.count(Payment.id)).where(Payment.status == status)
    ).scalar_one()


def open_manual_review_conditions() -> tuple[Any, ...]:
    """THE canonical "this manual review still needs operator attention"
    predicate: the payment is in manual_review AND has not been resolved via
    ``centralpay review resolve`` (which stamps review_resolved_at but keeps
    the status as history).

    Public (no leading underscore), like
    ``non_delivery_manual_review_conditions``: ``app.cli``'s legacy
    ``manual-review`` command and ``app.ops``' ``review list`` both compose it
    directly, so no surface can re-derive a subtly different notion of "open"
    and print resolved history as if it were an active worklist.
    """
    return (
        Payment.status == "manual_review",
        Payment.review_resolved_at.is_(None),
    )


def count_open_manual_reviews(db: Session) -> int:
    """count_by_status("manual_review") counts ALL rows ever left in that
    status; this counts only the unresolved ones operators must act on."""
    return db.execute(
        select(func.count(Payment.id)).where(*open_manual_review_conditions())
    ).scalar_one()


def oldest_open_manual_review_age_seconds(db: Session, *, now: datetime) -> float | None:
    """Age of the longest-open unresolved manual review, or None when there
    is none. Shares open_manual_review_conditions with
    count_open_manual_reviews so the two can never disagree about which
    rows are "open"."""
    oldest: datetime | None = db.execute(
        select(func.min(Payment.manual_review_at)).where(*open_manual_review_conditions())
    ).scalar_one()
    if oldest is None:
        return None
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return (now - oldest).total_seconds()


def open_manual_review_reason_buckets(db: Session) -> dict[str, int]:
    """Open manual-review count grouped by cause, for a monitor/dashboard
    summary. Labels only (bot_notify_reason values, or the fixed label
    "financial_or_verification" for the non-delivery half) — never an order
    id or other customer-identifying data."""
    rows = db.execute(
        select(Payment.bot_notify_reason, func.count(Payment.id))
        .where(*open_manual_review_conditions())
        .group_by(Payment.bot_notify_reason)
    ).tuples().all()
    buckets: dict[str, int] = {}
    for reason, count in rows:
        buckets[reason or "financial_or_verification"] = count
    return buckets


def oldest_pending_notification_age_seconds(db: Session, *, now: datetime) -> float | None:
    """Age of the longest-waiting bot_notify_pending row (by
    _notification_age_anchor), across the ENTIRE pending set -- not just
    rows already past a display staleness cutoff (contrast
    _stale_bot_notify_pending_conditions, which is scoped to /stuck's
    display threshold). None when nothing is pending."""
    oldest_anchor: datetime | None = db.execute(
        select(func.min(_notification_age_anchor())).where(
            Payment.status == "bot_notify_pending"
        )
    ).scalar_one()
    if oldest_anchor is None:
        return None
    if oldest_anchor.tzinfo is None:
        oldest_anchor = oldest_anchor.replace(tzinfo=UTC)
    return (now - oldest_anchor).total_seconds()


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
    """OPEN manual reviews only — resolved rows keep status manual_review as
    historical state but leave the operator's default worklist."""
    return list(
        db.execute(
            select(Payment)
            .where(*open_manual_review_conditions())
            .order_by(Payment.manual_review_at.asc().nulls_first())
            .limit(limit)
        ).scalars()
    )


def resolved_review_payments(db: Session, limit: int = 10) -> list[Payment]:
    """Reviews an operator has resolved (any status), newest resolution first."""
    return list(
        db.execute(
            select(Payment)
            .where(Payment.review_resolved_at.is_not(None))
            .order_by(Payment.review_resolved_at.desc())
            .limit(limit)
        ).scalars()
    )


@dataclass(frozen=True)
class StuckEntry:
    payment: Payment
    category: str  # exact reason category, never a generic "stuck"


# Every code path that sets status=bot_notify_pending records exactly one of
# these PaymentEvent types, atomically, in the SAME transaction as the status
# change (never a separate, later write) — see _notification_age_anchor.
_NOTIFICATION_ENTRY_EVENT_TYPES = (
    "bot_notification_queued",  # app.services.notification.queue_notification
    "manual_review_resend_requested",  # app.ops "review resend"
    "admin_bulk_resend_requested",  # app.services.bulk_resend.requeue_failed_deliveries
)


def _notification_age_anchor() -> Any:
    """The moment THIS payment most recently entered/re-entered the
    notification-pending phase.

    NOT ``created_at`` (order creation can precede payment completion by an
    arbitrary amount). NOT bare ``gateway_verified_at`` either: that's a
    one-time financial fact from the ORIGINAL gateway verification, and a
    resend (``app.ops`` "review resend", ``app.services.bulk_resend``)
    re-enters ``bot_notify_pending`` at a NEW time while ``gateway_verified_at``
    stays exactly what it was — a payment verified 2 days ago, resent just
    now, must look freshly-queued, not 2-days stale. This must never
    overwrite or repurpose ``gateway_verified_at`` itself; it stays the
    durable "gateway verified" fact, independent of delivery status.

    Every entry path (``queue_notification``, ``app.ops`` resend,
    ``app.services.bulk_resend``) records exactly one matching event in the
    SAME transaction as the status change, and ``payment_events`` is a
    permanent, append-only audit trail (never deleted) — so
    ``MAX(created_at)`` among a payment's own matching events is an exact,
    reliable record of when its CURRENT cycle began.

    Deliberately NOT ``next_retry_at``: that field is overwritten to a
    FUTURE value by every backoff reschedule
    (``app.services.notification.record_attempt_result``), so an actively
    retrying-and-failing row would perpetually look "fresh" and never
    surface even after being undelivered for a long time. This anchor is
    unaffected by backoff — it only moves on a genuine (re-)entry — so a row
    correctly keeps accumulating age through retries and becomes visible
    once truly overdue, while a row still within its normal backoff window
    (cycle just started) is correctly never flagged just because the
    original ``gateway_verified_at`` happens to be old.

    ``COALESCE`` falls back to ``gateway_verified_at``, then ``created_at``,
    for a row with no matching event — structurally shouldn't happen, since
    every entry path commits its event atomically with the status change,
    but this keeps such a row visible for review instead of crashing the
    query or silently treating it as fresh."""
    latest_entry_event = (
        select(func.max(PaymentEvent.created_at))
        .where(
            PaymentEvent.payment_id == Payment.id,
            PaymentEvent.event_type.in_(_NOTIFICATION_ENTRY_EVENT_TYPES),
        )
        .correlate(Payment)
        .scalar_subquery()
    )
    return func.coalesce(latest_entry_event, Payment.gateway_verified_at, Payment.created_at)


def _stale_bot_notify_pending_conditions(pending_cutoff: datetime) -> tuple[Any, ...]:
    """A bot_notify_pending row old enough to need operator attention —
    whether or not its notification claim is ADDITIONALLY stale (claim
    staleness only changes the displayed reason label below, never whether
    the row is included)."""
    return (Payment.status == "bot_notify_pending", _notification_age_anchor() <= pending_cutoff)


def _bot_delivery_manual_review_conditions() -> tuple[Any, ...]:
    """Open manual-review rows caused SPECIFICALLY by a customer-bot
    delivery failure. ``app.services.notification._move_to_manual_review``
    is the ONLY code path that sets ``bot_notify_reason`` alongside
    ``manual_review``; financial/verification manual-review rows
    (``app.services.verification`` — amount/user/reference mismatches)
    never touch ``bot_notify_reason``, so it stays whatever it was before
    (typically None, since those payments never reached notification at
    all). Never includes reconciliation-exhausted or unexpected-status
    rows — those never set ``status = manual_review`` in the first place."""
    return (*open_manual_review_conditions(), Payment.bot_notify_reason.is_not(None))


def non_delivery_manual_review_conditions() -> tuple[Any, ...]:
    """The EXACT complement of ``_bot_delivery_manual_review_conditions``
    within open manual review: financial/verification manual-review rows
    (amount, user id, reference id mismatches, callback/config failures —
    ``app.services.verification``) that never reached notification at all,
    so ``bot_notify_reason`` stayed ``None``. Together with
    ``_bot_delivery_manual_review_conditions`` this partitions EVERY open
    manual-review row into exactly one of the two buckets — never both,
    never neither — so a caller summing both counts can never double-count
    or silently drop a row.

    Public (no leading underscore): reused as-is by
    ``app.services.stuck_payments.count_other_attention`` so its
    non-delivery-manual-review predicate can never drift from this one —
    the same reason ``reconciliation_exhausted_conditions`` is public."""
    return (*open_manual_review_conditions(), Payment.bot_notify_reason.is_(None))


def count_non_delivery_manual_reviews(db: Session) -> int:
    """EXACT count of open manual-review rows that are NOT a bot-delivery
    problem (financial/verification mismatches). Shares
    ``open_manual_review_conditions`` with ``count_open_manual_reviews``
    (the /manual_review command's total) and is the exact complement of
    ``bot_delivery_snapshot``'s manual-review half."""
    return db.execute(
        select(func.count(Payment.id)).where(*non_delivery_manual_review_conditions())
    ).scalar_one()


@dataclass(frozen=True)
class BotDeliverySnapshot:
    # EXACT total matching the bot-delivery predicate (unbounded — never
    # reduced by `limit`; see `bot_delivery_snapshot`'s docstring for why).
    total: int
    entries: list[StuckEntry]


def bot_delivery_snapshot(
    db: Session,
    *,
    now: datetime,
    pending_age_minutes: int = 30,
    claim_timeout_seconds: float = 120.0,
    limit: int = 30,
) -> BotDeliverySnapshot:
    """/stuck's total AND its detail rows from ONE SQL statement.

    An earlier version ran the total as a separate ``COUNT(*)`` and the
    detail rows as a separate ``SELECT ... LIMIT``. Those are two distinct
    statements: a payment could be counted by the first and then get
    delivered (leaving `bot_notify_pending`/`manual_review`) by the
    notification worker before the second ran, so the rendered detail list
    could silently disagree with the summary count even though both used
    the same ``now``. Here, ``func.count().over()`` (a window function)
    computes the exact total over every row matching the WHERE clause
    BEFORE ``LIMIT`` is applied — standard SQL window-function semantics —
    in the SAME statement that fetches the (at most ``limit``) detail rows,
    so both numbers are read from one consistent result set.

    Combines both bot-delivery-problem shapes behind one predicate: open
    manual-review rows caused by a customer-bot delivery failure
    (``_bot_delivery_manual_review_conditions``), and stale/old
    ``bot_notify_pending`` rows (``_stale_bot_notify_pending_conditions``).
    NEVER financial/verification manual-review rows, reconciliation-
    exhausted rows, or unexpected-status rows.

    Ordering: manual-review delivery failures first, then stale/old
    pending rows (an explicit SQL ``CASE`` priority column, replacing what
    used to be Python-side list concatenation of two query results), each
    group ordered by its own timestamp (``manual_review_at`` / the
    notification-age anchor) ascending with NULLS FIRST, ties broken by
    ascending ``Payment.id`` for a fully deterministic result — the same
    classification and ordering as before, just from one statement."""
    pending_cutoff = now - timedelta(minutes=pending_age_minutes)
    is_manual_review = and_(*_bot_delivery_manual_review_conditions())
    priority = case((is_manual_review, 0), else_=1)
    sort_ts = case((is_manual_review, Payment.manual_review_at), else_=_notification_age_anchor())
    total_col = func.count().over().label("total")
    stmt = (
        select(Payment, priority.label("priority"), total_col)
        .where(
            or_(
                is_manual_review,
                and_(*_stale_bot_notify_pending_conditions(pending_cutoff)),
            )
        )
        .order_by(priority.asc(), sort_ts.asc().nulls_first(), Payment.id.asc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    total = rows[0].total if rows else 0

    claim_cutoff = now - timedelta(seconds=claim_timeout_seconds)
    entries: list[StuckEntry] = []
    for payment, priority_value, _total in rows:
        if priority_value == 0:
            entries.append(StuckEntry(payment, f"manual_review:{payment.bot_notify_reason}"))
            continue
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
    return BotDeliverySnapshot(total=total, entries=entries)


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
        .where(*_stale_bot_notify_pending_conditions(pending_cutoff))
        .order_by(_notification_age_anchor().asc())
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
                *open_manual_review_conditions(),
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
    """Shares `app.services.payment_lookup.find_payment_by_order_id` (also
    used by `app.cli`/`app.ops`) so a numeric identifier that ambiguously
    names two different payments (one by bot_order_id, another by
    gateway_order_id) raises AmbiguousOrderIdError here exactly as it does
    for the CLI, instead of silently guessing which payment to show an
    operator. Only when that lookup finds nothing at all does this fall
    back to an admin-bot-only convenience: an unambiguous reference_id
    match (the CLI has no equivalent fallback)."""
    payment = find_payment_by_order_id(db, identifier)
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
                    "verify_payable_amount_mismatch",
                    "verify_user_id_mismatch",
                    "verify_missing_reference_id",
                    "verify_invalid_reference_id",
                    "bot_notification_failed",
                    "bot_timeout_ambiguous",
                    "notification_recovered_after_restart",
                    "backup_failed",
                    "reconciliation_exhausted",
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
    # Three unambiguous totals: the bot's original invoices, the service
    # fees, and what payers actually paid through CentralPay.
    verified_amount, verified_fees, verified_payable = db.execute(
        select(
            func.coalesce(func.sum(Payment.amount), 0),
            func.coalesce(func.sum(Payment.fee_amount), 0),
            func.coalesce(func.sum(Payment.payable_amount), 0),
        ).where(
            Payment.gateway_verified_at.is_not(None), Payment.gateway_verified_at >= cutoff
        )
    ).one()
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
        "total_original_invoices_toman": int(verified_amount),
        "total_fees_toman": int(verified_fees),
        "total_collected_via_gateway_toman": int(verified_payable),
        # Open reviews only, consistent with /status: resolved rows keep the
        # manual_review status as history but need no operator attention.
        "manual_review": count_open_manual_reviews(db),
        "pending_retry": count_by_status(db, "bot_notify_pending"),
        "getlink_failures": event_count_since(db, "centralpay_getlink_failed", hours=hours),
        "verify_failures": event_count_since(db, "centralpay_verify_failed", hours=hours),
        "bot_delivery_failures": event_count_since(db, "bot_notification_failed", hours=hours),
    }
