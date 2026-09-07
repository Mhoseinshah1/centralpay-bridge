"""application/x-www-form-urlencoded compatibility: the COMPLETE request body
is a raw JSON OBJECT with ONE unescaped internal ``=`` and NO trailing ``=``.

Production incident (confirmed by evidence, then reproduced byte-for-byte
below, not guessed). One VPN-bot account repeatedly produced::

    representation=urlencoded
    content_type=application/x-www-form-urlencoded
    body_size=278
    total_pair_count=1
    extra_field_count=1
    missing_required_fields=["api_key","amount","order_id"]
    duplicate_required_fields=[]
    key_length=182
    value_length=95
    value_empty=false
    key_json_type=invalid
    value_json_type=invalid
    key_starts_json_object=true
    key_ends_json_object=false
    raw_pair_equals_count=1

ROOT CAUSE. The legacy bot declares the form content type but sends a raw JSON
object as the whole body. A literal ``=`` inside one JSON string value is not
percent-encoded, and unlike the shape handled by
``tests/test_custom_payment_urlencoded_unescaped_equals.py`` there is NO
trailing ``=`` form separator. ``parse_qsl`` therefore splits the document at
that single internal ``=`` and reports ONE syntactically valid pair -- so form
parsing neither raises (no fallback to the JSON decoder, which only triggers on
a *syntax* failure) nor matches any real field name. The request died in the
missing-required-fields branch with all three fields "missing".

    body_size 278 == key_length 182 + 1 ('=') + value_length 95

is arithmetic proof that the body was raw, unencoded JSON: percent-decoding
shortened nothing.

WHY THIS IS NOT A RELAXATION OF THE EXISTING BRANCH.
``_try_recover_raw_json_key_with_unescaped_equals`` requires a trailing ``=``
AND more than one literal ``=``; ``_try_recover_json_key_form`` requires the
pair's value to be empty (i.e. the body ends with ``=``). This shape has
neither. The three urlencoded recoveries are mutually exclusive BY
CONSTRUCTION: a body whose COMPLETE text parses as a JSON object cannot end
with ``=``, which is exactly what both siblings require.

BLAST RADIUS. ``_try_recover_raw_json_body`` activates only when ALL of:
form content type; EXACTLY one parsed pair; that pair's key is none of the
required/alias field names; the raw text begins with ``{``; and the COMPLETE,
unmodified body parses with exactly one ``json.loads`` to a dict. An ordinary
form is never reinterpreted merely because required fields are missing --
``tests/test_custom_payment_representation_matrix.py`` pins every other
representation's outcome.

The recovered object is fed through the EXACT SAME pipeline as every other
representation (``_normalize`` -> ``CreatePaymentRequest`` -> identity
extraction -> authentication -> amount policy -> rate limiting -> idempotency
-> payment creation -> getLink). No validation is weakened anywhere.

These tests exercise the real route through the strict model and fake both
CentralPay and the customer bot at the httpx transport layer (shared
fixtures) -- no real external service is contacted.
"""

import json
import logging
from urllib.parse import parse_qsl

import pytest
from sqlalchemy import func, select

