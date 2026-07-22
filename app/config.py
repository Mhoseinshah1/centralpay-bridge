"""Application configuration loaded from environment variables."""

import ipaddress
import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed error texts for outbound URLs. Submitted values are never echoed
# (they may embed credentials or tokens); each message names ONLY its
# variable and the applicable contract.
_CENTRALPAY_BASE_URL_ERROR = (
    "CENTRALPAY_BASE_URL must be an HTTPS service base URL "
    "(https://host[:port]/base-path) with no userinfo, query, fragment, "
    "endpoint filename, whitespace, or control characters — cleartext "
    "http:// is never accepted because the API key travels in request bodies"
)
_BOT_NOTIFY_URL_ERROR = (
    "BOT_PAYMENT_NOTIFY_URL must be an HTTPS endpoint URL "
    "(https://host[:port]/path) with no userinfo, query, fragment, "
    "whitespace, or control characters; cleartext http:// is accepted only "
    "with ALLOW_INSECURE_BOT_NOTIFY_URL=true and only for private/internal "
    "hosts (localhost, loopback/private/link-local IP literals, single-label "
    "service names, or *.internal/*.local) — the Token header would "
    "otherwise cross the network without TLS"
)

# Path-segment grammar shared by the outbound URLs: clean slash-separated
# segments, no empty segments, no dot-segments, no percent encoding.
_PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._~-]+")

# Fixed error text for an invalid PUBLIC_BASE_URL. The submitted value is
# deliberately never echoed anywhere (a malformed URL may embed userinfo,
# tokens, or other secrets); the message names only the variable.
# DNS-style label for registered hostnames: letters/digits/hyphen, no
# leading or trailing hyphen, no underscore, no percent encoding. Labels
# are bounded at 63 octets and the full hostname at 253.
_HOST_LABEL_PATTERN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")

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

    Accepted: absolute HTTPS URL with a non-empty ASCII hostname —
    structurally valid IPv4, bracketed IPv6 (ipaddress-validated), or a
    registered name of DNS-style labels (letters/digits/hyphen, no
    leading/trailing hyphen, no underscore, no empty labels, label <= 63
    and hostname <= 253 chars) — an optional numeric port (1-65535), and
    at most a bare "/" path. Internationalized hostnames are explicitly
    rejected — operators must supply the punycode form (which satisfies
    the label grammar). Percent signs anywhere in the authority are
    rejected: percent-encoded host syntax is parser-dependent ambiguity,
    never decoded-and-accepted. An explicit ":" port delimiter must be
    followed by digits in canonical decimal spelling — dangling colons
    and zero-padded ports are rejected, not repaired. Raw "?" and "#"
    delimiters are rejected even when empty.
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
    # No query or fragment is permitted, INCLUDING empty ones: urlsplit
    # cannot distinguish "https://host?" from "https://host" (parts.query
    # is "" for both), so the raw delimiters are rejected outright rather
    # than silently dropped in canonicalization.
    if "?" in value or "#" in value:
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError(_PUBLIC_BASE_URL_ERROR) from None
    # HTTPS only; a missing scheme also rejects protocol-relative values.
    if parts.scheme.lower() != "https":
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    if parts.query or parts.fragment:
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    if parts.path not in ("", "/"):
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    # The RAW authority is validated character by character; urlsplit's
    # convenience accessors are deliberately not trusted for acceptance
    # decisions because they silently repair malformed authorities (a
    # dangling ":" yields port=None, and percent-encoded bytes pass
    # through as hostname text). Nothing here is repaired: an authority
    # that would change on reconstruction — beyond the documented
    # case/slash normalizations — is rejected.
    netloc = parts.netloc
    if not netloc or "@" in netloc or "%" in netloc:
        raise ValueError(_PUBLIC_BASE_URL_ERROR)
    host_canonical, port_text = _split_raw_authority(netloc)
    # An explicit ":" delimiter demands canonical decimal digits — a
    # dangling colon or zero-padded spelling is malformed, not repaired.
    port = _parse_canonical_port(port_text, _PUBLIC_BASE_URL_ERROR)
    return f"https://{host_canonical}:{port}" if port is not None else f"https://{host_canonical}"


