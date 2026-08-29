"""application/x-www-form-urlencoded compatibility: a raw (NOT
percent-encoded) JSON document used as the KEY of a single form pair, where a
literal ``=`` inside the JSON content itself was left unescaped.

Production incident (confirmed by evidence, not guessed): the inaccessible
legacy sales bot serves ~200k customers with the same request code; a tiny
number of them trigger three consecutive production rejections carrying this
exact fingerprint:

    representation=urlencoded
    content_type=application/x-www-form-urlencoded
    total_pair_count=1
    extra_field_count=1
    missing_required_fields=["api_key","amount","order_id"]
    duplicate_required_fields=[]
    value_empty=false
    key_json_type=invalid
    value_json_type=invalid
    key_starts_json_object=true
    key_ends_json_object=false
    raw_pair_equals_count=2

PR #56's diagnostics (tests/test_custom_payment_urlencoded_diagnostics.py)
were added specifically to distinguish which of several plausible single-pair
shapes production was hitting. This exact fingerprint matches ONE hypothesis
precisely: the sender constructs a body conceptually like ``{"a":"x=y"}=``
with ``Content-Type: application/x-www-form-urlencoded`` -- a complete JSON
object as the form KEY, followed by the normal trailing ``=`` separator, but
an ``=`` character that is part of the JSON content itself is NOT
percent-encoded. ``parse_qsl`` splits on the FIRST literal ``=`` it finds,
truncating the key mid-document: the key starts with ``{`` but no longer ends
with ``}`` and is invalid JSON standing alone; the value is the JSON's
remainder plus the real trailing separator, and is therefore non-empty.
``raw_pair_equals_count == 2`` is the smoking gun (more than the one
intentional separator).

PR #55 (tests/test_custom_payment_urlencoded_json_key.py) already recovers
the sibling shape where the WHOLE JSON document is percent-encoded, so no
internal ``=`` is ever literal and the pair's value is empty.
``_try_recover_raw_json_key_with_unescaped_equals`` recovers this new,
narrower sibling: it reconstructs the candidate from the COMPLETE raw wire
TEXT (never the parse_qsl-truncated key/value), stripping ONLY the one known
trailing ``=`` form separator, and accepts it ONLY when what remains parses
DIRECTLY to a JSON object. The recovered object is then fed through the
EXACT SAME pipeline as every other representation (``_normalize`` ->
``CreatePaymentRequest`` -> identity extraction -> authentication -> amount
policy -> rate limiting -> idempotency -> payment creation -> getLink). No
separate, weaker validation path.

These tests exercise the real route through the strict model and fake both
CentralPay and the customer bot at the httpx transport layer (shared
fixtures) -- no real external service is contacted.
"""

import json
import logging
from urllib.parse import quote

import pytest
from sqlalchemy import func, select

