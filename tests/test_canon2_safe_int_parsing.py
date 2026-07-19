"""CANON-2 — strict ASCII integer parsing on the gateway and bot boundaries.

`str.isdigit()` accepts strings `int()` rejects (superscripts, Persian/Arabic
digits), and the old `lstrip("-")` gate let multi-sign strings through. Those
values used to raise `ValueError` from `app/centralpay.py::_to_int` (uncaught
→ HTTP 500 on the callback) and from `app/bot.py::_parse_retry_after`
(uncaught → worker-pass exception). Both parsers now use an explicit ASCII
grammar, never raise, and route malformed values through the existing
field-error / normal-backoff paths without leaking the raw value.
"""

import io
import logging

import httpx
import pytest

from app.bot import _parse_retry_after, classify_response
from app.centralpay import _to_int
from app.logging_setup import JsonFormatter, SecretRedactor, collect_secret_values
from app.models import PaymentStatus
from tests.conftest import (
    as_utc,
    create_order,
    event_types,
    get_events,
    get_payment,
    make_verified_pending,
    run_pass,
    valid_callback_path,
)
from tests.test_notification import FIXED_NOW, QUEUE_TIME


@pytest.fixture(autouse=True)
def deterministic_queue_clock(monkeypatch):
    """Pin the callback-side queue clock to QUEUE_TIME so payments queued via
    make_verified_pending are deterministically due at the worker's FIXED_NOW
    (same seam the notification suite injects)."""
    import app.services.verification as verification_module

    monkeypatch.setattr(verification_module, "utcnow", lambda: QUEUE_TIME)

# Digit-like strings that pass str.isdigit() / lstrip("-") but are NOT
# ASCII decimals int() accepts — the exact inputs that used to raise.
SUPERSCRIPT = "²"  # U+00B2; the latin-1 Retry-After byte 0xB2 decodes to this
PERSIAN = "۱۲۳"  # Persian (Extended Arabic-Indic) digits
ARABIC = "١٢٣"  # Arabic-Indic digits
MULTISIGN = "--5"
LONG_DIGITS = "9" * 6000  # ASCII digits, but int() refuses (str-digit limit)


# --- unit grammar: never raises; ASCII-only ---------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        (5, 5),
        (-7, -7),
        ("0", 0),
        ("123", 123),
        ("-1", -1),
        ("  42  ", 42),
    ],
)
def test_to_int_accepts_ascii_decimals_and_ints(value, expected):
    assert _to_int(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.5,
        [1],
        {"a": 1},
        "",
        " ",
        "+",
        "+5",
        MULTISIGN,
        "---5",
        "5-",
        SUPERSCRIPT,
        "⁵",
        PERSIAN,
        ARABIC,
        f"1{SUPERSCRIPT}3",
        LONG_DIGITS,
        "0x10",
        "10.5",
        None,
    ],
)
def test_to_int_rejects_non_ascii_and_malformed_without_raising(value):
    assert _to_int(value) is None


@pytest.mark.parametrize(
    "value,expected",
    [("10", 10), ("  30 ", 30), ("0", None), ("99999", 3600)],
)
def test_parse_retry_after_accepts_unsigned_ascii_only(value, expected):
    assert _parse_retry_after(value) == expected


@pytest.mark.parametrize(
    "value",
    [SUPERSCRIPT, MULTISIGN, "-5", "+5", PERSIAN, ARABIC, LONG_DIGITS, "", " ", "abc",
     "Wed, 21 Oct 2015 07:28:00 GMT", "10.5", None],
)
def test_parse_retry_after_rejects_non_ascii_and_malformed_without_raising(value):
    assert _parse_retry_after(value) is None


# --- gateway callback path: malformed number → manual review, never 500 -----


def _malformed_verify(amount: object, user_id: object, reference_id: object) -> httpx.Response:
    data: dict[str, object] = {"amount": amount, "userId": user_id}
    if reference_id is not None:
        data["referenceId"] = reference_id
    return httpx.Response(200, json={"status": "success", "data": data})