def _parse_canonical_port(port_text: str | None, error: str) -> int | None:
    """Validate an explicit raw port spelling: ASCII digits, range 1..65535,
    canonical decimal form (no zero padding). None means no delimiter."""
    if port_text is None:
        return None
    if not port_text or not port_text.isdigit():
        raise ValueError(error)
    port = int(port_text)
    if not 1 <= port <= 65535 or port_text != str(port):
        raise ValueError(error)
    return port


def _parse_outbound_url(value: object, error: str) -> tuple[str, str, int | None, str]:
    """Shared strict parsing for outbound URLs (CentralPay base, bot
    endpoint). Returns (scheme, canonical host, port, raw path) or raises
    ``ValueError(error)``. Reuses exactly the PUBLIC_BASE_URL authority
    grammar (_split_raw_authority) so there is one hostname contract, and
    the same raw-delimiter rules: no whitespace/control characters, no
    backslash, no '?'/'#' anywhere (even empty), no '%' or '@' in the
    authority, canonical port spelling."""
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError(error)
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value:
        raise ValueError(error)
    if "?" in value or "#" in value:
        raise ValueError(error)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError(error) from None
    netloc = parts.netloc
    if not netloc or "@" in netloc or "%" in netloc:
        raise ValueError(error)
    host, port_text = _split_raw_authority(netloc, error=error)
    port = _parse_canonical_port(port_text, error)
    return parts.scheme.lower(), host, port, parts.path


def _validate_path_segments(path: str, error: str) -> None:
    """An absolute path of clean segments: no empty ('//'), no '.'/'..',
    no percent encoding, characters limited to [A-Za-z0-9._~-]."""
    if not path.startswith("/") or "%" in path:
        raise ValueError(error)
    for segment in path[1:].split("/"):
        if (
            not segment
            or segment in (".", "..")
            or not _PATH_SEGMENT_PATTERN.fullmatch(segment)
        ):
            raise ValueError(error)


def normalize_centralpay_base_url(value: object) -> str:
    """Validate CENTRALPAY_BASE_URL: HTTPS always — no escape hatch exists,
    because getLink/verify carry the API key in POST bodies. The path is a
    canonical service BASE (the client appends getLink.php/verify.php), so
    an endpoint filename (*.php) is rejected. The only normalizations are
    scheme/host lowercasing and dropping one trailing slash (the client
    rstrips anyway, so generated endpoint URLs are unchanged for every
    currently valid configuration). Raises with a fixed message that never
    echoes the value.
    """
    scheme, host, port, path = _parse_outbound_url(value, _CENTRALPAY_BASE_URL_ERROR)
    if scheme != "https":
        raise ValueError(_CENTRALPAY_BASE_URL_ERROR)
    if path in ("", "/"):
        path = ""
    else:
        if path.endswith("/"):
            path = path[:-1]  # documented: one trailing slash
        _validate_path_segments(path, _CENTRALPAY_BASE_URL_ERROR)
        if path.rsplit("/", 1)[-1].lower().endswith(".php"):
            raise ValueError(_CENTRALPAY_BASE_URL_ERROR)
    port_part = f":{port}" if port is not None else ""
    return f"https://{host}{port_part}{path}"


def _is_private_bot_host(host: str) -> bool:
    """Purely syntactic private/internal classification — never DNS.

    ``host`` is the canonical lowercase output of _split_raw_authority:
    a bracketed IPv6 literal, a structurally valid IPv4 literal, or a
    grammar-valid registered name.
    """
    if host.startswith("["):
        ip6 = ipaddress.IPv6Address(host[1:-1])
        return ip6.is_loopback or ip6.is_private or ip6.is_link_local
    labels = host.split(".")
    if all(label.isdigit() for label in labels):
        ip4 = ipaddress.IPv4Address(host)
        return ip4.is_loopback or ip4.is_private or ip4.is_link_local
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if "." not in host:
        return True  # single-label container/service name (mock-bot, bot, ...)
    return host.endswith(".internal") or host.endswith(".local")


