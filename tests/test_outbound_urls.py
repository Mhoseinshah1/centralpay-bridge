"""Outbound URL transport security.

CENTRALPAY_BASE_URL carries the API key in POST bodies — HTTPS always,
no escape hatch. BOT_PAYMENT_NOTIFY_URL carries the Token header — HTTPS
by default; cleartext http:// requires ALLOW_INSECURE_BOT_NOTIFY_URL=true
AND a syntactically private/internal host (no DNS is ever consulted).
"""

import logging
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import ValidationError

from app.config import normalize_bot_notify_url, normalize_centralpay_base_url
from tests.conftest import (
    TEST_BOT_TOKEN,
    BotStub,
    create_order,
    get_payment,
    make_verified_pending,
    run_pass,
    valid_callback_path,
)

SENTINEL = "KP8FW6ZMR3"  # embedded in invalid values; must never leak


# --- CENTRALPAY_BASE_URL: HTTPS always ---------------------------------------


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("https://centralapi.org/webservice/basic", "https://centralapi.org/webservice/basic"),
        ("https://gateway.example.com/base", "https://gateway.example.com/base"),
        ("https://gateway.example.com:8443/base", "https://gateway.example.com:8443/base"),
        ("https://127.0.0.1/base", "https://127.0.0.1/base"),
        ("https://[2001:db8::1]/base", "https://[2001:db8::1]/base"),
        # documented normalizations only: host case, one trailing slash
        ("HTTPS://CENTRALAPI.ORG/webservice/basic", "https://centralapi.org/webservice/basic"),
        ("https://centralapi.org/webservice/basic/", "https://centralapi.org/webservice/basic"),
    ],
)
def test_centralpay_base_accepts_https(value, canonical):
    assert normalize_centralpay_base_url(value) == canonical


@pytest.mark.parametrize(
    "value",
    [
        f"http://{SENTINEL}.example.org/webservice/basic",  # cleartext: never
        "http://127.0.0.1/base",
        f"//{SENTINEL}.example.org/base",  # protocol-relative
        f"{SENTINEL}.example.org/base",  # schemeless
        f"https://user:pass@{SENTINEL}.example.org/base",  # userinfo
        f"https://{SENTINEL}.example.org/base?x=1",  # query
        f"https://{SENTINEL}.example.org/base#x",  # fragment
        f"https://{SENTINEL}.example.org:0443/base",  # non-canonical port
        f"https://{SENTINEL}.example.org:",  # dangling colon
        f"https://{SENTINEL}.example.org/base\x00",  # NUL
        f"https://{SENTINEL}.example.org/ba se",  # whitespace
        f"https://{SENTINEL}.example.org\\base",  # backslash
        f"https://{SENTINEL}.example.org/%2Fbase",  # percent-encoded path
        f"https://%65vil-{SENTINEL}.org/base",  # percent-encoded authority
        f"https://{SENTINEL}.example.org/a//b",  # empty path segment
        f"https://{SENTINEL}.example.org/a/../b",  # dot segment
        f"https://{SENTINEL}.example.org/webservice/verify.php",  # endpoint filename
        f"https://{SENTINEL}.example.org/getTransactionId.php",
        "",
        None,
    ],
)
def test_centralpay_base_rejects_without_echo(value, caplog):
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as excinfo:
        normalize_centralpay_base_url(value)
    assert "CENTRALPAY_BASE_URL" in str(excinfo.value)
    assert SENTINEL not in str(excinfo.value)
    assert SENTINEL not in caplog.text


def test_centralpay_default_preserved(settings):
    """The shipped default is canonical and generated endpoints are the
    same URLs the client built before this change."""
    assert normalize_centralpay_base_url("https://centralapi.org/webservice/basic") == (
        "https://centralapi.org/webservice/basic"
    )
    # The conftest test base also passes unchanged.
    assert settings.centralpay_base_url == "https://centralpay.test.local/basic"


# --- BOT_PAYMENT_NOTIFY_URL: secure by default -------------------------------

SECURE_ACCEPTED = [
    "https://bot.example.com/api/payment",
    "https://bot.example.com:8443/api/payment",
    "https://127.0.0.1/api/payment",
    "https://[2001:db8::1]/api/payment",
    "https://bot.zedservice.ir/api/payment",  # the real deployment shape
]

