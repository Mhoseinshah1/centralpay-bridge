"""Safe bot notification delivery.

Ordering that survives crashes:
1. The callback transaction commits the verified payment together with
   status=bot_notify_pending and a bot_notification_queued audit event.
2. The worker claims a due payment (row lock, attempt counter, started event)
   and COMMITS the claim.
3. The HTTP request runs with NO database transaction open.
4. The result is classified and recorded in a NEW transaction.

A crash between 2 and 4 leaves a claimed row whose attempt outcome is
unknown; release_stale_claims handles it according to the retry mode (safe:
manual review, idempotent: requeue).
"""

import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.bot import (
    AttemptOutcome,
    BotNotifier,
    OutcomeKind,
    classify_response,
    classify_transport_error,
)
from app.config import Settings
from app.logging_setup import request_id_var
from app.models import Payment, PaymentStatus
from app.reasons import ReasonCode

logger = logging.getLogger("app.services.notification")

# Bounded exponential backoff: 1, 2, 5, 10, 30, 60 minutes. Attempts beyond
# the table reuse the final delay until the attempt limit is reached.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 120, 300, 600, 1800, 3600)

_DEFAULT_BATCH_SIZE = 20

NowFn = Callable[[], datetime]
JitterFn = Callable[[], float]


def utcnow() -> datetime:
    return datetime.now(UTC)


def default_jitter() -> float:
    """Multiplicative jitter; not security-sensitive."""
    return random.uniform(0.85, 1.15)


def retry_delay_seconds(
    attempts_completed: int, retry_after_seconds: int | None, jitter: JitterFn
) -> float:
    index = min(max(attempts_completed, 1), len(RETRY_DELAYS_SECONDS)) - 1
    delay = RETRY_DELAYS_SECONDS[index] * jitter()
    if retry_after_seconds is not None and retry_after_seconds > delay:
        delay = float(retry_after_seconds)
    return delay


@dataclass(frozen=True)
class ClaimedPayment:
    payment_id: int
    bot_order_id: str
    gateway_order_id: int
    attempt: int


def queue_notification(db: Session, payment: Payment, *, now: datetime) -> None:
    """Mark a just-verified payment as pending bot notification.

    Runs inside the caller's verification transaction so the verified state
    and the pending notification state commit atomically. The caller commits.
    """
    payment.status = PaymentStatus.BOT_NOTIFY_PENDING.value
    payment.bot_notify_reason = None
    payment.next_retry_at = now
    record_event(
        db,
        payment_id=payment.id,
        event_type="bot_notification_queued",
        data={
            "bot_order_id": payment.bot_order_id,
            "gateway_order_id": payment.gateway_order_id,
        },
    )


def _clear_claim(payment: Payment) -> None:
    payment.notification_claimed_at = None
    payment.notification_claimed_by = None


def _log_extra(
    payment: Payment | ClaimedPayment, *, attempt: int, outcome: AttemptOutcome | None = None
) -> dict[str, object]:
    extra: dict[str, object] = {
        "payment_id": payment.payment_id if isinstance(payment, ClaimedPayment) else payment.id,
        "bot_order_id": payment.bot_order_id,
        "gateway_order_id": payment.gateway_order_id,
        "attempt": attempt,
    }
    if outcome is not None:
        extra["reason_code"] = outcome.reason_code
        if outcome.http_status is not None:
            extra["http_status"] = outcome.http_status
        if outcome.error_code is not None:
            extra["error_code"] = outcome.error_code
    return extra


