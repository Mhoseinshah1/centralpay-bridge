"""Application configuration loaded from environment variables."""

import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HTTP_URL_PATTERN = re.compile(r"^https?://[^\s]+$")

# Fixed error text for an invalid PUBLIC_BASE_URL. The submitted value is
# deliberately never echoed anywhere (a malformed URL may embed userinfo,
# tokens, or other secrets); the message names only the variable.
_PUBLIC_BASE_URL_ERROR = (
    "PUBLIC_BASE_URL must be an HTTPS origin only: https://host[:port] with "
    "no path, query, fragment, userinfo, whitespace, or control characters"
)


class ConfigurationError(RuntimeError):
    """Invalid configuration. Messages must never contain secret values."""


def normalize_public_base_url(value: object) -> str:
    """Validate and canonicalize PUBLIC_BASE_URL to ``https://host[:port]``.

    This URL is the base of the CentralPay return URL, which carries the
    gateway order id, the one-time callback token, and the callback HMAC
    signature — an http:// value would expose both secrets in cleartext,
    and any path/query/fragment/userinfo would corrupt or redirect the
    generated callback. The application enforces the contract itself;
    installer correctness is not a sufficient security control.

    Accepted: absolute HTTPS URL with a non-empty ASCII hostname
    (IPv4 / registered name / bracketed IPv6), an optional numeric port
    (1-65535), and at most a bare "/" path. Internationalized hostnames
    are explicitly rejected — operators must supply the punycode form.
    Nothing is silently repaired; the ONLY normalization applied is
    scheme/host lowercasing and dropping a lone trailing slash.

    Raises ValueError with a fixed message that never includes the value.
    """
    if not isinstance(value, str):
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    # Whitespace, ASCII control characters (incl. NUL/TAB/CR/LF/DEL),
    # backslashes, and non-ASCII are rejected before any URL parsing —
    # they are the raw material of URL-confusion attacks.
    if not value or not value.isascii():
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value:
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError(_PUBLIC_BASE_URL_ERROR) from None
    # HTTPS only; a missing scheme also rejects protocol-relative values.
    if parts.scheme.lower() != "https":
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    if parts.username is not None or parts.password is not None:
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    if parts.query or parts.fragment:
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    if parts.path not in ("", "/"):
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    try:
        hostname = parts.hostname
        port = parts.port  # raises ValueError for non-numeric/out-of-range
    except ValueError:
        raise ValueError(_PUBLIC_BASE_URL_ERROR) from None
    if not hostname:
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    # Reconstruct the canonical origin from parsed components only —
    # never from the raw string.
    host_out = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{host_out}:{port}" if port is not None else f"https://{host_out}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        # Validation errors must never echo submitted values: a malformed
        # PUBLIC_BASE_URL may embed userinfo, and other fields hold secrets.
        hide_input_in_errors=True,
    )

    environment: str = "development"
    log_level: str = "INFO"
    # json (default, production) or text (development convenience). Both
    # formats pass through secret redaction.
    log_format: Literal["json", "text"] = "json"

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
    # CALLBACK_HMAC_SECRET is canonical; CALLBACK_SECRET is accepted as an
    # alias for deployment-configuration compatibility.
    callback_hmac_secret: str = Field(
        min_length=16,
        validation_alias=AliasChoices("callback_hmac_secret", "callback_secret"),
    )

    # Payment amount bounds in TOMAN, enforced on POST /api/custom-payment.
    min_payment_amount_toman: int = Field(default=1_000, gt=0)
    max_payment_amount_toman: int = Field(default=100_000_000, gt=0)

    # Optional; shown as a "return to bot" link on payer-facing pages.
    telegram_bot_username: str = ""

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

    # Worker liveness heartbeat file, touched after every pass.
    worker_heartbeat_file: str = "/tmp/centralpay-worker-heartbeat"

    # --- Administrator Telegram bot (Phase 4). Optional; disabled unless
    # explicitly configured. The API/worker only use these to decide whether
    # to CREATE alert outbox rows — they never talk to Telegram themselves.
    admin_bot_enabled: bool = False
    admin_bot_token: str = ""
    # Comma-separated numeric Telegram user IDs. Authorization is by numeric
    # ID only — usernames are never trusted. Parsed and validated by
    # parse_admin_telegram_ids(); deliberately NOT validated at Settings
    # construction so a bad value can never block API or worker startup.
    admin_telegram_ids: str = ""
    admin_bot_alerts_enabled: bool = True
    admin_bot_payment_success_alerts: bool = False
    admin_bot_error_alerts: bool = True
    admin_bot_manual_review_alerts: bool = True
    admin_bot_backup_alerts: bool = True
    admin_bot_health_alerts: bool = True
    admin_bot_daily_report_enabled: bool = True
    admin_bot_daily_report_time: str = "09:00"
    admin_bot_timezone: str = "Asia/Tehran"
    admin_bot_max_message_length: int = Field(default=3500, ge=500, le=4096)
    admin_bot_alert_dedup_minutes: int = Field(default=30, ge=1)
    # Alert delivery / monitoring tuning.
    admin_bot_alert_poll_interval_seconds: float = Field(default=5.0, gt=0)
    admin_bot_alert_max_attempts: int = Field(default=8, gt=0, le=50)
    admin_bot_alert_claim_timeout_seconds: float = Field(default=300.0, gt=0)
    admin_bot_health_check_interval_seconds: float = Field(default=60.0, gt=0)
    admin_bot_health_failure_threshold: int = Field(default=3, ge=1)
    admin_bot_health_recovery_threshold: int = Field(default=2, ge=1)
    # Optional build commit for /version (set at deploy time).
    git_commit_sha: str = ""
    # First-production-payment guardrail: when true, the first verified
    # payment records a critical audit event and admin alert. Never alters
    # financial behavior; disabled by default.
    first_payment_guard_enabled: bool = False

    # Application-level rate limiting (in-memory, per process; see
    # app/ratelimit.py for distributed semantics).
    rate_limit_enabled: bool = True
    rate_limit_create_per_minute: int = Field(default=120, gt=0)
    rate_limit_invalid_key_per_10min: int = Field(default=20, gt=0)
    rate_limit_invalid_signature_per_10min: int = Field(default=100, gt=0)
    # Internal (non-public) API base URL the admin bot probes for health.
    admin_bot_api_url: str = "http://api:8000"
    # Admin bot container liveness heartbeat file.
    admin_bot_heartbeat_file: str = "/tmp/centralpay-adminbot-heartbeat"

    @field_validator("public_base_url")
    @classmethod
    def _validate_public_base_url(cls, value: object) -> str:
        # Runs wherever Settings is constructed — API startup, worker,
        # admin bot, CLI/ops — so an invalid callback base can never exist
        # silently in any service.
        return normalize_public_base_url(value)

    @model_validator(mode="after")
    def _validate_bot_settings(self) -> Self:
        if self.min_payment_amount_toman >= self.max_payment_amount_toman:
            raise ValueError(
                "MIN_PAYMENT_AMOUNT_TOMAN must be less than MAX_PAYMENT_AMOUNT_TOMAN"
            )
        if self.telegram_bot_username and not re.fullmatch(
            r"@?[A-Za-z0-9_]{1,64}", self.telegram_bot_username
        ):
            raise ValueError("TELEGRAM_BOT_USERNAME contains invalid characters")
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


