"""Tests for GET /api/centralpay/callback and CentralPay verification."""

import httpx

from app.models import PaymentStatus
from tests.conftest import (
    callback_path,
    create_order,
    event_types,
    get_events,
    get_payment,
    valid_callback_path,
    verify_ok_response,
)


def _create_paid_order(client, settings, session_factory, stub, *, order_id, amount=10000):
    assert create_order(client, settings, order_id=order_id, amount=amount).status_code == 200
    return get_payment(session_factory, order_id)


def test_invalid_callback_signature(client, settings, session_factory, stub):
    payment = _create_paid_order(client, settings, session_factory, stub, order_id="cb-badsig")
    events_before = len(get_events(session_factory))

    response = client.get(
        callback_path(settings, payment.gateway_order_id, sig="0" * 64)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_callback_signature"
    # Signature is validated before any database or gateway processing.
    assert stub.verify_requests == []
    assert len(get_events(session_factory)) == events_before
    assert get_payment(session_factory, "cb-badsig").status == PaymentStatus.LINK_CREATED.value


def test_signature_for_different_order_id_rejected(client, settings, session_factory, stub):
    payment = _create_paid_order(client, settings, session_factory, stub, order_id="cb-swap")
    from app.security import callback_signature

    ct = "0123456789abcdef0123456789abcdef"
    other_sig = callback_signature(
        settings.callback_hmac_secret, payment.gateway_order_id + 1, ct
    )
    response = client.get(
        callback_path(settings, payment.gateway_order_id, sig=other_sig, ct=ct)
    )
    assert response.status_code == 403
    assert stub.verify_requests == []


def test_stale_callback_token_rejected_before_verify(client, settings, session_factory, stub):
    """A token from a superseded link attempt must never reach verify."""
    import httpx

    stub.getlink_result = httpx.ConnectError("connection refused")
    assert create_order(client, settings, order_id="cb-stale").status_code == 502
    from tests.conftest import getlink_ok_response

    stub.getlink_result = getlink_ok_response()
    assert create_order(client, settings, order_id="cb-stale").status_code == 200
    payment = get_payment(session_factory, "cb-stale")

    # The first attempt's returnUrl (index 0) carries the stale token; its
    # signature is valid but the stored hash has been superseded.
    first_url = str(stub.getlink_requests[0]["returnUrl"])
    stale_path = first_url[first_url.index("/api/centralpay/callback"):]
    # The stale URL also carries the OLD gateway order id, which no longer
    # resolves — replaying it is a 404. Rebuild a stale-token URL against the
    # CURRENT order id to isolate the token check.
    from urllib.parse import parse_qs, urlsplit

    stale_ct = parse_qs(urlsplit(stale_path).query)["ct"][0]
    from app.security import callback_signature

    sig = callback_signature(settings.callback_hmac_secret, payment.gateway_order_id, stale_ct)
    response = client.get(
        f"/api/centralpay/callback?orderId={payment.gateway_order_id}"
        f"&ct={stale_ct}&sig={sig}"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_callback_token"
    # The gateway verify endpoint was never contacted.
    assert stub.verify_requests == []
    assert "callback_token_invalid" in event_types(
        get_events(session_factory, payment.id)
    )

    # The legitimate current link still works (not rejected too aggressively).
    stub.verify_result = verify_ok_response(amount=10000, reference_id="REF-cb-stale")
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="bot_pending"' in response.text


def test_callback_payment_not_found(client, settings, session_factory, stub):
    response = client.get(callback_path(settings, 999999999999))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "payment_not_found"
    assert stub.verify_requests == []

    events = get_events(session_factory)
    assert len(events) == 1
    assert events[0].event_type == "callback_received"
    assert events[0].payment_id is None
    assert events[0].level == "warning"


def test_verify_success(client, settings, session_factory, stub):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-ok", amount=10000
    )
    stub.verify_result = verify_ok_response(
        amount=10000, reference_id="REF-777", card_number="6037-9912-3456-7890"
    )

    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    # A verified payment gets a user-facing success page; bot delivery is
    # queued for the worker, so final processing is pending.
    assert 'data-status="bot_pending"' in response.text
    assert "cb-ok" in response.text

    payment = get_payment(session_factory, "cb-ok")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.gateway_verified_at is not None
    assert payment.reference_id == "REF-777"
    # Only the last four digits are stored, never the full card number.
    assert payment.card_last4 == "7890"

    [verify_request] = stub.verify_requests
    assert verify_request == {
        "api_key": settings.centralpay_verify_api_key,
        "orderId": payment.gateway_order_id,
    }

    assert event_types(get_events(session_factory, payment.id)) == [
        "payment_created",
        "payment_fee_snapshotted",
        "payment_link_created",
        "callback_received",
        "gateway_payment_verified",
        "bot_notification_queued",
    ]


def test_verify_payable_amount_mismatch_moves_to_manual_review(
    client, settings, session_factory, stub
):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-amount", amount=10000
    )
    stub.verify_result = verify_ok_response(amount=9000)

    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text

    payment = get_payment(session_factory, "cb-amount")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.reference_id is None

    types = event_types(get_events(session_factory, payment.id))
    assert "verify_payable_amount_mismatch" in types
    assert "manual_review_required" in types
    assert "gateway_payment_verified" not in types