from app.api.payments import (
    _CompatReject,
    _decode_urlencoded,
    _try_recover_json_key_form,
    _try_recover_raw_json_body,
    _try_recover_raw_json_key_with_unescaped_equals,
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


def _raw_json_body(fields: dict[str, object]) -> str:
    """The confirmed production wire shape: the whole JSON document sent RAW
    (never percent-encoded) as the ENTIRE body, with NO trailing separator.
    Any ``=`` inside a string value is literal, unescaped wire content."""
    return json.dumps(fields, separators=(",", ":"))


def _valid_fields(settings, *, amount=10000, order_id="raw=body", **extra) -> dict[str, object]:
    fields = {"api_key": settings.inbound_api_key, "amount": amount, "order_id": order_id}
    fields.update(extra)
    return fields


def _rejection_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_rejected"]
    return rec


def _normalized_record(caplog):
    [rec] = [r for r in caplog.records if r.getMessage() == "custom_payment_body_normalized"]
    return rec


# --- the exact production body, reconstructed to the byte --------------------


def _production_shaped_body(api_key: str) -> str:
    """A body carrying the customer's real api_key that reproduces the
    production fingerprint's STRUCTURE exactly: one literal '=' inside a
    string value, no trailing separator, and a single parsed pair."""
    return _raw_json_body(
        {
            "api_key": api_key,
            "amount": 230000,
            "order_id": "ord=9f3c1d7e",
            "user_id": 7123456789,
            "sign": "s" * 54,
        }
    )


def test_exact_production_byte_fingerprint_is_reproduced():
    """Independent of the fix: this wire shape yields the EXACT diagnostic
    numbers observed in production, confirming the root cause rather than
    assuming it. Pinning the byte counts makes the diagnosis falsifiable."""
    body = _raw_json_body(
        {
            "api_key": "k" * 137,
            "amount": 230000,
            "order_id": "ord=9f3c1d7e",
            "user_id": 7123456789,
            "sign": "s" * 54,
        }
    )
    assert len(body) == 278  # body_size
    assert body.count("=") == 1  # raw_pair_equals_count
    assert not body.endswith("=")  # distinguishes it from both siblings

    pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
    assert len(pairs) == 1  # total_pair_count
    key, value = pairs[0]
    assert len(key) == 182  # key_length
    assert len(value) == 95  # value_length
    assert value != ""  # value_empty=false
    assert key.lstrip().startswith("{")  # key_starts_json_object=true
    assert not key.rstrip().endswith("}")  # key_ends_json_object=false
    # key_json_type=invalid / value_json_type=invalid: parse_qsl truncated the
    # document at the internal '=', so neither half stands alone as JSON.
    for half in (key, value):
        with pytest.raises(ValueError):
            json.loads(half)
    # Arithmetic proof the body was raw, unencoded JSON.
    assert len(body) == len(key) + 1 + len(value)


def test_confirmed_production_fingerprint_body_now_succeeds(
    client, settings, session_factory, stub, caplog
):
    """THE regression: the reproduced production body is accepted, creates
    exactly one payment with the untouched amount, and is labelled with the
    new representation."""
    body = _production_shaped_body(settings.inbound_api_key)
    assert body.count("=") == 1
    assert not body.endswith("=")
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = _post_form(client, body)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    payment = get_payment(session_factory, "ord=9f3c1d7e")
    assert payment.amount == 230000
    assert isinstance(payment.amount, int)
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=7123456789)
    assert _normalized_record(caplog).representation == "urlencoded_raw_json_body"


# --- unit: the recovery helper's exact activation conditions -----------------


def test_recover_helper_accepts_the_target_shape():
    text = '{"a":"x=y"}'
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    assert _try_recover_raw_json_body(text, pairs) == {"a": "x=y"}


def test_recover_helper_requires_exactly_one_pair():
    # Two internal '=' produce two pairs: outside the evidenced fingerprint,
    # deliberately NOT recovered (minimal blast radius).
    text = '{"a":"p=q&r=s"}'
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    assert len(pairs) == 2
    assert _try_recover_raw_json_body(text, pairs) is None


def test_recover_helper_rejects_real_field_names():
    # A genuine form field whose VALUE merely looks like JSON must never be
    # reinterpreted as a whole-body JSON document.
    for name in ("api_key", "amount", "order_id", "user_id", "userId", "uid", "chat_id",
                 "telegram_id"):
        text = f'{name}={{"a":1}}'
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        assert _try_recover_raw_json_body(text, pairs) is None, name


def test_recover_helper_requires_body_to_begin_with_object_brace():
    text = 'x{"a":"p=q"}'
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    assert _try_recover_raw_json_body(text, pairs) is None


def test_recover_helper_rejects_trailing_garbage():
    # Nothing is trimmed, searched for, or unwrapped: the COMPLETE body must
    # parse on its own.
    text = '{"a":"x=y"}TRAILING'
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    assert _try_recover_raw_json_body(text, pairs) is None


