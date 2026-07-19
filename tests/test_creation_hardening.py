"""Payment-creation audit tests.

Strict request validation (no coercion, no control characters), crash
recovery at the getLink boundary, token/URL atomicity, and duplicate
handling for verified and manual-review payments.
"""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Payment, PaymentStatus
from tests.conftest import (
    create_order,
    event_types,
    get_events,
    get_payment,
    getlink_ok_response,
    make_verified_pending,
    run_pass,
    valid_callback_path,
    verify_ok_response,
)


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def _post(client, settings, body):
    return client.post("/api/custom-payment", json=body)


# --- strict request validation ----------------------------------------------


@pytest.mark.parametrize(
    "bad_amount",
    [
        True,  # bool is an int subtype; used to coerce to 1
        False,
        10000.0,  # integral float; used to coerce
        10000.5,
        # NOTE: a plain ASCII-decimal string ("10000") is now accepted and
        # converted by the legacy-body compatibility layer — see
        # tests/test_custom_payment_legacy_body.py. Only non-decimal strings
        # remain invalid here.
        "1e4",  # exponent notation: never a valid integer
        0,
        -100,
        1_000_000_000_001,  # above the absolute schema backstop
        [10000],
        {"value": 10000},
        None,
    ],
)
def test_invalid_amounts_rejected_without_side_effects(
    client, settings, session_factory, stub, bad_amount
):
    response = _post(
        client,
        settings,
        {"api_key": settings.inbound_api_key, "amount": bad_amount, "order_id": "amt-x"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    # Malformed requests create no payment rows, no audit rows, and no
    # gateway traffic — and never echo the api_key.
    assert settings.inbound_api_key not in response.text
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


@pytest.mark.parametrize(
    "bad_order_id",
    [
        "",
        "a\x00b",  # NUL previously reached PostgreSQL and produced a 500
        "a\nb",
        "a\tb",
        "a\x1bb",
        "a\x7fb",
        "x" * 129,  # over maximum length
        12345,  # non-string
        None,
        ["order"],
    ],
)
def test_invalid_order_ids_rejected_without_side_effects(
    client, settings, session_factory, stub, bad_order_id
):
    response = _post(
        client,
        settings,
        {"api_key": settings.inbound_api_key, "amount": 10000, "order_id": bad_order_id},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert settings.inbound_api_key not in response.text
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


def test_unicode_order_id_passed_through_byte_exact(client, settings, session_factory, stub):
    """order_id is opaque: no trimming, case-folding, or normalization."""
    order_id = "سفارش-۱۲۳ Mixed-CASE"
    assert create_order(client, settings, order_id=order_id).status_code == 200
    payment = get_payment(session_factory, order_id)
    assert payment.bot_order_id == order_id


def test_non_string_api_key_rejected_generically(client, settings, session_factory):
    response = _post(
        client, settings, {"api_key": 12345, "amount": 10000, "order_id": "k-1"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    # The generic validation error never reveals field contents.
    assert "12345" not in response.text


# --- duplicate handling for terminal states ----------------------------------


def test_create_for_verified_manual_review_order_rejected_without_state_reset(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A manual-review payment that WAS gateway-verified: verified-ness
    dominates (money moved), so the duplicate create reports
    order_already_verified and resets nothing."""
    make_verified_pending(client, settings, session_factory, stub, order_id="mr-dup")
    bot_stub.result = httpx.Response(422)
    run_pass(session_factory, notifier, settings)
    payment = get_payment(session_factory, "mr-dup")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    token_hash_before = payment.callback_token_hash

    response = create_order(client, settings, order_id="mr-dup")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_already_verified"

    payment = get_payment(session_factory, "mr-dup")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value  # not reset
    assert payment.callback_token_hash == token_hash_before  # no new link/token
    assert len(stub.getlink_requests) == 1


def test_create_for_unverified_manual_review_order_rejected_without_state_reset(
    client, settings, session_factory, stub
):
    """A manual-review payment that was NEVER verified (verify amount
    mismatch): the duplicate create reports order_under_review and must not
    reset the state or issue a new link."""
    assert create_order(client, settings, order_id="mr-unv", amount=10000).status_code == 200
    payment = get_payment(session_factory, "mr-unv")
    stub.verify_result = verify_ok_response(amount=999)  # mismatch → manual review
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    payment = get_payment(session_factory, "mr-unv")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.gateway_verified_at is None
    token_hash_before = payment.callback_token_hash

    response = create_order(client, settings, order_id="mr-unv", amount=10000)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_under_review"

    payment = get_payment(session_factory, "mr-unv")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.callback_token_hash == token_hash_before
    assert len(stub.getlink_requests) == 1


# --- crash recovery at the getLink boundary ----------------------------------


def test_crash_before_getlink_is_recoverable(client, settings, session_factory, stub):
    """An unexpected failure before the gateway call persists nothing: no
    token, no status change; the retry succeeds cleanly."""
    stub.getlink_result = RuntimeError("simulated crash before transport")
    response = create_order(client, settings, order_id="crash-pre")
    assert response.status_code == 500
    payment = get_payment(session_factory, "crash-pre")
    assert payment.status == PaymentStatus.CREATED.value
    assert payment.callback_token_hash is None  # rolled back atomically
    assert payment.redirect_url is None

    stub.getlink_result = getlink_ok_response()
    assert create_order(client, settings, order_id="crash-pre").status_code == 200
    payment = get_payment(session_factory, "crash-pre")
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert payment.callback_token_hash is not None


def test_crash_after_getlink_before_commit_is_atomic_and_recoverable(
    client, settings, session_factory, stub, monkeypatch
):
    """Crash AFTER CentralPay accepted getLink but BEFORE our commit: the
    new token and redirect URL must both be lost together (never one
    without the other), the crashed attempt's token must stay stale, and a
    retry must recover."""
    from app.audit import record_event as real_record_event

    def crash_on_link_created(db, **kwargs):
        if kwargs.get("event_type") == "payment_link_created":
            raise RuntimeError("simulated crash after getLink, before commit")
        return real_record_event(db, **kwargs)

    monkeypatch.setattr("app.services.payments.record_event", crash_on_link_created)
    response = create_order(client, settings, order_id="crash-post")
    assert response.status_code == 500
    assert len(stub.getlink_requests) == 1  # the gateway DID register a link

    # Atomicity: neither the new token hash nor the redirect URL survived.
    payment = get_payment(session_factory, "crash-post")
    assert payment.status == PaymentStatus.CREATED.value
    assert payment.callback_token_hash is None
    assert payment.redirect_url is None

    # Recovery: the retry issues a fresh token and link.
    monkeypatch.undo()
    assert create_order(client, settings, order_id="crash-post").status_code == 200
    payment = get_payment(session_factory, "crash-post")
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert len(stub.getlink_requests) == 2

    # The crashed attempt's token (visible only inside the orphaned
    # gateway invoice's returnUrl) is stale: replaying it is rejected
    # before any verify call, while the new link works.
    crashed_url = str(stub.getlink_requests[0]["returnUrl"])
    crashed_ct = parse_qs(urlsplit(crashed_url).query)["ct"][0]
    from app.security import callback_signature

    stale_sig = callback_signature(
        settings.callback_hmac_secret, payment.gateway_order_id, crashed_ct
    )
    stale = client.get(
        f"/api/centralpay/callback?orderId={payment.gateway_order_id}"
        f"&ct={crashed_ct}&sig={stale_sig}"
    )
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "invalid_callback_token"
    assert stub.verify_requests == []

    stub.verify_result = verify_ok_response(amount=10000, reference_id="REF-crash-post")
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200


def test_getlink_timeout_classified_as_connection_error(
    client, settings, session_factory, stub
):
    """Ambiguous read timeout (gateway may or may not have registered the
    invoice): explicit 502 transport code, durable getlink_failed state with
    an audit event, and the retry abandons the possibly-registered id."""
    stub.getlink_result = httpx.ReadTimeout("read timed out")
    response = create_order(client, settings, order_id="glt-1")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "centralpay_connection_error"
    payment = get_payment(session_factory, "glt-1")
    assert payment.status == PaymentStatus.GETLINK_FAILED.value
    ambiguous_gateway_id = payment.gateway_order_id
    assert "centralpay_getlink_failed" in event_types(get_events(session_factory, payment.id))

    stub.getlink_result = getlink_ok_response()
    assert create_order(client, settings, order_id="glt-1").status_code == 200
    payment = get_payment(session_factory, "glt-1")
    # The possibly-half-registered gateway id is never reused.
    assert payment.gateway_order_id != ambiguous_gateway_id
