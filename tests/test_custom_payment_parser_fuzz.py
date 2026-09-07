"""Deterministic structural fuzzing of the urlencoded body decoder.

Not a search for new crashes so much as a standing proof of the INVARIANTS the
raw-JSON-body compatibility branch relies on, checked across thousands of
generated bodies that straddle every gate: percent-encoding, literal ``=`` and
``&``, JSON objects/arrays/scalars/strings, truncation, trailing separators,
real field names, leading whitespace, and pure noise.

Seeded with a fixed value so CI is reproducible; no network, no database.

The invariants:

1. ``_decode_urlencoded`` always either returns ``(label, dict)`` or raises
   exactly one of ``_CompatReject`` / ``_UrlencodedSyntaxError``. It never
   raises anything else and never returns a non-dict -- the property that keeps
   an unauthenticated request from reaching a 500.
2. The three urlencoded recoveries are mutually exclusive: at most one claims
   any given body.
3. ``_try_recover_raw_json_body`` returns non-None ONLY when every documented
   gate holds -- one pair, no required/alias field name, body begins with
   ``{``, and the COMPLETE body parses to a dict. This is the blast-radius
   guarantee, verified against the generator rather than hand-picked cases.
4. Whatever it returns is exactly ``json.loads(body)`` -- nothing trimmed,
   unwrapped, re-encoded, or recursively decoded.
"""

import json
import random
import string
from urllib.parse import parse_qsl, quote

import pytest

from app.api.payments import (
    _ALIAS_FIELD_SET,
    _REQUIRED_FIELDS,
    _CompatReject,
    _decode_urlencoded,
    _try_recover_json_key_form,
    _try_recover_raw_json_body,
    _try_recover_raw_json_key_with_unescaped_equals,
    _UrlencodedSyntaxError,
)

SEED = 20260907
ITERATIONS = 3000

_ALPHABET = string.ascii_letters + string.digits + "=&%{}\"':,+ -_[]"


def _random_value(rng: random.Random) -> object:
    return rng.choice(
        [
            rng.randint(-5, 10**6),
            rng.random() * 1000,
            rng.choice([True, False, None]),
            "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 12))),
            [rng.randint(0, 5)],
            {"n": rng.randint(0, 5)},
        ]
    )


# Values for the TARGET family: no '&' (which would split the body into
# several pairs) and no '=' of their own, so the injected one is the only one.
_TAME = string.ascii_letters + string.digits + "-_."


def _target_family_body(rng: random.Random) -> str:
    """The evidenced production family: a raw JSON object whose values are
    otherwise clean, carrying a controlled number of literal ``=`` and no
    ``&``. Generated deliberately because the noisy families almost never
    produce exactly one ``=``, which is the branch's entry condition."""
    fields: dict[str, object] = {
        "api_key": "".join(rng.choice(_TAME) for _ in range(rng.randint(4, 40))),
        "amount": rng.randint(1, 10**6),
        "order_id": "".join(rng.choice(_TAME) for _ in range(rng.randint(1, 20))),
    }
    if rng.random() < 0.5:
        fields[rng.choice(sorted(_ALIAS_FIELD_SET))] = rng.randint(1, 10**10)
    # Inject 0..3 literal '=' into one string value: 1 is the production
    # fingerprint, 0 routes to the syntax fallback, >1 splits into >1 pair.
    equals = rng.randrange(4)
    if equals:
        target = rng.choice(["api_key", "order_id"])
        fields[target] = str(fields[target]) + "=" * equals
    body = json.dumps(fields, separators=(",", ":"))
    return body + "=" if rng.random() < 0.25 else body