def test_recover_helper_rejects_non_object_json():
    # An array/scalar/string body under the form content type stays rejected.
    for text in ('["a=b"]', '"a=b"'):
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        assert _try_recover_raw_json_body(text, pairs) is None, text


def test_recover_helper_rejects_malformed_json():
    text = '{"a":"x=y"'  # unbalanced
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    assert _try_recover_raw_json_body(text, pairs) is None


def test_recover_helper_never_decodes_more_than_one_layer():
    # A JSON STRING containing escaped JSON must not be unwrapped:
    # json.loads(body) must return a dict DIRECTLY.
    text = json.dumps(json.dumps({"a": "x=y"}))
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    assert _try_recover_raw_json_body(text, pairs) is None


def test_the_three_urlencoded_recoveries_are_mutually_exclusive():
    """Structural proof that adding this branch cannot capture either
    sibling's traffic: both siblings require a body ending in '=', which can
    never parse as a JSON object."""
    sibling_empty_value = '{"api_key":"k","amount":1,"order_id":"o"}='
    sibling_unescaped_eq = '{"api_key":"k","amount":1,"order_id":"a=b"}='
    target = '{"api_key":"k","amount":1,"order_id":"a=b"}'

    for text in (sibling_empty_value, sibling_unescaped_eq, target):
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        recoveries = [
            _try_recover_json_key_form(pairs) is not None,
            _try_recover_raw_json_key_with_unescaped_equals(text, pairs) is not None,
            _try_recover_raw_json_body(text, pairs) is not None,
        ]
        assert sum(recoveries) == 1, text
    # And each body is claimed by the expected one.
    def claimed(text):
        return _decode_urlencoded(text.encode())[0]

    assert claimed(sibling_empty_value) == "urlencoded_json_key"
    assert claimed(sibling_unescaped_eq) == "urlencoded_raw_json_key"
    assert claimed(target) == "urlencoded_raw_json_body"


def test_the_original_body_is_parsed_not_the_percent_decoded_pair(
    client, settings, session_factory, stub
):
    """Load-bearing detail: the candidate is the COMPLETE ORIGINAL body text,
    never the ``parse_qsl`` pair. ``parse_qsl`` applies ``unquote_plus``, which
    would rewrite a literal ``+`` to a space and decode ``%xx`` sequences --
    silently corrupting an order_id the sender never encoded. Parsing the raw
    text preserves it byte-for-byte."""
    order_id = "a+b=c%20d"
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id=order_id))
    assert _post_form(client, body).status_code == 200
    # Stored verbatim: no '+'->' ' and no %20 -> ' ' rewriting.
    payment = get_payment(session_factory, order_id)
    assert payment.bot_order_id == "a+b=c%20d"


def test_an_ampersand_inside_the_json_still_takes_the_pre_existing_path(
    client, settings, session_factory, stub, caplog
):
    """An unescaped '&' creates a real pair boundary, so parse_qsl fails to
    tokenize and the body reaches the PRE-EXISTING JSON syntax-error fallback,
    not this branch. Pinned so the two paths never silently swap."""
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id="amp&x"))
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        assert _post_form(client, body).status_code == 200
    assert _normalized_record(caplog).representation == "urlencoded_json_object"


def test_decode_urlencoded_returns_the_new_label_directly():
    body = '{"api_key":"k","amount":1,"order_id":"a=b"}'
    rep, data = _decode_urlencoded(body.encode())
    assert rep == "urlencoded_raw_json_body"
    assert data == {"api_key": "k", "amount": 1, "order_id": "a=b"}


# --- A / B / C: the mandated acceptance cases --------------------------------


def test_a_raw_json_with_one_internal_equals_is_accepted(
    client, settings, session_factory, stub
):
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id="A=case"))
    assert body.count("=") == 1 and not body.endswith("=")
    assert _post_form(client, body).status_code == 200
    payment = get_payment(session_factory, "A=case")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


