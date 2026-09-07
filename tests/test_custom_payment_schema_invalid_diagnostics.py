"""Safe structural diagnostics for ``representation=schema_invalid``.

SECOND PRODUCTION CLASS — INSTRUMENTED, NOT FIXED.

Alongside the recovered raw-JSON-body shape, production also logged repeated::

    representation=schema_invalid
    content_type=application/x-www-form-urlencoded
    body_size=286 / 288 / 292

``schema_invalid`` is raised at the very END of
``parse_create_payment_request``: the body DECODED and normalized cleanly, and
then failed strict ``CreatePaymentRequest`` validation. So the failure is a
value/type problem, not a parsing problem — but ``_reject`` overwrote the
representation label with the fixed string ``schema_invalid``, so **the decoder
that produced the offending value was never recorded**. Under the form content
type, five different decoders can reach that point (``urlencoded``,
``urlencoded_json_key``, ``urlencoded_raw_json_key``, ``urlencoded_json_object``,
``urlencoded_json_string_object``), and the existing evidence cannot distinguish
them. Several mutually exclusive explanations fit equally well, for example:

  * an ordinary form whose ``amount`` is a non-ASCII-decimal string
    (``"10000.0"``, ``"10,000"``, Persian/Arabic digits, surrounding
    whitespace) — every form value arrives as a string, and ``_normalize``
    only converts a pure ASCII-decimal one;
  * a JSON body (via the fallback) whose ``amount`` is a float or boolean;
  * an ``order_id`` that is empty, over 128 characters, or carries a control
    character;
  * an ``api_key`` that is not a JSON string.

**No acceptance is broadened for this class in this change.** Guessing which
one it is would mean relaxing strict validation on unauthenticated input with
no evidence — exactly what must not happen. Instead
``_schema_invalid_diagnostics`` records the missing structural facts, all
drawn from our own field names, pydantic's fixed error slugs, JSON type names,
booleans, and lengths.

WHAT THE NEXT PRODUCTION EVENT WILL TELL US, precisely:

  * ``decoded_representation`` — which decoder produced the value. If it is
    ``urlencoded``, the sender is submitting a genuine form and the problem is
    a field VALUE; if it is one of the JSON labels, the sender is the
    JSON-over-form-content-type bot and the problem is a JSON type.
  * ``invalid_fields`` — which of api_key / amount / order_id failed.
  * ``invalid_error_types`` — pydantic's reason: ``int_type`` (amount was not
    an int), ``string_type``, ``greater_than`` (amount <= 0),
    ``string_too_long`` / ``string_too_short`` (order_id length),
    ``string_pattern_mismatch`` (control character in order_id), ``missing``.
  * ``field_types`` — the JSON type each field actually arrived as.
  * ``amount_is_ascii_decimal_string`` — separates "a numeric string
    ``_normalize`` already converts" from "a string it deliberately will not".
  * ``order_id_length`` — settles the 128-character hypothesis outright.

Together those pin the shape exactly, at which point a targeted decision can be
made on evidence. ``api_key``'s LENGTH is deliberately omitted (only its type
is reported) so an unauthenticated caller can never probe secret-adjacent
length information.

These tests assert both that the diagnostics are ACCURATE and that they never
carry a submitted value.
"""

import json
import logging

import pytest
from sqlalchemy import func, select

from app.api.payments import _json_type_name
from app.models import Payment
from tests.conftest import get_events

CUSTOM_PAYMENT_URL = "/api/custom-payment"
FORM_CT = "application/x-www-form-urlencoded"
JSON_CT = "application/json"


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def _rejection_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    return rec


def _post(client, body, content_type=JSON_CT):
    return client.post(
        CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": content_type}
    )


def _reject_and_record(client, caplog, body, content_type=JSON_CT):
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post(client, body, content_type)
    assert response.status_code == 422, response.text
    record = _rejection_record(caplog)
    assert record.representation == "schema_invalid"
    return record


