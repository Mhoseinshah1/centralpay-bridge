"""Blast-radius lock for POST /api/custom-payment body interpretation.

The raw-JSON-body compatibility branch
(``app.api.payments._try_recover_raw_json_body``) must be SURGICAL: it exists
for ONE evidenced malformed wire shape produced by a single legacy VPN-bot
account, and it must leave every other representation -- accepted or rejected
-- byte-identically alone.

This module is the proof. It enumerates every supported representation and
every currently-rejected malformed body as an explicit table of
(content type, body) -> (HTTP status, representation label or rejection
category), so any future parser change that silently reclassifies traffic
fails here rather than in production.

EVERY case in ``ACCEPTED`` and ``REJECTED`` below is pre-existing behaviour
that must be unchanged by the raw-JSON-body patch; the single new case is
isolated in ``NEWLY_ACCEPTED`` and is the ONLY row this patch may move. Running
this file against the parent commit must therefore fail on exactly the
``NEWLY_ACCEPTED`` rows and pass everything else -- see the PR description.

``test_the_new_branch_declines_every_pre_existing_representation`` adds a
structural guarantee independent of the HTTP surface: the new helper returns
None for every pre-existing body, so it cannot be capturing their traffic even
if some later refactor changed the call order.
"""

import json
import logging
from collections.abc import Callable
from urllib.parse import parse_qsl, quote

import pytest
from sqlalchemy import func, select

from app.api.payments import _try_recover_raw_json_body
from app.models import Payment
from tests.conftest import DEFAULT_REDIRECT_URL, get_events, get_payment

CUSTOM_PAYMENT_URL = "/api/custom-payment"
FORM_CT = "application/x-www-form-urlencoded"
JSON_CT = "application/json"
TEXT_CT = "text/plain"

# The project's standard sanitized validation failure (app.main).
SANITIZED_422 = {
    "error": {"code": "validation_error", "message": "Invalid request"},
    "detail": [{"loc": ["body"], "msg": "Invalid request body"}],
}


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def _fields(api_key: str, order_id: str, amount: object = 10000) -> dict[str, object]:
    return {"api_key": api_key, "amount": amount, "order_id": order_id}


def _post(client, body, content_type: str | None):
    headers = {"Content-Type": content_type} if content_type is not None else {}
    return client.post(CUSTOM_PAYMENT_URL, content=body, headers=headers)


def _normalized_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_normalized"]
    return rec


def _rejection_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    return rec


# --- the table ---------------------------------------------------------------
#
# Each builder takes (api_key, order_id) and returns the exact wire body.

def _plain_json(key, order):
    return json.dumps(_fields(key, order)), order


def _json_string_object(key, order):
    # A JSON document encoded as a JSON *string* (one extra layer).
    return json.dumps(json.dumps(_fields(key, order))), order


def _ordinary_form(key, order):
    return f"api_key={quote(key)}&amount=10000&order_id={quote(order)}", order


def _ordinary_form_with_extra(key, order):
    return f"api_key={quote(key)}&amount=10000&order_id={quote(order)}&legacy=x&sign=y", order


def _ordinary_form_with_alias(key, order):
    return f"api_key={quote(key)}&amount=10000&order_id={quote(order)}&user_id=6583754142", order


def _percent_encoded_json_key(key, order):
    # Legacy shape D: the whole JSON document percent-encoded as the form KEY
    # with an empty value.
    body = quote(json.dumps(_fields(key, order), separators=(",", ":")), safe="") + "="
    return body, order


def _raw_json_key_trailing_equals(key, order):
    # Legacy shape E: raw JSON as the form key, an unescaped internal '=' in a
    # value, plus the real trailing separator.
    stored = f"{order}=eq"
    return json.dumps(_fields(key, stored), separators=(",", ":")) + "=", stored


def _raw_json_no_equals(key, order):
    # Form content type but a raw JSON body containing NO '=' at all:
    # parse_qsl cannot tokenize it, so this has always used the JSON
    # syntax-error fallback.
    return json.dumps(_fields(key, order), separators=(",", ":")), order