def claim_next_due(db: Session, *, worker_id: str, now: datetime) -> ClaimedPayment | None:
    """Claim one due payment with FOR UPDATE SKIP LOCKED and commit the claim."""
    payment = db.execute(
        select(Payment)
        .where(
            Payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value,
            or_(Payment.next_retry_at.is_(None), Payment.next_retry_at <= now),
            Payment.notification_claimed_at.is_(None),
        )
        .order_by(Payment.next_retry_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if payment is None:
        db.rollback()
        return None

    payment.bot_notify_attempts += 1
    payment.notification_claimed_at = now
    payment.notification_claimed_by = worker_id
    if payment.bot_notify_started_at is None:
        payment.bot_notify_started_at = now
    attempt = payment.bot_notify_attempts
    record_event(
        db,
        payment_id=payment.id,
        event_type="bot_notification_started",
        data={"attempt": attempt, "worker_id": worker_id},
    )
    claimed = ClaimedPayment(
        payment_id=payment.id,
        bot_order_id=payment.bot_order_id,
        gateway_order_id=payment.gateway_order_id,
        attempt=attempt,
    )
    db.commit()
    logger.info("bot_notification_started", extra=_log_extra(claimed, attempt=attempt))
    return claimed


def _move_to_manual_review(
    db: Session, payment: Payment, *, reason_code: str, attempt: int, now: datetime
) -> None:
    payment.status = PaymentStatus.MANUAL_REVIEW.value
    payment.bot_notify_reason = reason_code
    payment.manual_review_at = now
    payment.next_retry_at = None
    payment.last_error = f"bot notification: {reason_code}"
    _clear_claim(payment)
    record_event(
        db,
        payment_id=payment.id,
        event_type="manual_review_required",
        level="error",
        data={"reason": reason_code, "attempt": attempt},
    )
    logger.error(
        "manual_review_required",
        extra={
            "payment_id": payment.id,
            "bot_order_id": payment.bot_order_id,
            "gateway_order_id": payment.gateway_order_id,
            "attempt": attempt,
            "reason_code": reason_code,
        },
    )


def record_attempt_result(
    db: Session,
    settings: Settings,
    claimed: ClaimedPayment,
    outcome: AttemptOutcome,
    duration_ms: float,
    *,
    now: datetime,
    jitter: JitterFn = default_jitter,
) -> None:
    payment = db.execute(
        select(Payment).where(Payment.id == claimed.payment_id).with_for_update()
    ).scalar_one()
    if (
        payment.status != PaymentStatus.BOT_NOTIFY_PENDING.value
        or payment.notification_claimed_by is None
    ):
        # The claim was released (stale-claim recovery) or the payment was
        # resolved elsewhere while this attempt ran: never overwrite.
        db.rollback()
        logger.warning(
            "bot_notification_result_discarded",
            extra=_log_extra(claimed, attempt=claimed.attempt, outcome=outcome),
        )
        return

    payment.bot_last_http_status = outcome.http_status
    payment.bot_last_error_code = outcome.error_code

    effective_kind = outcome.kind
    if (
        outcome.kind is OutcomeKind.AMBIGUOUS
        and settings.bot_notify_retry_mode == "idempotent"
    ):
        # Only when the bot developer has explicitly confirmed duplicate
        # order_id delivery is idempotent.
        effective_kind = OutcomeKind.RETRYABLE

    extra = _log_extra(claimed, attempt=claimed.attempt, outcome=outcome)
    extra["duration_ms"] = duration_ms

    if effective_kind is OutcomeKind.ACCEPTED:
        # HTTP 2xx means the bot API accepted the request — never proof that
        # the user balance was credited.
        payment.status = PaymentStatus.BOT_NOTIFY_ACCEPTED.value
        payment.bot_notify_reason = ReasonCode.BOT_NOTIFY_ACCEPTED.value
        payment.bot_notify_accepted_at = now
        payment.next_retry_at = None
        payment.last_error = None
        _clear_claim(payment)
        record_event(
            db,
            payment_id=payment.id,
            event_type="bot_notification_accepted",
            data={
                "attempt": claimed.attempt,
                "http_status": outcome.http_status,
                "duration_ms": duration_ms,
            },
        )
        logger.info("bot_notification_accepted", extra=extra)
    elif effective_kind is OutcomeKind.RETRYABLE:
        record_event(
            db,
            payment_id=payment.id,
            event_type="bot_notification_failed",
            level="warning",
            data={
                "attempt": claimed.attempt,
                "reason_code": outcome.reason_code,
                "http_status": outcome.http_status,
                "error_code": outcome.error_code,
                "duration_ms": duration_ms,
            },
        )
        logger.warning(outcome.log_event, extra=extra)
        if claimed.attempt >= settings.bot_notify_max_attempts:
            logger.error("bot_retry_limit_reached", extra=extra)
            _move_to_manual_review(
                db,
                payment,
                reason_code=ReasonCode.RETRY_LIMIT_REACHED.value,
                attempt=claimed.attempt,
                now=now,
            )
        else:
            delay = retry_delay_seconds(claimed.attempt, outcome.retry_after_seconds, jitter)
            next_retry_at = now + timedelta(seconds=delay)
            payment.bot_notify_reason = outcome.reason_code
            payment.last_error = f"bot notification failed: {outcome.reason_code}"
            payment.next_retry_at = next_retry_at
            _clear_claim(payment)
            record_event(
                db,
                payment_id=payment.id,
                event_type="bot_notification_retry_scheduled",
                data={
                    "attempt": claimed.attempt,
                    "reason_code": outcome.reason_code,
                    "delay_seconds": round(delay),
                    "next_retry_at": next_retry_at.isoformat(),
                },
            )
            logger.info(
                "bot_retry_scheduled",
                extra={**extra, "next_retry_at": next_retry_at.isoformat()},
            )
    elif effective_kind is OutcomeKind.AMBIGUOUS:
        # The request may have been processed by the bot; retrying could
        # credit the user twice. Safe mode: administrator decides.
        record_event(
            db,
            payment_id=payment.id,
            event_type="bot_timeout_ambiguous",
            level="critical",
            data={
                "attempt": claimed.attempt,
                "reason_code": outcome.reason_code,
                "error_code": outcome.error_code,
                "duration_ms": duration_ms,
            },
        )
        logger.critical("bot_timeout_ambiguous", extra=extra)
        _move_to_manual_review(
            db,
            payment,
            reason_code=ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value,
            attempt=claimed.attempt,
            now=now,
        )
    else:
        record_event(
            db,
            payment_id=payment.id,
            event_type="bot_notification_failed",
            level="error",
            data={
                "attempt": claimed.attempt,
                "reason_code": outcome.reason_code,
                "http_status": outcome.http_status,
                "error_code": outcome.error_code,
                "duration_ms": duration_ms,
            },
        )
        logger.warning(outcome.log_event, extra=extra)
        _move_to_manual_review(
            db, payment, reason_code=outcome.reason_code, attempt=claimed.attempt, now=now
        )
    db.commit()


def execute_claimed_attempt(
    db: Session,
    notifier: BotNotifier | None,
    settings: Settings,
    claimed: ClaimedPayment,
    *,
    now_fn: NowFn = utcnow,
    jitter: JitterFn = default_jitter,
) -> AttemptOutcome:
    """Send the notification (no transaction open) and record the result."""
    if notifier is None:
        outcome = AttemptOutcome(
            kind=OutcomeKind.MANUAL,
            reason_code=ReasonCode.BOT_INVALID_CONFIGURATION.value,
            log_event="bot_invalid_configuration",
        )
        record_attempt_result(db, settings, claimed, outcome, 0.0, now=now_fn(), jitter=jitter)
        return outcome

    started = time.perf_counter()
    try:
        response = notifier.send_payment_notification(claimed.bot_order_id)
    except httpx.HTTPError as exc:
        outcome = classify_transport_error(exc)
    else:
        outcome = classify_response(response)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    record_attempt_result(db, settings, claimed, outcome, duration_ms, now=now_fn(), jitter=jitter)
    return outcome


def release_stale_claims(
    db: Session,
    settings: Settings,
    *,
    now: datetime,
    jitter: JitterFn = default_jitter,
) -> int:
    """Recover payments whose claiming worker died mid-attempt.

    The interrupted attempt's outcome is unknown — the request may or may not
    have reached the bot — so this is treated exactly like an ambiguous
    delivery: manual review in safe mode, requeue in idempotent mode.
    """
    cutoff = now - timedelta(seconds=settings.bot_notify_claim_timeout_seconds)
    payments = (
        db.execute(
            select(Payment)
            .where(
                Payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value,
                Payment.notification_claimed_at.is_not(None),
                Payment.notification_claimed_at <= cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not payments:
        db.rollback()
        return 0

    for payment in payments:
        attempt = payment.bot_notify_attempts
        data = {
            "attempt": attempt,
            "stale_worker_id": payment.notification_claimed_by,
            "retry_mode": settings.bot_notify_retry_mode,
        }
        if settings.bot_notify_retry_mode == "idempotent":
            delay = retry_delay_seconds(attempt, None, jitter)
            next_retry_at = now + timedelta(seconds=delay)
            payment.bot_notify_reason = ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value
            payment.next_retry_at = next_retry_at
            _clear_claim(payment)
            record_event(
                db,
                payment_id=payment.id,
                event_type="notification_recovered_after_restart",
                level="warning",
                data={**data, "action": "requeued", "next_retry_at": next_retry_at.isoformat()},
            )
        else:
            record_event(
                db,
                payment_id=payment.id,
                event_type="notification_recovered_after_restart",
                level="warning",
                data={**data, "action": "manual_review"},
            )
            record_event(
                db,
                payment_id=payment.id,
                event_type="bot_timeout_ambiguous",
                level="critical",
                data={**data, "stage": "stale_claim"},
            )
            _move_to_manual_review(
                db,
                payment,
                reason_code=ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value,
                attempt=attempt,
                now=now,
            )
        logger.warning(
            "notification_recovered_after_restart",
            extra={
                "payment_id": payment.id,
                "bot_order_id": payment.bot_order_id,
                "gateway_order_id": payment.gateway_order_id,
                "attempt": attempt,
                "retry_mode": settings.bot_notify_retry_mode,
            },
        )
    db.commit()
    return len(payments)


def run_worker_pass(
    db: Session,
    notifier: BotNotifier | None,
    settings: Settings,
    *,
    worker_id: str,
    now_fn: NowFn = utcnow,
    jitter: JitterFn = default_jitter,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """One polling pass: recover stale claims, then process due payments."""
    token = request_id_var.set(f"ntf-{uuid.uuid4().hex[:16]}")
    try:
        recovered = release_stale_claims(db, settings, now=now_fn(), jitter=jitter)
    finally:
        request_id_var.reset(token)

    processed = 0
    while processed < batch_size:
        token = request_id_var.set(f"ntf-{uuid.uuid4().hex[:16]}")
        try:
            claimed = claim_next_due(db, worker_id=worker_id, now=now_fn())
            if claimed is None:
                break
            execute_claimed_attempt(db, notifier, settings, claimed, now_fn=now_fn, jitter=jitter)
            processed += 1
        finally:
            request_id_var.reset(token)
    return {"recovered": recovered, "processed": processed}
