"""Bot notification delivery: safe workflow, retries, recovery, audit."""

import io
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.logging_setup import JsonFormatter, SecretRedactor, collect_secret_values
from app.models import Payment, PaymentStatus
from app.reasons import ReasonCode
from app.services.notification import claim_next_due
from tests.conftest import (
    TEST_BOT_TOKEN,
    as_utc,
    create_order,
    event_types,
    get_events,
    get_payment,
    make_verified_pending,
    run_pass,
    valid_callback_path,
)

FIXED_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
# Payments queued during test setup are stamped with this pinned clock so
# they are always due at FIXED_NOW.
QUEUE_TIME = FIXED_NOW - timedelta(minutes=5)


@pytest.fixture(autouse=True)
def deterministic_queue_clock(monkeypatch):
    """Pin the verification-side clock (callback audit fix).

    make_verified_pending queues payments through the real callback flow,
    which used to stamp next_retry_at with the REAL wall clock while the
    worker ran at the injected FIXED_NOW constant. The suite therefore
    passed only while the wall clock was behind 2026-07-18T12:00Z and
    broke permanently afterwards. Both sides of the clock are now
    injected: queue timestamps are pinned to QUEUE_TIME, so payments are
    deterministically due at FIXED_NOW on any date.
    """
    import app.services.verification as verification_module

    monkeypatch.setattr(verification_module, "utcnow", lambda: QUEUE_TIME)


def _set_payment(session_factory, payment_id, **values):
    with session_factory() as session:
        session.execute(update(Payment).where(Payment.id == payment_id).values(**values))
        session.commit()


def test_notification_never_sent_before_verification(
    client, settings, session_factory, stub, bot_stub, notifier
):
    # A payment that only has a link is never delivered to the bot.
    assert create_order(client, settings, order_id="ntf-unverified").status_code == 200
    result = run_pass(session_factory, notifier, settings)
    assert result["processed"] == 0
    assert bot_stub.requests == []
    assert (
        get_payment(session_factory, "ntf-unverified").status
        == PaymentStatus.LINK_CREATED.value
    )

    # A declined verification never queues a notification either.
    payment = get_payment(session_factory, "ntf-unverified")
    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid"})
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 409
    run_pass(session_factory, notifier, settings)
    assert bot_stub.requests == []


