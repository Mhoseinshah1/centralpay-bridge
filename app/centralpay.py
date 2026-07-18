"""HTTP client for the CentralPay basic web service (getLink and verify).

Request payloads contain API keys and must never be logged. Only safe
metadata (endpoint name, HTTP status, orderId, internal reason codes) may
appear in logs, exceptions, audit events, or API responses.

Gateway-controlled data policy: every byte of a gateway response body
(message text, HTML, JSON values) is attacker-influenceable-by-gateway
content. It is parsed, classified into one of the fixed internal reason
codes below, and then discarded — raw response text never leaves this
module.
"""

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.exceptions import (
    CentralPayConnectionError,
    CentralPayInvalidResponseError,
    CentralPayRejectedError,
)

logger = logging.getLogger("app.centralpay")

# --- Internal reason codes -------------------------------------------------
# The ONLY vocabulary allowed to describe a gateway response outside this
# module. Fixed strings; never derived from response content.
GATEWAY_RESPONSE_INVALID = "gateway_response_invalid"
GATEWAY_REJECTED = "gateway_rejected"
GATEWAY_MISSING_DATA = "gateway_missing_data"
GATEWAY_INVALID_REDIRECT_URL = "gateway_invalid_redirect_url"
GATEWAY_INVALID_REFERENCE_ID = "gateway_invalid_reference_id"
GATEWAY_INVALID_AMOUNT = "gateway_invalid_amount"
GATEWAY_INVALID_USER_ID = "gateway_invalid_user_id"

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
    # Explicit parse-level reason codes for gateway-successful responses
    # whose fields were missing or mistyped (e.g. gateway_invalid_amount).
    field_errors: tuple[str, ...] = ()


# Explicit positive success markers. Success is NEVER inferred from truthy
# values or from the mere presence of a data object.
_SUCCESS_STATUS_VALUES = {"1", "success", "ok", "completed", "done", "true"}


def _explicit_success(body: dict[str, Any]) -> bool:
    success = body.get("success")
    if success is True:
        return True
    if isinstance(success, str) and success.strip().lower() == "true":
        return True
    status = body.get("status")
    if status is True:
        return True
    return (
        not isinstance(status, bool)
        and isinstance(status, int | str)
        and str(status).strip().lower() in _SUCCESS_STATUS_VALUES
    )


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


def gateway_reason_code(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """Classify a gateway response into an internal (reason, marker) pair.

    Returns ``(None, None)`` for an explicitly successful response. Both
    values come from fixed vocabularies — the response's own text is never
    returned. The marker records WHICH failure signal was present (for
    debugging) without exposing gateway-controlled content.
    """
    if body.get("success") is False:
        return GATEWAY_REJECTED, "success_false"
    status = body.get("status")
    if (
        not isinstance(status, bool)
        and isinstance(status, int | str)
        and str(status).strip().lower() in _FAILURE_STATUS_VALUES
    ):
        return GATEWAY_REJECTED, "failure_status"
    if body.get("error"):
        return GATEWAY_REJECTED, "error_field"
    if not _explicit_success(body):
        # No explicit positive marker: success is never guessed.
        return GATEWAY_RESPONSE_INVALID, "no_success_marker"
    return None, None


# Redirect URL policy (documented in SECURITY.md): parsed with urlsplit,
# never substring checks. HTTPS only — CentralPay serves its payment pages
# over HTTPS, and an http:// redirect would downgrade the payer to
# cleartext. Bounded length; non-empty hostname; no userinfo credentials;
# no whitespace or control characters.
_REDIRECT_URL_MAX_LENGTH = 2048


def _validate_redirect_url(value: object) -> str | None:
    """Return the validated redirect URL, or None if it must be rejected."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > _REDIRECT_URL_MAX_LENGTH:
        return None
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        _ = parts.port  # raises ValueError for a malformed port (":abc")
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if not hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return url


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
        reason, marker = gateway_reason_code(body)
        redirect_url: str | None = None
        if reason is None:
            data = body.get("data")
            if not isinstance(data, dict):
                reason = GATEWAY_MISSING_DATA
            else:
                redirect_url = _validate_redirect_url(data.get("redirectUrl"))
                if redirect_url is None:
                    reason = GATEWAY_INVALID_REDIRECT_URL
        if redirect_url is not None:
            logger.info(
                "centralpay_getlink_ok",
                extra={
                    "endpoint": "getLink.php",
                    "gateway_order_id": order_id,
                    "http_status": 200,
                },
            )
            return redirect_url
        logger.warning(
            "centralpay_getlink_rejected",
            extra={
                "endpoint": "getLink.php",
                "gateway_order_id": order_id,
                "http_status": 200,
                "reason": reason,
                "marker": marker,
            },
        )
        raise CentralPayRejectedError(f"getLink rejected: {reason}")

    def verify(self, *, order_id: int) -> VerifyResult:
        """Verify a payment. Raises for transport/protocol errors; returns a
        VerifyResult with ``gateway_success=False`` when the gateway explicitly
        reports the payment as unsuccessful."""
        payload = {"api_key": self._verify_api_key, "orderId": order_id}
        body = self._post_json("verify.php", payload)
        failure, marker = gateway_reason_code(body)
        data = body.get("data")
        if failure is None and not isinstance(data, dict):
            failure = GATEWAY_MISSING_DATA
        if failure is not None:
            logger.warning(
                "centralpay_verify_not_successful",
                extra={
                    "endpoint": "verify.php",
                    "gateway_order_id": order_id,
                    "http_status": 200,
                    "reason": failure,
                    "marker": marker,
                },
            )
            return VerifyResult(
                gateway_success=False,
                reference_id=None,
                amount=None,
                user_id=None,
                card_number=None,
                failure_reason=failure,
            )
        assert isinstance(data, dict)  # narrowed above; failure covers the rest
        # Gateway reported success: parse fields strictly. Missing or
        # mistyped fields yield None plus an explicit reason code — the
        # verification service then routes the payment to manual review
        # (money may have moved; never guess).
        field_errors: list[str] = []
        reference_id = _to_nonempty_str(data.get("referenceId"))
        if reference_id is None:
            field_errors.append(GATEWAY_INVALID_REFERENCE_ID)
        amount = _to_int(data.get("amount"))
        if amount is None:
            field_errors.append(GATEWAY_INVALID_AMOUNT)
        user_id = _to_int(data.get("userId"))
        if user_id is None:
            field_errors.append(GATEWAY_INVALID_USER_ID)
        logger.info(
            "centralpay_verify_ok",
            extra={
                "endpoint": "verify.php",
                "gateway_order_id": order_id,
                "http_status": 200,
                "field_errors": field_errors or None,
            },
        )
        return VerifyResult(
            gateway_success=True,
            reference_id=reference_id,
            amount=amount,
            user_id=user_id,
            card_number=_to_nonempty_str(data.get("cardNumber")),
            failure_reason=None,
            field_errors=tuple(field_errors),
        )
