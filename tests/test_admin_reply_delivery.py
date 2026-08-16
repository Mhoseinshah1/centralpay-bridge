"""Resilient admin command reply transport (app.adminbot.reply_delivery).

Exercises the shared per-chunk retry/backoff loop directly against a fake
`send`/`sleep` pair -- no python-telegram-bot Update/Message objects needed
for that half. A single thin integration test at the bottom proves the
central wiring from AdminBotService.on_message and, critically, that a
transport retry never re-invokes the command handler (handlers.handle runs
exactly once per incoming update no matter how many times the reply send
is retried).
"""

import asyncio
import logging

import telegram.error as telegram_error

from app.adminbot.reply_delivery import (
    _INCOMPLETE_DELIVERY_WARNING,
    deliver_reply_chunks,
)
from app.adminbot.runner import AdminBotService
from tests.conftest import TEST_ADMIN_ID, TEST_ADMIN_ID_2


class ScriptedSender:
    """Fake `send` callable: each call pops one scripted outcome.

    A scripted entry that is an exception instance is raised; anything else
    (including an exhausted script) means the call succeeds.
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[str] = []

    async def __call__(self, text: str) -> object:
        self.calls.append(text)
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        return object()


class FakeClock:
    """Records sleep durations instead of actually waiting."""

    def __init__(self):
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _run(coro):
    return asyncio.run(coro)


# --- 1. all chunks succeed ---------------------------------------------------


def test_all_chunks_succeed_in_order():
    sender = ScriptedSender()
    clock = FakeClock()
    _run(deliver_reply_chunks(["a", "b", "c"], sender, command="stuck", sleep=clock))
    assert sender.calls == ["a", "b", "c"]
    assert clock.sleeps == []


# --- 2 & 3. transient failure then retry succeeds; remaining chunks continue -


def test_transient_failure_on_first_attempt_then_retry_succeeds():
    sender = ScriptedSender([telegram_error.NetworkError("boom")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="status", sleep=clock))
    assert sender.calls == ["only", "only"]
    assert clock.sleeps == [1.0]


def test_remaining_chunks_continue_after_a_retried_chunk_succeeds():
    sender = ScriptedSender([telegram_error.NetworkError("boom")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["c1", "c2", "c3"], sender, command="recent", sleep=clock))
    assert sender.calls == ["c1", "c1", "c2", "c3"]


# --- 4. exact chunk ordering preserved ---------------------------------------


def test_chunk_ordering_is_preserved_even_with_retries():
    # chunk "1" fails once then succeeds; chunk "2" fails once then succeeds
    # (the `None` is a scripted no-op success for the call in between).
    sender = ScriptedSender([telegram_error.TimedOut(), None, telegram_error.NetworkError("x")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["1", "2", "3", "4"], sender, command="recent", sleep=clock))
    assert sender.calls == ["1", "1", "2", "2", "3", "4"]


# --- 5 & 6. ordinary transient failure exhausts 3 attempts, blocks the rest -


def test_ordinary_transient_failure_exhausts_three_attempts_then_stops():
    sender = ScriptedSender(
        [
            telegram_error.NetworkError("a"),
            telegram_error.NetworkError("b"),
            telegram_error.NetworkError("c"),
        ]
    )
    clock = FakeClock()
    _run(deliver_reply_chunks(["c1", "c2", "c3"], sender, command="stuck", sleep=clock))
    # 3 attempts for c1, then the fallback warning -- c2/c3 never sent.
    assert sender.calls == ["c1", "c1", "c1", _INCOMPLETE_DELIVERY_WARNING]
    assert clock.sleeps == [1.0, 3.0]


# --- 7 & 8. fallback warning: exactly one attempt, failure exits cleanly ----


def test_exhausted_chunk_triggers_exactly_one_fallback_warning_attempt():
    sender = ScriptedSender(
        [
            telegram_error.NetworkError("a"),
            telegram_error.NetworkError("b"),
            telegram_error.NetworkError("c"),
        ]
    )
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    assert sender.calls.count(_INCOMPLETE_DELIVERY_WARNING) == 1


def test_fallback_warning_failure_exits_cleanly_without_raising():
    sender = ScriptedSender([telegram_error.Forbidden("blocked"), telegram_error.NetworkError("x")])
    clock = FakeClock()
    # Must not raise.
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    assert sender.calls == ["only", _INCOMPLETE_DELIVERY_WARNING]


# --- 9. permanent failure is not repeatedly retried --------------------------


def test_permanent_failure_is_abandoned_after_a_single_attempt():
    sender = ScriptedSender([telegram_error.Forbidden("blocked")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    assert sender.calls == ["only", _INCOMPLETE_DELIVERY_WARNING]
    assert clock.sleeps == []


def test_invalid_token_and_bad_request_are_also_never_retried():
    for exc in (telegram_error.InvalidToken(), telegram_error.BadRequest("nope")):
        sender = ScriptedSender([exc])
        clock = FakeClock()
        _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
        assert sender.calls == ["only", _INCOMPLETE_DELIVERY_WARNING]
        assert clock.sleeps == []


# --- 10, 11 & 12. RetryAfter policy ------------------------------------------


def test_retry_after_at_or_below_ceiling_waits_the_full_value_and_retries():
    sender = ScriptedSender([telegram_error.RetryAfter(5)])  # classified as 5 + 1 = 6 seconds
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    assert sender.calls == ["only", "only"]
    assert clock.sleeps == [6]


def test_retry_after_above_ceiling_is_not_slept_or_retried_early():
    sender = ScriptedSender([telegram_error.RetryAfter(20)])  # classified as 21 seconds > 15
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    # Abandoned specifically because of an active RetryAfter -- the fallback
    # warning is skipped, not attempted, so as not to hit the same flood
    # control window (and risk prolonging it for the shared-token alert
    # pipeline) that Telegram just imposed.
    assert sender.calls == ["only"]
    assert clock.sleeps == []


def test_retry_after_above_ceiling_stops_subsequent_chunks_and_skips_warning():
    sender = ScriptedSender([telegram_error.RetryAfter(20)])
    clock = FakeClock()
    _run(deliver_reply_chunks(["c1", "c2"], sender, command="recent", sleep=clock))
    assert sender.calls == ["c1"]


def test_cumulative_retry_after_budget_is_enforced_within_one_chunk():
    """Two individually-within-ceiling RetryAfters on the same chunk must not
    let total blocking exceed the 15s per-reply budget: 10s then another 10s
    would be 20s total, so the second one is abandoned rather than slept."""
    sender = ScriptedSender(
        [telegram_error.RetryAfter(9), telegram_error.RetryAfter(9)]  # each classified as 10s
    )
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    assert sender.calls == ["only", "only"]  # abandoned after 2 attempts, not 3
    assert clock.sleeps == [10]  # only the first (budget-affordable) wait happened


def test_cumulative_retry_after_budget_persists_across_chunks():
    sender = ScriptedSender(
        [
            # classified as 8s; within budget -- chunk1 retries then succeeds
            telegram_error.RetryAfter(7),
            None,
            # classified as 8s again; only 7s of budget left -> abandoned
            telegram_error.RetryAfter(7),
        ]
    )
    clock = FakeClock()
    _run(deliver_reply_chunks(["c1", "c2"], sender, command="recent", sleep=clock))
    assert sender.calls == ["c1", "c1", "c2"]  # c2 abandoned on its first attempt; warning skipped
    assert clock.sleeps == [8]


def test_ordinary_backoffs_share_the_same_reply_wide_time_budget_as_retry_after():
    """RetryAfter waits and ordinary fixed backoffs draw from one shared
    15s-per-reply budget: a RetryAfter that consumes the whole budget on
    chunk1 must leave chunk2's ordinary retry unable to sleep at all."""
    sender = ScriptedSender(
        [
            telegram_error.RetryAfter(14),  # classified as 15s -- consumes the whole budget
            None,
            telegram_error.NetworkError("x"),  # chunk2: ordinary retry, but budget is now 0
        ]
    )
    clock = FakeClock()
    _run(deliver_reply_chunks(["c1", "c2"], sender, command="recent", sleep=clock))
    # c2's ordinary backoff is skipped (no budget left) -- abandoned on first
    # attempt without sleeping. Not RetryAfter-driven, so the warning IS sent.
    assert sender.calls == ["c1", "c1", "c2", _INCOMPLETE_DELIVERY_WARNING]
    assert clock.sleeps == [15]


