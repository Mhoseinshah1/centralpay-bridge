"""Tests for POST /api/custom-payment."""

import httpx
from sqlalchemy import func, select

from app.models import Payment, PaymentStatus
from tests.conftest import (
    DEFAULT_GATEWAY_USER_ID,
    DEFAULT_REDIRECT_URL,
    create_order,
    event_types,
    get_events,
    get_payment,
    getlink_ok_response,
    valid_callback_path,
    verify_ok_response,
)


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def test_invalid_inbound_api_key(client, settings, session_factory, stub):
    response = create_order(client, settings, api_key="wrong-key-wrong-key-wrong")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert _payment_count(session_factory) == 0
    assert stub.getlink_requests == []


def test_create_payment_success(client, settings, session_factory, stub):
    response = create_order(client, settings, order_id="order-abc-1", amount=10000)
    assert response.status_code == 200
    # The bot expects exactly this shape.
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}

    payment = get_payment(session_factory, "order-abc-1")
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert payment.bot_order_id == "order-abc-1"
    assert payment.amount == 10000
    # Per-user isolation (incident 2026-07): the payment carries the end-user
    # derived gateway userId and a payer_identity_id, NEVER the legacy shared
    # CENTRALPAY_USER_ID.
    assert payment.gateway_user_id == DEFAULT_GATEWAY_USER_ID
    assert payment.gateway_user_id != settings.centralpay_user_id
    assert payment.payer_identity_id is not None
    assert payment.payer_derivation_version == 2  # raw-Telegram-id scheme
    assert payment.redirect_url == DEFAULT_REDIRECT_URL
    assert 10**11 <= payment.gateway_order_id < 10**12

    assert event_types(get_events(session_factory, payment.id)) == [
        "payment_created",
        "payment_fee_snapshotted",
        "payment_link_created",
    ]

    [request] = stub.getlink_requests
    assert request["api_key"] == settings.centralpay_getlink_api_key
    assert request["type"] == "deposit"
    assert request["amount"] == 10000
    # The gateway receives the per-user isolated userId, not the shared one.
    assert request["userId"] == DEFAULT_GATEWAY_USER_ID
    assert request["userId"] != settings.centralpay_user_id
    assert request["orderId"] == payment.gateway_order_id
    assert request["returnUrl"].startswith(
        f"{settings.public_base_url}/api/centralpay/callback?orderId="
    )
    assert f"orderId={payment.gateway_order_id}" in request["returnUrl"]
    assert "sig=" in request["returnUrl"]


def test_duplicate_order_returns_existing_link(client, settings, session_factory, stub):
    first = create_order(client, settings, order_id="order-dup", amount=5000)
    second = create_order(client, settings, order_id="order-dup", amount=5000)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    # getLink was only called once; no second payment attempt was made.
    assert len(stub.getlink_requests) == 1
    assert _payment_count(session_factory) == 1


def test_duplicate_order_with_different_amount_rejected(client, settings, session_factory):
    first = create_order(client, settings, order_id="order-mismatch", amount=5000)
    assert first.status_code == 200
    second = create_order(client, settings, order_id="order-mismatch", amount=9000)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_order_amount_mismatch"

    payment = get_payment(session_factory, "order-mismatch")
    # The stored payment is unchanged.
    assert payment.amount == 5000
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert "duplicate_order_amount_mismatch" in event_types(
        get_events(session_factory, payment.id)
    )


def test_getlink_rejected_response(client, settings, session_factory, stub):
    stub.getlink_result = httpx.Response(
        200, json={"status": "error", "message": "invalid merchant"}
    )
    response = create_order(client, settings, order_id="order-rejected")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "centralpay_rejected"
    # Gateway-controlled text never reaches the API caller.
    assert "invalid merchant" not in response.text

    payment = get_payment(session_factory, "order-rejected")
    assert payment.status == PaymentStatus.GETLINK_FAILED.value
    assert payment.last_error
    assert "invalid merchant" not in payment.last_error
    events = get_events(session_factory, payment.id)
    assert event_types(events) == [
        "payment_created",
        "payment_fee_snapshotted",
        "centralpay_getlink_failed",
    ]
    assert events[-1].level == "error"
    # ...nor the stored audit trail: internal reason codes only.
    assert "invalid merchant" not in str(events[-1].data)


def test_getlink_network_failure(client, settings, session_factory, stub):
    stub.getlink_result = httpx.ConnectError("connection refused")
    response = create_order(client, settings, order_id="order-neterr")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "centralpay_connection_error"

    payment = get_payment(session_factory, "order-neterr")
    assert payment.status == PaymentStatus.GETLINK_FAILED.value
    assert "centralpay_getlink_failed" in event_types(get_events(session_factory, payment.id))


def test_retry_after_getlink_failure_uses_fresh_gateway_order_id(
    client, settings, session_factory, stub
):
    stub.getlink_result = httpx.ConnectError("connection refused")
    assert create_order(client, settings, order_id="order-retry").status_code == 502
    failed_payment = get_payment(session_factory, "order-retry")
    first_gateway_order_id = failed_payment.gateway_order_id

    stub.getlink_result = getlink_ok_response()
    response = create_order(client, settings, order_id="order-retry")
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}

    payment = get_payment(session_factory, "order-retry")
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert payment.gateway_order_id != first_gateway_order_id
    assert payment.last_error is None
    assert stub.getlink_requests[-1]["orderId"] == payment.gateway_order_id


def test_create_for_already_verified_order_rejected(client, settings, session_factory, stub):
    assert create_order(client, settings, order_id="order-paid", amount=7000).status_code == 200
    payment = get_payment(session_factory, "order-paid")
    stub.verify_result = verify_ok_response(amount=7000)
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    response = create_order(client, settings, order_id="order-paid", amount=7000)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_already_verified"
    # The successful payment record was not overwritten.
    payment = get_payment(session_factory, "order-paid")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.gateway_verified_at is not None
