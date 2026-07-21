"""Callback processing and CentralPay verification.

The callback route validates the HMAC signature BEFORE this service runs; no
database or gateway work happens for unsigned requests. This service locks the
payment row for the whole verification so concurrent callbacks serialize and a
payment can never be verified twice.
"""

import enum
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import Settings

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.centralpay import CentralPayClient, VerifyResult
from app.exceptions import (
    CentralPayError,
    InvalidCallbackTokenError,
    PaymentNotFoundError,
    VerificationFailedError,
)
from app.models import Payment, PaymentStatus
from app.security import callback_token_matches
from app.services.notification import queue_notification, utcnow

logger = logging.getLogger("app.services.verification")

_ERROR_MAX_LENGTH = 500

# Statuses that mean CentralPay verification has already succeeded.
VERIFIED_STATUSES = frozenset(
    {
        PaymentStatus.GATEWAY_VERIFIED.value,
        PaymentStatus.BOT_NOTIFY_PENDING.value,
        PaymentStatus.BOT_NOTIFY_ACCEPTED.value,
    }
)


class CallbackStatus(enum.StrEnum):
    """User-facing outcome of a callback for a verified payment."""

    BOT_ACCEPTED = "bot_accepted"  # verified and the bot API accepted it
    BOT_PENDING = "bot_pending"  # verified; final bot processing pending
    UNDER_REVIEW = "under_review"  # administrator review required


@dataclass(frozen=True)
class CallbackResult:
    status: CallbackStatus
    bot_order_id: str


def _card_last4(card_number: str | None) -> str | None:
    """Extract the final four digits. The full card number is never stored."""
    if not card_number:
        return None
    digits = "".join(ch for ch in card_number if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _move_to_manual_review(
    db: Session,
    payment: Payment,
    *,
    mismatch_event: str,
    data: dict[str, Any],
) -> None:
    record_event(db, payment_id=payment.id, event_type=mismatch_event, level="error", data=data)
    payment.status = PaymentStatus.MANUAL_REVIEW.value
    payment.last_error = mismatch_event
    record_event(
        db,
        payment_id=payment.id,
        event_type="manual_review_required",
        level="error",
        data={"reason": mismatch_event},
    )
    db.commit()


def _validate_and_apply_verification(
    db: Session, payment: Payment, result: VerifyResult, settings: "Settings | None" = None
) -> CallbackResult:
    """Apply a gateway-successful verify result after validating its fields.

    Any inconsistency between what the gateway reports and what we recorded is
    a financial anomaly: the payment moves to manual_review and is never
    auto-verified.
    """
    field_errors = list(result.field_errors)
    if result.reference_id is None:
        # A referenceId that was PRESENT but violated the storage contract
        # (over-length, NUL/control characters, unsupported type) is a
        # different financial fact than one the gateway never sent — the
        # audit trail must not call it "missing". In both cases the raw
        # value never left app/centralpay.py, so neither event can leak it.
        _move_to_manual_review(
            db,
            payment,
            mismatch_event=(
                "verify_invalid_reference_id"
                if result.reference_id_invalid
                else "verify_missing_reference_id"
            ),
            data={"gateway_order_id": payment.gateway_order_id, "field_errors": field_errors},
        )
        return CallbackResult(CallbackStatus.UNDER_REVIEW, payment.bot_order_id)
    # CentralPay charged the PAYABLE amount (original + fee snapshot), so
    # verification compares against payable_amount — a gateway reporting
    # the original amount instead is a financial anomaly.
    if result.amount != payment.payable_amount:
        _move_to_manual_review(
            db,
            payment,
            mismatch_event="verify_payable_amount_mismatch",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "original_amount": payment.amount,
                "fee_rate_bps": payment.fee_rate_bps,
                "fee_amount": payment.fee_amount,
                "expected_payable_amount": payment.payable_amount,
                "reported_amount": result.amount,
                "field_errors": field_errors,
            },
        )
        return CallbackResult(CallbackStatus.UNDER_REVIEW, payment.bot_order_id)
    if result.user_id != payment.gateway_user_id:
        _move_to_manual_review(
            db,
            payment,
            mismatch_event="verify_user_id_mismatch",
            data={
                # The mismatch FACT only — under telegram_raw_v1 the expected
                # value IS the raw Telegram id, which never enters audit
                # events. Operators compare the payment row against the
                # gateway's report directly during manual review.
                "gateway_order_id": payment.gateway_order_id,
                "payer_identity_type": payment.payer_identity_type,
                "field_errors": field_errors,
            },
        )
        return CallbackResult(CallbackStatus.UNDER_REVIEW, payment.bot_order_id)

    # CentralPay must never report one referenceId for two different
    # payments. On collision: manual review, never overwrite either payment.
    collision = db.execute(
        select(Payment.id)
        .where(Payment.reference_id == result.reference_id, Payment.id != payment.id)
        .limit(1)
    ).first()
    if collision is not None:
        _move_to_manual_review(
            db,
            payment,
            mismatch_event="reference_id_collision",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "colliding_payment_id": collision[0],
            },
        )
        return CallbackResult(CallbackStatus.UNDER_REVIEW, payment.bot_order_id)

    # Verified state and pending notification state commit atomically; the
    # bot notification itself is sent later, by the worker, outside any
    # database transaction. The timestamp comes from the notification
    # pipeline's single utcnow() seam so the verified-at and next-retry
    # clocks can never diverge (and tests can pin them deterministically).
    now = utcnow()
    payment.gateway_verified_at = now
    payment.reference_id = result.reference_id
    payment.card_last4 = _card_last4(result.card_number)
    payment.last_error = None
    record_event(
        db,
        payment_id=payment.id,
        event_type="gateway_payment_verified",
        data={
            "gateway_order_id": payment.gateway_order_id,
            "reference_id": result.reference_id,
            "original_amount": payment.amount,
            "fee_rate_bps": payment.fee_rate_bps,
            "fee_amount": payment.fee_amount,
            "payable_amount": payment.payable_amount,
        },
    )
    queue_notification(db, payment, now=now)
    if settings is not None and settings.first_payment_guard_enabled:
        _maybe_record_first_payment(db, payment)
    db.commit()
    logger.info(
        "bot_notification_queued",
        extra={
            "payment_id": payment.id,
            "bot_order_id": payment.bot_order_id,
            "gateway_order_id": payment.gateway_order_id,
        },
    )
    return CallbackResult(CallbackStatus.BOT_PENDING, payment.bot_order_id)


