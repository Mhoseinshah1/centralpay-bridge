"""Constant-time secret comparison and HMAC callback signing."""

import hashlib
import hmac

from app.config import Settings


def constant_time_equals(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def callback_signature(secret: str, gateway_order_id: int, callback_token: str) -> str:
    """HMAC over the order id AND the per-link one-time token.

    The token is regenerated on every link-creation attempt and its hash is
    stored on the payment row; the signature binds the two together so
    neither can be swapped independently.
    """
    message = f"orderId={gateway_order_id}&ct={callback_token}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_callback_signature(
    secret: str, gateway_order_id: int, callback_token: str, signature: str
) -> bool:
    expected = callback_signature(secret, gateway_order_id, callback_token)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def generate_callback_token() -> str:
    import secrets

    return secrets.token_hex(16)


def callback_token_hash(callback_token: str) -> str:
    """Only the hash is stored; a database leak alone cannot forge callbacks."""
    return hashlib.sha256(callback_token.encode("utf-8")).hexdigest()


def callback_token_matches(callback_token: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    return hmac.compare_digest(callback_token_hash(callback_token), stored_hash)


def build_callback_url(
    settings: Settings, gateway_order_id: int, callback_token: str
) -> str:
    signature = callback_signature(
        settings.callback_hmac_secret, gateway_order_id, callback_token
    )
    base = settings.public_base_url.rstrip("/")
    return (
        f"{base}/api/centralpay/callback"
        f"?orderId={gateway_order_id}&ct={callback_token}&sig={signature}"
    )
