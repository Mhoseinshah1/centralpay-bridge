"""JSON fallback for a mislabeled application/x-www-form-urlencoded body.

Production confirmed the inaccessible legacy sales bot declares
``Content-Type: application/x-www-form-urlencoded`` while sending a JSON
document, so strict form parsing fails before required-field inspection
(the rejection carried no pair/field diagnostics). The parser now falls
back to the existing bounded one-extra-layer JSON decoder — but ONLY when
form parsing itself is a *syntax* failure. A validly parsed form that
fails a semantic rule (missing/duplicate required field, too many pairs)
is still rejected as a form request and never reaches the fallback.

Representation labels: ``urlencoded`` (strict form), ``urlencoded_json_object``
/ ``urlencoded_json_string_object`` (fallback accepted), and
``urlencoded_unparseable`` (both parsers failed).

The three required fields are api_key, amount, order_id; the OPTIONAL end-user
identity alias is carried alongside them and is parsed on the fallback path
too, driving only the derived gateway payer id.

These tests exercise the real route through the strict model and fake both
CentralPay and the customer bot at the httpx transport layer (shared
fixtures) — no real external service is contacted.
"""

import json
import logging

import pytest
from sqlalchemy import func, select

from app.api.payments import (
    _MAX_FORM_PAIRS,
    _CompatReject,
    _decode,
    _decode_urlencoded,
    _UrlencodedSyntaxError,
)
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


def _post_form(client, body: str | bytes):
    return client.post(CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": FORM_CT})


def _form_body(settings, *, amount="10000", order_id="fb-order") -> str:
    return f"api_key={settings.inbound_api_key}&amount={amount}&order_id={order_id}"


def _json_fields(settings, *, amount, order_id, **extras):
    fields = {
        "api_key": settings.inbound_api_key,
        "amount": amount,
        "order_id": order_id,
    }
    fields.update(extras)
    return fields


def _rejection_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    return rec


def _normalized_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_normalized"]
    return rec


# --- unit: syntax vs semantic failure is a typed distinction ------------------


def test_decoder_distinguishes_syntax_from_semantic_failure():
    """Syntax failures raise _UrlencodedSyntaxError (fallback allowed);
    semantic failures on a parsed form raise _CompatReject (no fallback)."""
    with pytest.raises(_UrlencodedSyntaxError):
        _decode_urlencoded(b'{"api_key": "k"}')  # not form syntax at all
    with pytest.raises(_UrlencodedSyntaxError):
        _decode_urlencoded(b"\xff\xfe")  # not UTF-8
    with pytest.raises(_CompatReject):
        _decode_urlencoded(b"amount=1&order_id=o")  # parsed, api_key missing
    with pytest.raises(_CompatReject):
        _decode_urlencoded(b"api_key=k&amount=1&amount=2&order_id=o")  # duplicate


def test_decode_falls_back_only_for_syntax_failures():
    """_decode returns the fallback labels for a JSON body under the form
    content type, and keeps the form rejection for a parsed-but-invalid form."""
    rep, data = _decode(FORM_CT, b'{"api_key": "k", "amount": 5, "order_id": "o"}')
    assert rep == "urlencoded_json_object"
    assert data == {"api_key": "k", "amount": 5, "order_id": "o"}
    inner = json.dumps({"api_key": "k", "amount": "5", "order_id": "o"})
    rep2, data2 = _decode(FORM_CT, json.dumps(inner).encode())
    assert rep2 == "urlencoded_json_string_object"
    assert data2 == {"api_key": "k", "amount": "5", "order_id": "o"}
    with pytest.raises(_CompatReject) as exc_info:
        _decode(FORM_CT, b"amount=1&order_id=o")  # valid form, missing api_key
    assert exc_info.value.category == "urlencoded"
    with pytest.raises(_CompatReject) as unparseable:
        _decode(FORM_CT, b"just some text, definitely not json")
    assert unparseable.value.category == "urlencoded_unparseable"


# --- accepted: canonical form behavior is unchanged ---------------------------


def test_valid_urlencoded_request_still_works(client, settings, session_factory, stub, caplog):
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post_form(client, _form_body(settings, order_id="form-canonical"))
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    assert get_payment(session_factory, "form-canonical").amount == 10000
    assert _normalized_record(caplog).representation == "urlencoded"


def test_valid_urlencoded_with_extras_still_works(client, settings, session_factory, stub):
    body = _form_body(settings, order_id="form-extras") + "&utm_source=tg&campaign=x"
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "form-extras").amount == 10000


# --- accepted: JSON under the form content type -------------------------------


def test_json_object_with_form_content_type(client, settings, session_factory, stub, caplog):
    body = json.dumps(_json_fields(settings, amount=10000, order_id="fb-json-obj"))
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    payment = get_payment(session_factory, "fb-json-obj")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)
    assert _normalized_record(caplog).representation == "urlencoded_json_object"