# --- the type-name vocabulary is fixed and never echoes a value --------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "boolean"),  # checked BEFORE int: bool is an int subclass
        (False, "boolean"),
        (1, "integer"),
        (1.5, "number"),
        ("x", "string"),
        ({"a": 1}, "object"),
        ([1], "array"),
        (None, "null"),
    ],
)
def test_json_type_name_vocabulary(value, expected):
    assert _json_type_name(value) == expected


# --- the diagnostics are accurate --------------------------------------------


def test_records_the_decoded_representation_that_was_previously_lost(
    client, settings, caplog, session_factory
):
    """THE missing datum. A form body that decodes as an ordinary form and then
    fails validation now says so explicitly."""
    body = f"api_key={settings.inbound_api_key}&amount=10000.5&order_id=si-1"
    record = _reject_and_record(client, caplog, body, FORM_CT)
    assert record.decoded_representation == "urlencoded"
    assert record.invalid_fields == ["amount"]
    assert record.field_types["amount"] == "string"
    assert record.amount_is_ascii_decimal_string is False
    assert _payment_count(session_factory) == 0


def test_distinguishes_a_json_body_sent_under_the_form_content_type(
    client, settings, caplog
):
    """The other side of the ambiguity: same content type, same
    ``schema_invalid`` label, but a JSON decoder produced the value."""
    body = json.dumps({"api_key": settings.inbound_api_key, "amount": 10000.5,
                       "order_id": "si-2"})
    record = _reject_and_record(client, caplog, body, FORM_CT)
    assert record.decoded_representation == "urlencoded_json_object"
    assert record.invalid_fields == ["amount"]
    # A JSON float, NOT a string — the fact that separates the two hypotheses.
    assert record.field_types["amount"] == "number"
    assert record.amount_is_ascii_decimal_string is False


def test_distinguishes_the_new_raw_json_body_representation(client, settings, caplog):
    body = json.dumps(
        {"api_key": settings.inbound_api_key, "amount": 10000.5, "order_id": "si=3"},
        separators=(",", ":"),
    )
    record = _reject_and_record(client, caplog, body, FORM_CT)
    assert record.decoded_representation == "urlencoded_raw_json_body"
    assert record.field_types["amount"] == "number"


@pytest.mark.parametrize(
    ("amount", "expected_type", "ascii_decimal"),
    [
        (10000.5, "number", False),
        (True, "boolean", False),
        ("10,000", "string", False),
        ("10000.0", "string", False),
        (" 10000", "string", False),
        ("\u06f1\u06f0\u06f0\u06f0\u06f0", "string", False),  # Persian digits
        (0, "integer", False),
        (-5, "integer", False),
        (None, "null", False),
        ({"v": 1}, "object", False),
        ([1], "array", False),
    ],
)
def test_amount_shapes_are_classified_without_echoing_the_value(
    client, settings, caplog, amount, expected_type, ascii_decimal
):
    body = json.dumps({"api_key": settings.inbound_api_key, "amount": amount,
                       "order_id": "si-amt"})
    record = _reject_and_record(client, caplog, body)
    assert record.invalid_fields == ["amount"]
    assert record.field_types["amount"] == expected_type
    assert record.amount_is_ascii_decimal_string is ascii_decimal


def test_order_id_length_hypothesis_is_settled_by_the_diagnostics(
    client, settings, caplog
):
    """``order_id_length`` answers the over-128 hypothesis directly."""
    long_order = "o" * 129
    record = _reject_and_record(
        client,
        caplog,
        json.dumps({"api_key": settings.inbound_api_key, "amount": 10000,
                    "order_id": long_order}),
    )
    assert record.invalid_fields == ["order_id"]
    assert record.order_id_length == 129
    assert "string_too_long" in record.invalid_error_types


def test_control_character_order_id_reports_a_pattern_mismatch(client, settings, caplog):
    record = _reject_and_record(
        client,
        caplog,
        json.dumps({"api_key": settings.inbound_api_key, "amount": 10000,
                    "order_id": "bad\x01id"}),
    )
    assert record.invalid_fields == ["order_id"]
    assert "string_pattern_mismatch" in record.invalid_error_types


