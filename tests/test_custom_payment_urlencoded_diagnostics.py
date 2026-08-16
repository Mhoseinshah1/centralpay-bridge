"""Safe, narrowly-scoped structural diagnostics for the ONE legacy
urlencoded shape still unresolved after PR #55.

Post-deploy production evidence: PR #55's ``_try_recover_json_key_form``
recovers the "whole JSON object as the form key, empty value" shape, but at
least one customer's request still hits the exact pre-PR#55 rejection
signature:

    representation=urlencoded
    total_pair_count=1
    extra_field_count=1
    missing_required_fields=["api_key","amount","order_id"]
    duplicate_required_fields=[]

Since PR #55's recovery helper declines on this request, at least one of
its assumptions (exactly one pair / empty value / key is valid JSON object)
does not hold for this customer's actual wire body — and the existing log
fields cannot tell us WHICH assumption fails, because many structurally
different bodies produce that identical four-field summary (proven below:
"JSON in the key with a non-empty value", "JSON in the key but truncated by
an unescaped internal '='", "double-percent-encoded JSON", "JSON in the
VALUE instead of the key", "PHP-serialized data", a stray prefix/suffix
around otherwise-valid JSON, and "a JSON STRING containing escaped JSON"
(one extra encode layer, deliberately NOT unwrapped) all match it).

``_unrecovered_single_pair_diagnostics`` adds SAFE, non-secret structural
fields (lengths, booleans, and a fixed-vocabulary JSON-shape classification)
to the rejection log ONLY for this exact narrow shape (form content type,
exactly one pair, none of the three required field names present). It never
logs the raw key, raw value, raw body, api_key, order_id, or any other
customer-supplied content, and it changes NOTHING about the response the
client receives (still the same fixed sanitized 422) or about any other
rejection/acceptance path.

This is diagnostic-only: it does not attempt to recover any of these
shapes. The next production occurrence will carry enough structural
information in the log to identify which shape it is, informing a targeted
follow-up fix instead of another guess.
"""

import json
import logging
from urllib.parse import quote

import pytest
from sqlalchemy import func, select

from app.api.payments import (
    _json_shape_label,
    _unrecovered_single_pair_diagnostics,
)
from app.models import Payment
from tests.conftest import get_events

CUSTOM_PAYMENT_URL = "/api/custom-payment"
FORM_CT = "application/x-www-form-urlencoded"

_DIAGNOSTIC_FIELDS = (
    "key_length",
    "value_length",
    "value_empty",
    "key_json_type",
    "value_json_type",
    "key_starts_json_object",
    "key_ends_json_object",
    "raw_pair_equals_count",
)


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


def _assert_no_side_effects(session_factory, stub) -> None:
    assert _payment_count(session_factory) == 0
    assert get_events(session_factory) == []
    assert stub.getlink_requests == []


def _post_form(client, body: str):
    return client.post(CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": FORM_CT})


def _rejection_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    return rec


def _base_signature_matches(rec) -> None:
    """The four fields production evidence directly showed, unchanged."""
    assert rec.representation == "urlencoded"
    assert rec.total_pair_count == 1
    assert rec.extra_field_count == 1
    assert rec.missing_required_fields == ["api_key", "amount", "order_id"]
    assert rec.duplicate_required_fields == []


# --- unit: _json_shape_label ---------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a":1}', "object"),
        ("[1,2,3]", "array"),
        ('"a string"', "string"),
        ("42", "scalar"),
        ("true", "scalar"),
        ("null", "scalar"),
        ("{not valid json", "invalid"),
        ("", "invalid"),
        ("api_key", "invalid"),
    ],
)
def test_json_shape_label_classifies_correctly(text, expected):
    assert _json_shape_label(text) == expected


# --- unit: _unrecovered_single_pair_diagnostics ---------------------------------


def test_diagnostics_helper_reports_lengths_and_types_not_content():
    pair = ('{"a":"secret-value"}', "")
    raw_text = '%7B%22a%22%3A%22secret-value%22%7D='
    result = _unrecovered_single_pair_diagnostics(pair, raw_text)
    assert result["key_length"] == len(pair[0])
    assert result["value_length"] == 0
    assert result["value_empty"] is True
    assert result["key_json_type"] == "object"
    assert result["value_json_type"] == "invalid"  # empty string is not valid JSON
    assert result["key_starts_json_object"] is True
    assert result["key_ends_json_object"] is True
    assert result["raw_pair_equals_count"] == 1
    # The actual secret content never appears in any diagnostic VALUE.
    assert "secret-value" not in repr(result)