def test_verify_user_id_mismatch_moves_to_manual_review(client, settings, session_factory, stub):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-user", amount=10000
    )
    stub.verify_result = verify_ok_response(amount=10000, user_id=1)

    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text

    payment = get_payment(session_factory, "cb-user")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    types = event_types(get_events(session_factory, payment.id))
    assert "verify_user_id_mismatch" in types
    assert "manual_review_required" in types


def test_verify_missing_reference_id_moves_to_manual_review(
    client, settings, session_factory, stub
):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-noref", amount=10000
    )
    stub.verify_result = verify_ok_response(amount=10000, reference_id=None)

    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text

    payment = get_payment(session_factory, "cb-noref")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    types = event_types(get_events(session_factory, payment.id))
    assert "verify_missing_reference_id" in types
    assert "manual_review_required" in types


def test_duplicate_callback_does_not_verify_again(client, settings, session_factory, stub):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-dup", amount=10000
    )
    stub.verify_result = verify_ok_response(amount=10000)

    first = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert 'data-status="bot_pending"' in first.text
    second = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert second.status_code == 200
    assert 'data-status="bot_pending"' in second.text

    # Verify was called exactly once; the verified record was not overwritten.
    assert len(stub.verify_requests) == 1
    payment = get_payment(session_factory, "cb-dup")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.gateway_verified_at is not None
    assert "duplicate_callback_ignored" in event_types(get_events(session_factory, payment.id))


def test_callback_after_manual_review_does_not_verify_again(
    client, settings, session_factory, stub
):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-review", amount=10000
    )
    stub.verify_result = verify_ok_response(amount=1)
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    assert get_payment(session_factory, "cb-review").status == PaymentStatus.MANUAL_REVIEW.value

    stub.verify_result = verify_ok_response(amount=10000)
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text
    # manual_review payments belong to an administrator; no auto re-verify.
    assert len(stub.verify_requests) == 1
    assert get_payment(session_factory, "cb-review").status == PaymentStatus.MANUAL_REVIEW.value


def test_verify_gateway_declined(client, settings, session_factory, stub):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-declined", amount=10000
    )
    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid"})

    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "verification_failed"

    payment = get_payment(session_factory, "cb-declined")
    # The payment stays link_created: the payer may still complete payment.
    assert payment.status == PaymentStatus.LINK_CREATED.value
    events = get_events(session_factory, payment.id)
    assert "centralpay_verify_failed" in event_types(events)


def test_verify_network_failure_is_recoverable(client, settings, session_factory, stub):
    payment = _create_paid_order(
        client, settings, session_factory, stub, order_id="cb-neterr", amount=10000
    )
    stub.verify_result = httpx.ConnectError("connection refused")

    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "centralpay_connection_error"
    payment = get_payment(session_factory, "cb-neterr")
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert "centralpay_verify_failed" in event_types(get_events(session_factory, payment.id))

    # A later callback retry verifies successfully.
    stub.verify_result = verify_ok_response(amount=10000)
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="bot_pending"' in response.text
    assert (
        get_payment(session_factory, "cb-neterr").status
        == PaymentStatus.BOT_NOTIFY_PENDING.value
    )
