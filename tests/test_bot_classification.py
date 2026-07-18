"""Unit tests for bot response/exception classification and retry backoff."""

import httpx
import pytest

from app.bot import OutcomeKind, classify_response, classify_transport_error
from app.reasons import ReasonCode
from app.services.notification import RETRY_DELAYS_SECONDS, retry_delay_seconds


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text=""),  # empty body
        httpx.Response(204),
        httpx.Response(200, text="this is not JSON {"),
        httpx.Response(200, text="OK"),
        httpx.Response(201, json={"anything": True}),
    ],
)
def test_any_2xx_is_accepted_regardless_of_body(response):
    outcome = classify_response(response)
    assert outcome.kind is OutcomeKind.ACCEPTED
    assert outcome.reason_code == ReasonCode.BOT_NOTIFY_ACCEPTED.value


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_retryable_5xx(status):
    outcome = classify_response(httpx.Response(status))
    assert outcome.kind is OutcomeKind.RETRYABLE
    assert outcome.reason_code == f"bot_http_{status}"
    assert outcome.http_status == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_manual_4xx(status):
    outcome = classify_response(httpx.Response(status))
    assert outcome.kind is OutcomeKind.MANUAL
    assert outcome.reason_code == f"bot_http_{status}"


@pytest.mark.parametrize("status", [302, 418, 501, 505])
def test_unexpected_statuses_go_to_manual_with_other(status):
    outcome = classify_response(httpx.Response(status))
    assert outcome.kind is OutcomeKind.MANUAL
    assert outcome.reason_code == ReasonCode.BOT_HTTP_OTHER.value


def test_429_uses_retry_after_when_valid():
    outcome = classify_response(
        httpx.Response(429, headers={"Retry-After": "120"})
    )
    assert outcome.kind is OutcomeKind.RETRYABLE
    assert outcome.reason_code == ReasonCode.BOT_HTTP_429.value
    assert outcome.retry_after_seconds == 120


@pytest.mark.parametrize("header", ["not-a-number", "-5", "0", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_429_invalid_retry_after_ignored(header):
    outcome = classify_response(httpx.Response(429, headers={"Retry-After": header}))
    assert outcome.retry_after_seconds is None


def test_429_retry_after_is_capped():
    outcome = classify_response(httpx.Response(429, headers={"Retry-After": "999999"}))
    assert outcome.retry_after_seconds == 3600


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("[Errno -2] Name or service not known", ReasonCode.BOT_DNS_FAILED),
        ("[Errno -3] Temporary failure in name resolution", ReasonCode.BOT_DNS_FAILED),
        ("getaddrinfo failed", ReasonCode.BOT_DNS_FAILED),
        ("[Errno 111] Connection refused", ReasonCode.BOT_CONNECTION_REFUSED),
        ("All connection attempts failed", ReasonCode.BOT_CONNECTION_FAILED),
    ],
)
def test_connect_errors_are_retryable(message, reason):
    outcome = classify_transport_error(httpx.ConnectError(message))
    assert outcome.kind is OutcomeKind.RETRYABLE
    assert outcome.reason_code == reason.value


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.PoolTimeout("t")])
def test_pre_send_timeouts_are_retryable(exc):
    outcome = classify_transport_error(exc)
    assert outcome.kind is OutcomeKind.RETRYABLE
    assert outcome.reason_code == ReasonCode.BOT_CONNECTION_FAILED.value


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("t"),
        httpx.WriteTimeout("t"),
        httpx.RemoteProtocolError("closed"),
        httpx.ReadError("e"),
        httpx.WriteError("e"),
    ],
)
def test_post_send_failures_are_ambiguous(exc):
    outcome = classify_transport_error(exc)
    assert outcome.kind is OutcomeKind.AMBIGUOUS
    assert outcome.reason_code == ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value
    assert outcome.error_code == type(exc).__name__


def test_retry_delays_follow_bounded_schedule():
    assert RETRY_DELAYS_SECONDS == (60, 120, 300, 600, 1800, 3600)
    for attempt, base in enumerate(RETRY_DELAYS_SECONDS, start=1):
        assert retry_delay_seconds(attempt, None, lambda: 1.0) == base
    # Attempts beyond the table reuse the final delay.
    assert retry_delay_seconds(99, None, lambda: 1.0) == 3600


def test_retry_delay_applies_jitter():
    assert retry_delay_seconds(1, None, lambda: 1.1) == pytest.approx(66.0)


def test_retry_after_extends_but_never_shortens_delay():
    assert retry_delay_seconds(1, 300, lambda: 1.0) == 300.0
    assert retry_delay_seconds(6, 300, lambda: 1.0) == 3600.0
