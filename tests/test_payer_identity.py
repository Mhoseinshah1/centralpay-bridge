"""Per-identity CentralPay payer isolation (incident 2026-07).

Proves the security invariants that stop cross-user card-suggestion leakage:
two different end users can never share one gateway ``userId``, the same
Telegram user is always stable across orders, an order with no supplied
identity is isolated per order, historical payments keep verifying against
their own snapshot, and the raw Telegram id never leaks. Also proves the
upstream-bot compatibility contract: the OPTIONAL end-user identity is accepted
from any supported alias (body or query) and a missing/invalid one falls back
to per-order isolation rather than being rejected.
"""

import logging
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from starlette.datastructures import QueryParams

from app.api.payments import _coerce_telegram_id, _extract_telegram_user_id
from app.models import CentralPayPayerIdentity, Payment, PaymentStatus
from app.security import (
    callback_signature,
    callback_token_hash,
    generate_callback_token,
)
from app.services.payer_identity import (
    GATEWAY_USER_ID_MIN,
    GATEWAY_USER_ID_SPAN,
    IDENTITY_TYPE_ORDER_FALLBACK,
    IDENTITY_TYPE_TELEGRAM_USER,
    derive_gateway_user_id,
    identity_fingerprint,
    identity_key_hash,
    order_identity_key,
    resolve_payer_identity,
    telegram_identity_key,
)
from tests.conftest import (
    DEFAULT_GATEWAY_USER_ID,
    TEST_CALLBACK_HMAC_SECRET,
    TEST_PAYER_ID_SECRET,
    build_app,
    create_order,
    expected_gateway_user_id,
    get_events,
    get_payment,
)

CUSTOM_PAYMENT_URL = "/api/custom-payment"


def _identity_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(
            select(func.count(CentralPayPayerIdentity.id))
        ).scalar_one()


def _seed_order_fallback_prelink(session_factory, order_id, gateway_order_id):
    """Seed a pre-fix-shaped, pre-link order_fallback row (real order mapping,
    status CREATED, no redirect) so reconciliation on the next create can be
    observed. Returns the order-scoped identity it was seeded with."""
    with session_factory() as db:
        payer = resolve_payer_identity(
            db,
            secret=TEST_PAYER_ID_SECRET,
            identity_key=order_identity_key(order_id),
        )
        db.add(
            Payment(
                bot_order_id=order_id,
                gateway_order_id=gateway_order_id,
                gateway_user_id=payer.gateway_user_id,
                payer_identity_id=payer.id,
                payer_identity_type=IDENTITY_TYPE_ORDER_FALLBACK,
                payer_derivation_version=payer.derivation_version,
                amount=10000,
                fee_rate_bps=0,
                fee_amount=0,
                payable_amount=10000,
                status=PaymentStatus.CREATED.value,
                callback_token_issued_at=datetime.now(UTC),
            )
        )
        db.commit()
    return payer


# --- derivation: determinism, isolation, stability ---------------------------


def test_derive_is_deterministic_and_in_range():
    keys = [
        telegram_identity_key(123),
        telegram_identity_key(999999999),
        order_identity_key("ord-A"),
        order_identity_key("سفارش-۷"),
        "arbitrary-" + "x" * 100,
    ]
    for key in keys:
        value = derive_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
        assert value == derive_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
        assert GATEWAY_USER_ID_MIN <= value <= GATEWAY_USER_ID_MIN + GATEWAY_USER_ID_SPAN - 1
        assert value > 0


def test_telegram_and_order_keys_cannot_collide():
    # tg:<id> is pure digits; order:<id> carries the opaque order under a
    # different prefix, so the same textual value never yields one key.
    assert telegram_identity_key(42) != order_identity_key("42")


def test_same_identity_resolves_to_one_stable_row(session_factory):
    key = telegram_identity_key(4242001)
    with session_factory() as db:
        first = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, identity_key=key)
        second = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, identity_key=key)
    assert first.gateway_user_id == second.gateway_user_id
    assert first.id == second.id
    assert _identity_count(session_factory) == 1


