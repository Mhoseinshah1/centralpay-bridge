"""Payment creation: idempotent by bot_order_id, serialized with row locks.

Flow:
1. Ensure a payment row exists for the bot order id (committed immediately so
   the attempt is durable and audited even if the process crashes later).
2. Re-select the row FOR UPDATE and act on its current state. The row lock is
   held across the CentralPay getLink call so concurrent requests for the
   same order serialize and can never produce two live payment links.
"""

import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_event
from app.centralpay import CentralPayClient
from app.config import Settings
from app.exceptions import (
    CentralPayError,
    DuplicateOrderAmountMismatchError,
    GatewayOrderIdAllocationError,
    OrderAlreadyVerifiedError,
    OrderUnderReviewError,
)
from app.models import Payment, PaymentStatus
from app.security import build_callback_url, callback_token_hash, generate_callback_token

logger = logging.getLogger("app.services.payments")

_ERROR_MAX_LENGTH = 500

# 12-digit ids: large enough that random collisions are negligible, small
# enough to stay far below any BIGINT limit on the CentralPay side.
_GATEWAY_ORDER_ID_MIN = 10**11
_GATEWAY_ORDER_ID_SPAN = 9 * 10**11
_GATEWAY_ORDER_ID_ATTEMPTS = 5


def _generate_gateway_order_id(db: Session) -> int:
    for _ in range(_GATEWAY_ORDER_ID_ATTEMPTS):
        candidate = _GATEWAY_ORDER_ID_MIN + secrets.randbelow(_GATEWAY_ORDER_ID_SPAN)
        exists = db.execute(
            select(Payment.id).where(Payment.gateway_order_id == candidate)
        ).first()
        if exists is None:
            return candidate
    raise GatewayOrderIdAllocationError()


def _lock_payment_by_bot_order_id(db: Session, bot_order_id: str) -> Payment | None:
    return db.execute(
        select(Payment).where(Payment.bot_order_id == bot_order_id).with_for_update()
    ).scalar_one_or_none()


def _ensure_payment_row(
    db: Session, settings: Settings, *, bot_order_id: str, amount: int
) -> None:
    """Create the payment row in its own committed transaction if missing."""
    exists = db.execute(select(Payment.id).where(Payment.bot_order_id == bot_order_id)).first()
    db.rollback()
    if exists is not None:
        return
    payment = Payment(
        bot_order_id=bot_order_id,
        gateway_order_id=_generate_gateway_order_id(db),
        gateway_user_id=settings.centralpay_user_id,
        amount=amount,
        status=PaymentStatus.CREATED.value,
    )
    db.add(payment)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request created the row first; fall through to the
        # locked re-select in create_payment.
        db.rollback()
        return
    record_event(
        db,
        payment_id=payment.id,
        event_type="payment_created",
        data={
            "bot_order_id": bot_order_id,
            "gateway_order_id": payment.gateway_order_id,
            "amount": amount,
        },
    )
    db.commit()


def create_payment(
    db: Session,
    client: CentralPayClient,
    settings: Settings,
    *,
    bot_order_id: str,
    amount: int,
) -> str:
    """Create (or idempotently return) a payment link for a bot order.

    Returns the CentralPay redirect URL.
    """
    _ensure_payment_row(db, settings, bot_order_id=bot_order_id, amount=amount)

    payment = _lock_payment_by_bot_order_id(db, bot_order_id)
    if payment is None:
        # The row was just ensured; its absence means an unexpected deletion.
        raise GatewayOrderIdAllocationError("payment row disappeared during creation")

    if payment.amount != amount:
        record_event(
            db,
            payment_id=payment.id,
            event_type="duplicate_order_amount_mismatch",
            level="warning",
            data={"existing_amount": payment.amount, "requested_amount": amount},
        )
        db.commit()
        raise DuplicateOrderAmountMismatchError()

    verified_statuses = (
        PaymentStatus.GATEWAY_VERIFIED.value,
        PaymentStatus.BOT_NOTIFY_PENDING.value,
        PaymentStatus.BOT_NOTIFY_ACCEPTED.value,
    )
    if payment.gateway_verified_at is not None or payment.status in verified_statuses:
        db.rollback()
        raise OrderAlreadyVerifiedError()
    if payment.status == PaymentStatus.MANUAL_REVIEW.value:
        db.rollback()
        raise OrderUnderReviewError()
    if payment.status == PaymentStatus.LINK_CREATED.value and payment.redirect_url:
        db.rollback()
        return payment.redirect_url

    # Status is created or getlink_failed: attempt link creation while holding
    # the row lock. A previously failed attempt gets a fresh gateway order id
    # in case CentralPay half-registered the old one.
    if payment.status == PaymentStatus.GETLINK_FAILED.value:
        payment.gateway_order_id = _generate_gateway_order_id(db)

    # Fresh one-time callback token per link-creation attempt. Only its hash
    # is stored; tokens from earlier attempts become stale and are rejected
    # before any CentralPay verify call.
    callback_token = generate_callback_token()
    payment.callback_token_hash = callback_token_hash(callback_token)
    payment.callback_token_issued_at = datetime.now(UTC)

    return_url = build_callback_url(settings, payment.gateway_order_id, callback_token)
    try:
        redirect_url = client.get_link(
            amount=payment.amount,
            user_id=payment.gateway_user_id,
            order_id=payment.gateway_order_id,
            return_url=return_url,
        )
    except CentralPayError as exc:
        payment.status = PaymentStatus.GETLINK_FAILED.value
        payment.last_error = exc.message[:_ERROR_MAX_LENGTH]
        record_event(
            db,
            payment_id=payment.id,
            event_type="centralpay_getlink_failed",
            level="error",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "error_code": exc.code,
                "reason": exc.message[:_ERROR_MAX_LENGTH],
            },
        )
        db.commit()
        raise

    payment.status = PaymentStatus.LINK_CREATED.value
    payment.redirect_url = redirect_url
    payment.last_error = None
    record_event(
        db,
        payment_id=payment.id,
        event_type="payment_link_created",
        data={"gateway_order_id": payment.gateway_order_id},
    )
    db.commit()
    return redirect_url
