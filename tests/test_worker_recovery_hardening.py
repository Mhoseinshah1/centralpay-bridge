"""Worker/retry/recovery audit tests.

Bounded retries through stale-claim recovery, claim-ownership integrity
for straggler results, bounded recovery batches, and manual-review /
scheduled-retry durability across restarts.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from app.bot import AttemptOutcome, OutcomeKind
from app.models import Payment, PaymentStatus
from app.reasons import ReasonCode
from app.services.notification import (
    ClaimedPayment,
    claim_next_due,
    record_attempt_result,
    release_stale_claims,
)
from tests.conftest import (
    event_types,
    get_events,
    get_payment,
    make_verified_pending,
    run_pass,
)

FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
QUEUE_TIME = FIXED_NOW - timedelta(minutes=5)


@pytest.fixture(autouse=True)
def deterministic_queue_clock(monkeypatch):
    """Pin the verification-side clock so payments queued through the real
    callback flow are deterministically due at FIXED_NOW on any date (both
    sides of the clock injected — see test_notification.py)."""
    import app.services.verification as verification_module

    monkeypatch.setattr(verification_module, "utcnow", lambda: QUEUE_TIME)


def _make_stale_claim(session_factory, payment_id, *, attempts, claimed_by="dead-worker-1"):
    with session_factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(
                bot_notify_attempts=attempts,
                notification_claimed_at=FIXED_NOW - timedelta(hours=1),
                notification_claimed_by=claimed_by,
            )
        )
        session.commit()


# --- bounded retries through stale-claim recovery ----------------------------


def test_stale_claim_at_retry_limit_goes_to_manual_review_in_idempotent_mode(
    client, settings, session_factory, stub
):
    """Regression for the audit finding: interrupted attempts must count
    against the retry limit. Before the fix, a delivery whose worker died on
    every attempt was requeued forever in idempotent mode."""
    idempotent = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    payment = make_verified_pending(client, settings, session_factory, stub, order_id="st-cap")
    _make_stale_claim(
        session_factory, payment.id, attempts=idempotent.bot_notify_max_attempts
    )

    with session_factory() as session:
        recovered = release_stale_claims(session, idempotent, now=FIXED_NOW, jitter=lambda: 1.0)
    assert recovered == 1

    payment = get_payment(session_factory, "st-cap")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.bot_notify_reason == ReasonCode.RETRY_LIMIT_REACHED.value
    assert payment.next_retry_at is None  # never scheduled again
    types = event_types(get_events(session_factory, payment.id))
    assert "notification_recovered_after_restart" in types
    assert "manual_review_required" in types


def test_stale_claim_below_limit_still_requeues_in_idempotent_mode(
    client, settings, session_factory, stub
):
    idempotent = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    payment = make_verified_pending(client, settings, session_factory, stub, order_id="st-req")
    _make_stale_claim(session_factory, payment.id, attempts=2)

    with session_factory() as session:
        assert release_stale_claims(session, idempotent, now=FIXED_NOW, jitter=lambda: 1.0) == 1

    payment = get_payment(session_factory, "st-req")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.next_retry_at is not None  # scheduled, not lost
    assert payment.notification_claimed_at is None  # claim released
    assert payment.bot_notify_attempts == 2  # history preserved


def test_stale_claim_recovery_batch_is_bounded(client, settings, session_factory, stub):
    """Recovery processes a bounded batch per pass; nothing is lost — the
    remainder is recovered by the following passes."""
    ids = []
    for i in range(5):
        payment = make_verified_pending(
            client, settings, session_factory, stub, order_id=f"st-batch-{i}"
        )
        _make_stale_claim(session_factory, payment.id, attempts=1)
        ids.append(payment.id)

    recovered_per_pass = []
    for _ in range(4):
        with session_factory() as session:
            recovered_per_pass.append(
                release_stale_claims(session, settings, now=FIXED_NOW, jitter=lambda: 1.0, limit=2)
            )
    assert recovered_per_pass == [2, 2, 1, 0]  # bounded and complete
    for i in range(5):
        # Safe mode: every interrupted attempt is an ambiguous delivery.
        payment = get_payment(session_factory, f"st-batch-{i}")
        assert payment.status == PaymentStatus.MANUAL_REVIEW.value
        assert payment.bot_notify_reason == ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value


# --- claim-ownership integrity ----------------------------------------------


def test_straggler_result_never_recorded_against_reowned_claim(
    client, settings, session_factory, stub
):
    """A worker whose attempt outlived its claim must not record its outcome
    once the claim belongs to another worker/attempt — and the discard is
    itself audited."""
    payment = make_verified_pending(client, settings, session_factory, stub, order_id="own-1")

    with session_factory() as session:
        claimed_a = claim_next_due(session, worker_id="worker-A", now=FIXED_NOW)
    assert claimed_a is not None and claimed_a.attempt == 1

    # Stale recovery released A's claim and worker B re-claimed (attempt 2).
    with session_factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.id == payment.id)
            .values(
                bot_notify_attempts=2,
                notification_claimed_at=FIXED_NOW,
                notification_claimed_by="worker-B",
            )
        )
        session.commit()

    # A's stale 2xx arrives: it must be discarded, not recorded.
    outcome = AttemptOutcome(
        kind=OutcomeKind.ACCEPTED,
        reason_code=ReasonCode.BOT_NOTIFY_ACCEPTED.value,
        log_event="bot_notification_accepted",
        http_status=200,
    )
    with session_factory() as session:
        record_attempt_result(
            session, settings, claimed_a, outcome, 10.0, now=FIXED_NOW, jitter=lambda: 1.0
        )

    refreshed = get_payment(session_factory, "own-1")
    assert refreshed.status == PaymentStatus.BOT_NOTIFY_PENDING.value  # untouched
    assert refreshed.notification_claimed_by == "worker-B"  # B's claim intact
    assert refreshed.bot_notify_accepted_at is None
    types = event_types(get_events(session_factory, payment.id))
    assert "bot_notification_result_discarded" in types
    assert "bot_notification_accepted" not in types

    # B's own result (matching worker AND attempt) still records normally.
    claimed_b = ClaimedPayment(
        payment_id=payment.id,
        bot_order_id=payment.bot_order_id,
        gateway_order_id=payment.gateway_order_id,
        attempt=2,
        worker_id="worker-B",
    )
    with session_factory() as session:
        record_attempt_result(
            session, settings, claimed_b, outcome, 10.0, now=FIXED_NOW, jitter=lambda: 1.0
        )
    refreshed = get_payment(session_factory, "own-1")
    assert refreshed.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


# --- durability across restarts ---------------------------------------------


def test_manual_review_survives_restart_and_worker_passes(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(client, settings, session_factory, stub, order_id="mr-dur")
    bot_stub.result = httpx.Response(422)
    run_pass(session_factory, notifier, settings)
    assert get_payment(session_factory, "mr-dur").status == PaymentStatus.MANUAL_REVIEW.value
    requests_before = len(bot_stub.requests)

    # "Restart": fresh sessions, repeated passes — the payment must never be
    # touched again by the worker, in either retry mode.
    bot_stub.result = httpx.Response(200, json={"ok": True})
    for mode in ("safe", "idempotent"):
        mode_settings = settings.model_copy(update={"bot_notify_retry_mode": mode})
        result = run_pass(session_factory, notifier, mode_settings)
        assert result["processed"] == 0
    assert len(bot_stub.requests) == requests_before
    payment = get_payment(session_factory, "mr-dur")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.manual_review_at is not None  # review fact preserved


def test_scheduled_retry_survives_restart(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A retry scheduled before a process restart is delivered after it —
    nothing lives in process memory."""
    make_verified_pending(client, settings, session_factory, stub, order_id="rt-dur")
    bot_stub.result = httpx.Response(500)
    run_pass(session_factory, notifier, settings, now=FIXED_NOW)
    payment = get_payment(session_factory, "rt-dur")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.next_retry_at is not None
    assert payment.bot_notify_attempts == 1

    # Restart: everything reconstructed from the database; the clock passes
    # the scheduled time and delivery succeeds.
    bot_stub.result = httpx.Response(200, json={"ok": True})
    run_pass(
        session_factory, notifier, settings, now=FIXED_NOW + timedelta(hours=1),
        worker_id="restarted-worker",
    )
    payment = get_payment(session_factory, "rt-dur")
    assert payment.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert payment.bot_notify_attempts == 2  # history preserved across restart
