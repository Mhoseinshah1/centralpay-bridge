"""Per-identity CentralPay payer isolation (incident 2026-07, raw-id revision).

Proves the current identity contract:

* ``telegram_raw_v1`` — a valid Telegram id (any supported alias, any body
  format or query) makes the gateway ``userId`` the EXACT Telegram number:
  no hashing, remapping, truncation, modulo, or alternate allocation.
* ``order_hmac_v1`` — no usable identity derives a stable per-order id inside
  the reserved fallback range, disjoint from every valid Telegram id.
* Historical HMAC rows (customer-era, v1 tg/order) stay immutable: retries
  reuse stored snapshots, live links are never re-pointed, callbacks keep
  verifying.
* Collisions fail closed: a Telegram id whose numeric value is already owned
  by another mapping (or equals the legacy shared id) is never remapped and
  never handed someone else's mapping.
* The legacy shared CENTRALPAY_USER_ID is never used for a new link, and the
  raw Telegram id never appears in logs or audit events.
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
    DERIVATION_VERSION,
    IDENTITY_SCHEME_HISTORICAL_HMAC,
    IDENTITY_SCHEME_ORDER_HMAC,
    IDENTITY_SCHEME_TELEGRAM_RAW,
    IDENTITY_TYPE_ORDER_FALLBACK,
    IDENTITY_TYPE_TELEGRAM_USER,
    MAX_TELEGRAM_USER_ID,
    ORDER_FALLBACK_MIN,
    ORDER_FALLBACK_SPAN,
    derive_order_gateway_user_id,
    historical_identity_key_hash,
    identity_fingerprint,
    identity_key_hash,
    order_identity_key,
    resolve_payer_identity,
    telegram_identity_key,
)
from tests.conftest import (
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


def _seed_mapping(session_factory, *, key_hash, gateway_user_id, scheme, version=1):
    with session_factory() as db:
        row = CentralPayPayerIdentity(
            customer_key_hash=key_hash,
            gateway_user_id=gateway_user_id,
            derivation_version=version,
            identity_scheme=scheme,
        )
        db.add(row)
        db.commit()
        return row.id


def _seed_payment(
    session_factory,
    *,
    order_id,
    gateway_order_id,
    gateway_user_id,
    payer_identity_id,
    payer_identity_type,
    derivation_version,
    linked,
):
    token = generate_callback_token()
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id=order_id,
                gateway_order_id=gateway_order_id,
                gateway_user_id=gateway_user_id,
                payer_identity_id=payer_identity_id,
                payer_identity_type=payer_identity_type,
                payer_derivation_version=derivation_version,
                amount=10000,
                fee_rate_bps=0,
                fee_amount=0,
                payable_amount=10000,
                status=(
                    PaymentStatus.LINK_CREATED.value if linked else PaymentStatus.CREATED.value
                ),
                redirect_url=f"https://gateway.test/pay/{order_id}" if linked else None,
                callback_token_hash=callback_token_hash(token) if linked else None,
                callback_token_issued_at=datetime.now(UTC),
            )
        )
        db.commit()
    return token


def _seed_v1_telegram_row(session_factory, order_id, gateway_order_id, tg_id, *, linked):
    """A row exactly as the retired v1 tg-HMAC scheme wrote it: mapping keyed
    by the v1 hash of ``tg:<id>``, an HMAC-derived (NOT raw) gateway id, and a
    typed payment snapshot."""
    hmac_gateway_id = 1_555_000_000 + gateway_order_id % 1000  # old-range value
    mapping_id = _seed_mapping(
        session_factory,
        key_hash=historical_identity_key_hash(
            TEST_PAYER_ID_SECRET, telegram_identity_key(tg_id)
        ),
        gateway_user_id=hmac_gateway_id,
        scheme=IDENTITY_SCHEME_HISTORICAL_HMAC,
    )
    token = _seed_payment(
        session_factory,
        order_id=order_id,
        gateway_order_id=gateway_order_id,
        gateway_user_id=hmac_gateway_id,
        payer_identity_id=mapping_id,
        payer_identity_type=IDENTITY_TYPE_TELEGRAM_USER,
        derivation_version=1,
        linked=linked,
    )
    return hmac_gateway_id, token


# --- schemes and numeric namespaces ------------------------------------------


def test_namespaces_are_disjoint_by_construction():
    assert ORDER_FALLBACK_MIN > MAX_TELEGRAM_USER_ID
    assert ORDER_FALLBACK_MIN + ORDER_FALLBACK_SPAN < 2**63
    assert MAX_TELEGRAM_USER_ID == 2**52 - 1  # documented Bot API bound


def test_order_derivation_is_deterministic_and_in_reserved_range():
    for key in (order_identity_key("ord-A"), order_identity_key("سفارش-۷")):
        value = derive_order_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
        assert value == derive_order_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
        assert ORDER_FALLBACK_MIN <= value < ORDER_FALLBACK_MIN + ORDER_FALLBACK_SPAN
        assert value > MAX_TELEGRAM_USER_ID  # can never equal a Telegram id


def test_current_and_historical_key_domains_differ():
    key = telegram_identity_key(123456789)
    assert identity_key_hash(TEST_PAYER_ID_SECRET, key) != historical_identity_key_hash(
        TEST_PAYER_ID_SECRET, key
    )


def test_telegram_resolution_is_the_exact_raw_id(session_factory):
    with session_factory() as db:
        payer = resolve_payer_identity(
            db,
            secret=TEST_PAYER_ID_SECRET,
            identity_key=telegram_identity_key(123456789),
            telegram_user_id=123456789,
        )
    assert payer.gateway_user_id == 123456789  # exact — never derived
    assert payer.scheme == IDENTITY_SCHEME_TELEGRAM_RAW
    assert payer.derivation_version == DERIVATION_VERSION


def test_order_resolution_lands_in_reserved_range(session_factory):
    key = order_identity_key("ord-9")
    with session_factory() as db:
        payer = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, identity_key=key)
    assert payer.gateway_user_id == derive_order_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
    assert payer.gateway_user_id >= ORDER_FALLBACK_MIN
    assert payer.scheme == IDENTITY_SCHEME_ORDER_HMAC


def test_same_identity_resolves_to_one_stable_row(session_factory):
    key = telegram_identity_key(4242001)
    with session_factory() as db:
        first = resolve_payer_identity(
            db, secret=TEST_PAYER_ID_SECRET, identity_key=key, telegram_user_id=4242001
        )
        second = resolve_payer_identity(
            db, secret=TEST_PAYER_ID_SECRET, identity_key=key, telegram_user_id=4242001
        )
    assert first.gateway_user_id == second.gateway_user_id == 4242001
    assert first.id == second.id
    assert _identity_count(session_factory) == 1


def test_reserved_legacy_id_skipped_for_order_derivation(session_factory):
    """If an order's attempt-0 candidate were the legacy shared id, the
    resolver re-derives instead of sharing it."""
    key = order_identity_key("ord-reserved")
    attempt0 = derive_order_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
    with session_factory() as db:
        resolved = resolve_payer_identity(
            db,
            secret=TEST_PAYER_ID_SECRET,
            identity_key=key,
            reserved_gateway_user_id=attempt0,
        )
    assert resolved.gateway_user_id != attempt0
    assert resolved.gateway_user_id == derive_order_gateway_user_id(
        TEST_PAYER_ID_SECRET, key, 1
    )


def test_order_collision_deterministically_re_derives(session_factory):
    """An order candidate already taken by ANOTHER identity re-derives —
    never returns the other identity's id, never fails."""
    key = order_identity_key("ord-collide")
    collided = derive_order_gateway_user_id(TEST_PAYER_ID_SECRET, key, 0)
    _seed_mapping(
        session_factory,
        key_hash="0" * 64,
        gateway_user_id=collided,
        scheme=IDENTITY_SCHEME_ORDER_HMAC,
        version=2,
    )
    with session_factory() as db:
        resolved = resolve_payer_identity(db, secret=TEST_PAYER_ID_SECRET, identity_key=key)
    assert resolved.gateway_user_id != collided
    assert resolved.gateway_user_id == derive_order_gateway_user_id(
        TEST_PAYER_ID_SECRET, key, 1
    )


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
        (2**52 - 1, 2**52 - 1),  # the documented Bot API maximum
        (2**52, None),  # beyond it: not a usable Telegram identity
        (2**63 - 1, None),
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
        (None, None),
        ([123], None),
        ({"x": 1}, None),
    ],
)
def test_coerce_telegram_id(value, expected):
    assert _coerce_telegram_id(value) == expected


