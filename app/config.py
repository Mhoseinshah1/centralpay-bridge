"""Application configuration loaded from environment variables."""

import re
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HTTP_URL_PATTERN = re.compile(r"^https?://[^\s]+$")


class ConfigurationError(RuntimeError):
    """Invalid configuration. Messages must never contain secret values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = Field(
        default="postgresql+psycopg://centralpay:centralpay@localhost:5432/centralpay",
        description="SQLAlchemy database URL; PostgreSQL is required in production.",
    )

    public_base_url: str = Field(
        description="Public HTTPS base URL of this bridge, used to build CentralPay return URLs.",
    )

    # Secrets. Minimum lengths guard against accidentally running with a
    # placeholder or truncated value; the installer generates long random values.
    inbound_api_key: str = Field(min_length=16)
    callback_hmac_secret: str = Field(min_length=16)

    centralpay_base_url: str = "https://centralapi.org/webservice/basic"
    centralpay_getlink_api_key: str = Field(min_length=1)
    centralpay_verify_api_key: str = Field(min_length=1)
    centralpay_user_id: int = Field(gt=0)
    centralpay_timeout_seconds: float = Field(default=15.0, gt=0)

    # Bot notification (Phase 2). Empty values are allowed so the API service
    # can run without notification configured; the worker refuses to start
    # until both are set (see validate_bot_notification_settings).
    bot_payment_notify_url: str = ""
    bot_notify_token: str = ""
    bot_notify_retry_mode: Literal["safe", "idempotent"] = "safe"
    bot_notify_max_attempts: int = Field(default=6, gt=0, le=50)
    bot_notify_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    bot_notify_read_timeout_seconds: float = Field(default=15.0, gt=0)
    bot_notify_worker_interval_seconds: float = Field(default=10.0, gt=0)
    bot_notify_claim_timeout_seconds: float = Field(default=120.0, gt=0)

    @model_validator(mode="after")
    def _validate_bot_settings(self) -> Self:
        if self.bot_payment_notify_url and not _HTTP_URL_PATTERN.fullmatch(
            self.bot_payment_notify_url
        ):
            raise ValueError("BOT_PAYMENT_NOTIFY_URL must be an http(s) URL")
        request_budget = (
            self.bot_notify_connect_timeout_seconds + self.bot_notify_read_timeout_seconds
        )
        if self.bot_notify_claim_timeout_seconds <= request_budget:
            raise ValueError(
                "BOT_NOTIFY_CLAIM_TIMEOUT_SECONDS must exceed connect + read timeouts"
            )
        return self


def validate_bot_notification_settings(settings: Settings) -> None:
    """Startup validation for the notification worker.

    Raises ConfigurationError with messages that name the variable but never
    include its value.
    """
    if not settings.bot_payment_notify_url:
        raise ConfigurationError("BOT_PAYMENT_NOTIFY_URL is not configured")
    if not settings.bot_notify_token:
        raise ConfigurationError("BOT_NOTIFY_TOKEN is not configured")
