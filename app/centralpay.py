"""HTTP client for the CentralPay basic web service (getLink and verify).

Request payloads contain API keys and must never be logged. Only safe
metadata (endpoint name, HTTP status, orderId) may appear in logs.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.exceptions import (
    CentralPayConnectionError,
    CentralPayInvalidResponseError,
    CentralPayRejectedError,
)

logger = logging.getLogger("app.centralpay")

_SAFE_REASON_MAX_LENGTH = 200

# Explicit gateway failure markers. Success detection is conservative: a
# response is only treated as successful when a data object is present and
# none of these markers are set.
_FAILURE_STATUS_VALUES = {"error", "failed", "fail", "0", "-1"}


@dataclass(frozen=True)
class VerifyResult:
    gateway_success: bool
    reference_id: str | None
    amount: int | None
    user_id: int | None
    card_number: str | None
    failure_reason: str | None


def _to_int(value: object) -> int | None:
    """Coerce gateway numeric fields (int or digit string) to int; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _to_nonempty_str(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _safe_reason(body: object) -> str:
    """Extract a short, safe failure reason from a gateway response body."""
    if isinstance(body, dict):
        for key in ("message", "error", "msg", "description"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_SAFE_REASON_MAX_LENGTH]
    return "gateway response did not indicate success"


def _explicit_failure(body: dict[str, Any]) -> str | None:
    if body.get("success") is False:
        return "success=false"
    status = body.get("status")
    if (
        not isinstance(status, bool)
        and isinstance(status, int | str)
        and str(status).strip().lower() in _FAILURE_STATUS_VALUES
    ):
        return f"status={status}"
    if body.get("error"):
        return _safe_reason(body)
    return None


class CentralPayClient:
    def __init__(
        self,
        *,
        base_url: str,
        getlink_api_key: str,
        verify_api_key: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._getlink_api_key = getlink_api_key
        self._verify_api_key = verify_api_key
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{endpoint}"
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.error(
                "centralpay_request_failed",
                extra={"endpoint": endpoint, "error_type": type(exc).__name__},
            )
            raise CentralPayConnectionError(
                f"{endpoint} request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            logger.error(
                "centralpay_http_error",
                extra={"endpoint": endpoint, "status_code": response.status_code},
            )
            raise CentralPayRejectedError(f"{endpoint} returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise CentralPayInvalidResponseError(f"{endpoint} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise CentralPayInvalidResponseError(f"{endpoint} returned a non-object JSON body")
        return body

    def get_link(
        self, *, amount: int, user_id: int, order_id: int, return_url: str
    ) -> str:
        """Create a payment link. Returns the redirect URL on success."""
        payload = {
            "api_key": self._getlink_api_key,
            "type": "deposit",
            "amount": amount,
            "userId": user_id,
            "orderId": order_id,
            "returnUrl": return_url,
        }
        body = self._post_json("getLink.php", payload)
        failure = _explicit_failure(body)
        data = body.get("data")
        redirect_url = data.get("redirectUrl") if isinstance(data, dict) else None
        if failure is not None or not isinstance(redirect_url, str) or not redirect_url.strip():
            reason = failure or _safe_reason(body)
            logger.warning(
                "centralpay_getlink_rejected",
                extra={"gateway_order_id": order_id, "reason": reason},
            )
            raise CentralPayRejectedError(f"getLink rejected: {reason}")
        logger.info("centralpay_getlink_ok", extra={"gateway_order_id": order_id})
        return redirect_url.strip()

    def verify(self, *, order_id: int) -> VerifyResult:
        """Verify a payment. Raises for transport/protocol errors; returns a
        VerifyResult with ``gateway_success=False`` when the gateway explicitly
        reports the payment as unsuccessful."""
        payload = {"api_key": self._verify_api_key, "orderId": order_id}
        body = self._post_json("verify.php", payload)
        failure = _explicit_failure(body)
        data = body.get("data")
        if failure is not None or not isinstance(data, dict):
            reason = failure or "missing data object in verify response"
            logger.warning(
                "centralpay_verify_not_successful",
                extra={"gateway_order_id": order_id, "reason": reason},
            )
            return VerifyResult(
                gateway_success=False,
                reference_id=None,
                amount=None,
                user_id=None,
                card_number=None,
                failure_reason=reason,
            )
        logger.info("centralpay_verify_ok", extra={"gateway_order_id": order_id})
        return VerifyResult(
            gateway_success=True,
            reference_id=_to_nonempty_str(data.get("referenceId")),
            amount=_to_int(data.get("amount")),
            user_id=_to_int(data.get("userId")),
            card_number=_to_nonempty_str(data.get("cardNumber")),
            failure_reason=None,
        )
