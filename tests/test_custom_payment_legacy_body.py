"""Legacy-body compatibility for POST /api/custom-payment.

Some legacy customer bots do not POST a plain JSON object: they send the
same three fields (api_key, amount, order_id) as a JSON string, as
application/x-www-form-urlencoded, or as text/plain containing JSON, and
they sometimes send ``amount`` as a decimal *string*. FastAPI's default
body binding rejected all of those with a 422 before the route ran.

``parse_create_payment_request`` normalizes the *allowed* representations
into a {api_key, amount, order_id} dict and validates it with the SAME
strict ``CreatePaymentRequest`` model. This suite proves:

- every accepted representation reaches the strict model and creates a
  payment identical to the canonical JSON-object request;
- every disallowed representation, malformed amount, and malformed
  order_id is rejected with the project's sanitized 422 and produces no
  payment row, no audit event, and no gateway traffic;
- the api_key is never echoed in an error and no field value (in
  particular order_id) is logged before authentication;
- the success response and the outbound customer-bot callback are
  byte-for-byte unchanged.

No real external service is contacted: CentralPay and the customer bot are
faked at the httpx transport layer via the shared fixtures.
"""

import json
import logging

import pytest
from sqlalchemy import func, select

from app.models import Payment
from tests.conftest import (
    DEFAULT_REDIRECT_URL,
    get_events,
    get_payment,
    run_pass,
    valid_callback_path,
    verify_ok_response,
)

CUSTOM_PAYMENT_URL = "/api/custom-payment"


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def _assert_no_side_effects(session_factory, stub) -> None:
    """A rejected request must not touch the database or the gateway."""
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


def _post_raw(client, body, content_type: str | None):
    headers = {} if content_type is None else {"Content-Type": content_type}
    return client.post(CUSTOM_PAYMENT_URL, content=body, headers=headers)


def _valid_fields(settings, *, amount, order_id="legacy-order-1"):
    return {"api_key": settings.inbound_api_key, "amount": amount, "order_id": order_id}


# --- accepted representations ------------------------------------------------


def test_plain_json_object_still_works(client, settings, session_factory, stub):
    """Backward compatibility: the canonical body is untouched."""
    body = json.dumps(_valid_fields(settings, amount=10000, order_id="json-int"))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    payment = get_payment(session_factory, "json-int")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


def test_json_object_with_amount_as_decimal_string(client, settings, session_factory, stub):
    """The legacy bot sends amount as a string; it is converted to int."""
    body = json.dumps(_valid_fields(settings, amount="10000", order_id="json-str-amt"))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 200
    payment = get_payment(session_factory, "json-str-amt")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


def test_json_string_containing_one_object(client, settings, session_factory, stub):
    """A JSON *string* whose content is one JSON object (one extra layer)."""
    inner = json.dumps(_valid_fields(settings, amount=10000, order_id="json-string-obj"))
    body = json.dumps(inner)  # a JSON string, not an object
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 200
    assert get_payment(session_factory, "json-string-obj").amount == 10000


def test_json_string_object_with_decimal_string_amount(client, settings, session_factory, stub):
    inner = json.dumps(_valid_fields(settings, amount="10000", order_id="json-string-str-amt"))
    body = json.dumps(inner)
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 200
    assert get_payment(session_factory, "json-string-str-amt").amount == 10000


def test_urlencoded_exact_fields(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}&amount=10000&order_id=urlenc-1"
    response = _post_raw(client, body, "application/x-www-form-urlencoded")
    assert response.status_code == 200
    payment = get_payment(session_factory, "urlenc-1")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


def test_text_plain_json_object(client, settings, session_factory, stub):
    body = json.dumps(_valid_fields(settings, amount=10000, order_id="text-json"))
    response = _post_raw(client, body, "text/plain")
    assert response.status_code == 200
    assert get_payment(session_factory, "text-json").amount == 10000


def test_text_plain_json_string_object(client, settings, session_factory, stub):
    inner = json.dumps(_valid_fields(settings, amount="10000", order_id="text-json-string"))
    body = json.dumps(inner)
    response = _post_raw(client, body, "text/plain")
    assert response.status_code == 200
    assert get_payment(session_factory, "text-json-string").amount == 10000


def test_absent_content_type_treated_as_json(client, settings, session_factory, stub):
    """An omitted Content-Type keeps the historical JSON default working."""
    body = json.dumps(_valid_fields(settings, amount=10000, order_id="no-ctype"))
    response = _post_raw(client, body, None)
    assert response.status_code == 200
    assert get_payment(session_factory, "no-ctype").amount == 10000


