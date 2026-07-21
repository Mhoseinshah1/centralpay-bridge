"""Per-customer CentralPay payer identity isolation (incident 2026-07).

Proves the security invariants that stop cross-customer card-suggestion
leakage: two different customers can never share one gateway ``userId``, the
same customer is always stable, historical payments keep verifying against
their own snapshot, and the raw customer_id never leaks.
"""

import logging

import pytest
from sqlalchemy import func, select

from app.models import CentralPayPayerIdentity, Payment, PaymentStatus
from app.security import (
    callback_signature,
    callback_token_hash,
    generate_callback_token,
)
from app.services.payer_identity import (
    GATEWAY_USER_ID_MIN,
    GATEWAY_USER_ID_SPAN,
    customer_fingerprint,
    customer_key_hash,
    derive_gateway_user_id,
    resolve_payer_identity,
)
from tests.conftest import (
    DEFAULT_GATEWAY_USER_ID,
    TEST_CALLBACK_HMAC_SECRET,
    TEST_PAYER_ID_SECRET,
    build_app,
    create_order,
    get_events,
    get_payment,
)

CUSTOM_PAYMENT_URL = "/api/custom-payment"


def _identity_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(
            select(func.count(CentralPayPayerIdentity.id))
        ).scalar_one()


# --- derivation: determinism, isolation, stability ---------------------------


def test_derive_is_deterministic_and_in_range():
    for customer in ("custA", "custB", "۱۲۳", "a-very-long-" + "x" * 100):
        value = derive_gateway_user_id(TEST_PAYER_ID_SECRET, customer, 0)
        assert value == derive_gateway_user_id(TEST_PAYER_ID_SECRET, customer, 0)
        assert GATEWAY_USER_ID_MIN <= value <= GATEWAY_USER_ID_MIN + GATEWAY_USER_ID_SPAN - 1
        assert value > 0


def test_same_customer_resolves_to_one_stable_identity(session_factory):
    with session_factory() as db:
        first = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, customer_id="cust-1")
        second = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, customer_id="cust-1")
    assert first.gateway_user_id == second.gateway_user_id
    assert first.id == second.id
    assert _identity_count(session_factory) == 1  # exactly one mapping row


def test_different_customers_get_different_gateway_ids(session_factory):
    with session_factory() as db:
        a = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, customer_id="cust-A")
        b = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, customer_id="cust-B")
    assert a.gateway_user_id != b.gateway_user_id
    assert a.id != b.id
    assert _identity_count(session_factory) == 2


def test_reserved_gateway_user_id_is_never_assigned(session_factory):
    """The legacy shared id is excluded from the derived range: a new customer
    whose attempt-0 candidate equals it re-derives instead of sharing it."""
    victim = "cust-reserved"
    attempt0 = derive_gateway_user_id(TEST_PAYER_ID_SECRET, victim, 0)
    with session_factory() as db:
        resolved = resolve_payer_identity(
            db,
            secret=TEST_PAYER_ID_SECRET,
            customer_id=victim,
            reserved_gateway_user_id=attempt0,
        )
    assert resolved.gateway_user_id != attempt0
    assert resolved.gateway_user_id == derive_gateway_user_id(TEST_PAYER_ID_SECRET, victim, 1)


def test_collision_deterministically_re_derives(session_factory):
    """If a customer's attempt-0 id is already taken by ANOTHER customer, the
    resolver re-derives (attempt 1) — never returns the other's id, never
    fails."""
    victim = "cust-collide"
    collided_id = derive_gateway_user_id(TEST_PAYER_ID_SECRET, victim, 0)
    with session_factory() as db:
        # A pre-existing DIFFERENT customer already owns victim's attempt-0 id.
        db.add(
            CentralPayPayerIdentity(
                customer_key_hash="0" * 64,
                gateway_user_id=collided_id,
                derivation_version=1,
            )
        )
        db.commit()
        resolved = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, customer_id=victim)
    assert resolved.gateway_user_id != collided_id
    assert resolved.gateway_user_id == derive_gateway_user_id(TEST_PAYER_ID_SECRET, victim, 1)


