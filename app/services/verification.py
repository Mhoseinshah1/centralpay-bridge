"""Callback processing and CentralPay verification.

The callback route validates the HMAC signature BEFORE this service runs; no
database or gateway work happens for unsigned requests. This service locks the
payment row for the whole verification so concurrent callbacks serialize and a
payment can never be verified twice.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.centralpay import CentralPayClient, VerifyResult
from app.exceptions import (
    CentralPayError,
    PaymentNotFoundError,
    VerificationFailedError,
)
from app.models import Payment, PaymentStatus

logger = logging.getLogger("app.services.verification")

_ERROR_MAX_LENGTH = 500


@dataclass(frozen=True)
class CallbackResult:
    status: str  # "verified" | "already_verified" | "under_review"
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
    db: Session, payment: Payment, result: VerifyResult
) -> CallbackResult:
    """Apply a gateway-successful verify result after validating its fields.

    Any inconsistency between what the gateway reports and what we recorded is
    a financial anomaly: the payment moves to manual_review and is never
    auto-verified.
    """
    if result.reference_id is None:
        _move_to_manual_review(
            db,
            payment,
            mismatch_event="verify_missing_reference_id",
            data={"gateway_order_id": payment.gateway_order_id},
        )
        return CallbackResult("under_review", payment.bot_order_id)
    if result.amount != payment.amount:
        _move_to_manual_review(
            db,
            payment,
            mismatch_event="verify_amount_mismatch",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "expected_amount": payment.amount,
                "reported_amount": result.amount,
            },
        )
        return CallbackResult("under_review", payment.bot_order_id)
    if result.user_id != payment.gateway_user_id:
        _move_to_manual_review(
            db,
            payment,
            mismatch_event="verify_user_id_mismatch",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "expected_user_id": payment.gateway_user_id,
                "reported_user_id": result.user_id,
            },
        )
        return CallbackResult("under_review", payment.bot_order_id)

    payment.status = PaymentStatus.GATEWAY_VERIFIED.value
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
            "amount": payment.amount,
        },
    )
    db.commit()
    return CallbackResult("verified", payment.bot_order_id)


def process_callback(
    db: Session,
    client: CentralPayClient,
    *,
    gateway_order_id: int,
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

    record_event(
        db,
        payment_id=payment.id,
        event_type="callback_received",
        data={"gateway_order_id": gateway_order_id, "payment_status": payment.status},
    )

    if payment.status == PaymentStatus.GATEWAY_VERIFIED.value:
        # Verification already succeeded: never call verify again.
        record_event(
            db,
            payment_id=payment.id,
            event_type="duplicate_callback_ignored",
            data={"gateway_order_id": gateway_order_id},
        )
        db.commit()
        return CallbackResult("already_verified", payment.bot_order_id)

    if payment.status == PaymentStatus.MANUAL_REVIEW.value:
        # An administrator owns this payment now; do not auto-verify.
        db.commit()
        return CallbackResult("under_review", payment.bot_order_id)

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

    return _validate_and_apply_verification(db, payment, result)