def parse_admin_telegram_ids(raw: str) -> tuple[int, ...]:
    """Parse comma-separated numeric Telegram user IDs.

    Raises ConfigurationError (never echoing full values beyond the IDs
    themselves, which are not secrets) on malformed input. Usernames are
    never accepted.
    """
    ids: list[int] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        if not candidate.isdigit() or int(candidate) <= 0:
            raise ConfigurationError(
                "ADMIN_TELEGRAM_IDS must contain only positive numeric "
                "Telegram user IDs separated by commas"
            )
        ids.append(int(candidate))
    return tuple(dict.fromkeys(ids))


def validate_admin_bot_settings(settings: Settings) -> tuple[int, ...]:
    """Startup validation for the admin bot service only.

    The API and worker never call this, so invalid admin-bot configuration
    can never block payment processing. Returns the parsed admin IDs.
    """
    if not settings.admin_bot_enabled:
        raise ConfigurationError("ADMIN_BOT_ENABLED is false")
    if not settings.admin_bot_token:
        raise ConfigurationError("ADMIN_BOT_TOKEN is not configured")
    admin_ids = parse_admin_telegram_ids(settings.admin_telegram_ids)
    if not admin_ids:
        raise ConfigurationError("ADMIN_TELEGRAM_IDS is empty")
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", settings.admin_bot_daily_report_time):
        raise ConfigurationError("ADMIN_BOT_DAILY_REPORT_TIME must be HH:MM")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(settings.admin_bot_timezone)
    except Exception as exc:
        raise ConfigurationError("ADMIN_BOT_TIMEZONE is not a valid IANA timezone") from exc
    return admin_ids
