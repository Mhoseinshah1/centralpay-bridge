"""GET /api/centralpay/callback — signed return URL from CentralPay.

The HMAC signature is validated before any database or gateway processing.
The signature value and the full query string are never logged.
"""

import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CentralPayDep, DbDep, SettingsDep
from app.exceptions import InvalidCallbackSignatureError
from app.security import verify_callback_signature
from app.services.verification import process_callback

logger = logging.getLogger("app.api.callback")

router = APIRouter()


class CallbackResponse(BaseModel):
    status: str
    order_id: str


@router.get("/api/centralpay/callback", response_model=CallbackResponse)
def centralpay_callback(
    db: DbDep,
    settings: SettingsDep,
    client: CentralPayDep,
    order_id: int = Query(alias="orderId"),
    sig: str = Query(min_length=1, max_length=128),
) -> CallbackResponse:
    if not verify_callback_signature(settings.callback_hmac_secret, order_id, sig):
        logger.warning("callback_signature_invalid", extra={"gateway_order_id": order_id})
        raise InvalidCallbackSignatureError()
    result = process_callback(db, client, gateway_order_id=order_id)
    return CallbackResponse(status=result.status, order_id=result.bot_order_id)