def test_json_content_type_with_charset_parameter(client, settings, session_factory, stub):
    """Content-Type parameters (charset) are stripped before matching."""
    body = json.dumps(_valid_fields(settings, amount=10000, order_id="json-charset"))
    response = _post_raw(client, body, "application/json; charset=utf-8")
    assert response.status_code == 200
    assert get_payment(session_factory, "json-charset").amount == 10000


def test_urlencoded_order_id_passed_through_byte_exact(client, settings, session_factory, stub):
    """order_id stays opaque through the urlencoded path — decoded but not
    trimmed, case-folded, or normalized."""
    order_id = "Order ABC-1"  # a space survives percent-decoding unchanged
    body = f"api_key={settings.inbound_api_key}&amount=10000&order_id=Order%20ABC-1"
    response = _post_raw(client, body, "application/x-www-form-urlencoded")
    assert response.status_code == 200
    assert get_payment(session_factory, order_id).bot_order_id == order_id


# --- disallowed representations ----------------------------------------------


def test_multipart_is_controlled_422_not_500(client, settings, session_factory, stub):
    """multipart/form-data has no safe parser here — reject cleanly, never a
    500, and never add a multipart dependency."""
    response = client.post(
        CUSTOM_PAYMENT_URL, files={"amount": (None, "10000")}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    _assert_no_side_effects(session_factory, stub)


def test_arbitrary_binary_content_type_rejected(client, settings, session_factory, stub):
    body = json.dumps(_valid_fields(settings, amount=10000))
    response = _post_raw(client, body, "application/octet-stream")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    _assert_no_side_effects(session_factory, stub)


def test_json_array_rejected(client, settings, session_factory, stub):
    body = json.dumps([_valid_fields(settings, amount=10000)])
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_json_string_containing_array_rejected(client, settings, session_factory, stub):
    body = json.dumps(json.dumps([1, 2, 3]))  # string -> array, not object
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_triple_encoded_json_rejected(client, settings, session_factory, stub):
    """At most ONE extra decode layer: a string containing a string
    containing an object must not be unwrapped recursively."""
    inner = json.dumps(_valid_fields(settings, amount=10000))
    body = json.dumps(json.dumps(inner))  # string -> string -> object
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_invalid_json_rejected(client, settings, session_factory, stub):
    response = _post_raw(client, "{not json", "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_urlencoded_duplicate_field_rejected(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}&amount=10000&amount=20000&order_id=dup"
    response = _post_raw(client, body, "application/x-www-form-urlencoded")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_urlencoded_extra_field_rejected(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}&amount=10000&order_id=x&extra=1"
    response = _post_raw(client, body, "application/x-www-form-urlencoded")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_urlencoded_missing_field_rejected(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}&amount=10000"  # no order_id
    response = _post_raw(client, body, "application/x-www-form-urlencoded")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


# --- amount normalization: only ASCII [0-9]+ strings convert ------------------


@pytest.mark.parametrize(
    "amount",
    [
        "10000.0",  # decimal point
        "10000.5",
        "1e4",  # exponent
        "+50000",  # explicit sign
        "-100",  # negative sign
        "50,000",  # thousands separator
        " 50000",  # leading whitespace
        "50000 ",  # trailing whitespace
        "50_000",  # underscore separator (valid for int() but not the contract)
        "۵۰۰۰۰",  # Persian digits for 50000 (not ASCII)
        "٥٠٠٠٠",  # Arabic-Indic digits for 50000
        "0x10",  # hex
        "",  # empty
        "abc",
    ],
)
def test_non_ascii_decimal_amount_strings_rejected(
    client, settings, session_factory, stub, amount
):
    """Only a bare ASCII [0-9]+ string is converted; every other string shape
    is left untouched for StrictInt to reject."""
    body = json.dumps(_valid_fields(settings, amount=amount, order_id="amt-bad"))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize("amount", [True, False, 10000.0, 10000.5, [10000], {"v": 1}, None])
def test_non_string_non_int_amounts_rejected(client, settings, session_factory, stub, amount):
    body = json.dumps(_valid_fields(settings, amount=amount, order_id="amt-type"))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize("amount", [0, "0", -100, 1_000_000_000_001, "1000000000001"])
def test_out_of_range_amounts_rejected(client, settings, session_factory, stub, amount):
    """Zero, negatives, and values past the absolute schema backstop are
    rejected whether sent as int or ASCII-decimal string."""
    body = json.dumps(_valid_fields(settings, amount=amount, order_id="amt-range"))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_absurdly_long_digit_string_rejected(client, settings, session_factory, stub):
    """A digit string past Python's int-string conversion limit is left as a
    string (int() raises) and rejected by the model — never a 500."""
    body = json.dumps(_valid_fields(settings, amount="1" * 6000, order_id="amt-huge"))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


# --- order_id / api_key policy through the compat layer ----------------------


@pytest.mark.parametrize(
    "order_id",
    ["", "a\x00b", "a\nb", "a\x7fb", "x" * 129],
)
def test_invalid_order_ids_rejected(client, settings, session_factory, stub, order_id):
    body = json.dumps(_valid_fields(settings, amount=10000, order_id=order_id))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize("missing", ["api_key", "amount", "order_id"])
def test_missing_required_field_rejected(client, settings, session_factory, stub, missing):
    fields = _valid_fields(settings, amount=10000, order_id="miss")
    del fields[missing]
    response = _post_raw(client, json.dumps(fields), "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize("field", ["api_key", "amount", "order_id"])
def test_null_required_field_rejected(client, settings, session_factory, stub, field):
    fields = _valid_fields(settings, amount=10000, order_id="null-field")
    fields[field] = None
    response = _post_raw(client, json.dumps(fields), "application/json")
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_wrong_api_key_is_401_not_created(client, settings, session_factory, stub):
    """A well-formed body with a wrong key normalizes fine but fails auth —
    the compat layer never weakens the constant-time key check."""
    body = json.dumps(_valid_fields(settings, amount=10000, order_id="badkey"))
    body = body.replace(settings.inbound_api_key, "wrong-key-value")
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 401
    _assert_no_side_effects(session_factory, stub)


# --- request-size bound ------------------------------------------------------


def test_oversized_body_rejected_before_decode(client, settings, session_factory, stub):
    """A body past the 64 KB edge limit is rejected up front (Content-Length),
    with a controlled 422 and no gateway traffic."""
    fields = _valid_fields(settings, amount=10000, order_id="x" * 70000)
    response = _post_raw(client, json.dumps(fields), "application/json")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    _assert_no_side_effects(session_factory, stub)


# --- secrets / observability -------------------------------------------------


def test_api_key_never_echoed_in_error(client, settings, session_factory, stub):
    body = json.dumps(_valid_fields(settings, amount="not-a-number", order_id="echo"))
    response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    assert settings.inbound_api_key not in response.text


def test_rejected_body_does_not_log_field_values(
    client, settings, session_factory, stub, caplog
):
    """A pre-auth rejection logs only representation/content_type/body_size —
    never the api_key and never the attacker-supplied order_id."""
    secret_order = "SECRET-ORDER-DO-NOT-LOG"
    body = json.dumps(_valid_fields(settings, amount="bad", order_id=secret_order))
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_raw(client, body, "application/json")
    assert response.status_code == 422
    rejected = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    assert rejected, "expected a custom_payment_body_rejected diagnostic"
    for record in caplog.records:
        blob = repr(record.__dict__)
        assert secret_order not in blob
        assert settings.inbound_api_key not in blob


def test_normalized_log_carries_only_safe_metadata(
    client, settings, session_factory, stub, caplog
):
    """The accepted-body observability event carries representation,
    content_type, and body_size only — no field values."""
    order_id = "OBSERVE-ORDER-1"
    body = json.dumps(_valid_fields(settings, amount=10000, order_id=order_id))
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post_raw(client, body, "application/json")
    assert response.status_code == 200
    events = [r for r in caplog.records if r.getMessage() == "custom_payment_body_normalized"]
    assert len(events) == 1
    record = events[0]
    assert record.representation == "json_object"
    assert record.content_type == "application/json"
    assert isinstance(record.body_size, int)
    blob = repr(record.__dict__)
    assert order_id not in blob
    assert settings.inbound_api_key not in blob
    assert "10000" not in blob


# --- unchanged downstream contract -------------------------------------------


def test_success_response_shape_unchanged(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}&amount=10000&order_id=shape"
    response = _post_raw(client, body, "application/x-www-form-urlencoded")
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}


def test_outbound_customer_bot_callback_unchanged(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A payment created via a legacy (urlencoded) body still produces the
    exact, unchanged outbound customer-bot notification payload."""
    order_id = "outbound-1"
    # Create through the legacy urlencoded representation.
    body = f"api_key={settings.inbound_api_key}&amount=10000&order_id={order_id}"
    assert _post_raw(client, body, "application/x-www-form-urlencoded").status_code == 200
    payment = get_payment(session_factory, order_id)
    # Verify it via the signed callback, leaving it pending notification.
    stub.verify_result = verify_ok_response(amount=10000, reference_id=f"REF-{order_id}")
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    # One worker pass delivers the unchanged outbound payload.
    result = run_pass(session_factory, notifier, settings)
    assert result["processed"] == 1
    [request] = bot_stub.requests
    assert request == {"order_id": order_id, "actions": "custom_payment_verify"}