from app.api.payments import (
    _decode_urlencoded,
    _try_recover_raw_json_key_with_unescaped_equals,
    _UrlencodedSyntaxError,
)
from app.models import Payment
from app.services.payer_identity import (
    IDENTITY_TYPE_ORDER_FALLBACK,
    IDENTITY_TYPE_TELEGRAM_USER,
)
from tests.conftest import (
    DEFAULT_REDIRECT_URL,
    expected_gateway_user_id,
    get_events,
    get_payment,
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


def _raw_json_key_body(fields: dict[str, object]) -> str:
    """The confirmed production wire shape: the whole JSON document sent
    RAW (never percent-encoded) as one form KEY, followed by a bare ``=``.
    Any ``=`` inside a string value is therefore literal, unescaped wire
    content -- exactly what production sent."""
    return json.dumps(fields, separators=(",", ":")) + "="


def _valid_fields(
    settings, *, amount=10000, order_id="uneq-order", **extra
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
    pair = ('{"a":"x', 'y"}')
    assert _try_recover_raw_json_key_with_unescaped_equals('{"a":"x=y"}=', [pair]) == {"a": "x=y"}
    assert (
        _try_recover_raw_json_key_with_unescaped_equals('{"a":"x=y"}=', [pair, ("b", "c")])
        is None
    )


def test_recover_helper_requires_non_empty_value():
    # The empty-value sibling is _try_recover_json_key_form's territory.
    assert (
        _try_recover_raw_json_key_with_unescaped_equals('{"a":"x=y"}', [('{"a":"x=y"}', "")])
        is None
    )


def test_recover_helper_rejects_real_field_names():
    recover = _try_recover_raw_json_key_with_unescaped_equals
    assert recover("api_key=v=", [("api_key", "v=")]) is None
    assert recover("amount=v=", [("amount", "v=")]) is None
    assert recover("order_id=v=", [("order_id", "v=")]) is None
    assert recover("user_id=v=", [("user_id", "v=")]) is None


def test_recover_helper_requires_more_than_one_literal_equals():
    # Exactly one '=' is the ordinary trailing separator -- already the
    # unmodified pre-existing path, never this recovery.
    assert (
        _try_recover_raw_json_key_with_unescaped_equals('{"a":1}', [('{"a":1}', "")]) is None
    )


def test_recover_helper_requires_trailing_equals():
    text = '{"a":"x=y"}'  # two literal '=' but no trailing separator at all
    assert _try_recover_raw_json_key_with_unescaped_equals(text, [("k", "v")]) is None


def test_recover_helper_rejects_when_candidate_is_not_valid_json():
    # Trailing garbage after the JSON object survives trimming ONLY the
    # final '=' -- must not be searched for or stripped.
    text = '{"a":"x=y"}TRAILING='
    pairs = [('{"a":"x', 'y"}TRAILING=')]
    assert _try_recover_raw_json_key_with_unescaped_equals(text, pairs) is None


def test_recover_helper_rejects_non_object_json():
    for text in ['[1,"=",2]=', '"a=b"=', "42=", "true="]:
        pairs = [("k", "v=")]  # gating fields are irrelevant once JSON shape fails
        assert _try_recover_raw_json_key_with_unescaped_equals(text, pairs) is None


def test_recover_helper_never_decodes_more_than_one_layer():
    # A JSON STRING containing escaped JSON (one extra layer) must not be
    # unwrapped -- json.loads(candidate) must return a dict DIRECTLY.
    inner = json.dumps({"a": "x=y"})
    text = json.dumps(inner) + "="
    assert text.count("=") > 1
    pairs = [(text.split("=", 1)[0], text.split("=", 1)[1])]
    assert _try_recover_raw_json_key_with_unescaped_equals(text, pairs) is None


# --- unit: _decode_urlencoded returns the new label directly ------------------


def test_decode_urlencoded_returns_raw_json_key_label():
    body = '{"api_key":"k","amount":1,"order_id":"a=b"}='
    rep, data = _decode_urlencoded(body.encode())
    assert rep == "urlencoded_raw_json_key"
    assert data == {"api_key": "k", "amount": 1, "order_id": "a=b"}


def test_decode_urlencoded_still_syntax_errors_on_unescaped_ampersand():
    # An unescaped '&' inside the JSON creates a real (if malformed) pair
    # boundary, so parse_qsl itself fails to tokenize the body -- a totally
    # different failure mode (_UrlencodedSyntaxError, not _CompatReject) that
    # never reaches this recovery at all. See
    # test_diagnostics_absent_for_urlencoded_unparseable for the full-stack
    # behavior once the caller's JSON fallback also declines.
    with pytest.raises(_UrlencodedSyntaxError):
        _decode_urlencoded(b'{"a":"x&y=z"}=')


# --- MANDATORY: the exact reproduced production incident ----------------------


def test_confirmed_production_fingerprint_body_now_succeeds(
    client, settings, session_factory, stub, caplog
):
    """The exact confirmed production shape: api_key/amount/order_id/user_id
    as a raw JSON object used as the form key, with order_id containing an
    unescaped internal '=' (so the sender's literal wire bytes -- not a
    round-tripped encoder -- carry a real un-percent-encoded '='), followed
    by the bare trailing '=' separator.

    Before this fix, ``_decode_urlencoded`` raised ``_CompatReject`` with
    total_pair_count=1, extra_field_count=1,
    missing_required_fields=["api_key","amount","order_id"],
    duplicate_required_fields=[], and (via PR #56's diagnostics)
    raw_pair_equals_count=2, key_json_type="invalid",
    key_starts_json_object=True, key_ends_json_object=False,
    value_empty=False -- byte-for-byte matching the production log. See the
    PR description for the git-stash proof that this body 422s on
    unmodified main. It now succeeds through the unmodified strict-model
    pipeline.
    """
    fields = {
        "api_key": settings.inbound_api_key,
        "amount": 10000,
        "order_id": "legacy=edge-case",
        "user_id": 6583754142,
    }
    body = _raw_json_key_body(fields)
    assert body.count("=") == 2  # order_id's one internal '=' + the trailing separator
    assert body.endswith("=")
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    payment = get_payment(session_factory, "legacy=edge-case")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=6583754142)
    assert _normalized_record(caplog).representation == "urlencoded_raw_json_key"


def test_decoder_reproduces_exact_production_diagnostics_before_recovery():
    """Direct proof, independent of the fix, that this wire shape produces
    the EXACT diagnostic fields observed in production once parse_qsl
    tokenizes it -- confirming the root cause: parse_qsl truncates the key
    at the first literal '=', unconditionally, regardless of any recovery
    logic layered on top."""
    from urllib.parse import parse_qsl

    payload = {
        "api_key": "sk_live_example",
        "amount": 50000,
        "order_id": "order=abc-123",
    }
    wire_body = json.dumps(payload, separators=(",", ":")) + "="
    pairs = parse_qsl(wire_body, keep_blank_values=True, strict_parsing=True)
    assert len(pairs) == 1
    key, value = pairs[0]
    assert value != ""
    assert key.startswith("{")
    assert not key.rstrip().endswith("}")
    with pytest.raises(ValueError):
        json.loads(key)  # truncated, unbalanced JSON -- confirms the corruption
    assert wire_body.count("=") == 2


# --- additional success tests ---------------------------------------------------


def test_equals_inside_order_id_succeeds(client, settings, session_factory, stub):
    body = _raw_json_key_body(_valid_fields(settings, amount=10000, order_id="abc=def"))
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "abc=def")
    assert payment.amount == 10000


