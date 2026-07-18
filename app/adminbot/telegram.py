"""Telegram send transport and error classification.

The bot token is passed only to the underlying library client and never
appears in logs, errors, or outcome objects.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import telegram
import telegram.error
from telegram.constants import ParseMode

logger = logging.getLogger("app.adminbot.telegram")


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    retryable: bool = False
    retry_after_seconds: int | None = None
    error_code: str | None = None


class AlertSender(Protocol):
    async def send(self, chat_id: int, text: str) -> SendOutcome: ...


def classify_send_error(exc: Exception) -> SendOutcome:
    """Map Telegram library errors to explicit reason codes.

    Permanent authorization/configuration errors are never retried forever;
    network-level failures and 429/5xx are retryable.
    """
    if isinstance(exc, telegram.error.RetryAfter):
        retry_after = exc.retry_after
        seconds = (
            int(retry_after.total_seconds())
            if isinstance(retry_after, timedelta)
            else int(retry_after)
        )
        return SendOutcome(
            ok=False,
            retryable=True,
            retry_after_seconds=seconds + 1,
            error_code="telegram_429",
        )
    if isinstance(exc, telegram.error.InvalidToken):
        return SendOutcome(ok=False, retryable=False, error_code="telegram_invalid_token")
    if isinstance(exc, telegram.error.Forbidden):
        # Bot blocked by the administrator or never started.
        return SendOutcome(ok=False, retryable=False, error_code="telegram_forbidden")
    if isinstance(exc, telegram.error.BadRequest):
        # e.g. chat not found / administrator removed.
        return SendOutcome(ok=False, retryable=False, error_code="telegram_bad_request")
    if isinstance(exc, telegram.error.TimedOut | telegram.error.NetworkError):
        return SendOutcome(ok=False, retryable=True, error_code="telegram_network")
    if isinstance(exc, telegram.error.TelegramError):
        # Unknown Telegram-side condition (includes 5xx): retry bounded.
        return SendOutcome(ok=False, retryable=True, error_code="telegram_error")
    return SendOutcome(ok=False, retryable=True, error_code="telegram_unknown")


class TelegramAlertSender:
    """Production sender backed by python-telegram-bot."""

    def __init__(self, token: str) -> None:
        self._bot = telegram.Bot(token=token)

    async def send(self, chat_id: int, text: str) -> SendOutcome:
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            outcome = classify_send_error(exc)
            logger.warning(
                "admin_alert_send_failed",
                extra={
                    "chat_id": chat_id,
                    "error_code": outcome.error_code,
                    "retryable": outcome.retryable,
                },
            )
            return outcome
        return SendOutcome(ok=True)