def test_different_identities_get_different_gateway_ids(session_factory):
    with session_factory() as db:
        a = resolve_payer_identity(
            db, secret=TEST_PAYER_ID_SECRET, identity_key=telegram_identity_key(1001)
        )
        b = resolve_payer_identity(
            db, secret=TEST_PAYER_ID_SECRET, identity_key=telegram_identity_key(1002)
        )
    assert a.gateway_user_id != b.gateway_user_id
    assert a.id != b.id
    assert _identity_count(session_factory) == 2


def test_reserved_gateway_user_id_is_never_assigned(session_factory):
    """The legacy shared id is excluded from the derived range: a new identity
    whose attempt-0 candidate equals it re-derives instead of sharing it."""
    key = telegram_identity_key(770077)
    attempt0 = derive_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
    with session_factory() as db:
        resolved = resolve_payer_identity(
            db,
            secret=TEST_PAYER_ID_SECRET,
            identity_key=key,
            reserved_gateway_user_id=attempt0,
        )
    assert resolved.gateway_user_id != attempt0
    assert resolved.gateway_user_id == derive_gateway_user_id(TEST_PAYER_ID_SECRET, key, 1)


def test_collision_deterministically_re_derives(session_factory):
    """If an identity's attempt-0 id is already taken by ANOTHER identity, the
    resolver re-derives (attempt 1) — never returns the other's id, never
    fails."""
    key = telegram_identity_key(550055)
    collided_id = derive_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
    with session_factory() as db:
        db.add(
            CentralPayPayerIdentity(
                customer_key_hash="0" * 64,
                gateway_user_id=collided_id,
                derivation_version=1,
            )
        )
        db.commit()
        resolved = resolve_payer_identity(
            db, secret=TEST_PAYER_ID_SECRET, identity_key=key
        )
    assert resolved.gateway_user_id != collided_id
    assert resolved.gateway_user_id == derive_gateway_user_id(TEST_PAYER_ID_SECRET, key, 1)


def test_unrelated_secret_change_does_not_move_a_payer_id(settings, session_factory, stub):
    """End-to-end: changing an UNRELATED secret (the callback HMAC secret) does
    not change an end user's gateway userId — derivation uses only the dedicated
    payer secret, and the stored mapping is immutable."""
    from fastapi.testclient import TestClient

    app1 = build_app(settings, session_factory, stub)
    with TestClient(app1, raise_server_exceptions=False) as client:
        r1 = create_order(client, settings, order_id="u-1", telegram_user_id=313131)
    app1.state.centralpay.close()
    assert r1.status_code == 200
    first_user_id = stub.getlink_requests[-1]["userId"]

    rotated = settings.model_copy(
        update={"callback_hmac_secret": TEST_CALLBACK_HMAC_SECRET + "-rotated"}
    )
    stub2 = type(stub)()
    app2 = build_app(rotated, session_factory, stub2)
    with TestClient(app2, raise_server_exceptions=False) as client:
        r2 = create_order(client, rotated, order_id="u-2", telegram_user_id=313131)
    app2.state.centralpay.close()
    assert r2.status_code == 200
    second_user_id = stub2.getlink_requests[-1]["userId"]

    assert second_user_id == first_user_id
    assert _identity_count(session_factory) == 1


def test_fingerprint_is_short_non_reversible_and_not_the_raw_id():
    key = telegram_identity_key(778899)
    fp = identity_fingerprint(TEST_PAYER_ID_SECRET, key)
    assert fp == identity_key_hash(TEST_PAYER_ID_SECRET, key)[:12]
    assert len(fp) == 12
    assert "778899" not in fp