def test_multiple_equals_inside_order_id_succeeds(client, settings, session_factory, stub):
    body = _raw_json_key_body(_valid_fields(settings, amount=10000, order_id="abc==def"))
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "abc==def")
    assert payment.amount == 10000


@pytest.mark.parametrize("alias", ["user_id", "userId", "uid", "chat_id", "telegram_id"])
def test_identity_alias_reaches_normal_identity_path(
    client, settings, session_factory, stub, alias
):
    body = _raw_json_key_body(
        _valid_fields(
            settings, amount=10000, order_id=f"uneq-al-{alias}=x", **{alias: 6583754142}
        )
    )
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, f"uneq-al-{alias}=x")
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=6583754142)


def test_no_identity_alias_falls_back_to_order_isolation(client, settings, session_factory, stub):
    body = _raw_json_key_body(_valid_fields(settings, amount=10000, order_id="uneq-noident=x"))
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "uneq-noident=x")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK


def test_ascii_decimal_amount_string_converted_exactly_like_existing_compat(
    client, settings, session_factory, stub
):
    body = _raw_json_key_body(_valid_fields(settings, amount="10000", order_id="uneq-str-amt=x"))
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "uneq-str-amt=x")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


def test_harmless_extra_json_fields_dropped_same_as_existing_policy(
    client, settings, session_factory, stub
):
    body = _raw_json_key_body(
        _valid_fields(settings, amount=10000, order_id="uneq-extra=x", tag="promo", note="hi")
    )
    response = _post_form(client, body)
    assert response.status_code == 200
    payment = get_payment(session_factory, "uneq-extra=x")
    assert payment.amount == 10000


# --- MUST REMAIN REJECTED: adjacent ambiguous shapes never accepted -----------


