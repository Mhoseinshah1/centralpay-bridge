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
    DuplicateOrderCustomerMismatchError,
    GatewayOrderIdAllocationError,
    OrderAlreadyVerifiedError,
    OrderUnderReviewError,
    PayableAmountOutOfRangeError,
)
from app.models import Payment, PaymentStatus
from app.security import build_callback_url, callback_token_hash, generate_callback_token
from app.services.fees import calculate_fee, select_effective_policy
from app.services.payer_identity import PayerIdentity, resolve_payer_identity

logger = logging.getLogger("app.services.payments")

_ERROR_MAX_LENGTH = 500

# 12-digit ids: large enough that random collisions are negligible, small
# enough to stay far below any BIGINT limit on the CentralPay side.
_GATEWAY_ORDER_ID_MIN = 10**11
_GATEWAY_ORDER_ID_SPAN = 9 * 10**11
_GATEWAY_ORDER_ID_ATTEMPTS = 5


def _generate_gateway_order_id(db: Session) -> int:
    """Allocate a random 12-digit gateway order id.

    Random (not a sequence) deliberately: ids are payer-visible in the
    CentralPay flow, so they must not be guessable or reveal volume, and
    random allocation has no sequence to drift or reset after a backup
    restore. Uniqueness is not probabilistic — it is enforced by the
    unique index on payments.gateway_order_id; this pre-check plus the
    IntegrityError path in _ensure_payment_row handle the (negligible)
    collision case, and a fresh id is drawn on retry.
    """
    for _ in range(_GATEWAY_ORDER_ID_ATTEMPTS):
        candidate = _GATEWAY_ORDER_ID_MIN + secrets.randbelow(_GATEWAY_ORDER_ID_SPAN)
        exists = db.execute(
            select(Payment.id).where(Payment.gateway_order_id == candidate)
        ).first()
        if exists is None:
            logger.info("gateway_order_id_allocated", extra={"gateway_order_id": candidate})
            return candidate
    raise GatewayOrderIdAllocationError()


def _lock_payment_by_bot_order_id(db: Session, bot_order_id: str) -> Payment | None:
    return db.execute(
        select(Payment).where(Payment.bot_order_id == bot_order_id).with_for_update()
    ).scalar_one_or_none()


def _ensure_payment_row(
    db: Session, settings: Settings, *, bot_order_id: str, amount: int, customer_id: str
) -> PayerIdentity | None:
    """Create the payment row in its own committed transaction if missing.

    Returns the resolved payer identity when THIS call created the row, else
    ``None`` (the row already existed or a concurrent request won the insert).

    The fee snapshot is captured HERE, exactly once: the effective policy is
    read a single time and the four snapshot fields (fee_policy_id,
    fee_rate_bps, fee_amount, payable_amount) are frozen into immutable locals
    from that one read BEFORE resolve_payer_identity runs — so a concurrent fee
    change yields entirely the old or entirely the new policy, never a mixed
    calculation, and the over-max check and the stored row use identical values.
    Later policy changes never touch this payment.

    Ordering (incident 2026-07): the payable-amount bound is enforced BEFORE
    the payer identity is resolved, so an over-max request creates no payment
    row, no fee snapshot, no gateway call — and no payer-identity mapping.
    """
    exists = db.execute(select(Payment.id).where(Payment.bot_order_id == bot_order_id)).first()
    db.rollback()
    if exists is not None:
        return None

    policy = select_effective_policy(db)
    rate_bps = policy.rate_bps if policy is not None else 0
    # Captured before resolve_payer_identity manages its own transaction
    # (which expires ORM instances), so `policy` is never touched afterward.
    fee_policy_id = policy.id if policy is not None else None
    fee_amount, payable_amount = calculate_fee(amount, rate_bps)
    # The configured MAXIMUM bounds the final gateway amount. Rejecting
    # here means: no payment row, no fee snapshot, no gateway call, no
    # payer mapping, no silent clamping, no fee reduction.
    if payable_amount > settings.max_payment_amount_toman:
        db.rollback()
        logger.warning(
            "payable_amount_out_of_range",
            extra={
                "bot_order_id": bot_order_id,
                "original_amount": amount,
                "fee_rate_bps": rate_bps,
                "payable_amount": payable_amount,
                "max_amount": settings.max_payment_amount_toman,
            },
        )
        raise PayableAmountOutOfRangeError(
            f"Payable amount {payable_amount} TOMAN (original {amount} + fee "
            f"{fee_amount}) exceeds the maximum "
            f"{settings.max_payment_amount_toman} TOMAN"
        )

    payer = resolve_payer_identity(
        db,
        secret=settings.centralpay_payer_id_secret,
        customer_id=customer_id,
        reserved_gateway_user_id=settings.centralpay_user_id,
    )
    payment = Payment(
        bot_order_id=bot_order_id,
        gateway_order_id=_generate_gateway_order_id(db),
        # Per-customer isolated gateway payer identity (incident 2026-07):
        # snapshotted once so verification and retries reuse the exact value,
        # and NEVER the old shared CENTRALPAY_USER_ID.
        gateway_user_id=payer.gateway_user_id,
        payer_identity_id=payer.id,
        payer_derivation_version=payer.derivation_version,
        amount=amount,
        fee_policy_id=fee_policy_id,
        fee_rate_bps=rate_bps,
        fee_amount=fee_amount,
        payable_amount=payable_amount,
        status=PaymentStatus.CREATED.value,
    )
    db.add(payment)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request created the row first; fall through to the
        # locked re-select in create_payment (which re-resolves the payer).
        db.rollback()
        return None
    record_event(
        db,
        payment_id=payment.id,
        event_type="payment_created",
        data={
            "bot_order_id": bot_order_id,
            "gateway_order_id": payment.gateway_order_id,
            "original_amount": amount,
            "fee_rate_bps": rate_bps,
            "fee_amount": fee_amount,
            "payable_amount": payable_amount,
        },
    )
    record_event(
        db,
        payment_id=payment.id,
        event_type="payment_fee_snapshotted",
        data={
            "fee_policy_id": payment.fee_policy_id,
            "fee_rate_bps": rate_bps,
            "original_amount": amount,
            "fee_amount": fee_amount,
            "payable_amount": payable_amount,
        },
    )
    db.commit()
    return payer