# --- alias parsing / coercion unit tests -------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (123456, 123456),
        ("123456", 123456),
        ("  123456  ", 123456),
        (1, 1),
        (2**63 - 1, 2**63 - 1),
        (True, None),  # bool is never an id (would coerce True -> 1)
        (False, None),
        (0, None),
        (-5, None),
        ("-5", None),
        ("0", None),
        ("12.5", None),
        ("abc", None),
        ("۱۲۳", None),  # non-ASCII digits
        ("", None),
        (2**63, None),  # out of int64 range
        (None, None),
        ([123], None),
        ({"x": 1}, None),
    ],
)
def test_coerce_telegram_id(value, expected):
    assert _coerce_telegram_id(value) == expected


def test_extract_prefers_body_then_query_in_alias_order():
    empty = QueryParams("")
    # Body alias precedence: user_id before the later aliases.
    assert (
        _extract_telegram_user_id({"user_id": 111, "telegram_id": 222}, empty) == 111
    )
    assert _extract_telegram_user_id({"chat_id": 333}, empty) == 333
    # An invalid body alias is skipped, and a later valid one wins.
    assert _extract_telegram_user_id({"user_id": 0, "uid": 444}, empty) == 444
    # Body wins over query.
    assert (
        _extract_telegram_user_id({"user_id": 555}, QueryParams("userId=666")) == 555
    )
    # No body identity -> query is consulted.
    assert _extract_telegram_user_id({}, QueryParams("telegram_id=777")) == 777
    # Nothing usable anywhere -> None (order fallback).
    assert _extract_telegram_user_id({"user_id": "nope"}, QueryParams("uid=bad")) is None


# --- request contract: 3 required fields, optional identity aliases ----------


def test_three_field_request_uses_order_fallback(client, settings, session_factory, stub):
    resp = create_order(client, settings, order_id="plain-3", telegram_user_id=None)
    assert resp.status_code == 200
    payment = get_payment(session_factory, "plain-3")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK
    assert payment.gateway_user_id == expected_gateway_user_id(order_id="plain-3")
    assert stub.getlink_requests[-1]["userId"] == payment.gateway_user_id
    assert payment.gateway_user_id != settings.centralpay_user_id


@pytest.mark.parametrize("alias", ["user_id", "userId", "uid", "chat_id", "telegram_id"])
def test_each_alias_maps_to_the_same_telegram_identity(
    client, settings, session_factory, stub, alias
):
    tg_id = 606060
    resp = create_order(
        client, settings, order_id=f"alias-{alias}", telegram_user_id=tg_id, identity_alias=alias
    )
    assert resp.status_code == 200
    payment = get_payment(session_factory, f"alias-{alias}")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=tg_id)
    assert stub.getlink_requests[-1]["userId"] == payment.gateway_user_id


def test_identity_alias_from_query_string(client, settings, session_factory, stub):
    tg_id = 818181
    resp = client.post(
        f"{CUSTOM_PAYMENT_URL}?user_id={tg_id}",
        json={"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "q-1"},
    )
    assert resp.status_code == 200
    payment = get_payment(session_factory, "q-1")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=tg_id)


@pytest.mark.parametrize("bad", [0, -1, "abc", "۱۲۳", "12.5", True, ""])
def test_invalid_identity_falls_back_to_order_isolation(
    client, settings, session_factory, stub, bad
):
    """An invalid/unusable identity alias is IGNORED (never a 4xx): the payment
    is created and isolated per order instead."""
    body = {
        "api_key": settings.inbound_api_key,
        "amount": 10000,
        "order_id": "bad-id",
        "user_id": bad,
    }
    resp = client.post(CUSTOM_PAYMENT_URL, json=body)
    assert resp.status_code == 200
    payment = get_payment(session_factory, "bad-id")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK
    assert payment.gateway_user_id == expected_gateway_user_id(order_id="bad-id")


# --- end-to-end isolation on the gateway request -----------------------------


