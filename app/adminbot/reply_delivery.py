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

RetryAfter is bounded by a single cumulative time budget for the *whole*
reply (all chunks combined), not per occurrence: python-telegram-bot's
default concurrent_updates=False means every second spent sleeping here
also stalls every other administrator's command, so allowing each
RetryAfter to independently spend up to the ceiling could let one reply
block far longer than the ceiling in aggregate. Once the budget is
exhausted, any further RetryAfter -- however small -- is treated as
over-ceiling. When a chunk is abandoned specifically because of an active
RetryAfter (ceiling breach, budget exhaustion, or attempts exhausted while
Telegram still says wait), the incomplete-delivery warning is skipped
rather than attempted: Telegram just told this bot token not to send, and
retrying immediately would likely also fail and risks prolonging the
flood-control window for the alert-outbox pipeline, which shares the same
token. For every other abandonment reason (ordinary retries exhausted,
permanent error) the warning is still attempted, best-effort, as before.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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

# Total time budget, across the WHOLE reply (every chunk, every attempt
# combined), that deliver_reply_chunks may spend blocked on Telegram-provided
# RetryAfter delays. Not a per-occurrence cap: it never truncates or shortens
# any single delay Telegram actually requires -- once the delay would exceed
# the remaining budget, that chunk is abandoned instead of sleeping.
_RETRY_AFTER_BUDGET_SECONDS = 15.0

_INCOMPLETE_DELIVERY_WARNING = "⚠️ ارسال کامل خروجی ممکن نشد. لطفاً دستور را دوباره اجرا کنید."


@dataclass(frozen=True)
class _ChunkResult:
    delivered: bool
    # True only when abandonment happened because Telegram is actively
    # asking this bot token to wait (ceiling breach, budget exhaustion, or
    # attempts exhausted while a RetryAfter was still in effect) -- signals
    # the caller to skip the incomplete-delivery warning for this reply.
    abandoned_due_to_retry_after: bool = False


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
    be delivered and never sends the chunks that would have followed it. A
    best-effort incomplete-delivery warning is attempted unless the chunk was
    abandoned specifically because of an active Telegram RetryAfter.
    """
    retry_after_budget = _RETRY_AFTER_BUDGET_SECONDS
    for index, chunk in enumerate(chunks):
        result, retry_after_budget = await _deliver_one_chunk(
            chunk,
            send,
            command=command,
            chunk_index=index,
            chunk_count=len(chunks),
            sleep=sleep,
            retry_after_budget=retry_after_budget,
        )
        if not result.delivered:
            if result.abandoned_due_to_retry_after:
                _log_warning_skipped(command)
            else:
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
    retry_after_budget: float,
) -> tuple[_ChunkResult, float]:
    for attempt in range(1, _ATTEMPTS_PER_CHUNK + 1):
        try:
            await send(chunk)
        except Exception as exc:
            outcome = classify_send_error(exc)
        else:
            return _ChunkResult(delivered=True), retry_after_budget

        is_last_attempt = attempt >= _ATTEMPTS_PER_CHUNK
        retry_after = outcome.retry_after_seconds

        if outcome.retryable and retry_after is not None:
            if retry_after > retry_after_budget:
                _log_failed(
                    command,
                    chunk_index,
                    chunk_count,
                    attempt,
                    outcome.error_code,
                    retry_after,
                    retry_after_budget_remaining=retry_after_budget,
                )
                return (
                    _ChunkResult(delivered=False, abandoned_due_to_retry_after=True),
                    retry_after_budget,
                )
            if is_last_attempt:
                _log_failed(
                    command, chunk_index, chunk_count, attempt, outcome.error_code, retry_after
                )
                return (
                    _ChunkResult(delivered=False, abandoned_due_to_retry_after=True),
                    retry_after_budget,
                )
            _log_retry_scheduled(
                command, chunk_index, chunk_count, attempt, outcome.error_code, retry_after
            )
            await sleep(retry_after)
            retry_after_budget -= retry_after
            continue

        if outcome.retryable and not is_last_attempt:
            delay = _RETRY_DELAYS_SECONDS[attempt - 1]
            _log_retry_scheduled(
                command, chunk_index, chunk_count, attempt, outcome.error_code, delay
            )
            await sleep(delay)
            continue

        _log_failed(command, chunk_index, chunk_count, attempt, outcome.error_code, None)
        return _ChunkResult(delivered=False), retry_after_budget

    return _ChunkResult(delivered=False), retry_after_budget


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
    *,
    retry_after_budget_remaining: float | None = None,
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
    if retry_after_budget_remaining is not None:
        extra["retry_after_budget_remaining_seconds"] = retry_after_budget_remaining
    logger.warning("admin_reply_failed", extra=extra)


def _log_warning_skipped(command: str) -> None:
    logger.warning(
        "admin_reply_incomplete_warning_skipped",
        extra={"command": command, "reason": "retry_after_active"},
    )
