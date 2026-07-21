"""application/x-www-form-urlencoded compatibility: tolerate extra fields.

The inaccessible legacy sales bot posts an urlencoded body that carries the
three required payment fields (api_key, amount, order_id) *plus* unrelated
legacy fields, and it may also carry the OPTIONAL end-user identity under one
of the accepted aliases (user_id/userId/uid/chat_id/telegram_id). The parser
must require each required field exactly once, capture an identity alias if
present, and safely **ignore** every other extra field: an unrelated extra is
never validated, never stored, never logged, and can never influence
authentication, the amount, the order identity, fees, or the gateway. An
identity alias only ever influences the derived, isolated gateway payer id —
never the strict model or the DB directly.

These tests exercise the real route through the strict model and fake both
CentralPay and the customer bot at the httpx transport layer (shared
fixtures) — no real external service is contacted.
"""

import logging

import pytest
from sqlalchemy import func, select

from app.api.payments import _MAX_FORM_PAIRS, _CompatReject, _decode_urlencoded
from app.models import Payment
from app.services.payer_identity import IDENTITY_TYPE_TELEGRAM_USER
from tests.conftest import (
    DEFAULT_REDIRECT_URL,
    expected_gateway_user_id,
    get_events,
    get_payment,
    run_pass,
    valid_callback_path,
    verify_ok_response,
)

CUSTOM_PAYMENT_URL = "/api/custom-payment"
FORM_CT = "application/x-www-form-urlencoded"


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def _assert_no_side_effects(session_factory, stub) -> None:
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


def _post_form(client, body: str):
    return client.post(CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": FORM_CT})


def _required(settings, *, amount="10000", order_id="urlenc-order") -> str:
    return f"api_key={settings.inbound_api_key}&amount={amount}&order_id={order_id}"


# --- unit: the decoder returns the required fields plus any identity alias -----


def test_decode_urlencoded_drops_extras_and_keeps_alias():
    """Unrelated extras (including a non-alias field like customer_id) never
    leave the decoder; the required fields and a supported identity alias do."""
    raw = (
        b"api_key=k&utm_source=telegram&amount=5000&debug=1"
        b"&order_id=o-1&customer_id=c-1&user_id=42&note=hi"
    )
    representation, data = _decode_urlencoded(raw)
    assert representation == "urlencoded"
    assert data == {"api_key": "k", "amount": "5000", "order_id": "o-1", "user_id": "42"}


def test_decode_urlencoded_without_alias_returns_only_required():
    raw = b"api_key=k&amount=5000&order_id=o-1&utm_source=telegram"
    _, data = _decode_urlencoded(raw)
    assert data == {"api_key": "k", "amount": "5000", "order_id": "o-1"}


def test_decode_urlencoded_requires_each_field_exactly_once():
    with pytest.raises(_CompatReject):
        # duplicate amount (the other two required fields present exactly once)
        _decode_urlencoded(b"api_key=k&amount=1&amount=2&order_id=o")
    with pytest.raises(_CompatReject):
        # missing amount (the other two required fields present)
        _decode_urlencoded(b"api_key=k&order_id=o")


def test_normalize_drops_extra_keys_and_aliases():
    """Second stripping layer: even the identity alias is dropped here —
    _normalize keeps only the three required fields for the strict model."""
    from app.api.payments import _normalize

    data = {
        "api_key": "k",
        "amount": "10",
        "order_id": "o",
        "user_id": "42",  # alias: used for identity, never for the model
        "evil": "x",
        "fee_amount": 1,
    }
    assert _normalize(data) == {"api_key": "k", "amount": 10, "order_id": "o"}


def test_only_required_fields_reach_the_strict_model(
    client, settings, session_factory, stub, monkeypatch
):
    """End-to-end, load-bearing proof of 'extras/aliases are never passed to
    CreatePaymentRequest': capture the exact kwargs the parser constructs the
    model with. (The model's default extra-tolerance would otherwise silently
    mask a stripping regression — DB/gateway assertions alone cannot see it.)"""
    import app.api.payments as payments_module

    captured: list[dict[str, object]] = []
    real_model = payments_module.CreatePaymentRequest

    class SpyModel(real_model):  # type: ignore[valid-type, misc]
        def __init__(self, **kwargs: object) -> None:
            captured.append(dict(kwargs))
            super().__init__(**kwargs)

    monkeypatch.setattr(payments_module, "CreatePaymentRequest", SpyModel)
    body = _required(settings, order_id="model-spy") + "&utm_source=x&debug=1&user_id=99&note=hi"
    assert _post_form(client, body).status_code == 200
    [kwargs] = captured
    assert set(kwargs) == {"api_key", "amount", "order_id"}


# --- accepted: exact fields, and extras ignored ------------------------------


def test_exact_required_fields_still_work(client, settings, session_factory, stub):
    response = _post_form(client, _required(settings, order_id="exact"))
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    assert get_payment(session_factory, "exact").amount == 10000


def test_one_extra_field_accepted(client, settings, session_factory, stub):
    body = _required(settings, order_id="one-extra") + "&utm_source=telegram"
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "one-extra").amount == 10000