def test_two_telegram_users_send_distinct_gateway_userid(
    client, settings, session_factory, stub
):
    assert create_order(client, settings, order_id="o-A", telegram_user_id=111).status_code == 200
    assert create_order(client, settings, order_id="o-B", telegram_user_id=222).status_code == 200
    users = {req["userId"] for req in stub.getlink_requests}
    assert len(users) == 2
    assert settings.centralpay_user_id not in users


def test_same_telegram_user_two_orders_share_gateway_userid(
    client, settings, session_factory, stub
):
    create_order(client, settings, order_id="o-1", telegram_user_id=424242)
    create_order(client, settings, order_id="o-2", telegram_user_id=424242)
    users = {req["userId"] for req in stub.getlink_requests}
    assert len(users) == 1
    assert _identity_count(session_factory) == 1


def test_two_orders_without_identity_are_isolated_per_order(
    client, settings, session_factory, stub
):
    create_order(client, settings, order_id="f-1", telegram_user_id=None)
    create_order(client, settings, order_id="f-2", telegram_user_id=None)
    users = {req["userId"] for req in stub.getlink_requests}
    assert len(users) == 2  # each order isolated
    assert settings.centralpay_user_id not in users


# --- idempotency and per-order reconciliation (incident 2026-07, item 9) ------


def test_same_order_retry_without_identity_is_stable(
    client, settings, session_factory, stub
):
    first = create_order(client, settings, order_id="retry", telegram_user_id=None)
    again = create_order(client, settings, order_id="retry", telegram_user_id=None)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert _identity_count(session_factory) == 1


def test_same_order_same_telegram_user_is_idempotent(
    client, settings, session_factory, stub
):
    first = create_order(client, settings, order_id="dup", amount=10000, telegram_user_id=9001)
    again = create_order(client, settings, order_id="dup", amount=10000, telegram_user_id=9001)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()


def test_duplicate_order_different_telegram_user_is_rejected(
    client, settings, session_factory, stub
):
    first = create_order(client, settings, order_id="dup2", amount=10000, telegram_user_id=1111)
    assert first.status_code == 200
    stub.getlink_requests.clear()
    intruder = create_order(client, settings, order_id="dup2", amount=10000, telegram_user_id=2222)
    assert intruder.status_code == 409
    assert intruder.json()["error"]["code"] == "duplicate_order_customer_mismatch"
    assert intruder.json() != first.json()
    assert stub.getlink_requests == []  # no gateway call for the intruder
    types = [e.event_type for e in get_events(session_factory)]
    assert "duplicate_order_customer_mismatch" in types


def test_order_fallback_prelink_adopts_arriving_telegram_identity(
    client, settings, session_factory, stub
):
    """No live link yet + a real Telegram user now appears: deterministically
    adopt the isolated Telegram identity (safe upgrade), issuing the link under
    it — never the order-scoped id and never the shared one."""
    order_payer = _seed_order_fallback_prelink(session_factory, "up-1", 910000001001)

    resp = create_order(client, settings, order_id="up-1", telegram_user_id=525252)
    assert resp.status_code == 200
    payment = get_payment(session_factory, "up-1")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=525252)
    assert payment.gateway_user_id != order_payer.gateway_user_id
    assert stub.getlink_requests[-1]["userId"] == payment.gateway_user_id
    types = [e.event_type for e in get_events(session_factory, payment.id)]
    assert "payment_payer_identity_adopted" in types


def test_live_order_link_is_not_switched_when_telegram_arrives(
    client, settings, session_factory, stub
):
    """First created without identity and a link WAS issued: a later retry that
    now carries a Telegram id must NOT silently re-point the live link. The
    order-scoped link (already isolated) is returned unchanged."""
    first = create_order(client, settings, order_id="olink", telegram_user_id=None)
    assert first.status_code == 200
    order_user_id = stub.getlink_requests[-1]["userId"]
    calls_before = len(stub.getlink_requests)

    again = create_order(client, settings, order_id="olink", telegram_user_id=707070)
    assert again.status_code == 200
    assert again.json() == first.json()  # same live link, no switch
    assert len(stub.getlink_requests) == calls_before  # no new gateway call
    payment = get_payment(session_factory, "olink")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK
    assert payment.gateway_user_id == order_user_id