def test_extract_prefers_body_then_query_in_alias_order():
    empty = QueryParams("")
    assert (
        _extract_telegram_user_id({"user_id": 111, "telegram_id": 222}, empty) == 111
    )
    assert _extract_telegram_user_id({"chat_id": 333}, empty) == 333
    assert _extract_telegram_user_id({"user_id": 0, "uid": 444}, empty) == 444
    assert (
        _extract_telegram_user_id({"user_id": 555}, QueryParams("userId=666")) == 555
    )
    assert _extract_telegram_user_id({}, QueryParams("telegram_id=777")) == 777
    assert _extract_telegram_user_id({"user_id": "nope"}, QueryParams("uid=bad")) is None


# --- the exact CentralPay payload (product requirement) ----------------------


def test_getlink_payload_carries_the_exact_telegram_id(
    client, settings, session_factory, stub
):
    """The verbatim required example: user_id 123456789 must reach CentralPay
    as userId 123456789 — the same exact integer, stored unchanged."""
    response = client.post(
        CUSTOM_PAYMENT_URL,
        json={
            "api_key": settings.inbound_api_key,
            "amount": 50000,
            "order_id": "order-123",
            "user_id": 123456789,
        },
    )
    assert response.status_code == 200
    [request] = stub.getlink_requests
    assert request["userId"] == 123456789
    payment = get_payment(session_factory, "order-123")
    assert payment.gateway_user_id == 123456789
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.payer_derivation_version == DERIVATION_VERSION