def test_verified_payment_commits_before_notification_starts(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    # The verified + pending state is durable in the database while the bot
    # has not been contacted at all.
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.gateway_verified_at is not None
    assert bot_stub.requests == []
    assert "bot_notification_queued" in event_types(get_events(session_factory, payment.id))

    result = run_pass(session_factory, notifier, settings)
    assert result["processed"] == 1
    [request] = bot_stub.requests
    assert request == {"order_id": payment.bot_order_id, "actions": "custom_payment_verify"}
    [headers] = bot_stub.headers
    assert headers["token"] == TEST_BOT_TOKEN


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text=""),
        httpx.Response(204),
        httpx.Response(200, text="not json {{{"),
    ],
)
def test_2xx_becomes_bot_notify_accepted(
    client, settings, session_factory, stub, bot_stub, notifier, response
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    bot_stub.result = response
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert payment.bot_notify_reason == ReasonCode.BOT_NOTIFY_ACCEPTED.value
    assert payment.bot_notify_attempts == 1
    assert payment.bot_last_http_status == response.status_code
    assert as_utc(payment.bot_notify_accepted_at) == FIXED_NOW
    assert payment.next_retry_at is None
    assert payment.notification_claimed_at is None

    types = event_types(get_events(session_factory, payment.id))
    assert types[-2:] == ["bot_notification_started", "bot_notification_accepted"]


@pytest.mark.parametrize("status", [401, 422])
def test_4xx_becomes_manual_review(
    client, settings, session_factory, stub, bot_stub, notifier, status
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    bot_stub.result = httpx.Response(status)
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.bot_notify_reason == f"bot_http_{status}"
    assert payment.bot_last_http_status == status
    assert as_utc(payment.manual_review_at) == FIXED_NOW
    types = event_types(get_events(session_factory, payment.id))
    assert "bot_notification_failed" in types
    assert "manual_review_required" in types


@pytest.mark.parametrize("status", [500, 503])
def test_5xx_schedules_retry(client, settings, session_factory, stub, bot_stub, notifier, status):
    payment = make_verified_pending(client, settings, session_factory, stub)
    bot_stub.result = httpx.Response(status)
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.bot_notify_reason == f"bot_http_{status}"
    assert payment.bot_notify_attempts == 1
    # First retry: one minute, deterministic jitter of 1.0.
    assert as_utc(payment.next_retry_at) == FIXED_NOW + timedelta(seconds=60)
    assert payment.notification_claimed_at is None
    assert "bot_notification_retry_scheduled" in event_types(
        get_events(session_factory, payment.id)
    )

    # Not due yet: nothing is sent.
    bot_stub.result = httpx.Response(200)
    result = run_pass(
        session_factory, notifier, settings, now=FIXED_NOW + timedelta(seconds=30)
    )
    assert result["processed"] == 0
    assert len(bot_stub.requests) == 1

    # Due: the retry is delivered and accepted.
    result = run_pass(
        session_factory, notifier, settings, now=FIXED_NOW + timedelta(seconds=61)
    )
    assert result["processed"] == 1
    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert payment.bot_notify_attempts == 2


def test_connection_refused_schedules_retry(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    bot_stub.result = httpx.ConnectError("[Errno 111] Connection refused")
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.bot_notify_reason == ReasonCode.BOT_CONNECTION_REFUSED.value
    assert payment.bot_last_error_code == "ConnectError"
    assert as_utc(payment.next_retry_at) == FIXED_NOW + timedelta(seconds=60)


def test_ambiguous_read_timeout_safe_mode_manual_review(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    bot_stub.result = httpx.ReadTimeout("read timed out")
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.bot_notify_reason == ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value

    events = get_events(session_factory, payment.id)
    ambiguous = [e for e in events if e.event_type == "bot_timeout_ambiguous"]
    assert len(ambiguous) == 1
    assert ambiguous[0].level == "critical"
    assert "manual_review_required" in event_types(events)

    # Never automatically retried afterwards.
    result = run_pass(session_factory, notifier, settings, now=FIXED_NOW + timedelta(hours=2))
    assert result["processed"] == 0
    assert len(bot_stub.requests) == 1


def test_ambiguous_read_timeout_retries_in_idempotent_mode(
    client, settings, session_factory, stub, bot_stub, notifier
):
    idempotent = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    payment = make_verified_pending(client, settings, session_factory, stub)
    bot_stub.result = httpx.ReadTimeout("read timed out")
    run_pass(session_factory, notifier, idempotent, now=FIXED_NOW)

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert as_utc(payment.next_retry_at) == FIXED_NOW + timedelta(seconds=60)

    bot_stub.result = httpx.Response(200)
    run_pass(session_factory, notifier, idempotent, now=FIXED_NOW + timedelta(seconds=61))
    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


def test_retry_limit_reached_becomes_manual_review(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    events_before = len(get_events(session_factory, payment.id))
    _set_payment(
        session_factory,
        payment.id,
        bot_notify_attempts=settings.bot_notify_max_attempts - 1,
    )
    bot_stub.result = httpx.Response(500)
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.bot_notify_reason == ReasonCode.RETRY_LIMIT_REACHED.value
    assert payment.bot_notify_attempts == settings.bot_notify_max_attempts
    # All prior audit events are preserved; the final attempt added its own.
    events = get_events(session_factory, payment.id)
    assert len(events) > events_before
    assert "manual_review_required" in event_types(events)


def test_duplicate_worker_execution_claims_once(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)

    session_a = session_factory()
    session_b = session_factory()
    try:
        claimed_a = claim_next_due(session_a, worker_id="worker-a", now=FIXED_NOW)
        assert claimed_a is not None
        assert claimed_a.payment_id == payment.id
        # A second worker sees the claim and gets nothing: the same attempt
        # can never be sent twice.
        claimed_b = claim_next_due(session_b, worker_id="worker-b", now=FIXED_NOW)
        assert claimed_b is None
    finally:
        session_a.close()
        session_b.close()
    assert bot_stub.requests == []


def test_restart_recovers_unclaimed_pending_payment(
    client, settings, session_factory, stub, bot_stub, notifier
):
    # Queued before a "crash" (no worker ever ran); a fresh worker instance
    # picks it up and delivers.
    payment = make_verified_pending(client, settings, session_factory, stub)
    result = run_pass(
        session_factory, notifier, settings, worker_id="restarted-worker", now=FIXED_NOW
    )
    assert result["processed"] == 1
    assert get_payment(session_factory, payment.bot_order_id).status == (
        PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    )


def test_stale_claim_safe_mode_goes_to_manual_review(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    stale = FIXED_NOW - timedelta(seconds=settings.bot_notify_claim_timeout_seconds + 1)
    _set_payment(
        session_factory,
        payment.id,
        bot_notify_attempts=1,
        notification_claimed_at=stale,
        notification_claimed_by="dead-worker",
    )

    result = run_pass(session_factory, notifier, settings, now=FIXED_NOW)
    assert result["recovered"] == 1
    # The interrupted attempt's outcome is unknown: no re-send in safe mode.
    assert bot_stub.requests == []

    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.bot_notify_reason == ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value
    types = event_types(get_events(session_factory, payment.id))
    assert "notification_recovered_after_restart" in types


def test_stale_claim_idempotent_mode_requeues_and_delivers(
    client, settings, session_factory, stub, bot_stub, notifier
):
    idempotent = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    payment = make_verified_pending(client, settings, session_factory, stub)
    stale = FIXED_NOW - timedelta(seconds=settings.bot_notify_claim_timeout_seconds + 1)
    _set_payment(
        session_factory,
        payment.id,
        bot_notify_attempts=1,
        notification_claimed_at=stale,
        notification_claimed_by="dead-worker",
    )

    result = run_pass(session_factory, notifier, idempotent, now=FIXED_NOW)
    assert result["recovered"] == 1
    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.notification_claimed_at is None
    assert as_utc(payment.next_retry_at) == FIXED_NOW + timedelta(seconds=60)
    types = event_types(get_events(session_factory, payment.id))
    assert "notification_recovered_after_restart" in types

    run_pass(session_factory, notifier, idempotent, now=FIXED_NOW + timedelta(seconds=61))
    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


def test_accepted_payment_is_never_retried(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    run_pass(session_factory, notifier, settings)
    assert len(bot_stub.requests) == 1

    # Further passes, later times, and duplicate callbacks change nothing.
    result = run_pass(session_factory, notifier, settings, now=FIXED_NOW + timedelta(days=1))
    assert result["processed"] == 0
    duplicate = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert 'data-status="bot_accepted"' in duplicate.text
    run_pass(session_factory, notifier, settings, now=FIXED_NOW + timedelta(days=2))
    assert len(bot_stub.requests) == 1
    payment = get_payment(session_factory, payment.bot_order_id)
    assert payment.bot_notify_attempts == 1
    assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


def test_attempt_events_contain_no_secret_values(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub)
    bot_stub.result = httpx.Response(500)
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)
    bot_stub.result = httpx.Response(200)
    run_pass(session_factory, notifier, settings, now=FIXED_NOW + timedelta(seconds=61))

    secrets = [
        TEST_BOT_TOKEN,
        settings.inbound_api_key,
        settings.callback_hmac_secret,
        settings.centralpay_getlink_api_key,
        settings.centralpay_verify_api_key,
    ]
    events = get_events(session_factory, payment.id)
    assert len(events) >= 6  # queue + 2 attempts with their outcomes
    for event in events:
        serialized = repr(event.data)
        for secret in secrets:
            assert secret not in serialized


def test_untrusted_response_text_is_never_reflected(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """Remote response bodies must not reach the DB, events, logs, or pages."""
    evil = "<script>alert('EVIL_MARKER_9000')</script>"

    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(JsonFormatter(SecretRedactor(collect_secret_values(settings))))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        payment = make_verified_pending(client, settings, session_factory, stub)
        bot_stub.result = httpx.Response(500, text=evil)
        run_pass(session_factory, notifier, settings, now=FIXED_NOW)
    finally:
        root.removeHandler(handler)

    assert "EVIL_MARKER_9000" not in handler.stream.getvalue()

    payment = get_payment(session_factory, payment.bot_order_id)
    assert "EVIL_MARKER_9000" not in (payment.last_error or "")
    for event in get_events(session_factory, payment.id):
        assert "EVIL_MARKER_9000" not in repr(event.data)

    # The payer-facing page for this payment contains no remote text either.
    page = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert page.status_code == 200
    assert "EVIL_MARKER_9000" not in page.text


def test_worker_batch_processes_multiple_payments(
    client, settings, session_factory, stub, bot_stub, notifier
):
    for index in range(3):
        make_verified_pending(
            client, settings, session_factory, stub, order_id=f"ntf-batch-{index}"
        )
    result = run_pass(session_factory, notifier, settings)
    assert result["processed"] == 3
    with session_factory() as session:
        statuses = set(
            session.execute(
                select(Payment.status).where(Payment.bot_order_id.like("ntf-batch-%"))
            ).scalars()
        )
    assert statuses == {PaymentStatus.BOT_NOTIFY_ACCEPTED.value}