def _random_body(rng: random.Random) -> str:
    """Generate a body from the families that actually reach this decoder."""
    kind = rng.randrange(11)
    if kind == 10:
        return _target_family_body(rng)
    fields: dict[str, object] = {}
    for name in _REQUIRED_FIELDS:
        if rng.random() < 0.8:
            fields[name] = _random_value(rng)
    if rng.random() < 0.3:
        fields[rng.choice(sorted(_ALIAS_FIELD_SET))] = _random_value(rng)
    if rng.random() < 0.3:
        fields["extra" + str(rng.randint(0, 3))] = _random_value(rng)

    if kind == 0:  # raw JSON object body
        return json.dumps(fields, separators=(",", ":"))
    if kind == 1:  # raw JSON object + trailing '=' (sibling family)
        return json.dumps(fields, separators=(",", ":")) + "="
    if kind == 2:  # percent-encoded JSON as the key, empty value (sibling)
        return quote(json.dumps(fields, separators=(",", ":")), safe="") + "="
    if kind == 3:  # ordinary form
        return "&".join(f"{quote(k)}={quote(str(v))}" for k, v in fields.items()) or "a=1"
    if kind == 4:  # ordinary form with a duplicated required field
        base = "&".join(f"{quote(k)}={quote(str(v))}" for k, v in fields.items()) or "a=1"
        return base + f"&{rng.choice(_REQUIRED_FIELDS)}=dup"
    if kind == 5:  # truncated JSON
        text = json.dumps(fields, separators=(",", ":"))
        return text[: rng.randint(0, len(text))]
    if kind == 6:  # non-object JSON
        return rng.choice(['["a=b"]', '"a=b"', "42", "true", "null", "[1,2]"])
    if kind == 7:  # JSON string wrapping JSON (one extra layer)
        return json.dumps(json.dumps(fields, separators=(",", ":")))
    if kind == 8:  # JSON with leading/trailing whitespace
        return rng.choice(["  ", "\t", "\n", ""]) + json.dumps(fields) + rng.choice(["  ", ""])
    # pure noise
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 60)))


def _bodies() -> list[str]:
    rng = random.Random(SEED)
    return [_random_body(rng) for _ in range(ITERATIONS)]


BODIES = _bodies()


def test_the_generator_actually_exercises_the_new_branch():
    """A fuzz suite that never reaches the code under test proves nothing."""
    claimed = 0
    for body in BODIES:
        try:
            pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            continue
        if _try_recover_raw_json_body(body, pairs) is not None:
            claimed += 1
    assert claimed > 50, claimed


def test_decoder_never_raises_an_unexpected_type_and_never_returns_a_non_dict():
    """Invariant 1 -- the property that keeps a hostile body from becoming a
    500 on an unauthenticated endpoint."""
    for body in BODIES:
        try:
            label, data = _decode_urlencoded(body.encode())
        except (_CompatReject, _UrlencodedSyntaxError):
            continue
        except Exception as exc:  # pragma: no cover - a failure here is the bug
            pytest.fail(f"unexpected {type(exc).__name__} for {body[:60]!r}")
        assert isinstance(label, str) and label
        assert isinstance(data, dict), body[:60]


def test_the_three_recoveries_are_mutually_exclusive_across_the_corpus():
    """Invariant 2 -- no body is claimed by more than one compatibility path,
    so adding the third can never have stolen a sibling's traffic."""
    for body in BODIES:
        try:
            pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            continue
        claims = [
            _try_recover_json_key_form(pairs) is not None,
            _try_recover_raw_json_key_with_unescaped_equals(body, pairs) is not None,
            _try_recover_raw_json_body(body, pairs) is not None,
        ]
        assert sum(claims) <= 1, body[:60]


def test_the_new_branch_fires_only_when_every_documented_gate_holds():
    """Invariant 3 -- the blast-radius guarantee, checked in both directions."""
    for body in BODIES:
        try:
            pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            continue
        recovered = _try_recover_raw_json_body(body, pairs)
        if recovered is None:
            continue
        # Every gate must hold for a claimed body.
        assert len(pairs) == 1, body[:60]
        key, _value = pairs[0]
        assert key not in _REQUIRED_FIELDS, body[:60]
        assert key not in _ALIAS_FIELD_SET, body[:60]
        assert body.lstrip().startswith("{"), body[:60]
        assert isinstance(json.loads(body), dict), body[:60]
        # ...and a body ending in '=' can never be claimed (the siblings' turf).
        assert not body.rstrip().endswith("="), body[:60]


