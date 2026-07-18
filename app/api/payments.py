"""POST /api/custom-payment — payment creation for the Telegram bot."""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StrictInt, StrictStr

from app.api.deps import CentralPayDep, DbDep, SettingsDep
from app.exceptions import AmountOutOfRangeError, InvalidApiKeyError, RateLimitedError
from app.security import constant_time_equals
from app.services.payments import create_payment

logger = logging.getLogger("app.api.payments")

router = APIRouter()


class CreatePaymentRequest(BaseModel):
    """Strict request contract (audit: no silent coercion).

    - api_key: string; never logged, never echoed in errors.
    - amount: JSON **integer** TOMAN. Booleans, floats, and numeric
      strings are rejected, never coerced (bool is a Python int subtype
      and would otherwise coerce True -> 1). The ``le`` bound is an
      absolute schema backstop far above any legitimate payment and far
      below BIGINT; the operational policy bounds are
      MIN/MAX_PAYMENT_AMOUNT_TOMAN, enforced after authentication.
    - order_id: opaque non-empty string, at most 128 characters, no
      control characters and no NUL (NUL previously reached PostgreSQL
      and produced a 500). It is passed through byte-exact — never
      trimmed, case-folded, or Unicode-normalized — because the bot
      contract treats it as an opaque identifier.
    """

    api_key: StrictStr
    amount: StrictInt = Field(gt=0, le=1_000_000_000_000, description="Amount in TOMAN")
    order_id: StrictStr = Field(
        min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$"
    )


class CreatePaymentResponse(BaseModel):
    url: str


@router.post("/api/custom-payment", response_model=CreatePaymentResponse)
def create_custom_payment(
    request: Request,
    body: CreatePaymentRequest,
    db: DbDep,
    settings: SettingsDep,
    client: CentralPayDep,
) -> CreatePaymentResponse:
    limiters = request.app.state.rate_limiters
    if not settings.inbound_api_key or not constant_time_equals(
        body.api_key, settings.inbound_api_key
    ):
        # The provided key is never logged. Repeated invalid keys hit a
        # strict limiter (credential guessing).
        logger.warning("invalid_inbound_api_key", extra={"bot_order_id": body.order_id})
        if not limiters.check(limiters.invalid_api_key, "invalid_api_key"):
            raise RateLimitedError()
        raise InvalidApiKeyError()
    if not limiters.check(limiters.create, "create_payment"):
        raise RateLimitedError()
    # Logged only AFTER authentication so unauthenticated probes cannot
    # write attacker-chosen order ids into this event stream.
    logger.info(
        "payment_create_requested",
        extra={"bot_order_id": body.order_id, "amount": body.amount},
    )
    if not (
        settings.min_payment_amount_toman <= body.amount <= settings.max_payment_amount_toman
    ):
        logger.warning(
            "amount_out_of_range",
            extra={
                "bot_order_id": body.order_id,
                "amount": body.amount,
                "min_amount": settings.min_payment_amount_toman,
                "max_amount": settings.max_payment_amount_toman,
            },
        )
        raise AmountOutOfRangeError(
            f"Amount must be between {settings.min_payment_amount_toman} and "
            f"{settings.max_payment_amount_toman} TOMAN"
        )
    url = create_payment(
        db, client, settings, bot_order_id=body.order_id, amount=body.amount
    )
    return CreatePaymentResponse(url=url)