def _maybe_record_first_payment(db: Session, payment: Payment) -> None:
    """First-production-payment guardrail (flushed in the caller's
    transaction; must never alter financial correctness)."""
    from sqlalchemy import func

    verified_count = db.execute(
        select(func.count(Payment.id)).where(Payment.gateway_verified_at.is_not(None))
    ).scalar_one()
    if verified_count == 1:
        record_event(
            db,
            payment_id=payment.id,
            event_type="first_production_payment_verified",
            level="critical",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "amount": payment.amount,
                "checklist": "run the first-payment checklist (PRODUCTION_CHECKLIST_FA.md)",
            },
        )
        logger.critical(
            "first_production_payment_verified",
            extra={"payment_id": payment.id, "gateway_order_id": payment.gateway_order_id},
        )


def process_callback(
    db: Session,
    client: CentralPayClient,
    *,
    gateway_order_id: int,
    callback_token: str,
    settings: "Settings | None" = None,
) -> CallbackResult:
    payment = db.execute(
        select(Payment).where(Payment.gateway_order_id == gateway_order_id).with_for_update()
    ).scalar_one_or_none()

    if payment is None:
        record_event(
            db,
            payment_id=None,
            event_type="callback_received",
            level="warning",
            data={"gateway_order_id": gateway_order_id, "result": "payment_not_found"},
        )
        db.commit()
        raise PaymentNotFoundError()

    # One-time-token consumption state, checked under the row lock and
    # BEFORE any CentralPay verify call. A stale token (from a superseded
    # link-creation attempt) is rejected; the token value itself is never
    # stored in events or logs.
    if not callback_token_matches(callback_token, payment.callback_token_hash):
        record_event(
            db,
            payment_id=payment.id,
            event_type="callback_token_invalid",
            level="warning",
            data={"gateway_order_id": gateway_order_id, "payment_status": payment.status},
        )
        db.commit()
        raise InvalidCallbackTokenError()

    record_event(
        db,
        payment_id=payment.id,
        event_type="callback_received",
        data={"gateway_order_id": gateway_order_id, "payment_status": payment.status},
    )

    if payment.status in VERIFIED_STATUSES or payment.gateway_verified_at is not None:
        # Verification already succeeded: never call verify again. The page
        # shown reflects the current bot delivery state.
        record_event(
            db,
            payment_id=payment.id,
            event_type="duplicate_callback_ignored",
            data={"gateway_order_id": gateway_order_id, "payment_status": payment.status},
        )
        db.commit()
        if payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value:
            return CallbackResult(CallbackStatus.BOT_ACCEPTED, payment.bot_order_id)
        if payment.status == PaymentStatus.MANUAL_REVIEW.value:
            return CallbackResult(CallbackStatus.UNDER_REVIEW, payment.bot_order_id)
        return CallbackResult(CallbackStatus.BOT_PENDING, payment.bot_order_id)

    if payment.status == PaymentStatus.MANUAL_REVIEW.value:
        # An administrator owns this payment now; do not auto-verify.
        db.commit()
        return CallbackResult(CallbackStatus.UNDER_REVIEW, payment.bot_order_id)

    try:
        result = client.verify(order_id=gateway_order_id)
    except CentralPayError as exc:
        payment.last_error = exc.message[:_ERROR_MAX_LENGTH]
        record_event(
            db,
            payment_id=payment.id,
            event_type="centralpay_verify_failed",
            level="error",
            data={
                "gateway_order_id": gateway_order_id,
                "stage": "transport",
                "error_code": exc.code,
                "reason": exc.message[:_ERROR_MAX_LENGTH],
            },
        )
        db.commit()
        raise

    if not result.gateway_success:
        reason = (result.failure_reason or "verify not successful")[:_ERROR_MAX_LENGTH]
        payment.last_error = reason
        record_event(
            db,
            payment_id=payment.id,
            event_type="centralpay_verify_failed",
            level="warning",
            data={"gateway_order_id": gateway_order_id, "stage": "gateway", "reason": reason},
        )
        db.commit()
        raise VerificationFailedError()

    return _validate_and_apply_verification(db, payment, result, settings)