HTTP_PRIVATE = [
    "http://localhost/api/payment",
    "http://127.0.0.1:8080/api/payment",
    "http://[::1]:8080/api/payment",
    "http://10.0.0.5/api/payment",
    "http://192.168.1.20/api/payment",
    "http://172.20.0.10/api/payment",
    "http://mock-bot:8080/api/payment",
    "http://bot/api/payment",
    "http://mock-bot.internal/api/payment",
    "http://svc.localhost/api/payment",
]

HTTP_PUBLIC_OR_MALFORMED = [
    f"http://bot-{SENTINEL}.example.com/api/payment",  # public-looking name
    "http://8.8.8.8/api/payment",  # public IPv4
    "http://1.1.1.1/api/payment",
    f"http://public-{SENTINEL}.example.org/api/payment",
    f"http://user:pass@mock-bot/api/{SENTINEL}",  # userinfo
    f"http://mock-bot/api/payment?token={SENTINEL}",  # query
    f"http://mock-bot/api/payment#{SENTINEL}",  # fragment
    "http://mock-bot/api//payment",  # empty segment
    "http://mock-bot:0443/api/payment",  # non-canonical port
    f"http://mock%2Dbot/{SENTINEL}",  # percent-encoded authority
    f"http://mock-bot/api/pay\x00ment-{SENTINEL}",  # control character
    "http://[2607:f8b0::1]/api/payment",  # genuinely public IPv6 literal
]


@pytest.mark.parametrize("value", SECURE_ACCEPTED)
def test_bot_url_https_accepted_with_flag_off(value):
    assert normalize_bot_notify_url(value, allow_insecure=False) == value


@pytest.mark.parametrize(
    "value",
    [
        "http://bot.example.com/api/payment",
        "http://mock-bot/api/payment",
        "http://127.0.0.1/api/payment",
        *HTTP_PRIVATE,
    ],
)
def test_bot_url_all_http_rejected_with_flag_off(value):
    with pytest.raises(ValueError, match="BOT_PAYMENT_NOTIFY_URL"):
        normalize_bot_notify_url(value, allow_insecure=False)


@pytest.mark.parametrize("value", HTTP_PRIVATE)
def test_bot_url_private_http_accepted_with_flag_on(value):
    assert normalize_bot_notify_url(value, allow_insecure=True) == value


@pytest.mark.parametrize("value", HTTP_PUBLIC_OR_MALFORMED)
def test_bot_url_public_or_malformed_http_rejected_even_with_flag(value, caplog):
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as excinfo:
        normalize_bot_notify_url(value, allow_insecure=True)
    assert "BOT_PAYMENT_NOTIFY_URL" in str(excinfo.value)
    assert SENTINEL not in str(excinfo.value)
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "https://bot.example.com",  # no path: not a complete endpoint
        "https://bot.example.com/api/payment/",  # trailing slash: empty segment
        f"https://bot.example.com/api/{SENTINEL}?x=1",
    ],
)
def test_bot_url_structure_enforced(value):
    with pytest.raises(ValueError, match="BOT_PAYMENT_NOTIFY_URL"):
        normalize_bot_notify_url(value, allow_insecure=True)


def test_bot_url_path_stored_exactly():
    """No appending or rewriting: the configured path is used verbatim."""
    url = "https://bot.example.com/custom/hook"
    assert normalize_bot_notify_url(url, allow_insecure=False) == url


# --- Settings integration -----------------------------------------------------


def _settings_with(settings, **overrides):
    values = settings.model_dump()
    values.update(overrides)
    return type(settings)(_env_file=None, **values)


def test_settings_reject_http_centralpay_before_any_side_effect(settings, stub):
    with pytest.raises(ValidationError) as excinfo:
        _settings_with(
            settings,
            centralpay_base_url=f"http://cp-{SENTINEL}.example.org/webservice/basic",
        )
    text = str(excinfo.value)
    assert "CENTRALPAY_BASE_URL" in text
    assert SENTINEL not in text
    assert settings.centralpay_getlink_api_key not in text  # API keys never echoed
    assert stub.getlink_requests == []  # nothing was ever sent


