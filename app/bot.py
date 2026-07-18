"""Bot notification HTTP client and outcome classification.

The bot API only guarantees that POST {url} with a Token header and
{"order_id": ..., "actions": "custom_payment_verify"} is accepted. It defines
no response schema, no idempotency behavior, and no status lookup. HTTP 2xx
therefore means "accepted by the bot API", never "balance credited".

Classification never reads the response body: the body is untrusted remote
content and must not reach logs, audit events, database columns, exception
messages, or user-facing pages. Only the HTTP status code and the Retry-After
header are inspected.
"""

import enum
from dataclasses import dataclass

import httpx

from app.reasons import ReasonCode


class OutcomeKind(enum.StrEnum):
    ACCEPTED = "accepted"
    RETRYABLE = "retryable"  # failure clearly before/without server processing
    AMBIGUOUS = "ambiguous"  # request transmission may have begun; result unknown
    MANUAL = "manual"  # non-retryable; requires administrator review


@dataclass(frozen=True)
class AttemptOutcome:
    kind: OutcomeKind
    reason_code: str
    log_event: str
    http_status: int | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = None


_RETRYABLE_HTTP = {
    500: ReasonCode.BOT_HTTP_500,
    502: ReasonCode.BOT_HTTP_502,
    503: ReasonCode.BOT_HTTP_503,
    504: ReasonCode.BOT_HTTP_504,
}

_MANUAL_HTTP = {
    400: ReasonCode.BOT_HTTP_400,
    401: ReasonCode.BOT_HTTP_401,
    403: ReasonCode.BOT_HTTP_403,
    404: ReasonCode.BOT_HTTP_404,
    409: ReasonCode.BOT_HTTP_409,
    422: ReasonCode.BOT_HTTP_422,
}

_DNS_MARKERS = (
    "getaddrinfo",
    "name or service not known",
    "temporary failure in name resolution",
    "no address associated",
    "nodename nor servname",
    "[errno -2]",
    "[errno -3]",
    "[errno -5]",
)

_REFUSED_MARKERS = ("connection refused", "[errno 111]", "econnrefused")

_MAX_RETRY_AFTER_SECONDS = 3600


def _parse_retry_after(value: str | None) -> int | None:
    """Integer-seconds Retry-After only; anything else is ignored."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped.isdigit():
        return None
    seconds = int(stripped)
    if seconds <= 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def classify_response(response: httpx.Response) -> AttemptOutcome:
    status = response.status_code
    if 200 <= status < 300:
        # Any 2xx is acceptance regardless of body: empty, plain text, or
        # JSON (valid or not). The body is never parsed or required.
        return AttemptOutcome(
            kind=OutcomeKind.ACCEPTED,
            reason_code=ReasonCode.BOT_NOTIFY_ACCEPTED.value,
            log_event="bot_notification_accepted",
            http_status=status,
            error_code=None,
        )
    if status in _RETRYABLE_HTTP:
        return AttemptOutcome(
            kind=OutcomeKind.RETRYABLE,
            reason_code=_RETRYABLE_HTTP[status].value,
            log_event="bot_http_5xx",
            http_status=status,
            error_code=f"http_{status}",
        )
    if status == 429:
        return AttemptOutcome(
            kind=OutcomeKind.RETRYABLE,
            reason_code=ReasonCode.BOT_HTTP_429.value,
            log_event="bot_http_4xx",
            http_status=status,
            error_code="http_429",
            retry_after_seconds=_parse_retry_after(response.headers.get("retry-after")),
        )
    if status in _MANUAL_HTTP:
        return AttemptOutcome(
            kind=OutcomeKind.MANUAL,
            reason_code=_MANUAL_HTTP[status].value,
            log_event="bot_http_4xx",
            http_status=status,
            error_code=f"http_{status}",
        )
    # Everything else (1xx, 3xx, unusual 4xx, 501/505/...) is unexpected for
    # this API; do not guess retry safety — send to review.
    return AttemptOutcome(
        kind=OutcomeKind.MANUAL,
        reason_code=ReasonCode.BOT_HTTP_OTHER.value,
        log_event="bot_http_5xx" if status >= 500 else "bot_http_4xx",
        http_status=status,
        error_code=f"http_{status}",
    )


def classify_transport_error(exc: httpx.HTTPError) -> AttemptOutcome:
    error_code = type(exc).__name__
    if isinstance(exc, httpx.ConnectError):
        message = str(exc).lower()
        if any(marker in message for marker in _DNS_MARKERS):
            reason = ReasonCode.BOT_DNS_FAILED
        elif any(marker in message for marker in _REFUSED_MARKERS):
            reason = ReasonCode.BOT_CONNECTION_REFUSED
        else:
            reason = ReasonCode.BOT_CONNECTION_FAILED
        return AttemptOutcome(
            kind=OutcomeKind.RETRYABLE,
            reason_code=reason.value,
            log_event="bot_connection_failed",
            error_code=error_code,
        )
    if isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout):
        # The connection was never established: nothing reached the bot.
        return AttemptOutcome(
            kind=OutcomeKind.RETRYABLE,
            reason_code=ReasonCode.BOT_CONNECTION_FAILED.value,
            log_event="bot_connection_failed",
            error_code=error_code,
        )
    # Read/write timeouts, protocol errors, and anything unrecognized: the
    # request may have been transmitted and processed. Ambiguous.
    return AttemptOutcome(
        kind=OutcomeKind.AMBIGUOUS,
        reason_code=ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value,
        log_event="bot_timeout_ambiguous",
        error_code=error_code,
    )


class BotNotifier:
    """Sends the documented bot payment notification. The Token header and
    request body are never logged."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = url
        self._token = token
        timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def send_payment_notification(self, bot_order_id: str) -> httpx.Response:
        """Raises httpx.HTTPError subclasses on transport failure."""
        return self._client.post(
            self._url,
            headers={"Token": self._token, "Content-Type": "application/json"},
            json={"order_id": bot_order_id, "actions": "custom_payment_verify"},
        )