# --- integration: each remaining plausible shape reproduces the EXACT
#     persisting production signature, with a distinguishing fingerprint ------


def test_non_empty_value_json_key_diagnostics(client, settings, session_factory, stub, caplog):
    """Hypothesis 1/2: JSON in the key, but the pair's value is non-empty."""
    body = quote('{"a":"x"}', safe="") + "=1"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    _base_signature_matches(rec)
    assert rec.value_empty is False
    assert rec.key_json_type == "object"
    _assert_no_side_effects(session_factory, stub)


def test_unescaped_internal_equals_truncates_key(
    client, settings, session_factory, stub, caplog
):
    """Hypothesis 2: the sender fails to percent-encode an '=' that is part
    of the JSON content, so parse_qsl splits on the FIRST '=' and truncates
    the key mid-JSON. raw_pair_equals_count > 1 is the smoking gun."""
    body = '{"a":"x=y"}='  # literal, un-percent-encoded '=' inside the JSON
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    _base_signature_matches(rec)
    assert rec.raw_pair_equals_count == 2  # more than the one intentional separator
    assert rec.key_json_type == "invalid"  # truncated, unbalanced JSON
    assert rec.key_starts_json_object is True
    assert rec.key_ends_json_object is False
    assert rec.value_empty is False
    _assert_no_side_effects(session_factory, stub)


def test_double_percent_encoded_key_diagnostics(
    client, settings, session_factory, stub, caplog
):
    """Hypothesis 6: the whole thing was percent-encoded twice, so after
    parse_qsl's one decode pass the key still looks like percent-encoded
    text, not JSON."""
    once = quote('{"a":1}', safe="")
    twice = quote(once, safe="") + "="
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, twice)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    _base_signature_matches(rec)
    assert rec.key_json_type == "invalid"
    assert rec.key_starts_json_object is False  # starts with '%', not '{'
    assert rec.value_empty is True
    _assert_no_side_effects(session_factory, stub)


def test_json_in_value_not_key_diagnostics(client, settings, session_factory, stub, caplog):
    """Hypothesis 7: an arbitrary label as the key, JSON in the VALUE."""
    body = "data=" + quote('{"a":1}', safe="")
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    _base_signature_matches(rec)
    assert rec.key_json_type == "invalid"
    assert rec.value_json_type == "object"  # the tell: JSON lives in the value
    assert rec.key_length == len("data")
    _assert_no_side_effects(session_factory, stub)


def test_php_serialized_value_with_trailing_equals_diagnostics(
    client, settings, session_factory, stub, caplog
):
    """Hypothesis 5: a PHP serialize()-style container, not JSON at all."""
    body = 'a:1:{s:1:"a";i:1;}='
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    _base_signature_matches(rec)
    assert rec.key_json_type == "invalid"
    assert rec.key_starts_json_object is False
    _assert_no_side_effects(session_factory, stub)


def test_prefix_before_json_diagnostics(client, settings, session_factory, stub, caplog):
    """Hypothesis 4: a non-JSON prefix glued in front of otherwise-valid
    JSON (e.g. a "json:" or "payload:" label the sender forgot to strip)."""
    body = "json:" + quote('{"a":1}', safe="") + "="
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    _base_signature_matches(rec)
    assert rec.key_json_type == "invalid"
    assert rec.key_starts_json_object is False
    assert rec.key_ends_json_object is True  # the JSON suffix is well-formed
    _assert_no_side_effects(session_factory, stub)


def test_nested_json_string_diagnostics(client, settings, session_factory, stub, caplog):
    """Hypothesis 10: one extra JSON-string encoding layer -- the key
    decodes to a STRING containing escaped JSON, not an object directly.
    Deliberately NOT unwrapped (no recursive/extra-layer decoding)."""
    inner = json.dumps({"a": 1})
    double_encoded = json.dumps(inner)  # a JSON string containing JSON text
    body = quote(double_encoded, safe="") + "="
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    _base_signature_matches(rec)
    assert rec.key_json_type == "string"  # decisive: a string, not an object
    _assert_no_side_effects(session_factory, stub)


# --- narrow scoping: diagnostics NEVER appear outside the exact shape ---------