def _raw_json_one_internal_equals(key, order):
    # THE new case: raw JSON body, one unescaped internal '=', no trailing '='.
    stored = f"{order}=eq"
    return json.dumps(_fields(key, stored), separators=(",", ":")), stored


# A builder maps (api_key, order_id) -> (wire body, order_id as stored).
_Builder = Callable[[str, str], tuple[str, str]]

# (case id, content type, body builder, expected representation label)
ACCEPTED: list[tuple[str, str | None, _Builder, str]] = [
    ("plain_json", JSON_CT, _plain_json, "json_object"),
    ("json_string_object", JSON_CT, _json_string_object, "json_string_object"),
    ("json_no_content_type", None, _plain_json, "json_object"),
    ("text_plain_json", TEXT_CT, _plain_json, "text_json"),
    ("ordinary_form", FORM_CT, _ordinary_form, "urlencoded"),
    ("ordinary_form_extra_fields", FORM_CT, _ordinary_form_with_extra, "urlencoded"),
    ("ordinary_form_with_alias", FORM_CT, _ordinary_form_with_alias, "urlencoded"),
    ("legacy_percent_encoded_json_key", FORM_CT, _percent_encoded_json_key, "urlencoded_json_key"),
    ("legacy_raw_json_key_trailing_eq", FORM_CT, _raw_json_key_trailing_equals,
     "urlencoded_raw_json_key"),
    ("form_ct_raw_json_no_equals", FORM_CT, _raw_json_no_equals, "urlencoded_json_object"),
]

# The ONLY row this patch is allowed to move from rejected to accepted.
NEWLY_ACCEPTED: list[tuple[str, str | None, _Builder, str]] = [
    (
        "form_ct_raw_json_one_internal_equals",
        FORM_CT,
        _raw_json_one_internal_equals,
        "urlencoded_raw_json_body",
    ),
]

# (case id, content type, literal body, expected rejection category)
REJECTED: list[tuple[str, str | None, str | bytes, str]] = [
    # G -- duplicate required fields (handling explicitly unchanged).
    ("duplicate_api_key", FORM_CT, "api_key=k&api_key=k2&amount=1&order_id=o", "urlencoded"),
    ("duplicate_amount", FORM_CT, "api_key=k&amount=1&amount=2&order_id=o", "urlencoded"),
    ("duplicate_order_id", FORM_CT, "api_key=k&amount=1&order_id=o&order_id=o2", "urlencoded"),
    # H -- missing required fields in an ordinary form.
    ("form_missing_all", FORM_CT, "foo=bar", "urlencoded"),
    ("form_missing_two", FORM_CT, "api_key=k", "urlencoded"),
    ("form_missing_one", FORM_CT, "api_key=k&amount=1", "urlencoded"),
    ("form_empty_body", FORM_CT, "", "urlencoded"),
    # I -- non-object JSON under the form content type.
    ("form_ct_json_array", FORM_CT, '["a","b"]', "urlencoded_unparseable"),
    ("form_ct_json_scalar", FORM_CT, "42", "urlencoded_unparseable"),
    ("form_ct_json_null", FORM_CT, "null", "urlencoded_unparseable"),
    ("form_ct_json_string", FORM_CT, '"a=b"', "urlencoded"),
    ("form_ct_malformed_json", FORM_CT, '{"a":"x=y"', "urlencoded"),
    ("form_ct_json_trailing_garbage", FORM_CT, '{"a":"x=y"}TRAILING', "urlencoded"),
    # Non-object JSON under the JSON / text content types.
    ("json_ct_array", JSON_CT, "[1,2]", "json_object"),
    ("json_ct_scalar", JSON_CT, "42", "json_object"),
    ("json_ct_malformed", JSON_CT, "{", "json_object"),
    ("text_ct_array", TEXT_CT, "[1,2]", "text_json"),
    # Unsupported media type.
    ("multipart", "multipart/form-data", "--x--", "unsupported"),
    ("octet_stream", "application/octet-stream", "{}", "unsupported"),
]