def test_several_extra_fields_accepted(client, settings, session_factory, stub):
    extras = "&campaign=spring&debug=true&ref=abc&lang=fa&ts=1720000000"
    body = _required(settings, order_id="many-extra") + extras
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "many-extra").amount == 10000


def test_repeated_extra_field_is_allowed(client, settings, session_factory, stub):
    """Only REQUIRED-field duplicates are rejected; a repeated *extra* field is
    ignored like any other extra."""
    body = _required(settings, order_id="dup-extra") + "&tag=a&tag=b&tag=c"
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "dup-extra").amount == 10000


# --- optional identity alias -------------------------------------------------


@pytest.mark.parametrize("alias", ["user_id", "userId", "uid", "chat_id", "telegram_id"])
def test_urlencoded_identity_alias_drives_gateway_id(
    client, settings, session_factory, stub, alias
):
    body = _required(settings, order_id=f"al-{alias}") + f"&{alias}=909001"
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, f"al-{alias}")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=909001)


def test_urlencoded_alias_last_value_wins(client, settings, session_factory, stub):
    """A repeated identity alias is not a required-field duplicate; last wins."""
    body = _required(settings, order_id="al-dup") + "&user_id=101010&user_id=202020"
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "al-dup")
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=202020)


# --- extras cannot influence the payment -------------------------------------


def test_extra_fields_not_stored_and_do_not_change_payment(
    client, settings, session_factory, stub
):
    """The persisted row reflects only the required inputs; extras — including
    ones whose names shadow internal columns — change nothing. (End-to-end
    sanity check; the load-bearing stripping proofs are the decoder/_normalize
    unit tests and test_only_required_fields_reach_the_strict_model.)"""
    body = (
        _required(settings, amount="10000", order_id="store-check")
        + "&amount_extra=999999&fee_amount=999999&payable_amount=1&bot_order_id=evil"
    )
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "store-check")
    assert payment.bot_order_id == "store-check"  # not "evil"
    assert payment.amount == 10000  # not 999999
    assert payment.fee_amount == 0
    assert payment.payable_amount == 10000  # not 1


def test_extra_fields_do_not_change_gateway_request(client, settings, session_factory, stub):
    body = _required(settings, amount="12345", order_id="gw-check") + "&x=1&y=2"
    response = _post_form(client, body)
    assert response.status_code == 200
    [getlink] = stub.getlink_requests
    assert getlink["amount"] == 12345  # original + zero test fee
    assert getlink["orderId"] == get_payment(session_factory, "gw-check").gateway_order_id


# --- rejections: duplicate required fields -----------------------------------


@pytest.mark.parametrize("field", ["api_key", "amount", "order_id"])
def test_duplicate_required_field_rejected(client, settings, session_factory, stub, field):
    values = {
        "api_key": settings.inbound_api_key,
        "amount": "10000",
        "order_id": "dup",
    }
    # Emit the target field twice; the other two once.
    parts = []
    for name in ("api_key", "amount", "order_id"):
        parts.append(f"{name}={values[name]}")
        if name == field:
            parts.append(f"{name}={values[name]}")
    response = _post_form(client, "&".join(parts))
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_duplicate_required_field_rejected_even_with_extras(
    client, settings, session_factory, stub
):
    body = _required(settings, order_id="dup2") + "&amount=20000&extra=1"
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


# --- rejections: missing required fields -------------------------------------


@pytest.mark.parametrize(
    "body_fields",
    [
        ("amount", "order_id"),  # missing api_key
        ("api_key", "order_id"),  # missing amount
        ("api_key", "amount"),  # missing order_id
    ],
)
def test_missing_required_field_rejected(
    client, settings, session_factory, stub, body_fields
):
    values = {
        "api_key": settings.inbound_api_key,
        "amount": "10000",
        "order_id": "miss",
    }
    # Include unrelated extras to prove they cannot substitute for a missing field.
    body = "&".join(f"{name}={values[name]}" for name in body_fields) + "&extra=1&more=2"
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


# --- rejections: pair-count bound --------------------------------------------


def test_pair_count_at_limit_accepted(client, settings, session_factory, stub):
    extras = "&".join(f"e{i}=v{i}" for i in range(_MAX_FORM_PAIRS - 3))
    body = _required(settings, order_id="at-limit") + "&" + extras
    assert body.count("=") == _MAX_FORM_PAIRS  # 3 required + (limit-3) extras
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "at-limit").amount == 10000


def test_too_many_pairs_rejected(client, settings, session_factory, stub):
    extras = "&".join(f"e{i}=v{i}" for i in range(_MAX_FORM_PAIRS + 5))
    body = _required(settings, order_id="too-many") + "&" + extras
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_malformed_form_body_rejected(client, settings, session_factory, stub):
    # A segment with no '=' is malformed under strict parsing (unchanged).
    response = _post_form(client, "api_key=k&brokenpair&amount=1&order_id=o")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