@pytest.mark.parametrize("alias", ["user_id", "userId", "uid", "chat_id", "telegram_id"])
def test_b_identity_alias_reaches_the_normal_identity_path(
    client, settings, session_factory, stub, alias
):
    order_id = f"B-{alias}=x"
    body = _raw_json_body(
        _valid_fields(settings, amount=10000, order_id=order_id, **{alias: 6583754142})
    )
    assert _post_form(client, body).status_code == 200
    payment = get_payment(session_factory, order_id)
    assert payment.payer_identity_type == IDENTITY_TYPE_TELEGRAM_USER
    assert payment.gateway_user_id == expected_gateway_user_id(telegram_user_id=6583754142)


def test_b_absent_alias_still_falls_back_to_order_isolation(
    client, settings, session_factory, stub
):
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id="B-noident=x"))
    assert _post_form(client, body).status_code == 200
    payment = get_payment(session_factory, "B-noident=x")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK


def test_c_raw_json_without_any_equals_keeps_its_existing_fallback_path(
    client, settings, session_factory, stub, caplog
):
    """A raw JSON body with NO '=' cannot be tokenized by parse_qsl at all, so
    it has always reached the JSON syntax-error fallback. That path is
    untouched: it still succeeds and still reports its ORIGINAL representation
    label, not the new one."""
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id="C-noeq"))
    assert "=" not in body
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        assert _post_form(client, body).status_code == 200
    assert _normalized_record(caplog).representation == "urlencoded_json_object"
    assert get_payment(session_factory, "C-noeq").amount == 10000


# --- amount handling is the shared pipeline, not a new one -------------------


def test_ascii_decimal_amount_string_converted_exactly_like_every_other_path(
    client, settings, session_factory, stub
):
    body = _raw_json_body(_valid_fields(settings, amount="10000", order_id="raw-str=amt"))
    assert _post_form(client, body).status_code == 200
    payment = get_payment(session_factory, "raw-str=amt")
    assert payment.amount == 10000
    assert isinstance(payment.amount, int)


@pytest.mark.parametrize(
    "amount",
    [
        10000.0,
        True,
        "10000.0",
        "10_000",
        " 10000",
        "1\u0660\u0660\u0660\u0660",  # Arabic-Indic digits
        None,
        {"v": 1},
        [1],
    ],
)
def test_non_strict_amounts_are_still_rejected_through_this_path(
    client, settings, session_factory, stub, amount
):
    """The recovered dict gets NO type leniency the other representations
    lack: only an ASCII-decimal string is converted, everything else is left
    for the strict model to reject."""
    body = _raw_json_body(_valid_fields(settings, amount=amount, order_id="raw-bad=amt"))
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_wrong_api_key_is_still_rejected_through_this_path(
    client, settings, session_factory, stub
):
    """No auth bypass: the recovered dict is authenticated by the same
    constant-time comparison as every other representation."""
    body = _raw_json_body(
        {"api_key": "wrong-key", "amount": 10000, "order_id": "raw-badkey=x"}
    )
    response = _post_form(client, body)
    assert response.status_code == 401
    _assert_no_side_effects(session_factory, stub)


def test_amount_below_policy_minimum_is_still_rejected(
    client, settings, session_factory, stub
):
    body = _raw_json_body(_valid_fields(settings, amount=1, order_id="raw-min=x"))
    response = _post_form(client, body)
    assert response.status_code == 400
    _assert_no_side_effects(session_factory, stub)