def test_settings_reject_http_bot_url_by_default(settings, bot_stub):
    with pytest.raises(ValidationError) as excinfo:
        _settings_with(settings, bot_payment_notify_url="http://mock-bot:8080/api/payment")
    text = str(excinfo.value)
    assert "BOT_PAYMENT_NOTIFY_URL" in text
    assert TEST_BOT_TOKEN not in text  # the Token never appears
    assert bot_stub.requests == []  # the worker never sent anything


def test_default_flag_is_false(settings):
    assert settings.allow_insecure_bot_notify_url is False


def test_explicit_private_mock_bot_http_flow(settings, session_factory, stub):
    """With the explicit opt-in and a private mock endpoint, exactly one
    request is sent and the Token header + JSON body are unchanged."""
    from fastapi.testclient import TestClient

    from app.bot import BotNotifier
    from tests.conftest import build_app

    insecure = _settings_with(
        settings,
        bot_payment_notify_url="http://mock-bot:8080/api/payment",
        allow_insecure_bot_notify_url=True,
    )
    assert insecure.bot_payment_notify_url == "http://mock-bot:8080/api/payment"

    application = build_app(insecure, session_factory, stub)
    bot_stub = BotStub()
    notifier = BotNotifier(
        url=insecure.bot_payment_notify_url,
        token=insecure.bot_notify_token,
        connect_timeout_seconds=2.0,
        read_timeout_seconds=2.0,
        transport=httpx.MockTransport(bot_stub.handler),
    )
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            make_verified_pending(
                client, insecure, session_factory, stub, order_id="mock-http-1"
            )
        run_pass(session_factory, notifier, insecure)
    finally:
        notifier.close()
        application.state.centralpay.close()

    [request] = bot_stub.requests  # exactly one request
    assert request == {"order_id": "mock-http-1", "actions": "custom_payment_verify"}
    [headers] = bot_stub.headers
    assert headers["token"] == insecure.bot_notify_token  # Token header unchanged


def test_production_https_bot_regression(settings, session_factory, stub, bot_stub):
    """The real deployment shape works with the flag OFF, and notification
    classification/persistence are unchanged."""
    from fastapi.testclient import TestClient

    from app.bot import BotNotifier
    from tests.conftest import build_app

    production_like = _settings_with(
        settings, bot_payment_notify_url="https://bot.zedservice.ir/api/payment"
    )
    assert production_like.allow_insecure_bot_notify_url is False

    application = build_app(production_like, session_factory, stub)
    notifier = BotNotifier(
        url=production_like.bot_payment_notify_url,
        token=production_like.bot_notify_token,
        connect_timeout_seconds=2.0,
        read_timeout_seconds=2.0,
        transport=httpx.MockTransport(bot_stub.handler),
    )
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            make_verified_pending(
                client, production_like, session_factory, stub, order_id="prod-https-1"
            )
        result = run_pass(session_factory, notifier, production_like)
    finally:
        notifier.close()
        application.state.centralpay.close()

    assert result["processed"] == 1
    assert get_payment(session_factory, "prod-https-1").status == "bot_notify_accepted"
    [request] = bot_stub.requests
    assert request["order_id"] == "prod-https-1"


def test_centralpay_requests_are_https_structurally(settings, client, session_factory, stub):
    """Structural proof from the real client: getLink and verify request
    URLs use https with the configured host and expected endpoint paths;
    credential placement and financial payload are unchanged."""
    urls: list[str] = []
    original_handler = stub.handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return original_handler(request)

    app_client = client.app.state.centralpay  # rewire transport capture
    app_client._client._transport = httpx.MockTransport(recording_handler)

    assert create_order(client, settings, order_id="https-struct", amount=10_000).status_code == 200
    payment = get_payment(session_factory, "https-struct")
    from tests.conftest import verify_ok_response

    stub.verify_result = verify_ok_response(amount=10_000)
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    assert len(urls) == 2
    for url in urls:
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.hostname == "centralpay.test.local"
    assert urls[0].endswith("/basic/getLink.php")
    assert urls[1].endswith("/basic/verify.php")
    # Credential placement unchanged: keys in the POST bodies, amounts intact.
    assert stub.getlink_requests[0]["api_key"] == settings.centralpay_getlink_api_key
    assert stub.getlink_requests[0]["amount"] == 10_000
    assert stub.verify_requests[0]["api_key"] == settings.centralpay_verify_api_key