# --- observability: only safe diagnostics, never values ----------------------


def test_rejection_logs_only_safe_diagnostics(client, settings, session_factory, stub, caplog):
    """A duplicate-amount rejection records the fixed field name and counts —
    never the amount value, the order_id, or the api_key."""
    order_id = "SECRET-ORDER-DIAG"
    # All required fields present exactly once except the duplicated amount, so
    # the diagnostic isolates the duplicate (missing_required_fields stays empty).
    body = (
        f"api_key={settings.inbound_api_key}&amount=13579&amount=24680&order_id={order_id}"
    )
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    assert rec.representation == "urlencoded"
    assert rec.duplicate_required_fields == ["amount"]
    assert rec.missing_required_fields == []
    assert isinstance(rec.total_pair_count, int)
    assert isinstance(rec.extra_field_count, int)
    # No submitted value anywhere in the record.
    blob = repr(rec.__dict__)
    assert "13579" not in blob
    assert "24680" not in blob
    assert order_id not in blob
    assert settings.inbound_api_key not in blob


def test_extra_field_values_never_logged(client, settings, session_factory, stub, caplog):
    extra_key = "utm_campaign"
    extra_val = "EXTRA-VALUE-SHOULD-NOT-LOG-7f3a"
    body = _required(settings, order_id="log-extra") + f"&{extra_key}={extra_val}"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    for record in caplog.records:
        blob = repr(record.__dict__)
        assert extra_val not in blob
        assert extra_key not in blob


def test_required_values_absent_from_preauth_logs(client, settings, session_factory, stub, caplog):
    order_id = "PREAUTH-ORDER-9c1"
    body = _required(settings, amount="45678", order_id=order_id) + "&x=1"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    # The pre-auth parser event carries only representation/content_type/body_size
    # plus a boolean identity-presence flag — never a field value.
    [norm] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_normalized"]
    blob = repr(norm.__dict__)
    assert order_id not in blob
    assert "45678" not in blob
    assert settings.inbound_api_key not in blob


def test_raw_alias_value_absent_from_preauth_logs(client, settings, session_factory, stub, caplog):
    raw = 8123456789012345
    body = _required(settings, order_id="al-preauth") + f"&user_id={raw}"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    [norm] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_normalized"]
    assert norm.has_end_user_identity is True
    for record in caplog.records:
        assert str(raw) not in repr(record.__dict__)


def test_api_key_absent_from_logs_and_error_responses(
    client, settings, session_factory, stub, caplog
):
    # Success path: api_key is never logged even after authentication.
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        ok = _post_form(client, _required(settings, order_id="key-ok"))
    assert ok.status_code == 200
    for record in caplog.records:
        assert settings.inbound_api_key not in repr(record.__dict__)
    # Error path: a duplicate rejection never echoes the api_key.
    bad = _post_form(client, _required(settings, order_id="key-bad") + "&amount=1")
    assert bad.status_code == 422
    assert settings.inbound_api_key not in bad.text


# --- unchanged downstream contract -------------------------------------------


def test_success_response_shape_unchanged(client, settings, session_factory, stub):
    body = _required(settings, order_id="shape") + "&extra=1"
    response = _post_form(client, body)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}


def test_outbound_customer_bot_payload_unchanged(
    client, settings, session_factory, stub, bot_stub, notifier
):
    order_id = "outbound-extra"
    body = _required(settings, order_id=order_id) + "&utm_source=x&campaign=y"
    assert _post_form(client, body).status_code == 200
    payment = get_payment(session_factory, order_id)
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id=f"REF-{order_id}"
    )
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    result = run_pass(session_factory, notifier, settings)
    assert result["processed"] == 1
    [request] = bot_stub.requests
    assert request == {"order_id": order_id, "actions": "custom_payment_verify"}


def test_json_and_text_plain_paths_unchanged(client, settings, session_factory, stub):
    """The urlencoded change must not touch the JSON / text-plain behavior."""
    import json

    obj = {
        "api_key": settings.inbound_api_key,
        "amount": 10000,
        "order_id": "json-still",
    }
    assert client.post(
        CUSTOM_PAYMENT_URL, content=json.dumps(obj), headers={"Content-Type": "application/json"}
    ).status_code == 200
    obj2 = {
        "api_key": settings.inbound_api_key,
        "amount": "10000",
        "order_id": "text-still",
    }
    assert client.post(
        CUSTOM_PAYMENT_URL, content=json.dumps(obj2), headers={"Content-Type": "text/plain"}
    ).status_code == 200
    # A JSON object with an extra key stays accepted (model ignores extras);
    # form-style extra tolerance did not change JSON strictness elsewhere.
    assert get_payment(session_factory, "json-still").amount == 10000
    assert get_payment(session_factory, "text-still").amount == 10000