def test_unrelated_secret_change_does_not_move_a_payer_id(settings, session_factory, stub):
    """End-to-end: changing an UNRELATED secret (the callback HMAC secret) does
    not change a customer's gateway userId — derivation uses only the dedicated
    payer secret, and the stored mapping is immutable."""
    from fastapi.testclient import TestClient

    app1 = build_app(settings, session_factory, stub)
    with TestClient(app1, raise_server_exceptions=False) as client:
        r1 = create_order(client, settings, order_id="u-1", customer_id="cust-x")
    app1.state.centralpay.close()
    assert r1.status_code == 200
    first_user_id = stub.getlink_requests[-1]["userId"]

    # Rotate an unrelated secret; the payer secret and DB are unchanged.
    rotated = settings.model_copy(
        update={"callback_hmac_secret": TEST_CALLBACK_HMAC_SECRET + "-rotated"}
    )
    stub2 = type(stub)()
    app2 = build_app(rotated, session_factory, stub2)
    with TestClient(app2, raise_server_exceptions=False) as client:
        r2 = create_order(client, rotated, order_id="u-2", customer_id="cust-x")
    app2.state.centralpay.close()
    assert r2.status_code == 200
    second_user_id = stub2.getlink_requests[-1]["userId"]

    assert second_user_id == first_user_id  # unchanged by the unrelated rotation
    assert _identity_count(session_factory) == 1  # still one mapping row


def test_fingerprint_is_short_non_reversible_and_not_the_raw_id():
    fp = customer_fingerprint(TEST_PAYER_ID_SECRET, "cust-secret-123")
    assert fp == customer_key_hash(TEST_PAYER_ID_SECRET, "cust-secret-123")[:12]
    assert len(fp) == 12
    assert "cust-secret-123" not in fp


# --- request contract: customer_id is required and strictly validated --------

_REJECTED_CUSTOMER_IDS = [
    None,  # missing/null
    123,  # not a string
    True,  # bool
    "",  # empty
    "   ",  # whitespace-only
    " cust ",  # whitespace-padded
    "cust\x00id",  # NUL
    "cust\tid",  # control
    "cust‮id",  # bidi override
    "cust​id",  # zero-width space
    "c" * 129,  # over length
]