def create_payment(
    db: Session,
    client: CentralPayClient,
    settings: Settings,
    *,
    bot_order_id: str,
    amount: int,
    customer_id: str,
) -> str:
    """Create (or idempotently return) a payment link for a bot order.

    ``customer_id`` is the stable upstream customer identity; it is resolved to
    a per-customer gateway payer id (isolated from every other customer) so two
    different customers can never share one gateway payer identity. Returns the
    CentralPay redirect URL.
    """
    created_payer = _ensure_payment_row(
        db, settings, bot_order_id=bot_order_id, amount=amount, customer_id=customer_id
    )
    # For a brand-new row _ensure_payment_row already resolved the payer; for an
    # existing row resolve it now (deterministic; the mapping already exists).
    # Done BEFORE taking the row lock so the resolver's own transaction handling
    # can never release that lock.
    payer = created_payer or resolve_payer_identity(
        db,
        secret=settings.centralpay_payer_id_secret,
        customer_id=customer_id,
        reserved_gateway_user_id=settings.centralpay_user_id,
    )

    payment = _lock_payment_by_bot_order_id(db, bot_order_id)
    if payment is None:
        # The row was just ensured; its absence means an unexpected deletion.
        raise GatewayOrderIdAllocationError("payment row disappeared during creation")

    # An existing order recreated for a DIFFERENT customer must never reuse the
    # first customer's link (that would cross payer identities). Legacy rows
    # (payer_identity_id NULL, pre-fix) predate customer isolation and are not
    # matched here. Recording uses only internal ids, never the raw customer_id.
    if payment.payer_identity_id is not None and payment.payer_identity_id != payer.id:
        record_event(
            db,
            payment_id=payment.id,
            event_type="duplicate_order_customer_mismatch",
            level="warning",
            data={
                "existing_payer_identity_id": payment.payer_identity_id,
                "requested_payer_identity_id": payer.id,
            },
        )
        db.commit()
        raise DuplicateOrderCustomerMismatchError()

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
        logger.info(
            "payment_duplicate_returned",
            extra={
                "payment_id": payment.id,
                "bot_order_id": bot_order_id,
                "gateway_order_id": payment.gateway_order_id,
            },
        )
        return payment.redirect_url

    # Legacy pre-fix rows (payer_identity_id NULL) that never produced a live
    # link carry the OLD SHARED gateway_user_id snapshot. Before issuing a link
    # for one, adopt the resolved per-customer identity so the link is created
    # under the isolated id — never the shared one (incident 2026-07). Already
    # verified / LINK_CREATED legacy rows were returned/refused above and are
    # left untouched (forward-only history; their link, if any, already exists).
    if payment.payer_identity_id is None:
        payment.gateway_user_id = payer.gateway_user_id
        payment.payer_identity_id = payer.id
        payment.payer_derivation_version = payer.derivation_version
        record_event(
            db,
            payment_id=payment.id,
            event_type="legacy_payment_payer_identity_adopted",
            data={
                "payer_identity_id": payer.id,
                "gateway_user_id": payer.gateway_user_id,
                "derivation_version": payer.derivation_version,
            },
        )

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
    logger.info(
        "payment_link_creation_started",
        extra={"payment_id": payment.id, "gateway_order_id": payment.gateway_order_id},
    )
    try:
        # CentralPay charges the FINAL payable amount (original + fee); the
        # snapshot was taken at creation and is reused verbatim on retries.
        redirect_url = client.get_link(
            amount=payment.payable_amount,
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
        data={
            "gateway_order_id": payment.gateway_order_id,
            "original_amount": payment.amount,
            "fee_rate_bps": payment.fee_rate_bps,
            "fee_amount": payment.fee_amount,
            "payable_amount": payment.payable_amount,
        },
    )
    db.commit()
    return redirect_url