def test_non_string_api_key_is_reported_by_type_only(client, caplog):
    record = _reject_and_record(
        client, caplog, json.dumps({"api_key": 12345, "amount": 10000, "order_id": "si-k"})
    )
    assert record.invalid_fields == ["api_key"]
    assert record.field_types["api_key"] == "integer"
    # No api_key length is ever emitted.
    assert not any(k.startswith("api_key_len") for k in vars(record))


def test_missing_field_is_reported_as_missing(client, settings, caplog):
    record = _reject_and_record(
        client, caplog, json.dumps({"api_key": settings.inbound_api_key, "amount": 10000})
    )
    assert record.invalid_fields == ["order_id"]
    assert record.field_types["order_id"] == "missing"
    assert record.order_id_length is None
    assert "missing" in record.invalid_error_types


def test_several_invalid_fields_are_all_reported(client, caplog):
    record = _reject_and_record(
        client, caplog, json.dumps({"api_key": 1, "amount": "x", "order_id": ""})
    )
    assert record.invalid_fields == ["amount", "api_key", "order_id"]
    assert record.order_id_length == 0


# --- K: no values, no secrets, ever ------------------------------------------


def test_diagnostics_never_carry_values_or_secrets(client, settings, caplog):
    probe_order_id = "DISTINCTIVE-ORDER-ID-abcdef"
    configured_key = settings.inbound_api_key
    record = _reject_and_record(
        client,
        caplog,
        json.dumps({"api_key": configured_key, "amount": "NOT-A-NUMBER-XYZ",
                    "order_id": probe_order_id, "user_id": 6583754142}),
    )
    blob = json.dumps({k: str(v) for k, v in vars(record).items() if not k.startswith("_")})
    for leaked in (configured_key, probe_order_id, "NOT-A-NUMBER-XYZ", "6583754142"):
        assert leaked not in blob, leaked


def test_diagnostic_values_are_only_names_types_flags_and_lengths(client, caplog):
    """Every emitted diagnostic is drawn from a fixed vocabulary or is a
    number/boolean — structurally proving no free-form content escapes."""
    record = _reject_and_record(
        client, caplog, json.dumps({"api_key": 1, "amount": 1.5, "order_id": "ok"})
    )
    allowed_names = {"api_key", "amount", "order_id", "other"}
    allowed_types = {"object", "array", "string", "integer", "number", "boolean",
                     "null", "unknown", "missing"}
    assert set(record.invalid_fields) <= allowed_names
    assert set(record.field_types) == {"api_key", "amount", "order_id"}
    assert set(record.field_types.values()) <= allowed_types
    assert isinstance(record.amount_is_ascii_decimal_string, bool)
    assert record.order_id_length is None or isinstance(record.order_id_length, int)
    # pydantic error slugs are snake_case identifiers, never input text.
    for slug in record.invalid_error_types:
        assert slug.replace("_", "").isalnum(), slug


def test_schema_invalid_still_returns_the_sanitized_body_unchanged(
    client, settings, session_factory, stub, caplog
):
    """Diagnostics are log-only: the HTTP response is byte-identical to before."""
    response = _post(
        client,
        json.dumps({"api_key": settings.inbound_api_key, "amount": 1.5,
                    "order_id": "si-resp"}),
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "Invalid request"},
        "detail": [{"loc": ["body"], "msg": "Invalid request body"}],
    }
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


def test_acceptance_is_not_broadened_for_this_class(client, settings, session_factory, stub):
    """The instrumented shapes are still REJECTED — this change records why,
    it does not start accepting anything."""
    for amount in (10000.5, True, "10,000", "10000.0", " 10000", 0, -1):
        body = json.dumps({"api_key": settings.inbound_api_key, "amount": amount,
                           "order_id": "si-strict"})
        assert _post(client, body).status_code == 422, amount
    assert _payment_count(session_factory) == 0
    assert stub.getlink_requests == []