def normalize_bot_notify_url(value: object, *, allow_insecure: bool) -> str:
    """Validate BOT_PAYMENT_NOTIFY_URL — a COMPLETE endpoint URL.

    HTTPS is required by default. Cleartext http:// is accepted only when
    ALLOW_INSECURE_BOT_NOTIFY_URL=true AND the host is syntactically
    private/internal (see _is_private_bot_host) — intended solely for a
    mock bot on an isolated/container network; the Token header crosses
    the wire without TLS there. Public-looking hosts and public IP
    literals are rejected even with the flag. The configured path is
    stored exactly (no appending, no trailing-slash handling); the only
    normalizations are scheme/host lowercasing. Never echoes the value.
    """
    scheme, host, port, path = _parse_outbound_url(value, _BOT_NOTIFY_URL_ERROR)
    if scheme == "http":
        if not allow_insecure or not _is_private_bot_host(host):
            raise ValueError(_BOT_NOTIFY_URL_ERROR)
    elif scheme != "https":
        raise ValueError(_BOT_NOTIFY_URL_ERROR)
    _validate_path_segments(path, _BOT_NOTIFY_URL_ERROR)
    port_part = f":{port}" if port is not None else ""
    return f"{scheme}://{host}{port_part}{path}"


def _split_raw_authority(
    netloc: str, error: str = _PUBLIC_BASE_URL_ERROR
) -> tuple[str, str | None]:
    """Split a raw (already ASCII, userinfo-free, percent-free) authority
    into a canonical lower-cased host and the raw port text.

    Returns ``(host, None)`` when no ":" delimiter is present and
    ``(host, port_text)`` — possibly empty — when one is. Raises for
    malformed brackets, invalid hostname grammar, or trailing junk.
    """
    if netloc.startswith("["):
        closing = netloc.find("]")
        if closing == -1:
            raise ValueError(error)
        host_raw = netloc[1:closing]
        rest = netloc[closing + 1 :]
        try:
            ipaddress.IPv6Address(host_raw)
        except ValueError:
            raise ValueError(error) from None
        if rest == "":
            return f"[{host_raw.lower()}]", None
        if not rest.startswith(":"):
            raise ValueError(error)  # e.g. "[::1]extra"
        return f"[{host_raw.lower()}]", rest[1:]
    host_raw, sep, port_text = netloc.partition(":")
    host = host_raw.lower()
    labels = host.split(".")
    if any(not label for label in labels):
        raise ValueError(error)  # empty label ("..", leading/trailing dot)
    if all(label.isdigit() for label in labels):
        # All-numeric dotted form MUST be a structurally valid IPv4 address
        # (rejects 999.999.999.999 and 1.2.3.4.5 alike).
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError(error) from None
    else:
        if len(host) > 253:
            raise ValueError(error)
        for label in labels:
            if len(label) > 63 or not _HOST_LABEL_PATTERN.fullmatch(label):
                raise ValueError(error)
    return host, port_text if sep else None


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
    # LEGACY: the single shared gateway payer id used before the per-user
    # isolation fix (incident 2026-07). No longer used to create new payments —
    # each payment now carries its end user's exact Telegram id or a reserved
    # per-order fallback id — but kept for configuration compatibility and to
    # interpret historical payments.
    centralpay_user_id: int = Field(gt=0)
    # Dedicated secret keying the payer-identity lookup hashes and the
    # order-fallback derivation.
    # MUST NOT be reused from any other secret (callback HMAC, inbound/gateway
    # API keys, bot token, DB password). Required for payment creation; an
    # empty value fails closed at creation time. Callback verification of
    # already-created payments never depends on it.
    centralpay_payer_id_secret: str = ""
    # Emergency privacy-containment switch. When false, POST /api/custom-payment
    # returns a fixed 503 and creates no payment link. Callback verification for
    # payments already in flight is never affected.
    payment_creation_enabled: bool = True
    centralpay_timeout_seconds: float = Field(default=15.0, gt=0)

    # Bot notification (Phase 2). Empty values are allowed so the API service
    # can run without notification configured; the worker refuses to start
    # until both are set (see validate_bot_notification_settings).
    bot_payment_notify_url: str = ""
    # Cleartext-HTTP escape hatch for the bot endpoint: default OFF. When
    # true, http:// is still limited to private/internal hosts — it can
    # never become an "http anywhere" switch (see normalize_bot_notify_url).
    allow_insecure_bot_notify_url: bool = False
    bot_notify_token: str = ""
    bot_notify_retry_mode: Literal["safe", "idempotent"] = "safe"
    bot_notify_max_attempts: int = Field(default=6, gt=0, le=50)
    bot_notify_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    bot_notify_read_timeout_seconds: float = Field(default=15.0, gt=0)
    bot_notify_worker_interval_seconds: float = Field(default=10.0, gt=0)
    bot_notify_claim_timeout_seconds: float = Field(default=120.0, gt=0)

    # Server-side payment reconciliation: the worker verifies link_created
    # payments whose browser callback never arrived, through the SAME shared
    # verification path the callback uses. The browser callback stays the
    # fast primary path — reconciliation waits reconciliation_min_age_seconds
    # before the first server-side check and then retries with bounded
    # exponential backoff (initial * 2^(attempt-1), capped at max_backoff)
    # until reconciliation_max_attempts. Disabling it only stops the polling;
    # callbacks are unaffected.
    reconciliation_enabled: bool = True
    reconciliation_min_age_seconds: int = Field(default=30, ge=0)
    reconciliation_interval_seconds: float = Field(default=10.0, gt=0)
    reconciliation_batch_size: int = Field(default=10, gt=0, le=100)
    reconciliation_max_attempts: int = Field(default=60, gt=0, le=1000)
    reconciliation_initial_backoff_seconds: int = Field(default=20, gt=0)
    reconciliation_max_backoff_seconds: int = Field(default=900, gt=0)

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

    @field_validator("centralpay_payer_id_secret")
    @classmethod
    def _validate_payer_id_secret(cls, value: str) -> str:
        # Optional (empty fails closed at the route), but a configured value
        # must be strong enough to be an HMAC key — a hand-edited short secret
        # would silently weaken payer isolation. Matches the 16-char floor of
        # the other secrets.
        if value and len(value) < 16:
            raise ValueError("CENTRALPAY_PAYER_ID_SECRET must be empty or at least 16 characters")
        return value

    @field_validator("centralpay_base_url")
    @classmethod
    def _validate_centralpay_base_url(cls, value: object) -> str:
        # HTTPS always: getLink/verify POST bodies carry the API key.
        return normalize_centralpay_base_url(value)

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
        if self.bot_payment_notify_url:
            self.bot_payment_notify_url = normalize_bot_notify_url(
                self.bot_payment_notify_url,
                allow_insecure=self.allow_insecure_bot_notify_url,
            )
        request_budget = (
            self.bot_notify_connect_timeout_seconds + self.bot_notify_read_timeout_seconds
        )
        if self.bot_notify_claim_timeout_seconds <= request_budget:
            raise ValueError(
                "BOT_NOTIFY_CLAIM_TIMEOUT_SECONDS must exceed connect + read timeouts"
            )
        if self.reconciliation_max_backoff_seconds < self.reconciliation_initial_backoff_seconds:
            raise ValueError(
                "RECONCILIATION_MAX_BACKOFF_SECONDS must be >= "
                "RECONCILIATION_INITIAL_BACKOFF_SECONDS"
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