# --- accepted representations are unchanged ----------------------------------


@pytest.mark.parametrize(
    ("case_id", "content_type", "build", "representation"),
    [pytest.param(*row, id=row[0]) for row in ACCEPTED + NEWLY_ACCEPTED],
)
def test_accepted_representation_is_stable(
    client, settings, session_factory, stub, caplog, case_id, content_type, build, representation
):
    """Every supported representation still returns 200, still creates exactly
    one payment with the untouched amount, and is still labelled with its
    ORIGINAL representation string."""
    order_id = f"matrix-{case_id}"
    body, stored_order_id = build(settings.inbound_api_key, order_id)
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post(client, body, content_type)
    assert response.status_code == 200, response.text
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    assert _normalized_record(caplog).representation == representation
    payment = get_payment(session_factory, stored_order_id)
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)
    assert _payment_count(session_factory) == 1


# --- rejected bodies are unchanged -------------------------------------------


@pytest.mark.parametrize(
    ("case_id", "content_type", "body", "category"),
    [pytest.param(*row, id=row[0]) for row in REJECTED],
)
def test_rejected_body_is_stable(
    client, session_factory, stub, caplog, case_id, content_type, body, category
):
    """Every currently-rejected body still returns the SAME sanitized 422 under
    the SAME rejection category, and still has zero side effects."""
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post(client, body, content_type)
    assert response.status_code == 422, response.text
    assert response.json() == SANITIZED_422
    assert _rejection_record(caplog).representation == category
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


# --- J: the sanitized 422 contract itself ------------------------------------


def test_schema_invalid_returns_the_exact_sanitized_body(
    client, settings, session_factory, stub
):
    """A body that decodes cleanly but fails strict validation returns the
    documented sanitized payload verbatim -- never field values or the input."""
    body = json.dumps(_fields(settings.inbound_api_key, "schema-x", amount="10000.5"))
    response = _post(client, body, JSON_CT)
    assert response.status_code == 422
    assert response.json() == SANITIZED_422
    assert _payment_count(session_factory) == 0


# --- the structural blast-radius guarantee -----------------------------------


def test_the_new_branch_declines_every_pre_existing_representation(settings):
    """Independent of HTTP and of call ordering: ``_try_recover_raw_json_body``
    returns None for EVERY pre-existing body, accepted or rejected. It can
    therefore never be the branch that interprets their traffic."""
    bodies: list[str] = []
    for _case_id, _ct, build, _rep in ACCEPTED:
        bodies.append(build(settings.inbound_api_key, "blast-radius")[0])
    for _case_id, _ct, body, _cat in REJECTED:
        if isinstance(body, str):
            bodies.append(body)

    for body in bodies:
        try:
            pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            # Not urlencoded syntax at all -- the helper is unreachable for
            # this body (the caller only invokes it on a successful parse).
            continue
        assert _try_recover_raw_json_body(body, pairs) is None, body[:60]


def test_the_new_branch_claims_only_the_evidenced_shape(settings):
    """The positive half of the same guarantee."""
    body = json.dumps(
        _fields(settings.inbound_api_key, "claimed=eq"), separators=(",", ":")
    )
    pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
    assert len(pairs) == 1
    assert _try_recover_raw_json_body(body, pairs) == _fields(
        settings.inbound_api_key, "claimed=eq"
    )


def test_an_ordinary_form_is_never_reinterpreted_merely_for_missing_fields(settings):
    """The explicit scope rule: missing required fields alone must NOT trigger
    JSON reinterpretation. These bodies have zero recognized required fields
    yet are still declined, because they are not raw JSON objects."""
    for body in ("foo=bar", "a=1", "sig=abc", "payload=%7B%22a%22%3A1%7D"):
        pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        assert _try_recover_raw_json_body(body, pairs) is None, body