@pytest.mark.parametrize("bad", _REJECTED_CUSTOMER_IDS)
def test_invalid_customer_id_rejected_without_side_effects(
    client, settings, session_factory, stub, bad
):
    body = {"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "cid-bad"}
    if bad is not None:
        body["customer_id"] = bad
    response = client.post(CUSTOM_PAYMENT_URL, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    with session_factory() as db:
        assert db.execute(select(func.count(Payment.id))).scalar_one() == 0
    assert _identity_count(session_factory) == 0
    assert stub.getlink_requests == []


def test_valid_unicode_customer_id_accepted(client, settings, session_factory, stub):
    # A non-ASCII but non-control opaque id is allowed (opaque upstream ids).
    response = create_order(client, settings, order_id="cid-ok", customer_id="مشتری-۷۷")
    assert response.status_code == 200


# --- end-to-end isolation on the gateway request -----------------------------


def test_two_customers_send_distinct_gateway_userid(client, settings, session_factory, stub):
    assert create_order(client, settings, order_id="o-A", customer_id="alice").status_code == 200
    assert create_order(client, settings, order_id="o-B", customer_id="bob").status_code == 200
    users = {req["userId"] for req in stub.getlink_requests}
    assert len(users) == 2  # the two customers never shared a gateway userId
    # And neither used the legacy shared id.
    assert settings.centralpay_user_id not in users


def test_same_customer_two_orders_share_gateway_userid(client, settings, session_factory, stub):
    create_order(client, settings, order_id="o-1", customer_id="same-cust")
    create_order(client, settings, order_id="o-2", customer_id="same-cust")
    users = {req["userId"] for req in stub.getlink_requests}
    assert len(users) == 1  # stable per customer
    assert _identity_count(session_factory) == 1


def test_duplicate_order_same_customer_is_idempotent(client, settings, session_factory, stub):
    first = create_order(client, settings, order_id="dup", amount=10000, customer_id="c1")
    assert first.status_code == 200
    again = create_order(client, settings, order_id="dup", amount=10000, customer_id="c1")
    assert again.status_code == 200
    assert again.json() == first.json()  # same link returned


def test_duplicate_order_different_customer_is_rejected(client, settings, session_factory, stub):
    first = create_order(client, settings, order_id="dup2", amount=10000, customer_id="owner")
    assert first.status_code == 200
    stub.getlink_requests.clear()
    intruder = create_order(client, settings, order_id="dup2", amount=10000, customer_id="intruder")
    assert intruder.status_code == 409
    assert intruder.json()["error"]["code"] == "duplicate_order_customer_mismatch"
    # The intruder never receives the owner's link, and no gateway call is made.
    assert intruder.json() != first.json()
    assert stub.getlink_requests == []
    types = [e.event_type for e in get_events(session_factory)]
    assert "duplicate_order_customer_mismatch" in types


# --- fail-closed guards ------------------------------------------------------


def test_missing_payer_secret_fails_closed(settings, session_factory, stub):
    unsafe = settings.model_copy(update={"centralpay_payer_id_secret": ""})
    app = build_app(unsafe, session_factory, stub)
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        response = create_order(client, unsafe, order_id="no-secret", customer_id="c")
    app.state.centralpay.close()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_creation_unavailable"
    with session_factory() as db:
        assert db.execute(select(func.count(Payment.id))).scalar_one() == 0
    assert _identity_count(session_factory) == 0


def test_payment_creation_disabled_returns_503(settings, session_factory, stub):
    disabled = settings.model_copy(update={"payment_creation_enabled": False})
    app = build_app(disabled, session_factory, stub)
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        response = create_order(client, disabled, order_id="disabled", customer_id="c")
    app.state.centralpay.close()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_creation_disabled"
    with session_factory() as db:
        assert db.execute(select(func.count(Payment.id))).scalar_one() == 0


# --- historical (legacy shared-id) payments still verify against snapshot -----


def test_legacy_payment_verifies_against_its_own_snapshot(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A payment created under the OLD shared payer id (payer_identity_id NULL)
    must keep verifying against its stored gateway_user_id, never the mapping
    table or the config value."""
    from datetime import UTC, datetime

    import httpx

    # A snapshot value distinct from BOTH the config value and any derived id,
    # so the test fails if verification ever consulted settings.centralpay_user_id
    # or the mapping table instead of the payment's own snapshot.
    legacy_user_id = 987654321
    assert legacy_user_id != settings.centralpay_user_id
    assert legacy_user_id != DEFAULT_GATEWAY_USER_ID
    token = generate_callback_token()
    with session_factory() as db:
        payment = Payment(
            bot_order_id="legacy-1",
            gateway_order_id=910000000001,
            gateway_user_id=legacy_user_id,
            payer_identity_id=None,  # the legacy marker
            payer_derivation_version=None,
            amount=10000,
            fee_rate_bps=0,
            fee_amount=0,
            payable_amount=10000,
            status=PaymentStatus.LINK_CREATED.value,
            redirect_url="https://gateway.test/pay/legacy",
            callback_token_hash=callback_token_hash(token),
            callback_token_issued_at=datetime.now(UTC),
        )
        db.add(payment)
        db.commit()

    # Verify reporting the payment's OWN snapshot id -> success (pending).
    stub.verify_result = httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "amount": 10000,
                "userId": legacy_user_id,
                "referenceId": "REF-legacy",
            },
        },
    )
    path = f"/api/centralpay/callback?orderId=910000000001&ct={token}&sig=" + callback_signature(
        settings.callback_hmac_secret, 910000000001, token
    )
    assert client.get(path).status_code == 200
    assert get_payment(session_factory, "legacy-1").status == PaymentStatus.BOT_NOTIFY_PENDING.value


def test_legacy_prelink_row_adopts_isolated_identity(
    client, settings, session_factory, stub
):
    """A pre-fix legacy row still awaiting its link (payer_identity_id NULL,
    shared gateway_user_id) must NOT mint a new link under the shared id: on
    the next create it adopts the requesting customer's isolated identity."""
    from datetime import UTC, datetime

    shared = settings.centralpay_user_id
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="legacy-prelink",
                gateway_order_id=910000000077,
                gateway_user_id=shared,  # the old shared id
                payer_identity_id=None,  # legacy marker, still pre-link
                payer_derivation_version=None,
                amount=10000,
                fee_rate_bps=0,
                fee_amount=0,
                payable_amount=10000,
                status=PaymentStatus.CREATED.value,
                callback_token_issued_at=datetime.now(UTC),
            )
        )
        db.commit()

    resp = create_order(client, settings, order_id="legacy-prelink", customer_id="newcust")
    assert resp.status_code == 200
    # The link was issued under the per-customer id, never the shared one.
    assert stub.getlink_requests[-1]["userId"] != shared
    payment = get_payment(session_factory, "legacy-prelink")
    assert payment.payer_identity_id is not None
    assert payment.gateway_user_id != shared
    assert stub.getlink_requests[-1]["userId"] == payment.gateway_user_id
    types = [e.event_type for e in get_events(session_factory, payment.id)]
    assert "legacy_payment_payer_identity_adopted" in types


# --- no raw customer_id in logs / events / errors ----------------------------


def test_customer_id_never_appears_in_logs_or_events(
    client, settings, session_factory, stub, caplog
):
    raw = "TOP-SECRET-CUSTOMER-9f8e7d"
    with caplog.at_level(logging.DEBUG):
        response = create_order(client, settings, order_id="leak-check", customer_id=raw)
    assert response.status_code == 200
    assert raw not in caplog.text
    # No audit event anywhere carries the raw id.
    events = get_events(session_factory)
    for event in events:
        assert raw not in str(event.data)
    # The identity-created event carries only a short fingerprint.
    identity_events = [
        e for e in events if e.event_type == "centralpay_payer_identity_created"
    ]
    assert identity_events
    assert raw not in str(identity_events[0].data)