def test_repeated_identical_request_is_idempotent(client, settings, session_factory, stub):
    """Financial/idempotency behaviour is the shared service's, unchanged: a
    replay returns the same redirect and creates no second payment."""
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id="raw-idem=x"))
    first = _post_form(client, body)
    second = _post_form(client, body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert _payment_count(session_factory) == 1
    assert len(stub.getlink_requests) == 1


# --- K: logging exposes only fixed structural metadata -----------------------


def test_accepted_body_logs_no_values_or_secrets(client, settings, session_factory, stub, caplog):
    # A distinctive order id, echoed nowhere: the assertion below proves it.
    probe_order_id = "raw-log=probe-order-id"
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        assert (
            _post_form(
                client,
                _raw_json_body(
                    _valid_fields(
                        settings, amount=10000, order_id=probe_order_id, user_id=6583754142
                    )
                ),
            ).status_code
            == 200
        )
    record = _normalized_record(caplog)
    assert record.representation == "urlencoded_raw_json_body"
    assert record.content_type == FORM_CT
    assert isinstance(record.body_size, int)
    assert record.has_end_user_identity is True
    # The pre-auth normalization log never carries values.
    text = record.getMessage() + json.dumps(
        {k: str(v) for k, v in vars(record).items() if not k.startswith("_")}
    )
    assert settings.inbound_api_key not in text
    assert probe_order_id not in text
    assert "6583754142" not in text


def test_rejected_body_logs_no_values_or_secrets(client, settings, session_factory, stub, caplog):
    """A body that this branch declines still logs only structural facts."""
    marker = "DISTINCTIVE-FIELD-VALUE"
    body = f'{{"a":"x=y","b":"{marker}"}}TRAILING'
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        assert _post_form(client, body).status_code == 422
    record = _rejection_record(caplog)
    assert record.representation == "urlencoded"
    blob = json.dumps({k: str(v) for k, v in vars(record).items() if not k.startswith("_")})
    assert marker not in blob
    assert "x=y" not in blob
    _assert_no_side_effects(session_factory, stub)


# --- L: downstream bot contract is untouched ---------------------------------


def test_outbound_customer_bot_payload_unchanged(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A payment created through the NEW representation notifies the VPN bot
    with the byte/shape-identical downstream contract."""
    order_id = "raw-outbound=x"
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id=order_id))
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


# --- bounds and hostile input ------------------------------------------------


def test_oversize_raw_json_body_is_still_bounded(client, settings, session_factory, stub):
    """The body-size bound is enforced before any decode: this branch cannot
    be used to smuggle unbounded parsing work past it."""
    padding = "p" * (64 * 1024)
    body = _raw_json_body({"api_key": "k", "amount": 1, "order_id": "a=b", "pad": padding})
    assert len(body) > 64 * 1024
    response = _post_form(client, body)
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_nested_object_amount_cannot_smuggle_a_value(client, settings, session_factory, stub):
    body = _raw_json_body(
        {"api_key": settings.inbound_api_key, "amount": {"$ne": 1}, "order_id": "raw-nest=x"}
    )
    assert _post_form(client, body).status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_duplicate_json_keys_follow_python_last_wins_without_new_ambiguity(
    client, settings, session_factory, stub
):
    """A raw JSON object with a repeated key is resolved by json.loads
    (last wins) exactly as on every other JSON representation -- this branch
    introduces no new duplicate-parameter semantics of its own."""
    body = (
        '{"api_key":"' + settings.inbound_api_key + '","amount":10000,'
        '"order_id":"first=x","order_id":"second=x"}'
    )
    assert _post_form(client, body).status_code == 200
    assert get_payment(session_factory, "second=x").amount == 10000
    assert _payment_count(session_factory) == 1


def test_non_utf8_body_is_rejected_without_reaching_the_recovery(
    client, session_factory, stub
):
    response = _post_form(client, b'{"a":"\xff\xfe=x"}')
    assert response.status_code == 422
    _assert_no_side_effects(session_factory, stub)


def test_json_content_type_is_unaffected_by_this_branch(
    client, settings, session_factory, stub, caplog
):
    """The new branch is reachable ONLY from the form content type."""
    body = _raw_json_body(_valid_fields(settings, amount=10000, order_id="raw-ct=x"))
    with caplog.at_level(logging.INFO, logger="app.api.payments"):
        response = client.post(
            CUSTOM_PAYMENT_URL, content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 200
    assert _normalized_record(caplog).representation == "json_object"


def test_decode_urlencoded_rejects_when_recovery_declines(session_factory):
    """A single-pair body that begins with '{' but does not parse still lands
    in the unchanged rejection path, diagnostics intact."""
    with pytest.raises(_CompatReject) as excinfo:
        _decode_urlencoded(b'{"a":"x=y"')
    assert excinfo.value.category == "urlencoded"
    assert excinfo.value.detail["missing_required_fields"] == ["api_key", "amount", "order_id"]
    assert excinfo.value.detail["raw_pair_equals_count"] == 1