def test_json_string_object_with_form_content_type(
    client, settings, session_factory, stub, caplog
):
    inner = json.dumps(_json_fields(settings, amount=10000, order_id="fb-json-str"))
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post_form(client, json.dumps(inner))
    assert response.status_code == 200
    assert get_payment(session_factory, "fb-json-str").amount == 10000
    assert _normalized_record(caplog).representation == "urlencoded_json_string_object"


def test_fallback_amount_ascii_decimal_string_converted(
    client, settings, session_factory, stub
):
    body = json.dumps(_json_fields(settings, amount="10000", order_id="fb-str-amt"))
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "fb-str-amt")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


def test_fallback_identity_alias_parsed(client, settings, session_factory, stub):
    """The optional identity alias is parsed on the JSON-under-form fallback
    path too, and drives the derived per-user gateway id."""
    body = json.dumps(
        _json_fields(settings, amount=10000, order_id="fb-alias", user_id=767601)
    )
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "fb-alias")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=767601)


# --- fallback extras: ignored, never reach the model, DB, or gateway ----------


def test_fallback_extra_json_keys_never_reach_the_model(
    client, settings, session_factory, stub, monkeypatch
):
    """Load-bearing stripping proof for the fallback path: capture the exact
    kwargs the parser constructs CreatePaymentRequest with."""
    import app.api.payments as payments_module

    captured: list[dict[str, object]] = []
    real_model = payments_module.CreatePaymentRequest

    class SpyModel(real_model):  # type: ignore[valid-type, misc]
        def __init__(self, **kwargs: object) -> None:
            captured.append(dict(kwargs))
            super().__init__(**kwargs)

    monkeypatch.setattr(payments_module, "CreatePaymentRequest", SpyModel)
    body = json.dumps(
        _json_fields(settings, amount=10000, order_id="fb-spy", utm="x", debug=1, note="hi")
    )
    assert _post_form(client, body).status_code == 200
    [kwargs] = captured
    assert set(kwargs) == {"api_key", "amount", "order_id"}


def test_fallback_extra_json_keys_not_stored_and_cannot_alter_payment(
    client, settings, session_factory, stub
):
    body = json.dumps(
        _json_fields(
            settings,
            amount=10000,
            order_id="fb-store",
            fee_amount=999999,
            payable_amount=1,
            bot_order_id="evil",
        )
    )
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "fb-store")
    assert payment.bot_order_id == "fb-store"
    assert payment.amount == 10000
    assert payment.fee_amount == 0
    assert payment.payable_amount == 10000


def test_fallback_extra_json_keys_do_not_change_gateway_request(
    client, settings, session_factory, stub
):
    body = json.dumps(
        _json_fields(settings, amount=12345, order_id="fb-gw", amount_override=1, x=2)
    )
    response = _post_form(client, body)
    assert response.status_code == 200
    [getlink] = stub.getlink_requests
    assert getlink["amount"] == 12345
    assert getlink["orderId"] == get_payment(session_factory, "fb-gw").gateway_order_id


# --- a validly parsed form NEVER falls back -----------------------------------


@pytest.mark.parametrize(
    "present",
    [
        ("amount", "order_id"),  # missing api_key
        ("api_key", "order_id"),  # missing amount
        ("api_key", "amount"),  # missing order_id
    ],
)
def test_parsed_form_missing_required_field_rejected_without_fallback(
    client, settings, session_factory, stub, caplog, present
):
    values = {
        "api_key": settings.inbound_api_key,
        "amount": "10000",
        "order_id": "nf-miss",
    }
    body = "&".join(f"{name}={values[name]}" for name in present)
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    # Rejected AS A FORM: the semantic diagnostics prove the fallback never ran.
    assert rec.representation == "urlencoded"
    assert rec.missing_required_fields == [
        f for f in ("api_key", "amount", "order_id") if f not in present
    ]
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize("field", ["api_key", "amount", "order_id"])
def test_parsed_form_duplicate_required_field_rejected_without_fallback(
    client, settings, session_factory, stub, caplog, field
):
    values = {
        "api_key": settings.inbound_api_key,
        "amount": "10000",
        "order_id": "nf-dup",
    }
    parts = []
    for name in ("api_key", "amount", "order_id"):
        parts.append(f"{name}={values[name]}")
        if name == field:
            parts.append(f"{name}={values[name]}")
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, "&".join(parts))
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded"
    assert rec.duplicate_required_fields == [field]
    _assert_no_side_effects(session_factory, stub)


def test_parsed_form_over_pair_limit_rejected_without_fallback(
    client, settings, session_factory, stub, caplog
):
    extras = "&".join(f"e{i}=v{i}" for i in range(_MAX_FORM_PAIRS + 5))
    body = _form_body(settings, order_id="nf-pairs") + "&" + extras
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded"
    # 3 required pairs + (_MAX_FORM_PAIRS + 5) extras.
    assert rec.total_pair_count == _MAX_FORM_PAIRS + 8
    _assert_no_side_effects(session_factory, stub)


# --- both parsers fail: urlencoded_unparseable, controlled 422 ----------------


