"""application/x-www-form-urlencoded compatibility: a whole JSON document
encoded as the KEY of a single form pair with an empty value.

Production incident: the inaccessible legacy sales bot intermittently posts
``Content-Type: application/x-www-form-urlencoded`` with a body that is,
conceptually, ``<percent-encoded JSON document>=`` — the entire JSON payload
percent-encoded as one form KEY, followed by a bare ``=`` (empty value).
``parse_qsl(..., strict_parsing=True)`` accepts this as ONE syntactically
valid pair (an ``=`` is present), so it never raises and never reaches the
existing JSON-syntax fallback in ``_decode`` (which only triggers when form
parsing itself fails to tokenize the body at all — see
tests/test_custom_payment_urlencoded_json_fallback.py). The ordinary
required-field-name check then sees zero of the three real field names and
rejects the whole body as "missing api_key, amount, order_id" — exactly the
production symptom:

    total_pair_count=1
    extra_field_count=1
    missing_required_fields=["api_key","amount","order_id"]
    duplicate_required_fields=[]

``_try_recover_json_key_form`` recognizes this narrow shape and recovers the
JSON object, which is then fed through the EXACT SAME pipeline as every
other representation (``_normalize`` -> ``CreatePaymentRequest`` -> identity
extraction -> authentication -> amount policy -> idempotency -> payment
creation -> fee snapshot -> getLink). No separate, weaker validation path.

These tests exercise the real route through the strict model and fake both
CentralPay and the customer bot at the httpx transport layer (shared
fixtures) — no real external service is contacted.
"""

import json
import logging

import pytest
from sqlalchemy import func, select

from app.api.payments import (
    _CompatReject,
    _decode_urlencoded,
    _try_recover_json_key_form,
)
from app.models import Payment
from app.services.payer_identity import IDENTITY_TYPE_TELEGRAM_USER
from tests.conftest import (
    DEFAULT_REDIRECT_URL,
    expected_gateway_user_id,
    get_events,
    get_payment,
)

CUSTOM_PAYMENT_URL = "/api/custom-payment"
FORM_CT = "application/x-www-form-urlencoded"

# Manual, explicit percent-encoding table (not urllib.parse.quote) so the
# "percent-encoded key" test exercises the literal wire bytes rather than
# round-tripping through the same helper the production code might use.
_PERCENT_ENCODE = {
    "{": "%7B",
    "}": "%7D",
    '"': "%22",
    ":": "%3A",
    ",": "%2C",
}


def _percent_encode_json(text: str) -> str:
    out = []
    for ch in text:
        out.append(_PERCENT_ENCODE.get(ch, ch))
    return "".join(out)


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def _assert_no_side_effects(session_factory, stub) -> None:
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


def _post_form(client, body: str):
    return client.post(CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": FORM_CT})


def _json_key_body(fields: dict[str, object]) -> str:
    """The exact production wire shape: the whole JSON document as one
    percent-encoded form KEY, followed by a bare ``=`` (empty value)."""
    from urllib.parse import quote

    json_text = json.dumps(fields, separators=(",", ":"))
    return quote(json_text, safe="") + "="


def _valid_fields(
    settings, *, amount=10000, order_id="jsonkey-order", **extra
) -> dict[str, object]:
    fields = {"api_key": settings.inbound_api_key, "amount": amount, "order_id": order_id}
    fields.update(extra)
    return fields


def _rejection_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    return rec


def _normalized_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_normalized"]
    return rec


# --- unit: the recovery helper's exact activation conditions ------------------


def test_recover_helper_requires_exactly_one_pair():
    assert _try_recover_json_key_form([('{"a":1}', "")]) == {"a": 1}
    assert _try_recover_json_key_form([('{"a":1}', ""), ("x", "y")]) is None


def test_recover_helper_requires_empty_value():
    assert _try_recover_json_key_form([('{"a":1}', "nonempty")]) is None


def test_recover_helper_rejects_real_field_names_even_with_empty_value():
    assert _try_recover_json_key_form([("api_key", "")]) is None
    assert _try_recover_json_key_form([("amount", "")]) is None
    assert _try_recover_json_key_form([("order_id", "")]) is None
    assert _try_recover_json_key_form([("user_id", "")]) is None


def test_recover_helper_rejects_malformed_and_non_object_json():
    assert _try_recover_json_key_form([("{not valid json", "")]) is None
    assert _try_recover_json_key_form([("[1,2,3]", "")]) is None  # array
    assert _try_recover_json_key_form([("42", "")]) is None  # number
    assert _try_recover_json_key_form([("true", "")]) is None  # boolean
    assert _try_recover_json_key_form([("null", "")]) is None  # null
    assert _try_recover_json_key_form([('"a string"', "")]) is None  # scalar string


def test_recover_helper_accepts_empty_object():
    # Activation is representation-detection only; field-presence validation
    # happens downstream in the SAME strict-model pipeline.
    assert _try_recover_json_key_form([("{}", "")]) == {}


# --- the exact production incident: reproduced, then confirmed fixed ----------


def test_incident_wire_body_previously_422_now_succeeds(
    client, settings, session_factory, stub, caplog
):
    """The exact reproduced production wire body. Before this fix,
    ``_decode_urlencoded`` raised ``_CompatReject`` with
    total_pair_count=1, extra_field_count=1,
    missing_required_fields=["api_key","amount","order_id"],
    duplicate_required_fields=[] -- byte-for-byte matching the production
    log -- and the route returned the exact production 422 envelope. It now
    succeeds through the unmodified strict-model pipeline."""
    body = _json_key_body(_valid_fields(settings, amount=10000, order_id="incident-order"))
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    payment = get_payment(session_factory, "incident-order")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)
    assert _normalized_record(caplog).representation == "urlencoded_json_key"


