"""Resilient transport for admin command reply chunks.

Shared by every interactive admin command (wired centrally from
``app.adminbot.runner.AdminBotService.on_message``) so a transient
Telegram/network error on one chunk no longer silently drops every chunk
that follows it. Callers must produce the full list of reply chunks
*before* calling ``deliver_reply_chunks`` -- this module only ever retries
the transport send of the current chunk, never any business/query logic.

Delivery guarantee: at-least-once at the Telegram-message level (a network
timeout can occur after Telegram accepted a message but before our client
saw the response, so a retry of that chunk can duplicate an
operator-visible message) combined with exactly-once at the command level
(the handler that produced ``chunks`` already ran, once, before this
function was ever called). Telegram's Bot API provides no idempotency
token for ``sendMessage``, so no de-duplication is attempted here -- this
is an accepted tradeoff for an interactive, best-effort operator reply.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.adminbot.telegram import classify_send_error

logger = logging.getLogger("app.adminbot.reply_delivery")

# The callable ignores whatever `send` returns (python-telegram-bot's
# reply_text returns a Message, not None) -- typed as `object` rather than
# `None` so callers can pass `message.reply_text` directly without wrapping.
SendFn = Callable[[str], Awaitable[object]]
SleepFn = Callable[[float], Awaitable[None]]

# Fixed, short, interactive-path backoff: attempt 1, retry after 1s, retry
# after 3s. Deliberately not configurable (no Settings/env var) -- this is
# an interactive chat reply, not a background delivery pipeline, so bounded
# latency matters more than tunability. Also deliberately not reusing
# app.services.notification's jitter helpers: this path has no concurrent
# fan-out to de-synchronize, so jitter would add nothing but complexity.
_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 3.0)
_ATTEMPTS_PER_CHUNK = len(_RETRY_DELAYS_SECONDS) + 1

# Ceiling on how long on_message may block waiting out a Telegram-provided
# RetryAfter delay. python-telegram-bot's ApplicationBuilder() defaults to
# concurrent_updates=False, so a long in-loop sleep here would stall every
# other admin's command too. This is a ceiling on how long WE will block for
# -- it never truncates or shortens the delay Telegram actually requires;
# above it we simply stop and let the operator re-run the command.
_RETRY_AFTER_INTERACTIVE_CEILING_SECONDS = 15

_INCOMPLETE_DELIVERY_WARNING = "⚠️ ارسال کامل خروجی ممکن نشد. لطفاً دستور را دوباره اجرا کنید."


async def deliver_reply_chunks(
    chunks: list[str],
    send: SendFn,
    *,
    command: str,
    sleep: SleepFn = asyncio.sleep,
) -> None:
    """Send ``chunks`` in order over ``send``, retrying only the transport step.

    Never re-invokes any business/query logic -- the caller must already have
    produced the full ``chunks`` list. Stops at the first chunk that cannot
    be delivered (permanent error, retries exhausted, or a RetryAfter beyond
    the interactive ceiling), attempts one best-effort incomplete-delivery
    warning, and never sends the chunks that would have followed it.
    """
    for index, chunk in enumerate(chunks):
        delivered = await _deliver_one_chunk(
            chunk,
            send,
            command=command,
            chunk_index=index,
            chunk_count=len(chunks),
            sleep=sleep,
        )
        if not delivered:
            await _send_incomplete_warning(send, command=command)
            return


async def _deliver_one_chunk(
    chunk: str,
    send: SendFn,
    *,
    command: str,
    chunk_index: int,
    chunk_count: int,
    sleep: SleepFn,
) -> bool:
    for attempt in range(1, _ATTEMPTS_PER_CHUNK + 1):
        try:
            await send(chunk)
        except Exception as exc:
            outcome = classify_send_error(exc)
        else:
            return True

        is_last_attempt = attempt >= _ATTEMPTS_PER_CHUNK
        retry_after = outcome.retry_after_seconds

        if outcome.retryable and retry_after is not None:
            if retry_after > _RETRY_AFTER_INTERACTIVE_CEILING_SECONDS:
                _log_failed(
                    command, chunk_index, chunk_count, attempt, outcome.error_code, retry_after
                )
                return False
            if is_last_attempt:
                _log_failed(
                    command, chunk_index, chunk_count, attempt, outcome.error_code, retry_after
                )
                return False
            _log_retry_scheduled(
                command, chunk_index, chunk_count, attempt, outcome.error_code, retry_after
            )
            await sleep(retry_after)
            continue

        if outcome.retryable and not is_last_attempt:
            delay = _RETRY_DELAYS_SECONDS[attempt - 1]
            _log_retry_scheduled(
                command, chunk_index, chunk_count, attempt, outcome.error_code, delay
            )
            await sleep(delay)
            continue

        _log_failed(command, chunk_index, chunk_count, attempt, outcome.error_code, None)
        return False

    return False


async def _send_incomplete_warning(send: SendFn, *, command: str) -> None:
    try:
        await send(_INCOMPLETE_DELIVERY_WARNING)
    except Exception as exc:
        outcome = classify_send_error(exc)
        logger.warning(
            "admin_reply_incomplete_warning_failed",
            extra={"command": command, "error_code": outcome.error_code},
        )


def _log_retry_scheduled(
    command: str,
    chunk_index: int,
    chunk_count: int,
    attempt: int,
    error_code: str | None,
    delay_seconds: float,
) -> None:
    logger.warning(
        "admin_reply_retry_scheduled",
        extra={
            "command": command,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "attempt": attempt,
            "error_code": error_code,
            "delay_seconds": delay_seconds,
        },
    )


def _log_failed(
    command: str,
    chunk_index: int,
    chunk_count: int,
    attempt: int,
    error_code: str | None,
    retry_after_seconds: float | None,
) -> None:
    extra: dict[str, object] = {
        "command": command,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "attempt": attempt,
        "error_code": error_code,
    }
    if retry_after_seconds is not None:
        extra["retry_after_seconds"] = retry_after_seconds
    logger.warning("admin_reply_failed", extra=extra)
