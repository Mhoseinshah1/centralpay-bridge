"""Logs must never expose configured secrets, signatures, or card numbers."""

import io
import logging

from app.logging_setup import (
    REDACTED,
    JsonFormatter,
    SecretRedactor,
    collect_secret_values,
)
from app.security import callback_signature
from tests.conftest import (
    TEST_DB_PASSWORD,
    callback_path,
    create_order,
    get_payment,
    verify_ok_response,
)


def test_secret_redactor_replaces_all_occurrences():
    redactor = SecretRedactor(["super-secret-value", "other-secret-123"])
    text = 'key="super-secret-value" again super-secret-value and other-secret-123'
    redacted = redactor.redact(text)
    assert "super-secret-value" not in redacted
    assert "other-secret-123" not in redacted
    assert redacted.count(REDACTED) == 3


def test_short_values_are_not_redacted():
    redactor = SecretRedactor(["ab", ""])
    assert redactor.redact("abcdef") == "abcdef"


def test_collect_secret_values_includes_database_password(settings):
    values = collect_secret_values(settings)
    assert settings.inbound_api_key in values
    assert settings.callback_hmac_secret in values
    assert settings.centralpay_getlink_api_key in values
    assert settings.centralpay_verify_api_key in values
    assert settings.bot_notify_token in values
    assert TEST_DB_PASSWORD in values


class _CapturingHandler(logging.Handler):
    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(formatter)
        self.stream = io.StringIO()

    def emit(self, record: logging.LogRecord) -> None:
        self.stream.write(self.format(record) + "\n")


def test_logs_do_not_expose_configured_secrets(client, settings, session_factory, stub):
    """End-to-end: run real flows, capture all log output, assert no secret leaks."""
    handler = _CapturingHandler(JsonFormatter(SecretRedactor(collect_secret_values(settings))))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        # A failed auth attempt, a successful payment, and a verified callback.
        create_order(client, settings, api_key="attacker-guessed-key-000")
        create_order(client, settings, order_id="log-1", amount=10000)
        payment = get_payment(session_factory, "log-1")
        stub.verify_result = verify_ok_response(amount=10000, card_number="6037991234567890")
        client.get(callback_path(settings, payment.gateway_order_id))

        # Prove the redaction backstop works even if code logs a secret directly.
        logging.getLogger("app.test").info(
            "backstop", extra={"leaked": settings.inbound_api_key}
        )
    finally:
        root.removeHandler(handler)

    output = handler.stream.getvalue()
    assert output  # sanity: something was captured

    for secret in (
        settings.inbound_api_key,
        settings.callback_hmac_secret,
        settings.centralpay_getlink_api_key,
        settings.centralpay_verify_api_key,
        settings.bot_notify_token,
        TEST_DB_PASSWORD,
    ):
        assert secret not in output

    assert REDACTED in output  # the backstop line was redacted, not dropped

    # Callback signatures, full card numbers, and full redirect URLs never
    # appear in logs either.
    signature = callback_signature(settings.callback_hmac_secret, payment.gateway_order_id)
    assert signature not in output
    assert "6037991234567890" not in output
    assert "gateway.test/pay/tok123" not in output


def test_request_logs_contain_path_but_not_query_string(client, settings, session_factory, stub):
    handler = _CapturingHandler(JsonFormatter(SecretRedactor(collect_secret_values(settings))))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        create_order(client, settings, order_id="log-2", amount=10000)
        payment = get_payment(session_factory, "log-2")
        stub.verify_result = verify_ok_response(amount=10000)
        client.get(callback_path(settings, payment.gateway_order_id))
    finally:
        root.removeHandler(handler)

    output = handler.stream.getvalue()
    assert "/api/centralpay/callback" in output
    assert "sig=" not in output