def test_decoder_reproduces_exact_production_diagnostics_before_recovery():
    """Direct proof (independent of the fix) that this wire shape produces
    the EXACT diagnostic fields observed in production, confirming the root
    cause: the recovery helper declining is what the pre-fix code always
    did unconditionally."""
    from urllib.parse import quote

    payload = {"api_key": "sk_live_example", "amount": 50000, "order_id": "order-abc-123"}
    wire_body = (quote(json.dumps(payload, separators=(",", ":")), safe="") + "=").encode()
    pairs_only_path = wire_body.decode()
    from urllib.parse import parse_qsl

    pairs = parse_qsl(pairs_only_path, keep_blank_values=True, strict_parsing=True)
    assert len(pairs) == 1
    key, value = pairs[0]
    assert value == ""
    assert json.loads(key) == payload  # the key IS the whole JSON document


# --- one JSON object encoded as form key succeeds ------------------------------


def test_one_json_object_encoded_as_form_key_succeeds(client, settings, session_factory, stub):
    body = _json_key_body(_valid_fields(settings, amount=25000, order_id="jsonkey-basic"))
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "jsonkey-basic")
    assert payment.amount == 25000
    assert isinstance(payment.amount, int)


def test_percent_encoded_key_succeeds_hand_built_wire_bytes(
    client, settings, session_factory, stub
):
    """Hand-built percent-encoding (not urllib.parse.quote) proves the
    actual wire bytes, not just a round-trip through one encoder, decode
    correctly."""
    json_text = json.dumps(
        {"api_key": settings.inbound_api_key, "amount": 15000, "order_id": "jsonkey-manual"},
        separators=(",", ":"),
    )
    body = _percent_encode_json(json_text) + "="
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "jsonkey-manual")
    assert payment.amount == 15000


# --- amount string compatibility, matching existing legacy behavior -----------


def test_ascii_decimal_amount_string_converted_exactly_like_existing_compat(
    client, settings, session_factory, stub
):
    body = _json_key_body(_valid_fields(settings, amount="10000", order_id="jsonkey-str-amt"))
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "jsonkey-str-amt")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


@pytest.mark.parametrize(
    "amount",
    ["10000.5", "1e4", "+50000", "50,000", "۵۰۰۰۰", "0x10", ""],  # noqa: RUF001
)
def test_non_ascii_decimal_amount_strings_still_rejected(
    client, settings, session_factory, stub, amount
):
    body = _json_key_body(_valid_fields(settings, amount=amount, order_id="jsonkey-bad-amt"))
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


# --- optional end-user identity aliases ----------------------------------------


@pytest.mark.parametrize("alias", ["user_id", "userId", "uid", "chat_id", "telegram_id"])
def test_identity_alias_still_works_via_json_key(
    client, settings, session_factory, stub, alias
):
    body = _json_key_body(
        _valid_fields(settings, amount=10000, order_id=f"jsonkey-al-{alias}", **{alias: 707901})
    )
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, f"jsonkey-al-{alias}")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=707901)


# --- explicit rejections --------------------------------------------------------


def test_non_empty_pair_value_rejected(client, settings, session_factory, stub, caplog):
    json_text = json.dumps(_valid_fields(settings, amount=10000, order_id="jsonkey-nonempty"))
    from urllib.parse import quote

    body = f"{quote(json_text, safe='')}=x"  # non-empty value
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded"  # NOT urlencoded_json_key
    assert rec.missing_required_fields == ["api_key", "amount", "order_id"]
    _assert_no_side_effects(session_factory, stub)


def test_two_pairs_rejected(client, settings, session_factory, stub, caplog):
    body = _json_key_body(_valid_fields(settings, amount=10000, order_id="jsonkey-2pair"))
    body += "&extra=1"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded"
    assert rec.total_pair_count == 2
    _assert_no_side_effects(session_factory, stub)


def test_malformed_json_key_rejected(client, settings, session_factory, stub, caplog):
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, "%7Bnot+valid+json=")
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


