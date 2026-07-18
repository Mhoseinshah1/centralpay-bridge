"""Audit trail: every financial state transition is recorded with a request id."""

from tests.conftest import (
    callback_path,
    create_order,
    event_types,
    get_events,
    get_payment,
    verify_ok_response,
)


def test_full_flow_records_ordered_audit_events(client, settings, session_factory, stub):
    create_response = create_order(client, settings, order_id="audit-1", amount=12000)
    assert create_response.status_code == 200
    payment = get_payment(session_factory, "audit-1")

    stub.verify_result = verify_ok_response(amount=12000)
    callback_response = client.get(callback_path(settings, payment.gateway_order_id))
    assert callback_response.status_code == 200

    events = get_events(session_factory, payment.id)
    assert event_types(events) == [
        "payment_created",
        "payment_link_created",
        "callback_received",
        "gateway_payment_verified",
    ]

    # Every event carries the request id of the HTTP request that caused it.
    create_request_id = create_response.headers["x-request-id"]
    callback_request_id = callback_response.headers["x-request-id"]
    assert create_request_id != callback_request_id
    assert [event.request_id for event in events] == [
        create_request_id,
        create_request_id,
        callback_request_id,
        callback_request_id,
    ]


def test_incoming_request_id_is_propagated(client, settings, session_factory):
    response = create_order(client, settings, order_id="audit-2")
    assert response.status_code == 200

    inbound_id = "proxy-generated-id-123"
    response = client.post(
        "/api/custom-payment",
        json={
            "api_key": settings.inbound_api_key,
            "amount": 10000,
            "order_id": "audit-2",
        },
        headers={"x-request-id": inbound_id},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == inbound_id


def test_audit_events_never_contain_secrets(client, settings, session_factory, stub):
    create_order(client, settings, order_id="audit-3", amount=8000)
    payment = get_payment(session_factory, "audit-3")
    stub.verify_result = verify_ok_response(amount=8000, card_number="6037991234567890")
    client.get(callback_path(settings, payment.gateway_order_id))

    secrets = [
        settings.inbound_api_key,
        settings.callback_hmac_secret,
        settings.centralpay_getlink_api_key,
        settings.centralpay_verify_api_key,
    ]
    for event in get_events(session_factory):
        serialized = repr(event.data)
        for secret in secrets:
            assert secret not in serialized
        assert "6037991234567890" not in serialized  # full card number
        assert "gateway.test/pay" not in serialized  # full redirect URL