def test_a_claimed_body_is_exactly_json_loads_of_the_original():
    """Invariant 4 -- nothing is trimmed, unwrapped, or re-decoded."""
    for body in BODIES:
        try:
            pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            continue
        recovered = _try_recover_raw_json_body(body, pairs)
        if recovered is not None:
            assert recovered == json.loads(body), body[:60]


# --- deeply nested JSON must reject, never 500 -------------------------------
#
# CPython's JSON scanner raises RecursionError -- which is NOT a ValueError --
# for a deeply nested document. Within the 64 KB body bound an unauthenticated
# caller reaches roughly 10,000 nesting levels, past the interpreter limit.
# Before `_JSON_DECODE_ERRORS`, every content type answered such a body with a
# 500 and a full traceback instead of the sanitized 422.

_NEST = 10000
# One literal '=' so parse_qsl yields a single pair and the raw-JSON-body
# branch is the site that attempts the decode.
DEEP_WITH_EQUALS = '{"a":' * _NEST + '"x=y"' + "}" * _NEST
DEEP_NO_EQUALS = '{"a":' * _NEST + '"x"' + "}" * _NEST


def test_the_nesting_fixture_really_exceeds_the_interpreter_limit():
    """Guard: if a future runtime raised the limit, the tests below would pass
    vacuously."""
    with pytest.raises(RecursionError):
        json.loads(DEEP_WITH_EQUALS)


@pytest.mark.parametrize("body", [DEEP_WITH_EQUALS, DEEP_NO_EQUALS])
def test_deeply_nested_body_is_rejected_not_raised(body):
    """The decoder rejects through its normal path instead of letting
    RecursionError escape as an unhandled exception."""
    with pytest.raises((_CompatReject, _UrlencodedSyntaxError)):
        _decode_urlencoded(body.encode())


def test_deeply_nested_body_never_claimed_by_the_new_branch():
    pairs = parse_qsl(DEEP_WITH_EQUALS, keep_blank_values=True, strict_parsing=True)
    assert _try_recover_raw_json_body(DEEP_WITH_EQUALS, pairs) is None


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/x-www-form-urlencoded", "text/plain"],
)
def test_deeply_nested_body_returns_the_sanitized_422_on_every_content_type(
    client, content_type
):
    """End-to-end. All three content types previously answered 500."""
    response = client.post(
        "/api/custom-payment",
        content=DEEP_WITH_EQUALS,
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 422, content_type
    assert response.json() == {
        "error": {"code": "validation_error", "message": "Invalid request"},
        "detail": [{"loc": ["body"], "msg": "Invalid request body"}],
    }


def test_deeply_nested_json_string_layer_also_rejects(client):
    """The one-extra-layer decoder performs a SECOND json.loads; it is hardened
    too."""
    body = json.dumps(DEEP_WITH_EQUALS)  # a JSON string containing the nest
    response = client.post(
        "/api/custom-payment", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_an_ordinary_form_carrying_all_required_fields_is_never_reclaimed():
    """The scope rule, fuzzed: whenever ordinary form parsing DOES find the
    required fields, the decoder reports the plain ``urlencoded``
    representation and the new branch is not involved."""
    rng = random.Random(SEED + 1)
    for _ in range(500):
        values = {name: "".join(rng.choice(string.ascii_letters) for _ in range(6))
                  for name in _REQUIRED_FIELDS}
        values["amount"] = str(rng.randint(1, 10**6))
        body = "&".join(f"{k}={quote(v)}" for k, v in values.items())
        label, data = _decode_urlencoded(body.encode())
        assert label == "urlencoded", body
        assert data["order_id"] == values["order_id"]
        pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        assert _try_recover_raw_json_body(body, pairs) is None
