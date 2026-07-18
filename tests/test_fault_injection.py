"""Fault injection at transaction boundaries.

Proves the crash-safety claims: the gateway-verified fact and the queued
notification commit atomically (or not at all), crashes are recoverable, and
duplicate callbacks can never enqueue duplicate bot notifications.
"""

from sqlalchemy import func, select

from app.models import PaymentEvent, PaymentStatus
from tests.conftest import (
    create_order,
    event_types,
    get_events,
    get_payment,
    valid_callback_path,
    verify_ok_response,
)


def test_crash_during_verification_commit_is_recoverable(
    client, settings, session_factory, stub, monkeypatch
):
    """Simulated crash INSIDE the verification transaction (after CentralPay
    verify succeeded, before commit): nothing is persisted, and a later
    callback retry verifies again and completes."""
    assert create_order(client, settings, order_id="fi-crash", amount=10000).status_code == 200
    payment = get_payment(session_factory, "fi-crash")
    stub.verify_result = verify_ok_response(amount=10000, reference_id="REF-fi-crash")

    import app.services.verification as verification_module

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(verification_module, "queue_notification", crash)
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 500
    assert len(stub.verify_requests) == 1

    # The transaction rolled back atomically: no verified fact, no queue
    # state, no partial audit events for the verification.
    payment = get_payment(session_factory, "fi-crash")
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert payment.gateway_verified_at is None
    assert payment.reference_id is None
    types = event_types(get_events(session_factory, payment.id))
    assert "gateway_payment_verified" not in types
    assert "bot_notification_queued" not in types

    # Recovery: the next legitimate callback verifies again (allowed — the
    # verified fact was never recorded) and completes normally.
    monkeypatch.undo()
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    payment = get_payment(session_factory, "fi-crash")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.gateway_verified_at is not None
    assert len(stub.verify_requests) == 2


def test_duplicate_callback_cannot_enqueue_duplicate_notification(
    client, settings, session_factory, stub
):
    assert create_order(client, settings, order_id="fi-dup", amount=9000).status_code == 200
    payment = get_payment(session_factory, "fi-dup")
    stub.verify_result = verify_ok_response(amount=9000, reference_id="REF-fi-dup")

    for _ in range(3):
        assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    # Exactly one verification, one queue event, one pending delivery.
    assert len(stub.verify_requests) == 1
    with session_factory() as db:
        queued_events = db.execute(
            select(func.count(PaymentEvent.id)).where(
                PaymentEvent.payment_id == payment.id,
                PaymentEvent.event_type == "bot_notification_queued",
            )
        ).scalar_one()
    assert queued_events == 1
    payment = get_payment(session_factory, "fi-dup")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.bot_notify_attempts == 0  # queued once, not yet attempted


def test_verified_fact_commits_before_any_bot_contact(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """Boundary proof: after the callback returns, the verified+pending state
    is durable while zero bytes have been sent to the bot API."""
    assert create_order(client, settings, order_id="fi-order", amount=8000).status_code == 200
    payment = get_payment(session_factory, "fi-order")
    stub.verify_result = verify_ok_response(amount=8000, reference_id="REF-fi-order")
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    payment = get_payment(session_factory, "fi-order")
    assert payment.gateway_verified_at is not None
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert bot_stub.requests == []  # notification strictly after, via worker
