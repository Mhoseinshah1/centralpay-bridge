"""POST /api/custom-payment — payment creation for the Telegram bot."""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import CentralPayDep, DbDep, SettingsDep
from app.exceptions import AmountOutOfRangeError, InvalidApiKeyError, RateLimitedError
from app.security import constant_time_equals
from app.services.payments import create_payment

logger = logging.getLogger("app.api.payments")

router = APIRouter()


class CreatePaymentRequest(BaseModel):
    api_key: str
    amount: int = Field(gt=0, description="Amount in TOMAN")
    order_id: str = Field(min_length=1, max_length=128)


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
