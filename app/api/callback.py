"""GET /api/centralpay/callback — signed return URL from CentralPay.

The HMAC signature is validated before any database or gateway processing.
The signature value and the full query string are never logged.
"""

import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.api.deps import CentralPayDep, DbDep, SettingsDep
from app.api.pages import payment_status_page
from app.exceptions import InvalidCallbackSignatureError
from app.security import verify_callback_signature
from app.services.verification import process_callback

logger = logging.getLogger("app.api.callback")

router = APIRouter()


@router.get("/api/centralpay/callback", response_class=HTMLResponse)
def centralpay_callback(
    db: DbDep,
    settings: SettingsDep,
    client: CentralPayDep,
    order_id: int = Query(alias="orderId"),
    sig: str = Query(min_length=1, max_length=128),
) -> HTMLResponse:
    if not verify_callback_signature(settings.callback_hmac_secret, order_id, sig):
        logger.warning("callback_signature_invalid", extra={"gateway_order_id": order_id})
        raise InvalidCallbackSignatureError()
    result = process_callback(db, client, gateway_order_id=order_id)
    # Once CentralPay verification has succeeded the payer always gets a
    # success page, even while bot delivery is pending or under review.
    return HTMLResponse(
        payment_status_page(
            result.status,
            result.bot_order_id,
            bot_username=settings.telegram_bot_username,
        )
    )
