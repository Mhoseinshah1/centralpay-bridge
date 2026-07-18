"""Callback replay/concurrency audit tests.

Covers: HTTP parameter pollution rejection, query validation bounds,
rate-limiter and signature-tracker memory bounds, and proof that callback
responses never echo the token or signature.
"""

from urllib.parse import parse_qs, urlsplit

from app.api.callback import SignatureFailureTracker
from app.ratelimit import SlidingWindowLimiter
from tests.conftest import (
    create_order,
    get_events,
    get_payment,
    valid_callback_path,
    verify_ok_response,
)

# --- HTTP parameter pollution ------------------------------------------------


def _prepare_valid_path(client, settings, session_factory, stub, order_id):
    assert create_order(client, settings, order_id=order_id).status_code == 200
    payment = get_payment(session_factory, order_id)
    return payment, valid_callback_path(stub, payment.gateway_order_id)


def test_duplicate_query_parameters_rejected(client, settings, session_factory, stub):
    """Duplicated orderId/ct/sig parameters are rejected before signature
    validation, even when every copy carries the legitimate value."""
    payment, path = _prepare_valid_path(
        client, settings, session_factory, stub, "hpp-1"
    )
    query = parse_qs(urlsplit(path).query)
    events_before = len(get_events(session_factory))

    polluted = [
        path + "&orderId=999999",  # conflicting duplicate
        path + f"&orderId={payment.gateway_order_id}",  # identical duplicate
        path + f"&ct={query['ct'][0]}",  # identical token duplicate
        path + "&ct=" + "a" * 32,  # conflicting token duplicate
        path + f"&sig={query['sig'][0]}",  # identical signature duplicate
        path + "&sig=" + "0" * 64,  # conflicting signature duplicate
    ]
    for url in polluted:
        response = client.get(url)
        assert response.status_code == 403, url
        assert response.json()["error"]["code"] == "invalid_callback_signature"

    # Rejected before any database or gateway work.
    assert stub.verify_requests == []
    assert len(get_events(session_factory)) == events_before
    assert get_payment(session_factory, "hpp-1").status == "link_created"

    # The legitimate single-valued link still works.
    stub.verify_result = verify_ok_response(amount=10000, reference_id="REF-hpp-1")
    assert client.get(path).status_code == 200


# --- query validation bounds -------------------------------------------------


def test_order_id_bounds_rejected(client, settings, session_factory, stub):
    ct = "0123456789abcdef0123456789abcdef"
    sig = "0" * 64
    for bad_order_id in ("0", "-5", "1000000000000000001"):
        response = client.get(
            f"/api/centralpay/callback?orderId={bad_order_id}&ct={ct}&sig={sig}"
        )
        assert response.status_code == 422, bad_order_id
    assert stub.verify_requests == []
    assert get_events(session_factory) == []


def test_non_hex_token_or_signature_rejected(client, settings, session_factory, stub):
    bad_requests = [
        f"/api/centralpay/callback?orderId=1&ct={'Z' * 32}&sig={'0' * 64}",
        f"/api/centralpay/callback?orderId=1&ct={'ABCDEF' * 4}&sig={'0' * 64}",  # uppercase
        f"/api/centralpay/callback?orderId=1&ct={'a' * 32}&sig=not-hex-at-all",
        f"/api/centralpay/callback?orderId=1&ct={'a' * 100}&sig={'0' * 64}",  # overlong ct
        f"/api/centralpay/callback?orderId=1&ct={'a' * 32}&sig={'0' * 200}",  # overlong sig
    ]
    for url in bad_requests:
        assert client.get(url).status_code == 422, url
    # Validation rejects these before HMAC or database work: no audit rows.
    assert stub.verify_requests == []
    assert get_events(session_factory) == []


# --- memory bounds under flood -----------------------------------------------


def test_signature_tracker_memory_bounded_under_flood():
    """Regression for the audit finding: the tracker used to append every
    invalid-signature timestamp with no cap, so a flood grew memory without
    limit for the whole 600s window. The deque is now bounded."""
    tracker = SignatureFailureTracker(threshold=5, window_seconds=600.0, max_events=100)
    reports = 0
    for i in range(50_000):
        if tracker.record(now=1000.0 + i * 0.001) is not None:
            reports += 1
    assert len(tracker._events) <= 100  # bounded regardless of flood size
    assert reports == 1  # still reported exactly once per window

    # A later storm in a fresh window is still detected after saturation.
    results = [tracker.record(now=2500.0 + float(i)) for i in range(5)]
    assert results[:4] == [None] * 4
    assert results[4] is not None


def test_rate_limiter_memory_bounded_under_flood():
    """SlidingWindowLimiter never records rejected events, so its deque is
    bounded by the limit itself."""
    limiter = SlidingWindowLimiter(limit=10, window_seconds=60.0)
    allowed = sum(1 for i in range(10_000) if limiter.allow(now=100.0 + i * 0.001))
    assert allowed == 10
    assert len(limiter._events) <= 10


# --- response redaction ------------------------------------------------------


def test_callback_responses_never_echo_token_or_signature(
    client, settings, session_factory, stub
):
    _, path = _prepare_valid_path(
        client, settings, session_factory, stub, "redact-1"
    )
    query = parse_qs(urlsplit(path).query)
    ct, sig = query["ct"][0], query["sig"][0]

    # Verified success page.
    stub.verify_result = verify_ok_response(amount=10000, reference_id="REF-redact-1")
    response = client.get(path)
    assert response.status_code == 200
    assert ct not in response.text
    assert sig not in response.text

    # Under-review page (amount mismatch on a second payment).
    _, path2 = _prepare_valid_path(
        client, settings, session_factory, stub, "redact-2"
    )
    query2 = parse_qs(urlsplit(path2).query)
    stub.verify_result = verify_ok_response(amount=1, reference_id="REF-redact-2")
    response = client.get(path2)
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text
    assert query2["ct"][0] not in response.text
    assert query2["sig"][0] not in response.text
