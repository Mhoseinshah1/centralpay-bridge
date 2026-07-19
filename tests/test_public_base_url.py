"""PUBLIC_BASE_URL security contract.

The callback return URL carries the gateway order id, the one-time
callback token, and the HMAC signature — so the base URL must be a
strictly validated HTTPS origin (https://host[:port], nothing else),
canonicalized without silent repair, enforced by Settings itself for
every service that constructs it.
"""

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from app.config import normalize_public_base_url
from app.security import CALLBACK_PATH, callback_signature
from tests.conftest import (
    DEFAULT_REDIRECT_URL,
    create_order,
    get_payment,
    valid_callback_path,
    verify_ok_response,
)

SENTINEL = "WV5KT3XBN1"  # embedded in invalid values; must never leak


# --- accepted values and canonicalization ------------------------------------


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("https://pay.example.com", "https://pay.example.com"),
        ("https://pay.example.com/", "https://pay.example.com"),  # trailing slash
        ("https://pay.example.com:8443", "https://pay.example.com:8443"),
        ("https://pay.example.com:443", "https://pay.example.com:443"),  # standard port kept
        ("https://127.0.0.1", "https://127.0.0.1"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
        ("https://[2001:db8::1]:8443", "https://[2001:db8::1]:8443"),
        ("HTTPS://PAY.EXAMPLE.COM", "https://pay.example.com"),  # scheme+host lowered
    ],
)
def test_accepts_and_canonicalizes(value, canonical):
    assert normalize_public_base_url(value) == canonical


# --- rejected values ----------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        f"http://{SENTINEL}.example.com",  # cleartext HTTP
        f"//{SENTINEL}.example.com",  # protocol-relative
        f"{SENTINEL}.example.com",  # missing scheme
        "https://",  # missing hostname
        "",  # empty
        "   ",  # whitespace-only
        f" https://{SENTINEL}.example.com",  # leading whitespace
        f"https://{SENTINEL}.example.com ",  # trailing whitespace
        f"https://{SENTINEL}.example\ncom",  # embedded newline
        f"https://{SENTINEL}.example\rcom",  # carriage return
        f"https://{SENTINEL}.example\tcom",  # tab
        f"https://{SENTINEL}.example.com\x00",  # NUL
        f"https://{SENTINEL}.example.com\x7f",  # DEL
        f"https://user:pass@{SENTINEL}.example.com",  # literal userinfo
        f"https://user%40x@{SENTINEL}.example.com",  # encoded userinfo
        f"https://{SENTINEL}.example.com?x=1",  # query
        f"https://{SENTINEL}.example.com#fragment",  # fragment
        f"https://{SENTINEL}.example.com/path",  # path
        f"https://{SENTINEL}.example.com/api",  # path
        f"https://{SENTINEL}.example.com//callback",  # double-slash path
        f"https://{SENTINEL}.example.com/%2Fcallback",  # encoded-slash path
        f"https://{SENTINEL}.example.com\\@evil.example",  # backslash
        f"https://{SENTINEL}.example.com evil.example",  # space-confused hosts
        f"https://{SENTINEL}.example.com:abc",  # malformed port
        f"https://{SENTINEL}.example.com:99999",  # out-of-range port
        f"https://{SENTINEL}.example.com:0",  # port zero
        "https://[2001:db8",  # malformed IPv6
        f"https://пример-{SENTINEL}.example",  # internationalized hostname: rejected
        12345,  # not a string
        None,
    ],
)
def test_rejects_invalid_values_without_echoing_them(value, caplog):
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as excinfo:
        normalize_public_base_url(value)
    # The fixed message names only the variable — never the value.
    assert "PUBLIC_BASE_URL" in str(excinfo.value)
    assert SENTINEL not in str(excinfo.value)
    assert SENTINEL not in caplog.text


