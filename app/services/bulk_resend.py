"""Administrator bulk requeue of delivery-failed manual-review payments.

Requeues gateway-verified payments whose customer-bot notification ended in
``retry_limit_reached`` or ``bot_timeout_ambiguous`` back into the existing
notification worker's queue. This service:

* NEVER contacts the customer bot — it only changes the notification-delivery
  state so the worker performs the real delivery;
* NEVER fabricates CentralPay verification, an acknowledgement, or a
  resolution;
* NEVER touches financial or gateway facts (amount, fee_*, payable_amount,
  reference_id, gateway_verified_at);
* NEVER resets ``bot_notify_attempts`` — each requeue is one new operational
  delivery opportunity, so the worker's next claim is the next attempt number,
  and a repeated failure returns to manual review under the existing logic (no
  infinite automatic retry);
* requires idempotent retry mode — the caller must enforce it before invoking
  the execute path.

Concurrency: selection and mutation use ``SELECT ... FOR UPDATE SKIP LOCKED``
in bounded batches ordered by ``(manual_review_at, id)``, so two administrators
running the command at the same time never requeue the same row twice and never
record duplicate per-payment events.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.audit import record_event
from app.models import Payment, PaymentStatus
from app.reasons import ReasonCode

logger = logging.getLogger("app.services.bulk_resend")

# The ONLY manual-review reasons eligible for bulk requeue: both are
# customer-bot DELIVERY outcomes on a payment CentralPay already verified.
# Financial/verification manual-review reasons (amount/user/reference
# mismatches, invalid callbacks, configuration failures, explicit bot 4xx)
# are deliberately excluded and must never be broadened here.
ELIGIBLE_RESEND_REASONS: frozenset[str] = frozenset(
    {ReasonCode.RETRY_LIMIT_REACHED.value, ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value}
)

PREVIEW_ORDER_LIMIT = 20
DEFAULT_BATCH_SIZE = 100


@dataclass(frozen=True)
class BulkResendPreview:
    count: int
    total_amount: int
    order_ids: tuple[str, ...]  # at most PREVIEW_ORDER_LIMIT


@dataclass(frozen=True)
class BulkResendResult:
    selected_count: int
    requeued_count: int
    skipped_count: int
    total_amount: int
    order_ids: tuple[str, ...]  # requeued ids, at most PREVIEW_ORDER_LIMIT


def _eligible_predicate() -> tuple[ColumnElement[bool], ...]:
    """Exact eligibility (must all hold):

    * status == manual_review
    * gateway_verified_at IS NOT NULL
    * review_resolved_at IS NULL
    * notification_claimed_at IS NULL
    * bot_notify_reason IN {retry_limit_reached, bot_timeout_ambiguous}
    """
    return (
        Payment.status == PaymentStatus.MANUAL_REVIEW.value,
        Payment.gateway_verified_at.is_not(None),
        Payment.review_resolved_at.is_(None),
        Payment.notification_claimed_at.is_(None),
        Payment.bot_notify_reason.in_(tuple(ELIGIBLE_RESEND_REASONS)),
    )


def preview_bulk_resend(db: Session) -> BulkResendPreview:
    """Read-only preview: eligible count, total ORIGINAL invoice amount, and up
    to the first PREVIEW_ORDER_LIMIT order ids. Performs no mutation and no
    network delivery."""
    rows = db.execute(
        select(Payment.bot_order_id, Payment.amount)
        .where(*_eligible_predicate())
        .order_by(Payment.manual_review_at.asc().nulls_first(), Payment.id.asc())
    ).all()
    total = sum(row.amount for row in rows)
    order_ids = tuple(row.bot_order_id for row in rows[:PREVIEW_ORDER_LIMIT])
    db.rollback()
    return BulkResendPreview(count=len(rows), total_amount=total, order_ids=order_ids)


def requeue_failed_deliveries(
    db: Session,
    *,
    telegram_user_id: int | None,
    now: datetime,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BulkResendResult:
    """Atomically requeue every currently-eligible payment for the worker.

    Only the notification-delivery state is changed (status ->
    bot_notify_pending, next_retry_at -> now, claim cleared). Records one
    permanent ``admin_bulk_resend_requested`` event per requeued payment and one
    batch-level ``admin_bulk_resend_completed`` event. Never resets the attempt
    counter; never touches financial or gateway facts.
    """
    # Informational snapshot of eligibility at the start (not a lock); used to
    # report how many rows a concurrent execution handled instead of this one.
    selected_count = db.execute(
        select(func.count(Payment.id)).where(*_eligible_predicate())
    ).scalar_one()
    db.rollback()

    requeued_count = 0
    total_amount = 0
    requeued_order_ids: list[str] = []

    while True:
        locked = (
            db.execute(
                select(Payment)
                .where(*_eligible_predicate())
                .order_by(Payment.manual_review_at.asc().nulls_first(), Payment.id.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        if not locked:
            db.rollback()
            break
        for payment in locked:
            previous_reason = payment.bot_notify_reason
            previous_attempts = payment.bot_notify_attempts
            # Notification-delivery state ONLY — the worker does the real send.
            payment.status = PaymentStatus.BOT_NOTIFY_PENDING.value
            payment.next_retry_at = now
            payment.notification_claimed_at = None
            payment.notification_claimed_by = None
            # bot_notify_attempts is intentionally NOT reset; manual_review_at,
            # amount, fee_*, payable_amount, reference_id, and gateway_verified_at
            # are intentionally NOT modified.
            record_event(
                db,
                payment_id=payment.id,
                event_type="admin_bulk_resend_requested",
                level="warning",
                data={
                    "telegram_user_id": telegram_user_id,
                    "previous_reason": previous_reason,
                    "previous_attempts": previous_attempts,
                    "command": "resend_failed",
                },
            )
            requeued_count += 1
            total_amount += payment.amount
            if len(requeued_order_ids) < PREVIEW_ORDER_LIMIT:
                requeued_order_ids.append(payment.bot_order_id)
        db.commit()

    skipped_count = max(0, selected_count - requeued_count)
    record_event(
        db,
        payment_id=None,
        event_type="admin_bulk_resend_completed",
        data={
            "telegram_user_id": telegram_user_id,
            "selected_count": selected_count,
            "requeued_count": requeued_count,
            "skipped_count": skipped_count,
        },
    )
    db.commit()
    logger.warning(
        "admin_bulk_resend_completed",
        extra={
            "telegram_user_id": telegram_user_id,
            "selected_count": selected_count,
            "requeued_count": requeued_count,
            "skipped_count": skipped_count,
        },
    )
    return BulkResendResult(
        selected_count=selected_count,
        requeued_count=requeued_count,
        skipped_count=skipped_count,
        total_amount=total_amount,
        order_ids=tuple(requeued_order_ids),
    )