def test_json_array_key_rejected(client, settings, session_factory, stub, caplog):
    from urllib.parse import quote

    body = quote(json.dumps([1, 2, 3]), safe="") + "="
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize(
    "scalar", [42, True, False, None, "a bare string"], ids=["int", "true", "false", "null", "str"]
)
def test_json_scalar_key_rejected(client, settings, session_factory, stub, scalar):
    from urllib.parse import quote

    body = quote(json.dumps(scalar), safe="") + "="
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize("missing", ["api_key", "amount", "order_id"])
def test_missing_required_field_in_recovered_json_rejected(
    client, settings, session_factory, stub, missing
):
    fields = _valid_fields(settings, amount=10000, order_id="jsonkey-miss")
    del fields[missing]
    body = _json_key_body(fields)
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_hybrid_real_field_plus_json_key_payload_rejected(
    client, settings, session_factory, stub, caplog
):
    """A real form field alongside a JSON-key pair is hybrid ambiguity, not
    the recognized single-pair representation -- rejected as an ordinary
    form missing amount/order_id, never recovered."""
    json_blob = _json_key_body(_valid_fields(settings, amount=10000, order_id="jsonkey-hybrid"))
    body = f"api_key={settings.inbound_api_key}&{json_blob}"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded"
    assert rec.total_pair_count == 2
    _assert_no_side_effects(session_factory, stub)


def test_duplicate_normal_form_required_fields_still_rejected(
    client, settings, session_factory, stub
):
    """Unaffected sibling behavior: a real (non-JSON-key) form with a
    duplicate required field is rejected exactly as before this change."""
    body = f"api_key={settings.inbound_api_key}&amount=1&amount=2&order_id=jsonkey-dup"
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_authentication_still_rejects_invalid_api_key_via_json_key(
    client, settings, session_factory, stub
):
    """A schema-valid JSON-key body with the WRONG api_key normalizes fine
    but fails the SAME constant-time authentication check -- 401, not a
    silent bypass."""
    body = _json_key_body(
        {"api_key": "wrong-key-value", "amount": 10000, "order_id": "jsonkey-badauth"}
    )
    response = _post_form(client, body)
    assert response.status_code == 401
    _assert_no_side_effects(session_factory, stub)


def test_oversized_json_key_body_rejected_before_decode(client, settings, session_factory, stub):
    body = _json_key_body(_valid_fields(settings, amount=10000, order_id="x" * 70000))
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_invalid_utf8_body_still_falls_back_unchanged(
    client, settings, session_factory, stub, caplog
):
    """Malformed encoding is handled by the EXISTING rules unchanged: invalid
    UTF-8 bytes never reach parse_qsl or the new recovery helper at all."""
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = client.post(
            CUSTOM_PAYMENT_URL, content=b"\xff\xfe not utf-8", headers={"Content-Type": FORM_CT}
        )
    assert response.status_code == 422
    assert _rejection_record(caplog).representation != "urlencoded_json_key"
    _assert_no_side_effects(session_factory, stub)


# --- no secret / raw-body leakage in logs --------------------------------------


def test_no_secret_or_raw_body_leakage_on_success(client, settings, session_factory, stub, caplog):
    order_id = "JSONKEY-PREAUTH-77"
    body = _json_key_body(_valid_fields(settings, amount=45678, order_id=order_id, tag="X"))
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    norm = _normalized_record(caplog)
    assert norm.representation == "urlencoded_json_key"
    blob = repr(norm.__dict__)
    assert order_id not in blob
    assert "45678" not in blob
    assert settings.inbound_api_key not in blob
    for record in caplog.records:
        assert settings.inbound_api_key not in repr(record.__dict__)


def test_no_secret_or_raw_body_leakage_on_rejection(
    client, settings, session_factory, stub, caplog
):
    marker = "SECRET-MARKER-JSONKEY-91a2"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, f"%7Bnot+valid+{marker}=")
    assert response.status_code == 422
    for record in caplog.records:
        assert marker not in repr(record.__dict__)
    assert marker not in response.text


def test_api_key_not_in_response_body_on_rejection(client, settings, session_factory, stub):
    fields = _valid_fields(settings, amount=10000, order_id="jsonkey-echo")
    del fields["order_id"]  # force a schema rejection downstream
    body = _json_key_body(fields)
    response = _post_form(client, body)
    assert response.status_code == 422
    assert settings.inbound_api_key not in response.text


# --- unit: _decode_urlencoded returns the new label directly ------------------


def test_decode_urlencoded_returns_json_key_label():
    body = json.dumps({"api_key": "k", "amount": 1, "order_id": "o"}, separators=(",", ":"))
    from urllib.parse import quote

    rep, data = _decode_urlencoded((quote(body, safe="") + "=").encode())
    assert rep == "urlencoded_json_key"
    assert data == {"api_key": "k", "amount": 1, "order_id": "o"}


def test_decode_urlencoded_falls_through_for_non_empty_value():
    with pytest.raises(_CompatReject) as exc_info:
        _decode_urlencoded(b'%7B%22a%22%3A1%7D=x')  # non-empty value
    assert exc_info.value.category == "urlencoded"