def test_json_object_in_non_empty_form_value_still_rejected(
    client, settings, session_factory, stub, caplog
):
    """``data=<json>`` -- JSON lives in the VALUE, not the key; the key
    ``data`` never starts with ``{`` so the candidate can never parse."""
    json_text = json.dumps(_valid_fields(settings, amount=10000, order_id="uneq-dataval"))
    body = "data=" + quote(json_text, safe="")
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


def test_double_percent_encoded_json_key_still_rejected(
    client, settings, session_factory, stub, caplog
):
    json_text = json.dumps(_valid_fields(settings, amount=10000, order_id="uneq-double"))
    once = quote(json_text, safe="")
    twice = quote(once, safe="") + "="
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, twice)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


def test_php_serialized_data_still_rejected(client, settings, session_factory, stub, caplog):
    body = 'a:1:{s:1:"a=b";i:1;}='
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


def test_arbitrary_prefix_before_json_still_rejected(
    client, settings, session_factory, stub, caplog
):
    """A non-JSON prefix glued in front of otherwise-valid JSON, even
    combined with an internal unescaped '=' -- the candidate (raw text minus
    the trailing '=') starts with ``json:``, not ``{``, so it can never
    parse as JSON."""
    json_text = json.dumps(_valid_fields(settings, amount=10000, order_id="a=b"))
    body = "json:" + json_text + "="
    assert body.count("=") > 1
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


def test_json_string_containing_json_still_rejected(
    client, settings, session_factory, stub, caplog
):
    """One extra JSON-string encoding layer -- the candidate parses to a
    STRING, not an object, directly. Deliberately not unwrapped."""
    inner = json.dumps(_valid_fields(settings, amount=10000, order_id="a=b"))
    double_encoded = json.dumps(inner)
    body = double_encoded + "="
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


@pytest.mark.parametrize(
    "candidate",
    ['[1,"=",2]', '"a=b"', "42", "true", "null"],
    ids=["array", "string", "int", "bool", "null"],
)
def test_non_object_json_with_internal_equals_still_rejected(
    client, settings, session_factory, stub, candidate
):
    body = candidate + "=x=y="  # >1 literal '=', ends with '='
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_malformed_json_with_internal_equals_still_rejected(
    client, settings, session_factory, stub, caplog
):
    body = '{"a":"x=y"'  # unbalanced -- no closing brace at all
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body + "=")
    assert response.status_code == 422
    assert _rejection_record(caplog).representation == "urlencoded"
    _assert_no_side_effects(session_factory, stub)


def test_real_form_missing_fields_unaffected(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}"
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_duplicate_required_fields_unaffected(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}&amount=1&amount=2&order_id=o"
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_too_many_pairs_unaffected(client, settings, session_factory, stub):
    from app.api.payments import _MAX_FORM_PAIRS

    body = "&".join(f"e{i}=v{i}" for i in range(_MAX_FORM_PAIRS + 5))
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_malformed_utf8_unaffected(client, settings, session_factory, stub, caplog):
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = client.post(
            CUSTOM_PAYMENT_URL, content=b"\xff\xfe not utf-8", headers={"Content-Type": FORM_CT}
        )
    assert response.status_code == 422
    assert _rejection_record(caplog).representation != "urlencoded_raw_json_key"
    _assert_no_side_effects(session_factory, stub)


def test_oversized_body_unaffected(client, settings, session_factory, stub):
    body = _raw_json_key_body(_valid_fields(settings, amount=10000, order_id="x" * 70000))
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_multipart_unsupported_unaffected(client, settings, session_factory, stub):
    body = _raw_json_key_body(_valid_fields(settings, amount=10000, order_id="a=b"))
    response = client.post(
        CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": "multipart/form-data"}
    )
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


# --- PR #55 / #37 regression protection ----------------------------------------