@pytest.mark.parametrize("alias", ["user_id", "userId", "uid", "chat_id", "telegram_id"])
def test_each_alias_sends_the_exact_telegram_id(
    client, settings, session_factory, stub, alias
):
    tg_id = 606060
    resp = create_order(
        client, settings, order_id=f"alias-{alias}", telegram_user_id=tg_id, identity_alias=alias
    )
    assert resp.status_code == 200
    assert stub.getlink_requests[-1]["userId"] == tg_id  # the exact integer
    payment = get_payment(session_factory, f"alias-{alias}")
    assert payment.gateway_user_id == tg_id
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER


def test_identity_alias_from_query_string(client, settings, session_factory, stub):
    tg_id = 818181
    resp = client.post(
        f"{CUSTOM_PAYMENT_URL}?user_id={tg_id}",
        json={"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "q-1"},
    )
    assert resp.status_code == 200
    assert stub.getlink_requests[-1]["userId"] == tg_id
    assert get_payment(session_factory, "q-1").gateway_user_id == tg_id


def test_three_field_request_uses_reserved_order_fallback(
    client, settings, session_factory, stub
):
    resp = create_order(client, settings, order_id="plain-3", telegram_user_id=None)
    assert resp.status_code == 200
    payment = get_payment(session_factory, "plain-3")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK
    assert payment.gateway_user_id == expected_gateway_user_id(order_id="plain-3")
    assert payment.gateway_user_id >= ORDER_FALLBACK_MIN  # reserved range
    assert stub.getlink_requests[-1]["userId"] == payment.gateway_user_id
    assert payment.gateway_user_id != settings.centralpay_user_id


@pytest.mark.parametrize("bad", [0, -1, "abc", "۱۲۳", "12.5", True, "", 2**52])
def test_invalid_identity_falls_back_to_order_isolation(
    client, settings, session_factory, stub, bad
):
    """An invalid/unusable identity alias (including one beyond the documented
    Telegram range) is IGNORED (never a 4xx): the payment is created and
    isolated per order instead."""
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


def test_two_telegram_users_send_their_own_exact_ids(
    client, settings, session_factory, stub
):
    a = create_order(client, settings, order_id="o-A", telegram_user_id=111222)
    b = create_order(client, settings, order_id="o-B", telegram_user_id=333444)
    assert a.status_code == b.status_code == 200
    users = [req["userId"] for req in stub.getlink_requests]
    assert users == [111222, 333444]  # each user's own exact id
    assert settings.centralpay_user_id not in users


def test_same_telegram_user_two_orders_same_exact_id(
    client, settings, session_factory, stub
):
    create_order(client, settings, order_id="o-1", telegram_user_id=424242)
    create_order(client, settings, order_id="o-2", telegram_user_id=424242)
    users = {req["userId"] for req in stub.getlink_requests}
    assert users == {424242}
    assert _identity_count(session_factory) == 1


def test_two_orders_without_identity_are_isolated_per_order(
    client, settings, session_factory, stub
):
    create_order(client, settings, order_id="f-1", telegram_user_id=None)
    create_order(client, settings, order_id="f-2", telegram_user_id=None)
    users = {req["userId"] for req in stub.getlink_requests}
    assert len(users) == 2  # each order isolated
    assert all(u >= ORDER_FALLBACK_MIN for u in users)
    assert settings.centralpay_user_id not in users


def test_no_new_link_ever_uses_the_legacy_shared_id(client, settings, session_factory, stub):
    create_order(client, settings, order_id="s-1", telegram_user_id=4040)
    create_order(client, settings, order_id="s-2", telegram_user_id=None)
    users = {req["userId"] for req in stub.getlink_requests}
    assert settings.centralpay_user_id not in users


def test_unrelated_secret_change_does_not_move_a_payer_id(settings, session_factory, stub):
    """Changing an UNRELATED secret (the callback HMAC secret) does not change
    an end user's gateway userId."""
    from fastapi.testclient import TestClient

    app1 = build_app(settings, session_factory, stub)
    with TestClient(app1, raise_server_exceptions=False) as client:
        r1 = create_order(client, settings, order_id="u-1", telegram_user_id=313131)
    app1.state.centralpay.close()
    assert r1.status_code == 200

    rotated = settings.model_copy(
        update={"callback_hmac_secret": TEST_CALLBACK_HMAC_SECRET + "-rotated"}
    )
    stub2 = type(stub)()
    app2 = build_app(rotated, session_factory, stub2)
    with TestClient(app2, raise_server_exceptions=False) as client:
        r2 = create_order(client, rotated, order_id="u-2", telegram_user_id=313131)
    app2.state.centralpay.close()
    assert r2.status_code == 200

    assert stub.getlink_requests[-1]["userId"] == 313131
    assert stub2.getlink_requests[-1]["userId"] == 313131
    assert _identity_count(session_factory) == 1


# --- idempotency and per-order reconciliation --------------------------------


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
    payment = get_payment(session_factory, "dup2")
    assert payment.gateway_user_id == 1111  # the owner's exact id, untouched
    types = [e.event_type for e in get_events(session_factory)]
    assert "duplicate_order_customer_mismatch" in types


def test_order_fallback_prelink_adopts_arriving_telegram_identity(
    client, settings, session_factory, stub
):
    """No live link yet + a real Telegram user now appears: adopt the exact
    Telegram id (safe upgrade), never the order-scoped or shared one."""
    order_key = order_identity_key("up-1")
    order_id_value = derive_order_gateway_user_id(TEST_PAYER_ID_SECRET, order_key, 0)
    mapping_id = _seed_mapping(
        session_factory,
        key_hash=identity_key_hash(TEST_PAYER_ID_SECRET, order_key),
        gateway_user_id=order_id_value,
        scheme=IDENTITY_SCHEME_ORDER_HMAC,
        version=2,
    )
    _seed_payment(
        session_factory,
        order_id="up-1",
        gateway_order_id=910000001001,
        gateway_user_id=order_id_value,
        payer_identity_id=mapping_id,
        payer_identity_type=IDENTITY_TYPE_ORDER_FALLBACK,
        derivation_version=2,
        linked=False,
    )

    resp = create_order(client, settings, order_id="up-1", telegram_user_id=525252)
    assert resp.status_code == 200
    payment = get_payment(session_factory, "up-1")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == 525252  # the exact id
    assert stub.getlink_requests[-1]["userId"] == 525252
    types = [e.event_type for e in get_events(session_factory, payment.id)]
    assert "payment_payer_identity_adopted" in types


def test_live_order_link_is_not_switched_when_telegram_arrives(
    client, settings, session_factory, stub
):
    first = create_order(client, settings, order_id="olink", telegram_user_id=None)
    assert first.status_code == 200
    order_user_id = stub.getlink_requests[-1]["userId"]
    calls_before = len(stub.getlink_requests)

    again = create_order(client, settings, order_id="olink", telegram_user_id=707070)
    assert again.status_code == 200
    assert again.json() == first.json()  # same live link, no switch
    assert len(stub.getlink_requests) == calls_before
    payment = get_payment(session_factory, "olink")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK
    assert payment.gateway_user_id == order_user_id


def test_telegram_retry_dropping_optional_id_keeps_the_identity(
    client, settings, session_factory, stub
):
    first = create_order(client, settings, order_id="keep", telegram_user_id=616161)
    assert first.status_code == 200
    calls_before = len(stub.getlink_requests)

    again = create_order(client, settings, order_id="keep", telegram_user_id=None)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(stub.getlink_requests) == calls_before
    payment = get_payment(session_factory, "keep")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == 616161


# --- collisions fail closed --------------------------------------------------


def test_telegram_id_occupied_by_historical_mapping_fails_closed(
    client, settings, session_factory, stub
):
    """A historical HMAC mapping already owns the numeric value of a real
    Telegram id: the user is never remapped to another number and never handed
    the existing mapping — creation fails closed with an actionable event."""
    tg_id = 1_812_262_739  # a plausible real id inside the old derived range
    occupied_id = _seed_mapping(
        session_factory,
        key_hash="a" * 64,  # some other identity's retired-scheme hash
        gateway_user_id=tg_id,
        scheme=IDENTITY_SCHEME_HISTORICAL_HMAC,
    )
    resp = create_order(client, settings, order_id="coll-1", telegram_user_id=tg_id)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "payer_identity_conflict"
    assert stub.getlink_requests == []  # no link under any substitute id
    with session_factory() as db:
        # No payment row was created and the occupied mapping is untouched.
        assert db.execute(select(func.count(Payment.id))).scalar_one() == 0
        row = db.get(CentralPayPayerIdentity, occupied_id)
        assert row is not None and row.customer_key_hash == "a" * 64
    events = [e for e in get_events(session_factory) if e.event_type == "payer_identity_collision"]
    assert len(events) == 1
    data = events[0].data
    assert data is not None
    assert data["occupied_by_payer_identity_id"] == occupied_id
    assert str(tg_id) not in str(data)  # no raw id in the event


def test_telegram_id_equal_to_legacy_shared_id_fails_closed(
    client, settings, session_factory, stub
):
    """A user whose real Telegram id equals the legacy SHARED id must not be
    sent under it (that would attach the shared multi-user card history)."""
    resp = create_order(
        client, settings, order_id="coll-2", telegram_user_id=settings.centralpay_user_id
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "payer_identity_conflict"
    assert stub.getlink_requests == []
    events = [e for e in get_events(session_factory) if e.event_type == "payer_identity_collision"]
    assert len(events) == 1
    data = events[0].data
    assert data is not None
    assert data["occupied_by_reserved_legacy_id"] is True


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


# --- historical HMAC rows: immutable, retries reuse stored snapshots ----------


def test_historical_v1_telegram_row_with_link_reuses_stored_snapshot(
    client, settings, session_factory, stub
):
    """A live link created under the retired v1 tg-HMAC derivation is returned
    unchanged when the SAME user retries — never re-pointed to the raw id and
    no new gateway call."""
    tg_id = 909111
    hmac_gateway_id, _ = _seed_v1_telegram_row(
        session_factory, "v1-live", 910000000201, tg_id, linked=True
    )
    resp = create_order(client, settings, order_id="v1-live", telegram_user_id=tg_id)
    assert resp.status_code == 200
    assert resp.json() == {"url": "https://gateway.test/pay/v1-live"}
    assert stub.getlink_requests == []
    payment = get_payment(session_factory, "v1-live")
    assert payment.gateway_user_id == hmac_gateway_id  # stored snapshot, not tg_id
    assert payment.payer_derivation_version == 1
    assert _identity_count(session_factory) == 1  # no new mapping allocated


def test_historical_v1_telegram_row_prelink_retry_reuses_stored_snapshot(
    client, settings, session_factory, stub
):
    """Even before a link exists, a same-user retry of a v1 row reuses the
    stored HMAC snapshot (retries reuse stored snapshots — history is never
    re-derived), issuing the link under it."""
    tg_id = 909222
    hmac_gateway_id, _ = _seed_v1_telegram_row(
        session_factory, "v1-pre", 910000000202, tg_id, linked=False
    )
    resp = create_order(client, settings, order_id="v1-pre", telegram_user_id=tg_id)
    assert resp.status_code == 200
    assert stub.getlink_requests[-1]["userId"] == hmac_gateway_id
    payment = get_payment(session_factory, "v1-pre")
    assert payment.gateway_user_id == hmac_gateway_id
    assert payment.payer_derivation_version == 1
    assert _identity_count(session_factory) == 1


def test_historical_v1_telegram_row_rejects_a_different_user(
    client, settings, session_factory, stub
):
    tg_id = 909333
    _seed_v1_telegram_row(session_factory, "v1-guard", 910000000203, tg_id, linked=True)
    intruder = create_order(client, settings, order_id="v1-guard", telegram_user_id=404404)
    assert intruder.status_code == 409
    assert intruder.json()["error"]["code"] == "duplicate_order_customer_mismatch"
    assert stub.getlink_requests == []


def test_historical_v1_order_row_retry_reuses_stored_snapshot(
    client, settings, session_factory, stub
):
    """A v1 order-HMAC row (old low-range id) retried without identity reuses
    its stored snapshot — never re-derived into the new reserved range."""
    order_key = order_identity_key("v1-order")
    old_id = 1_777_000_123  # old-range derived value
    mapping_id = _seed_mapping(
        session_factory,
        key_hash=historical_identity_key_hash(TEST_PAYER_ID_SECRET, order_key),
        gateway_user_id=old_id,
        scheme=IDENTITY_SCHEME_HISTORICAL_HMAC,
    )
    _seed_payment(
        session_factory,
        order_id="v1-order",
        gateway_order_id=910000000204,
        gateway_user_id=old_id,
        payer_identity_id=mapping_id,
        payer_identity_type=IDENTITY_TYPE_ORDER_FALLBACK,
        derivation_version=1,
        linked=False,
    )
    resp = create_order(client, settings, order_id="v1-order", telegram_user_id=None)
    assert resp.status_code == 200
    assert stub.getlink_requests[-1]["userId"] == old_id
    assert get_payment(session_factory, "v1-order").gateway_user_id == old_id
    assert _identity_count(session_factory) == 1


# --- legacy shared-id and customer-era rows (unchanged behavior) --------------


def test_legacy_payment_verifies_against_its_own_snapshot(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A payment created under the OLD shared payer id (payer_identity_id NULL)
    must keep verifying against its stored gateway_user_id."""
    legacy_user_id = 987654321
    assert legacy_user_id != settings.centralpay_user_id
    token = _seed_payment(
        session_factory,
        order_id="legacy-1",
        gateway_order_id=910000000001,
        gateway_user_id=legacy_user_id,
        payer_identity_id=None,  # the legacy marker
        payer_identity_type=None,
        derivation_version=None,
        linked=True,
    )

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
    """A pre-fix legacy row still awaiting its link must NOT mint a new link
    under the shared id: it adopts the requester's exact Telegram id."""
    shared = settings.centralpay_user_id
    _seed_payment(
        session_factory,
        order_id="legacy-prelink",
        gateway_order_id=910000000077,
        gateway_user_id=shared,  # the old shared id
        payer_identity_id=None,  # legacy marker, still pre-link
        payer_identity_type=None,
        derivation_version=None,
        linked=False,
    )

    resp = create_order(client, settings, order_id="legacy-prelink", telegram_user_id=1414)
    assert resp.status_code == 200
    assert stub.getlink_requests[-1]["userId"] == 1414  # the exact id, not shared
    payment = get_payment(session_factory, "legacy-prelink")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == 1414
    types = [e.event_type for e in get_events(session_factory, payment.id)]
    assert "payment_payer_identity_adopted" in types


def test_untyped_customer_era_linked_row_returns_its_link_unchanged(
    client, settings, session_factory, stub
):
    """A customer-era row (mapped identity, scope never stored) with a live
    link is returned unchanged on retry; its scope stays NULL (immutable)."""
    mapping_id = _seed_mapping(
        session_factory,
        key_hash="b" * 64,  # retired customer-scoped hash (unrecognizable)
        gateway_user_id=1_900_000_088,
        scheme=IDENTITY_SCHEME_HISTORICAL_HMAC,
    )
    _seed_payment(
        session_factory,
        order_id="era7-live",
        gateway_order_id=910000000088,
        gateway_user_id=1_900_000_088,
        payer_identity_id=mapping_id,
        payer_identity_type=None,  # scope tracking arrived later
        derivation_version=1,
        linked=True,
    )
    resp = create_order(client, settings, order_id="era7-live", telegram_user_id=616161)
    assert resp.status_code == 200
    assert resp.json() == {"url": "https://gateway.test/pay/era7-live"}
    assert stub.getlink_requests == []
    payment = get_payment(session_factory, "era7-live")
    assert payment.payer_identity_type is None
    assert payment.gateway_user_id == 1_900_000_088


def test_untyped_customer_era_prelink_row_adopts_current_identity(
    client, settings, session_factory, stub
):
    mapping_id = _seed_mapping(
        session_factory,
        key_hash="c" * 64,
        gateway_user_id=1_900_000_089,
        scheme=IDENTITY_SCHEME_HISTORICAL_HMAC,
    )
    _seed_payment(
        session_factory,
        order_id="era7-pre",
        gateway_order_id=910000000089,
        gateway_user_id=1_900_000_089,
        payer_identity_id=mapping_id,
        payer_identity_type=None,
        derivation_version=1,
        linked=False,
    )
    resp = create_order(client, settings, order_id="era7-pre", telegram_user_id=717171)
    assert resp.status_code == 200
    payment = get_payment(session_factory, "era7-pre")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == 717171  # the exact id
    assert stub.getlink_requests[-1]["userId"] == 717171


# --- no raw Telegram id in logs / events -------------------------------------


def test_raw_telegram_id_never_appears_in_logs_or_events(
    client, settings, session_factory, stub, caplog
):
    # A large, distinctive, VALID Telegram id (below 2**52). It IS stored as
    # gateway_user_id and sent to the gateway (product requirement) — but it
    # must never appear in logs or audit-event data.
    raw = 4_400_000_000_000_123
    with caplog.at_level(logging.DEBUG):
        response = create_order(client, settings, order_id="leak-check", telegram_user_id=raw)
    assert response.status_code == 200
    assert str(raw) not in caplog.text
    events = get_events(session_factory)
    assert events  # the identity-created audit trail exists...
    for event in events:
        assert str(raw) not in str(event.data)  # ...but never carries the id
    identity_events = [
        e for e in events if e.event_type == "centralpay_payer_identity_created"
    ]
    assert identity_events
    created_data = identity_events[0].data
    assert created_data is not None
    assert "gateway_user_id" not in created_data
    assert created_data["identity_scheme"] == IDENTITY_SCHEME_TELEGRAM_RAW