def test_settings_construction_rejects_invalid_url_without_echo(settings):
    values = settings.model_dump()
    values["public_base_url"] = f"http://{SENTINEL}.example.com"
    with pytest.raises(ValidationError) as excinfo:
        type(settings)(_env_file=None, **values)
    text = str(excinfo.value)
    assert "PUBLIC_BASE_URL" in text
    # Neither the invalid URL nor any other submitted value (secrets!) is
    # echoed — hide_input_in_errors covers the whole Settings model.
    assert SENTINEL not in text
    assert settings.inbound_api_key not in text


def test_settings_canonicalizes_trailing_slash(settings):
    values = settings.model_dump()
    values["public_base_url"] = "https://pay.test.local/"
    loaded = type(settings)(_env_file=None, **values)
    assert loaded.public_base_url == "https://pay.test.local"


# --- integration: invalid URL fails before any side effect -------------------


def test_app_construction_fails_before_any_request_or_query(settings, stub):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models import Base

    values = settings.model_dump()
    values["public_base_url"] = f"https://user:pass@{SENTINEL}.example.com"
    with pytest.raises(ValidationError):
        type(settings)(_env_file=None, **values)

    # Settings construction is the app's first step, so no engine, request,
    # or row can exist for the invalid configuration: prove the database
    # and gateway stub saw nothing.
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    from sqlalchemy import func, select

    from app.models import Payment, PaymentEvent

    with factory() as db:
        assert db.execute(select(func.count(Payment.id))).scalar_one() == 0
        assert db.execute(select(func.count(PaymentEvent.id))).scalar_one() == 0
    assert stub.getlink_requests == []
    engine.dispose()


# --- integration: structural proof of the generated callback URL -------------


def test_return_url_structure_and_signature(client, settings, session_factory, stub):
    assert create_order(client, settings, order_id="url-1", amount=10_000).status_code == 200
    payment = get_payment(session_factory, "url-1")

    [request] = stub.getlink_requests
    parts = urlsplit(str(request["returnUrl"]))
    assert parts.scheme == "https"
    assert parts.hostname == "pay.test.local"  # the configured host
    assert parts.path == CALLBACK_PATH
    assert parts.username is None and parts.password is None
    assert parts.fragment == ""
    query = parse_qs(parts.query, keep_blank_values=True)
    assert sorted(query) == ["ct", "orderId", "sig"]  # exactly these keys
    assert all(len(values) == 1 for values in query.values())  # exactly once
    assert query["orderId"] == [str(payment.gateway_order_id)]

    # The signature verifies with the existing HMAC function, unchanged.
    expected = callback_signature(
        settings.callback_hmac_secret, payment.gateway_order_id, query["ct"][0]
    )
    assert query["sig"] == [expected]

    # And the callback completes normally end to end.
    stub.verify_result = verify_ok_response(amount=10_000)
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert get_payment(session_factory, "url-1").status == "bot_notify_pending"


def test_trailing_slash_base_produces_identical_callback_url(
    settings, session_factory, stub
):
    """https://host/ and https://host generate exactly the same returnUrl."""
    from fastapi.testclient import TestClient

    from tests.conftest import build_app

    values = settings.model_dump()
    values["public_base_url"] = "https://pay.test.local/"
    slashed = type(settings)(_env_file=None, **values)
    assert slashed.public_base_url == settings.public_base_url  # canonical equal

    application = build_app(slashed, session_factory, stub)
    with TestClient(application, raise_server_exceptions=False) as test_client:
        assert (
            create_order(test_client, slashed, order_id="url-slash", amount=10_000)
            .status_code
            == 200
        )
    application.state.centralpay.close()
    payment = get_payment(session_factory, "url-slash")
    url = str(stub.getlink_requests[0]["returnUrl"])
    assert url.startswith(f"https://pay.test.local{CALLBACK_PATH}?")
    assert f"orderId={payment.gateway_order_id}" in url
    assert stub.getlink_requests[0]["amount"] == 10_000


def test_getlink_response_contract_unchanged(client, settings, session_factory, stub):
    """Regression: the create response and payment row are unchanged."""
    response = create_order(client, settings, order_id="url-reg", amount=10_000)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}
    payment = get_payment(session_factory, "url-reg")
    assert payment.amount == 10_000
    assert payment.payable_amount == 10_000