def test_diagnostics_absent_when_two_of_three_required_fields_missing(
    client, settings, session_factory, stub, caplog
):
    """A single real field alone (2 of 3 missing, not all 3) is a totally
    different, already-understood shape -- no new diagnostics."""
    body = f"api_key={settings.inbound_api_key}"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.missing_required_fields == ["amount", "order_id"]
    for field in _DIAGNOSTIC_FIELDS:
        assert not hasattr(rec, field), field


def test_diagnostics_absent_for_multi_pair_missing_field_rejection(
    client, settings, session_factory, stub, caplog
):
    body = "amount=1&order_id=o&extra=1"  # 3 pairs, api_key missing
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.total_pair_count == 3
    for field in _DIAGNOSTIC_FIELDS:
        assert not hasattr(rec, field), field


def test_diagnostics_absent_for_duplicate_field_rejection(
    client, settings, session_factory, stub, caplog
):
    body = f"api_key={settings.inbound_api_key}&amount=1&amount=2&order_id=o"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.duplicate_required_fields == ["amount"]
    for field in _DIAGNOSTIC_FIELDS:
        assert not hasattr(rec, field), field


def test_diagnostics_absent_for_too_many_pairs_rejection(
    client, settings, session_factory, stub, caplog
):
    from app.api.payments import _MAX_FORM_PAIRS

    body = "&".join(f"e{i}=v{i}" for i in range(_MAX_FORM_PAIRS + 5))
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded"
    for field in _DIAGNOSTIC_FIELDS:
        assert not hasattr(rec, field), field


def test_diagnostics_absent_for_urlencoded_unparseable(
    client, settings, session_factory, stub, caplog
):
    """An unescaped '&' inside the JSON splits parse_qsl into a syntax
    failure (a totally different code path -- the JSON fallback, which also
    fails here), never reaching the single-pair diagnostics at all."""
    body = '{"a":"x&y"}='
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    rec = _rejection_record(caplog)
    assert rec.representation == "urlencoded_unparseable"
    for field in _DIAGNOSTIC_FIELDS:
        assert not hasattr(rec, field), field


def test_diagnostics_absent_on_successful_recovery(
    client, settings, session_factory, stub, caplog
):
    """The already-fixed PR #55 shape succeeds and never reaches the
    rejection path at all -- no diagnostics, no rejection log record."""
    fields = {"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "diag-ok"}
    body = quote(json.dumps(fields), safe="") + "="
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    assert not [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]


# --- response body / behavior is completely unchanged --------------------------


def test_response_body_unchanged_by_diagnostics(client, settings, session_factory, stub):
    """The client-facing response never carries any diagnostic field —
    diagnostics are logged only, and the fixed sanitized envelope is
    unchanged for every one of the plausible shapes."""
    bodies = [
        quote('{"a":"x"}', safe="") + "=1",
        '{"a":"x=y"}=',
        "data=" + quote('{"a":1}', safe=""),
        'a:1:{s:1:"a";i:1;}=',
    ]
    for body in bodies:
        response = _post_form(client, body)
        assert response.status_code == 422
        assert response.json() == {
            "error": {"code": "validation_error", "message": "Invalid request"},
            "detail": [{"loc": ["body"], "msg": "Invalid request body"}],
        }


# --- no secret / raw-content leakage --------------------------------------------


def test_no_marker_leakage_across_all_diagnostic_shapes(
    client, settings, session_factory, stub, caplog
):
    marker = "SECRET-DIAG-MARKER-771a"
    marker_json = json.dumps({"a": marker})
    bodies = [
        quote(marker_json, safe="") + "=1",  # non-empty value
        '{"a":"' + marker + '=y"}=',  # unescaped internal '='
        "data=" + quote(marker_json, safe=""),  # JSON in value
        f"{marker}=",  # arbitrary unknown key, not JSON at all
        "json:" + quote(marker_json, safe="") + "=",  # prefixed
    ]
    for body in bodies:
        with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
            response = _post_form(client, body)
        assert response.status_code == 422
        assert marker not in response.text
        for record in caplog.records:
            assert marker not in repr(record.__dict__)
        caplog.clear()


def test_no_api_key_leakage_in_diagnostic_fields(client, settings, session_factory, stub, caplog):
    """Even when the real api_key value happens to appear inside the
    rejected JSON-shaped key, only lengths/booleans/labels are logged."""
    body = quote(json.dumps({"api_key": settings.inbound_api_key, "x": 1}), safe="") + "=1"
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    for record in caplog.records:
        assert settings.inbound_api_key not in repr(record.__dict__)
