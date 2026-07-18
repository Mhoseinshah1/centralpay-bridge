"""Constant-time secret comparison and HMAC callback signing."""

import hashlib
import hmac

from app.config import Settings


def constant_time_equals(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def callback_signature(secret: str, gateway_order_id: int) -> str:
    message = f"orderId={gateway_order_id}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_callback_signature(secret: str, gateway_order_id: int, signature: str) -> bool:
    expected = callback_signature(secret, gateway_order_id)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def build_callback_url(settings: Settings, gateway_order_id: int) -> str:
    signature = callback_signature(settings.callback_hmac_secret, gateway_order_id)
    base = settings.public_base_url.rstrip("/")
    return f"{base}/api/centralpay/callback?orderId={gateway_order_id}&sig={signature}"
