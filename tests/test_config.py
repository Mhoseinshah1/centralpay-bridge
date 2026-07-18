"""Configuration validation for the bot notification settings."""

import pytest
from pydantic import ValidationError

from app.config import ConfigurationError, validate_bot_notification_settings
from tests.conftest import TEST_BOT_TOKEN


def test_default_retry_mode_is_safe(settings):
    assert settings.bot_notify_retry_mode == "safe"


def test_invalid_retry_mode_rejected(settings):
    with pytest.raises(ValidationError):
        type(settings)(**{**settings.model_dump(), "bot_notify_retry_mode": "aggressive"})


def test_invalid_bot_url_rejected(settings):
    with pytest.raises(ValidationError, match="BOT_PAYMENT_NOTIFY_URL"):
        type(settings)(**{**settings.model_dump(), "bot_payment_notify_url": "not-a-url"})


def test_claim_timeout_must_exceed_request_budget(settings):
    with pytest.raises(ValidationError, match="BOT_NOTIFY_CLAIM_TIMEOUT_SECONDS"):
        type(settings)(
            **{
                **settings.model_dump(),
                "bot_notify_connect_timeout_seconds": 60.0,
                "bot_notify_read_timeout_seconds": 90.0,
                "bot_notify_claim_timeout_seconds": 120.0,
            }
        )


def test_worker_startup_validation_names_variable_but_not_value(settings):
    missing_url = settings.model_copy(update={"bot_payment_notify_url": ""})
    with pytest.raises(ConfigurationError) as exc_info:
        validate_bot_notification_settings(missing_url)
    assert "BOT_PAYMENT_NOTIFY_URL" in str(exc_info.value)

    missing_token = settings.model_copy(update={"bot_notify_token": ""})
    with pytest.raises(ConfigurationError) as exc_info:
        validate_bot_notification_settings(missing_token)
    assert "BOT_NOTIFY_TOKEN" in str(exc_info.value)
    assert TEST_BOT_TOKEN not in str(exc_info.value)


def test_valid_configuration_passes(settings):
    validate_bot_notification_settings(settings)