@pytest.mark.parametrize(
    "amount,user_id,marker",
    [
        (SUPERSCRIPT, 4242, SUPERSCRIPT),
        (10000, SUPERSCRIPT, SUPERSCRIPT),
        (MULTISIGN, 4242, MULTISIGN),
        (PERSIAN, 4242, PERSIAN),
        (ARABIC, 4242, ARABIC),
        (LONG_DIGITS, 4242, LONG_DIGITS),
    ],
)
def test_malformed_gateway_number_routes_to_manual_review(
    client, settings, session_factory, stub, amount, user_id, marker
):
    assert create_order(client, settings, order_id="cb-canon2", amount=10000).status_code == 200
    before = get_payment(session_factory, "cb-canon2")
    snapshot = (before.amount, before.fee_amount, before.payable_amount, before.fee_rate_bps)
    stub.verify_result = _malformed_verify(amount, user_id, "REF-canon2")

    # Capture app logs to prove the raw malformed value never reaches them.
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(JsonFormatter(SecretRedactor(collect_secret_values(settings))))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = client.get(valid_callback_path(stub, before.gateway_order_id))
    finally:
        root.removeHandler(handler)

    # No unhandled 500; the payer sees the under-review page.
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text

    payment = get_payment(session_factory, "cb-canon2")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.gateway_verified_at is None
    assert payment.reference_id is None
    # No bot notification was queued or sent.
    types = event_types(get_events(session_factory, payment.id))
    assert "manual_review_required" in types
    assert "gateway_payment_verified" not in types
    assert "bot_notification_queued" not in types
    assert payment.status != PaymentStatus.BOT_NOTIFY_PENDING.value
    # Financial snapshot is untouched.
    assert (
        payment.amount,
        payment.fee_amount,
        payment.payable_amount,
        payment.fee_rate_bps,
    ) == snapshot

    # The raw malformed value never appears anywhere operator-visible.
    logs = handler.stream.getvalue()
    assert marker not in logs
    assert marker not in response.text
    assert marker not in (payment.last_error or "")
    for event in get_events(session_factory, payment.id):
        assert marker not in repr(event.data)


# --- bot Retry-After: malformed header → normal backoff, never a worker crash -


def _run_429(session_factory, notifier, settings, bot_stub, retry_after):
    # Real HTTP headers are latin-1 bytes on the wire (that is how the 0xB2
    # byte reaches the parser as "²"); construct the response with raw bytes
    # so httpx does not reject a non-ASCII header value at test time.
    raw = retry_after.encode("latin-1")
    bot_stub.result = httpx.Response(429, headers=[(b"retry-after", raw)])
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(JsonFormatter(SecretRedactor(collect_secret_values(settings))))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        result = run_pass(session_factory, notifier, settings, now=FIXED_NOW)
    finally:
        root.removeHandler(handler)
    return result, handler.stream.getvalue()


@pytest.mark.parametrize("retry_after,marker", [(SUPERSCRIPT, SUPERSCRIPT), (MULTISIGN, MULTISIGN)])
def test_malformed_retry_after_falls_back_to_normal_backoff(
    client, settings, session_factory, stub, bot_stub, notifier, retry_after, marker
):
    from datetime import timedelta

    payment = make_verified_pending(client, settings, session_factory, stub, order_id="rtry-canon2")
    result, logs = _run_429(session_factory, notifier, settings, bot_stub, retry_after)

    # No worker-loop exception: the pass completed and processed the payment.
    assert result == {"recovered": 0, "processed": 1}
    assert len(bot_stub.requests) == 1  # the attempt was actually made

    payment = get_payment(session_factory, "rtry-canon2")
    # Classified through the existing 429 retry semantics with the normal
    # 60s backoff (invalid Retry-After ignored), not the stale-claim path.
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.bot_notify_attempts == 1
    assert payment.bot_last_http_status == 429
    assert as_utc(payment.next_retry_at) == FIXED_NOW + timedelta(seconds=60)
    # Claim fields cleared normally (no stale-claim recovery needed).
    assert payment.notification_claimed_at is None
    assert payment.notification_claimed_by is None
    types = event_types(get_events(session_factory, payment.id))
    assert "bot_notification_retry_scheduled" in types

    # The Token and the raw malformed header value never reach logs/events.
    from tests.conftest import TEST_BOT_TOKEN

    assert TEST_BOT_TOKEN not in logs
    assert marker not in logs
    for event in get_events(session_factory, payment.id):
        assert marker not in repr(event.data)


def test_classify_response_429_with_raw_latin1_retry_after_byte_does_not_raise():
    """The reported trigger: a Retry-After header carrying the latin-1 byte
    0xB2 (decodes to '²'). classify_response must return a RETRYABLE 429
    outcome with retry_after_seconds=None, never raise."""
    response = httpx.Response(429, headers=[(b"retry-after", b"\xb2")])
    outcome = classify_response(response)
    assert outcome.http_status == 429
    assert outcome.retry_after_seconds is None
    assert outcome.reason_code  # a fixed reason code, not the raw header