@pytest.mark.parametrize(
    "body",
    [
        "just some legacy text that is neither form nor json",
        'a:1:{s:7:"api_key";s:1:"k";}',  # PHP-serialized value
        "{not valid json either",
        json.dumps([1, 2, 3]),  # JSON array
        json.dumps(None),  # JSON null
        json.dumps(True),  # JSON boolean
        json.dumps(42),  # JSON number
        json.dumps("a bare string that is not JSON itself"),
        json.dumps(json.dumps([1, 2])),  # string containing an array
    ],
)
def test_unparseable_bodies_rejected(client, settings, session_factory, stub, caplog, body):
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert _rejection_record(caplog).representation == "urlencoded_unparseable"
    _assert_no_side_effects(session_factory, stub)


def test_triple_encoded_json_rejected(client, settings, session_factory, stub, caplog):
    """At most ONE extra decode layer, exactly as on the JSON path."""
    inner = json.dumps(_json_fields(settings, amount=10000, order_id="fb-triple"))
    body = json.dumps(json.dumps(inner))  # string -> string -> object
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded_unparseable"
    _assert_no_side_effects(session_factory, stub)


def test_oversized_body_rejected_before_either_parser(
    client, settings, session_factory, stub, caplog
):
    body = json.dumps(_json_fields(settings, amount=10000, order_id="x" * 70000))
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "too_large"
    _assert_no_side_effects(session_factory, stub)


def test_fallback_schema_failures_still_rejected(client, settings, session_factory, stub):
    """The fallback feeds the SAME strict model: bad amounts and order ids die
    there exactly as on the JSON path."""
    for fields in (
        _json_fields(settings, amount="1e4", order_id="fb-bad"),  # non-decimal string
        _json_fields(settings, amount=10000.5, order_id="fb-bad"),  # float
        _json_fields(settings, amount=10000, order_id=""),  # empty order_id
        _json_fields(settings, amount=10000, order_id="a\x00b"),  # NUL
    ):
        assert _post_form(client, json.dumps(fields)).status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_fallback_never_bypasses_authentication(client, settings, session_factory, stub):
    # A schema-valid body that fails only the key check.
    body = json.dumps(
        {
            "api_key": "wrong-key-value",
            "amount": 10000,
            "order_id": "fb-auth",
        }
    )
    response = _post_form(client, body)
    assert response.status_code == 401
    _assert_no_side_effects(session_factory, stub)


# --- observability: labels present, values absent -----------------------------


def test_unparseable_rejection_logs_no_body_content(
    client, settings, session_factory, stub, caplog
):
    marker = "SECRET-FRAGMENT-c41d"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, f"opaque legacy blob {marker} not json")
    assert response.status_code == 422
    for record in caplog.records:
        assert marker not in repr(record.__dict__)


def test_accepted_fallback_logs_no_field_values(
    client, settings, session_factory, stub, caplog
):
    order_id = "FB-PREAUTH-ORDER-77"
    body = json.dumps(_json_fields(settings, amount=45678, order_id=order_id, tag="EXTRA-V"))
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    norm = _normalized_record(caplog)
    assert norm.representation == "urlencoded_json_object"
    assert norm.content_type == FORM_CT
    assert isinstance(norm.body_size, int)
    blob = repr(norm.__dict__)
    assert order_id not in blob
    assert "45678" not in blob
    assert "EXTRA-V" not in blob
    assert settings.inbound_api_key not in blob


def test_api_key_absent_from_fallback_logs_and_errors(
    client, settings, session_factory, stub, caplog
):
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        ok = _post_form(
            client, json.dumps(_json_fields(settings, amount=10000, order_id="fb-key"))
        )
    assert ok.status_code == 200
    for record in caplog.records:
        assert settings.inbound_api_key not in repr(record.__dict__)
    bad = _post_form(client, "definitely not parseable %%% ")
    assert bad.status_code == 422
    assert settings.inbound_api_key not in bad.text


# --- unchanged sibling paths and downstream contract --------------------------


def test_json_and_text_plain_paths_unchanged(client, settings, session_factory, stub):
    obj = _json_fields(settings, amount=10000, order_id="fb-json-path")
    assert client.post(
        CUSTOM_PAYMENT_URL, content=json.dumps(obj), headers={"Content-Type": "application/json"}
    ).status_code == 200
    obj2 = _json_fields(settings, amount="10000", order_id="fb-text-path")
    assert client.post(
        CUSTOM_PAYMENT_URL, content=json.dumps(obj2), headers={"Content-Type": "text/plain"}
    ).status_code == 200
    assert get_payment(session_factory, "fb-json-path").amount == 10000
    assert get_payment(session_factory, "fb-text-path").amount == 10000


def test_success_response_shape_unchanged(client, settings, session_factory, stub):
    body = json.dumps(_json_fields(settings, amount=10000, order_id="fb-shape"))
    response = _post_form(client, body)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}


def test_outbound_customer_bot_payload_unchanged(
    client, settings, session_factory, stub, bot_stub, notifier
):
    order_id = "fb-outbound"
    body = json.dumps(_json_fields(settings, amount=10000, order_id=order_id, legacy="x"))
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