def test_regression_a_normal_json_object_unaffected(client, settings, session_factory, stub):
    body = json.dumps(_valid_fields(settings, amount=10000, order_id="reg-a"))
    response = client.post(
        CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert get_payment(session_factory, "reg-a").amount == 10000


def test_regression_b_json_string_object_unaffected(client, settings, session_factory, stub):
    inner = json.dumps(_valid_fields(settings, amount=10000, order_id="reg-b"))
    body = json.dumps(inner)
    response = client.post(
        CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert get_payment(session_factory, "reg-b").amount == 10000


def test_regression_c_canonical_form_unaffected(client, settings, session_factory, stub):
    body = f"api_key={settings.inbound_api_key}&amount=10000&order_id=reg-c"
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "reg-c").amount == 10000


def test_regression_d_pr37_mislabeled_json_fallback_unaffected(
    client, settings, session_factory, stub
):
    """PR #37: a JSON document mislabeled as form content-type where strict
    form parsing fails to even TOKENIZE (a totally different failure mode
    from a successfully-parsed single pair) still falls back to the JSON
    decoder."""
    body = json.dumps(_valid_fields(settings, amount=10000, order_id="reg-d"))
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "reg-d").amount == 10000


def test_regression_e_pr55_percent_encoded_empty_value_unaffected(
    client, settings, session_factory, stub
):
    """PR #55's shape (percent-encoded JSON key, empty value) is handled by
    ``_try_recover_json_key_form`` BEFORE this new helper ever runs."""
    json_text = json.dumps(_valid_fields(settings, amount=10000, order_id="reg-e"))
    body = quote(json_text, safe="") + "="
    response = _post_form(client, body)
    assert response.status_code == 200
    assert get_payment(session_factory, "reg-e").amount == 10000


# --- security tests --------------------------------------------------------------


def test_invalid_api_key_via_recovered_representation_rejected(
    client, settings, session_factory, stub
):
    """Structural recovery succeeds; authentication still fails normally --
    no bypass, no payment row, no gateway call."""
    fields = {"api_key": "wrong-key-value", "amount": 10000, "order_id": "uneq-badauth=x"}
    body = _raw_json_key_body(fields)
    response = _post_form(client, body)
    assert response.status_code == 401
    _assert_no_side_effects(session_factory, stub)


def test_schema_invalid_recovered_json_rejected(client, settings, session_factory, stub):
    fields = {"api_key": settings.inbound_api_key, "order_id": "uneq-noamount=x"}  # amount missing
    body = _raw_json_key_body(fields)
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_no_secret_or_raw_body_leakage_on_success(client, settings, session_factory, stub, caplog):
    order_id = "UNEQ-PREAUTH-77=x"
    body = _raw_json_key_body(
        _valid_fields(settings, amount=45678, order_id=order_id, tag="X")
    )
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    norm = _normalized_record(caplog)
    assert norm.representation == "urlencoded_raw_json_key"
    blob = repr(norm.__dict__)
    assert order_id not in blob
    assert "45678" not in blob
    assert settings.inbound_api_key not in blob
    for record in caplog.records:
        assert settings.inbound_api_key not in repr(record.__dict__)


def test_no_marker_leakage_on_rejection(client, settings, session_factory, stub, caplog):
    marker = "SECRET-UNEQ-MARKER-55c1"
    body = f'{{"a":"{marker}=y","b"}}='  # unescaped '=' AND malformed (never recovers)
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 422
    assert marker not in response.text
    for record in caplog.records:
        assert marker not in repr(record.__dict__)


def test_no_marker_leakage_on_auth_failure(client, settings, session_factory, stub, caplog):
    marker = "SECRET-UNEQ-MARKER-AUTH-9b"
    fields = {"api_key": "wrong-key", "amount": 10000, "order_id": f"{marker}=x"}
    body = _raw_json_key_body(fields)
    with caplog.at_level(logging.DEBUG, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 401
    assert marker not in response.text


# --- side-effect / idempotency ---------------------------------------------------


def test_repeated_identical_recovered_request_is_idempotent(
    client, settings, session_factory, stub
):
    fields = _valid_fields(settings, amount=10000, order_id="uneq-idem=x")
    body = _raw_json_key_body(fields)
    first = _post_form(client, body)
    assert first.status_code == 200
    assert _payment_count(session_factory) == 1
    assert len(stub.getlink_requests) == 1
    second = _post_form(client, body)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _payment_count(session_factory) == 1  # no duplicate row
    assert len(stub.getlink_requests) == 1  # no second gateway call
