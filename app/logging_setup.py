"""Structured JSON logging with request IDs and secret redaction.

Every log line is a single JSON object. A redaction pass runs over the final
serialized line so that configured secret values can never appear in output,
regardless of which code path tried to log them.
"""

import json
import logging
import sys
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy.engine.url import make_url

from app.config import Settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "message",
    "asctime",
    "taskName",
}

# Values shorter than this are not redacted: replacing very short strings
# would corrupt unrelated output (e.g. a 2-character secret matching "id").
_MIN_REDACTABLE_LENGTH = 6

REDACTED = "[REDACTED]"


class SecretRedactor:
    """Replaces known secret values in formatted log output."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= _MIN_REDACTABLE_LENGTH},
            key=len,
            reverse=True,
        )

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text


def collect_secret_values(settings: Settings) -> list[str]:
    """All configured secret values that must never appear in logs."""
    secrets = [
        settings.inbound_api_key,
        settings.callback_hmac_secret,
        settings.centralpay_getlink_api_key,
        settings.centralpay_verify_api_key,
        settings.bot_notify_token,
        settings.admin_bot_token,
    ]
    try:
        password = make_url(settings.database_url).password
    except Exception:
        password = None
    if password:
        secrets.append(password)
    return [s for s in secrets if s]


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        line = json.dumps(payload, default=str, ensure_ascii=False)
        if self._redactor is not None:
            line = self._redactor.redact(line)
        return line


class TextFormatter(logging.Formatter):
    """Human-readable single-line format for development. Still redacted."""

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        parts = [timestamp, record.levelname, record.name, record.getMessage()]
        request_id = request_id_var.get()
        if request_id is not None:
            parts.append(f"request_id={request_id}")
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                parts.append(f"{key}={value}")
        if record.exc_info:
            parts.append(self.formatException(record.exc_info))
        line = " ".join(str(part) for part in parts)
        if self._redactor is not None:
            line = self._redactor.redact(line)
        return line


def configure_logging(settings: Settings) -> None:
    redactor = SecretRedactor(collect_secret_values(settings))
    handler = logging.StreamHandler(sys.stdout)
    formatter: logging.Formatter
    if settings.log_format == "text":
        formatter = TextFormatter(redactor)
    else:
        formatter = JsonFormatter(redactor)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # Uvicorn's access log prints the full request line including query
    # strings, which would leak callback signatures. It must stay disabled;
    # our middleware logs method + path only.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx/httpcore log full request URLs at INFO, which would expose any
    # query string (e.g. callback signatures). Cap them at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