def test_telegram_retry_dropping_optional_id_keeps_the_identity(
    client, settings, session_factory, stub
):
    """A Telegram user's order retried WITHOUT the optional id keeps its
    Telegram identity — never downgraded to per-order, never a new link."""
    first = create_order(client, settings, order_id="keep", telegram_user_id=616161)
    assert first.status_code == 200
    tg_user_id = stub.getlink_requests[-1]["userId"]
    calls_before = len(stub.getlink_requests)

    again = create_order(client, settings, order_id="keep", telegram_user_id=None)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(stub.getlink_requests) == calls_before
    payment = get_payment(session_factory, "keep")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == tg_user_id


# --- fail-closed guards ------------------------------------------------------


def test_missing_payer_secret_fails_closed(settings, session_factory, stub):
    unsafe = settings.model_copy(update={"centralpay_payer_id_secret": ""})
    app = build_app(unsafe, session_factory, stub)
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        response = create_order(client, unsafe, order_id="no-secret", telegram_user_id=1)
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
        response = create_order(client, disabled, order_id="disabled", telegram_user_id=1)
    app.state.centralpay.close()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_creation_disabled"
    with session_factory() as db:
        assert db.execute(select(func.count(Payment.id))).scalar_one() == 0


def test_no_new_link_ever_uses_the_legacy_shared_id(client, settings, session_factory, stub):
    """Across every path — telegram user and order fallback — the shared legacy
    CENTRALPAY_USER_ID is never sent to the gateway for a new link."""
    create_order(client, settings, order_id="s-1", telegram_user_id=4040)
    create_order(client, settings, order_id="s-2", telegram_user_id=None)
    users = {req["userId"] for req in stub.getlink_requests}
    assert settings.centralpay_user_id not in users


# --- historical (legacy shared-id) payments still verify against snapshot -----


def test_legacy_payment_verifies_against_its_own_snapshot(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A payment created under the OLD shared payer id (payer_identity_id NULL)
    must keep verifying against its stored gateway_user_id, never the mapping
    table or the config value."""
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
            payer_identity_type=None,
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
    the next create it adopts the requesting user's isolated identity."""
    shared = settings.centralpay_user_id
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="legacy-prelink",
                gateway_order_id=910000000077,
                gateway_user_id=shared,  # the old shared id
                payer_identity_id=None,  # legacy marker, still pre-link
                payer_identity_type=None,
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

    resp = create_order(client, settings, order_id="legacy-prelink", telegram_user_id=1414)
    assert resp.status_code == 200
    assert stub.getlink_requests[-1]["userId"] != shared
    payment = get_payment(session_factory, "legacy-prelink")
    assert payment.payer_identity_id is not None
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id != shared
    assert stub.getlink_requests[-1]["userId"] == payment.gateway_user_id
    types = [e.event_type for e in get_events(session_factory, payment.id)]
    assert "payment_payer_identity_adopted" in types


# --- no raw Telegram id in logs / events / errors ----------------------------


def test_raw_telegram_id_never_appears_in_logs_or_events(
    client, settings, session_factory, stub, caplog
):
    # A 16-digit id: larger than any 12-digit gateway_order_id or 10-digit
    # gateway_user_id, so it can never appear as an incidental substring.
    raw = 8123456789012345
    with caplog.at_level(logging.DEBUG):
        response = create_order(client, settings, order_id="leak-check", telegram_user_id=raw)
    assert response.status_code == 200
    assert str(raw) not in caplog.text
    events = get_events(session_factory)
    for event in events:
        assert str(raw) not in str(event.data)
    identity_events = [
        e for e in events if e.event_type == "centralpay_payer_identity_created"
    ]
    assert identity_events
    assert str(raw) not in str(identity_events[0].data)