def test_ordinary_backoff_budget_exhaustion_logs_remaining_budget(caplog):
    caplog.set_level(logging.WARNING, logger="app.adminbot.reply_delivery")
    sender = ScriptedSender([telegram_error.RetryAfter(14), None, telegram_error.NetworkError("x")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["c1", "c2"], sender, command="recent", sleep=clock))
    failed = [r for r in caplog.records if r.getMessage() == "admin_reply_failed"]
    assert len(failed) == 1
    assert failed[0].chunk_index == 1
    assert failed[0].reply_time_budget_remaining_seconds == 0.0


def test_warning_skipped_log_emitted_when_abandoned_via_retry_after(caplog):
    caplog.set_level(logging.WARNING, logger="app.adminbot.reply_delivery")
    sender = ScriptedSender([telegram_error.RetryAfter(20)])
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    skipped = [
        r for r in caplog.records if r.getMessage() == "admin_reply_incomplete_warning_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].command == "stuck"
    assert _INCOMPLETE_DELIVERY_WARNING not in sender.calls
    failed_records = [r for r in caplog.records if r.getMessage() == "admin_reply_failed"]
    assert failed_records  # sanity: the abandonment itself was still logged


def test_unrecognized_exception_logs_bare_type_name_for_diagnostics(caplog):
    """classify_send_error's catch-all ("telegram_unknown") on its own gives
    no signal to tell a real bug apart from an unrecognized transient
    condition -- the bare exception class name (never message/args/
    traceback) should be attached so operators have an explicit failure
    reason without risking leaked request content."""
    caplog.set_level(logging.WARNING, logger="app.adminbot.reply_delivery")
    sender = ScriptedSender([RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))

    retry_records = [r for r in caplog.records if r.getMessage() == "admin_reply_retry_scheduled"]
    assert len(retry_records) == 2
    assert all(r.exception_type == "RuntimeError" for r in retry_records)

    failed_records = [r for r in caplog.records if r.getMessage() == "admin_reply_failed"]
    assert len(failed_records) == 1
    assert failed_records[0].exception_type == "RuntimeError"

    # Still never the exception's message/args -- only the bare type name.
    for record in caplog.records:
        assert "boom" not in record.getMessage()
        for value in vars(record).values():
            assert "boom" not in str(value)


def test_recognized_telegram_errors_do_not_carry_exception_type(caplog):
    caplog.set_level(logging.WARNING, logger="app.adminbot.reply_delivery")
    sender = ScriptedSender([telegram_error.NetworkError("boom")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="stuck", sleep=clock))
    retry_records = [r for r in caplog.records if r.getMessage() == "admin_reply_retry_scheduled"]
    assert len(retry_records) == 1
    assert not hasattr(retry_records[0], "exception_type")


# --- 16. logs never contain reply chunk text ---------------------------------


def test_logs_never_include_reply_chunk_text(caplog):
    caplog.set_level(logging.WARNING, logger="app.adminbot.reply_delivery")
    secret_chunk = "sensitive-operational-payload-13579"
    sender = ScriptedSender(
        [
            telegram_error.NetworkError("a"),
            telegram_error.NetworkError("b"),
            telegram_error.NetworkError("c"),
        ]
    )
    clock = FakeClock()
    _run(deliver_reply_chunks([secret_chunk], sender, command="stuck", sleep=clock))
    assert caplog.records  # sanity: something was actually logged
    for record in caplog.records:
        assert secret_chunk not in record.getMessage()
        for value in vars(record).values():
            assert secret_chunk not in str(value)


def test_retry_and_failure_logs_carry_expected_metadata(caplog):
    caplog.set_level(logging.WARNING, logger="app.adminbot.reply_delivery")
    sender = ScriptedSender([telegram_error.NetworkError("boom")])
    clock = FakeClock()
    _run(deliver_reply_chunks(["only"], sender, command="recent", sleep=clock))
    retry_records = [r for r in caplog.records if r.getMessage() == "admin_reply_retry_scheduled"]
    assert len(retry_records) == 1
    assert retry_records[0].command == "recent"
    assert retry_records[0].chunk_index == 0
    assert retry_records[0].chunk_count == 1
    assert retry_records[0].attempt == 1
    assert retry_records[0].error_code == "telegram_network"


# --- integration: central wiring + handler-executes-once invariant ----------


class _FakeMessage:
    def __init__(self, text, script=None):
        self.text = text
        self.replies: list[str] = []
        self.reply_kwargs: list[dict[str, object]] = []
        self.script = list(script or [])

    async def reply_text(self, text, parse_mode=None, disable_web_page_preview=None):
        self.replies.append(text)
        self.reply_kwargs.append(
            {"parse_mode": parse_mode, "disable_web_page_preview": disable_web_page_preview}
        )
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        return object()  # python-telegram-bot's reply_text returns a Message, not None


class _FakeUser:
    id = 111
    username = "op"


class _FakeChat:
    id = 222
    type = "private"


class _FakeUpdate:
    def __init__(self, message):
        self.effective_message = message
        self.effective_user = _FakeUser()
        self.effective_chat = _FakeChat()


class _FakeHandlers:
    def __init__(self, replies):
        self.replies = replies
        self.calls = 0

    def handle(self, ctx, command, args):
        self.calls += 1
        return self.replies


def test_on_message_retries_transport_only_and_runs_handler_exactly_once(
    admin_settings, session_factory
):
    service = AdminBotService(admin_settings, session_factory, (TEST_ADMIN_ID, TEST_ADMIN_ID_2))
    handlers = _FakeHandlers(["chunk-a", "chunk-b"])
    service.handlers = handlers  # type: ignore[assignment]

    message = _FakeMessage("/stuck", script=[telegram_error.NetworkError("blip")])
    update = _FakeUpdate(message)

    _run(service.on_message(update, context=None))  # type: ignore[arg-type]

    assert handlers.calls == 1
    # chunk-a fails once transiently then succeeds; chunk-b sent once after.
    assert message.replies == ["chunk-a", "chunk-a", "chunk-b"]
    assert all(
        kwargs == {"parse_mode": "HTML", "disable_web_page_preview": True}
        for kwargs in message.reply_kwargs
    )


def test_on_message_single_chunk_command_unaffected_when_telegram_is_healthy(
    admin_settings, session_factory
):
    service = AdminBotService(admin_settings, session_factory, (TEST_ADMIN_ID, TEST_ADMIN_ID_2))
    handlers = _FakeHandlers(["single reply body"])
    service.handlers = handlers  # type: ignore[assignment]

    message = _FakeMessage("/version")
    update = _FakeUpdate(message)

    _run(service.on_message(update, context=None))  # type: ignore[arg-type]

    assert handlers.calls == 1
    assert message.replies == ["single reply body"]
    assert message.reply_kwargs == [{"parse_mode": "HTML", "disable_web_page_preview": True}]
