"""Server-side reconciliation of stuck ``link_created`` payments.

Production incident: a payer completed payment on CentralPay but the browser
never delivered the signed callback (no request reached the edge at all), so
the payment stayed in ``link_created`` forever and the customer was not
credited. The browser callback remains the fast PRIMARY path; this module is
the trusted server-side safety net.

Design:

* Selection: ``link_created`` payments at least
  ``RECONCILIATION_MIN_AGE_SECONDS`` old (age measured from the moment the
  payment link was issued), whose ``reconciliation_next_at`` is NULL (never
  attempted) or due, with fewer than ``RECONCILIATION_MAX_ATTEMPTS`` attempts.
  Oldest due first. NOTHING else is ever selected — verified, notification,
  and ``manual_review`` states are excluded by the status predicate alone.
* Settlement: the SAME shared :func:`app.services.verification.verify_and_settle`
  the callback uses — one settlement path, all financial checks identical
  (explicit success, referenceId validity/uniqueness, payable-amount and
  gateway_user_id snapshot matching, mismatch -> manual_review, atomic
  notification queueing). No callback URL, token, or signature is ever faked:
  reconciliation is server-to-server verification only, and the one-time
  callback token machinery is untouched (a later browser callback is handled
  by the normal duplicate path).
* Concurrency: each payment is claimed with ``FOR UPDATE SKIP LOCKED`` and
  the ROW LOCK IS HELD ACROSS THE VERIFY CALL — exactly how the callback path
  serializes. Two reconciliation workers therefore skip each other's rows,
  and a callback racing a reconciliation waits on the lock and then takes the
  duplicate path. The claim columns are operational visibility, not the
  correctness mechanism.
* Outcomes: gateway success settles and queues the bot notification (once);
  "not paid" and transport failures schedule a retry on the two-stage
  AGE-based schedule (see reconciliation_retry_delay_seconds — every 10 s
  while the link is under 10 minutes old, every 5 minutes afterwards, by
  default) and NEVER move the payment to a failed or manual state; financial
  mismatches keep the existing manual_review behavior; attempt exhaustion
  (default 1000 attempts ≈ 3 days of coverage) stops the polling while
  leaving the payment in ``link_created`` for operators. Reconciliation
  stops immediately once the payment is verified, leaves link_created, or
  moves to manual_review.
* Privacy: events and logs carry only payment_id, gateway_order_id, attempt,
  worker_id, and fixed internal reason codes — never tokens, signatures, API
  keys, card numbers, raw gateway responses, or raw Telegram ids.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.centralpay import CentralPayClient
from app.config import Settings
from app.exceptions import CentralPayError
from app.logging_setup import request_id_var
from app.models import Payment, PaymentStatus
from app.services.verification import SettlementOutcome, verify_and_settle

logger = logging.getLogger("app.services.reconciliation")

NowFn = Callable[[], datetime]

# Fixed internal error codes stored in reconciliation_last_error_code. Never
# raw gateway text.
ERROR_GATEWAY_NOT_PAID = "gateway_not_paid"
ERROR_INTERNAL = "internal_error"


def utcnow() -> datetime:
    return datetime.now(UTC)


def reconciliation_backoff_seconds(settings: Settings, attempt: int) -> int:
    """DEPRECATED utility — the exponential backoff of the original
    reconciliation release (initial * 2^(attempt-1), capped). NOT called by
    the reconciliation scheduler anymore: the active schedule is the
    two-stage age-based :func:`reconciliation_retry_delay_seconds`. Retained
    only because the corresponding settings remain accepted for environment
    compatibility."""
    exponent = max(attempt - 1, 0)
    # Cap the exponent first so huge attempt numbers cannot overflow.
    if exponent > 30:
        return settings.reconciliation_max_backoff_seconds
    delay = settings.reconciliation_initial_backoff_seconds * (1 << exponent)
    return min(delay, settings.reconciliation_max_backoff_seconds)


def reconciliation_retry_delay_seconds(
    settings: Settings,
    *,
    payment: Payment,
    now: datetime,
) -> int:
    """Two-stage, AGE-based retry delay — the ACTIVE default schedule.

    The stage is derived from the REAL age of the payment link (anchored on
    ``callback_token_issued_at``, falling back to ``created_at``), never from
    the attempt counter, so stopping or restarting the worker can never
    restart the fast window: a 20-minute-old payment with one recorded
    attempt goes straight to the slow interval.

    * age <  ``reconciliation_fast_window_seconds`` (default 600 s): retry in
      ``reconciliation_fast_interval_seconds`` (default 10 s);
    * age >= the window (including exactly at the boundary): retry in
      ``reconciliation_slow_interval_seconds`` (default 300 s).

    A clock skew that makes the link look issued in the future clamps the
    age to zero (fast interval) instead of producing a negative age.
    """
    issued_at = payment.callback_token_issued_at or payment.created_at
    if issued_at.tzinfo is None:  # SQLite returns naive UTC datetimes
        issued_at = issued_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age_seconds = max((now - issued_at).total_seconds(), 0)
    if age_seconds < settings.reconciliation_fast_window_seconds:
        return settings.reconciliation_fast_interval_seconds
    return settings.reconciliation_slow_interval_seconds


def _claim_next_due(
    db: Session, settings: Settings, *, worker_id: str, now: datetime
) -> Payment | None:
    """Select and claim ONE due payment, keeping its row lock.

    The lock is intentionally held across the verify call (like the callback
    path) — that lock IS the double-settlement guard. SKIP LOCKED makes a
    second worker pick a different row instead of waiting.
    """
    min_age_cutoff = now - timedelta(seconds=settings.reconciliation_min_age_seconds)
    link_age_anchor = func.coalesce(Payment.callback_token_issued_at, Payment.created_at)
    payment = db.execute(
        select(Payment)
        .where(
            # ONLY stuck link_created rows: verified / notification /
            # manual_review / created / getlink_failed states never match.
            Payment.status == PaymentStatus.LINK_CREATED.value,
            Payment.gateway_verified_at.is_(None),  # belt-and-braces
            link_age_anchor <= min_age_cutoff,  # give the browser callback time
            or_(
                Payment.reconciliation_next_at.is_(None),
                Payment.reconciliation_next_at <= now,
            ),
            Payment.reconciliation_attempts < settings.reconciliation_max_attempts,
        )
        .order_by(func.coalesce(Payment.reconciliation_next_at, Payment.created_at).asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if payment is None:
        db.rollback()
        return None
    payment.reconciliation_attempts += 1
    payment.reconciliation_last_at = now
    payment.reconciliation_claimed_at = now
    payment.reconciliation_claimed_by = worker_id
    # PROVISIONAL pessimistic schedule, closing the multi-worker gap between
    # the shared settlement path's commit (which releases this row lock) and
    # _finalize's bookkeeping transaction: the not-paid/transport paths commit
    # with the row still link_created, and without this the row would sit with
    # a NULL (= due) next_at in that gap, so another worker could claim it and
    # fire an immediate extra verify, defeating the bounded backoff. Committed
    # atomically WITH the outcome; _finalize then replaces it (None when
    # verified/exhausted, recomputed on retry). A crash before any commit
    # rolls all of this back — no schedule is ever lost or invented.
    payment.reconciliation_next_at = now + timedelta(
        seconds=reconciliation_retry_delay_seconds(settings, payment=payment, now=now)
    )
    # Not committed here: the claim rides in the same transaction as the
    # settlement outcome (verify is read-only on the gateway side, so a crash
    # mid-verify loses only this bookkeeping, never financial state).
    return payment


def _finalize(
    db: Session,
    settings: Settings,
    *,
    payment_id: int,
    worker_id: str,
    attempt: int,
    outcome: str,
    error_code: str | None,
    now: datetime,
) -> str:
    """Record the attempt outcome under a fresh row lock.

    The settlement itself was already committed by the shared verification
    path (releasing the claim transaction), so bookkeeping re-locks the row
    and re-checks its status: if a concurrent callback settled the payment in
    the gap, retry scheduling is skipped — the stored state is never touched
    beyond clearing the claim. Returns the recorded disposition.
    """
    payment = db.execute(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    ).scalar_one()
    # Idempotent attempt-count repair: if the claim transaction rolled back
    # (unexpected exception mid-verify), re-record this attempt.
    if payment.reconciliation_attempts < attempt:
        payment.reconciliation_attempts = attempt
        payment.reconciliation_last_at = now
    payment.reconciliation_claimed_at = None
    payment.reconciliation_claimed_by = None

    safe_extra = {
        "payment_id": payment.id,
        "gateway_order_id": payment.gateway_order_id,
        "attempt": attempt,
        "worker_id": worker_id,
    }

    if outcome == "verified":
        payment.reconciliation_next_at = None
        payment.reconciliation_last_error_code = None
        record_event(
            db,
            payment_id=payment.id,
            event_type="reconciliation_verified",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "attempt": attempt,
                "worker_id": worker_id,
            },
        )
        db.commit()
        logger.info("reconciliation_verified", extra=safe_extra)
        return "verified"

    if outcome == "under_review":
        # The shared path already recorded the mismatch and moved the payment
        # to manual_review (never auto-processed again). Only the claim is
        # cleared; polling stops via the status predicate.
        payment.reconciliation_next_at = None
        db.commit()
        logger.error("reconciliation_manual_review", extra=safe_extra)
        return "under_review"

    # Retryable outcomes (gateway_not_paid / transport / internal): schedule
    # the next attempt — but only while the payment is still link_created. If
    # a callback settled it in the meantime, there is nothing left to retry.
    if payment.status != PaymentStatus.LINK_CREATED.value:
        db.commit()
        return "superseded"

    payment.reconciliation_last_error_code = error_code
    event_type = (
        "reconciliation_gateway_not_paid"
        if outcome == "gateway_not_paid"
        else "reconciliation_transport_failed"
    )
    record_event(
        db,
        payment_id=payment.id,
        event_type=event_type,
        level="warning",
        data={
            "gateway_order_id": payment.gateway_order_id,
            "attempt": attempt,
            "worker_id": worker_id,
            "error_code": error_code,
        },
    )
    if attempt >= settings.reconciliation_max_attempts:
        # Exhausted: stop frequent polling but change NOTHING financial — the
        # payment stays link_created and visible to operators (privacy-audit,
        # events, admin tooling). Never marked paid or failed.
        payment.reconciliation_next_at = None
        record_event(
            db,
            payment_id=payment.id,
            event_type="reconciliation_exhausted",
            level="error",
            data={
                "gateway_order_id": payment.gateway_order_id,
                "attempt": attempt,
                "worker_id": worker_id,
                "error_code": error_code,
            },
        )
        db.commit()
        logger.error("reconciliation_exhausted", extra=safe_extra)
        return "exhausted"

    delay = reconciliation_retry_delay_seconds(settings, payment=payment, now=now)
    next_at = now + timedelta(seconds=delay)
    payment.reconciliation_next_at = next_at
    record_event(
        db,
        payment_id=payment.id,
        event_type="reconciliation_retry_scheduled",
        data={
            "gateway_order_id": payment.gateway_order_id,
            "attempt": attempt,
            "worker_id": worker_id,
            "delay_seconds": delay,
            "next_at": next_at.isoformat(),
        },
    )
    db.commit()
    logger.warning(
        "reconciliation_retry_scheduled",
        extra={**safe_extra, "error_code": error_code, "delay_seconds": delay},
    )
    return "retry_scheduled"


def run_reconciliation_pass(
    db: Session,
    client: CentralPayClient,
    settings: Settings,
    *,
    worker_id: str,
    now_fn: NowFn = utcnow,
    batch_size: int | None = None,
    time_budget_seconds: float | None = None,
) -> dict[str, int]:
    """One reconciliation pass: claim due payments one at a time and settle
    or reschedule each in its own transaction.

    A per-payment failure never terminates the pass. The wall-clock budget
    bounds the pass LENGTH by refusing to START another claim once exceeded;
    it cannot interrupt an in-flight verify call, so a pass may overrun by up
    to one gateway timeout. Bot-notification latency does not depend on this
    budget at all: the worker runs reconciliation in a DEDICATED THREAD (see
    app/worker.py), never inline in the notification loop.

    Load note: ``batch_size / interval`` is an AVERAGE upper bound on verify
    calls, not a burst bound — a single pass may issue its whole batch
    back-to-back.
    """
    stats = {
        "processed": 0,
        "verified": 0,
        "retry_scheduled": 0,
        "under_review": 0,
        "exhausted": 0,
        "superseded": 0,
    }
    if not settings.reconciliation_enabled:
        return stats
    limit = batch_size if batch_size is not None else settings.reconciliation_batch_size
    budget = (
        time_budget_seconds
        if time_budget_seconds is not None
        else settings.reconciliation_interval_seconds
    )
    started = time.monotonic()

    while stats["processed"] < limit and (time.monotonic() - started) < budget:
        token = request_id_var.set(f"rec-{uuid.uuid4().hex[:16]}")
        try:
            now = now_fn()
            payment = _claim_next_due(db, settings, worker_id=worker_id, now=now)
            if payment is None:
                break
            payment_id = payment.id
            attempt = payment.reconciliation_attempts
            gateway_order_id = payment.gateway_order_id
            stats["processed"] += 1
            try:
                settled = verify_and_settle(
                    db, client, payment, settings=settings, source="reconciliation"
                )
            except CentralPayError as exc:
                # The shared path recorded centralpay_verify_failed
                # (stage=transport, internal code only) and committed.
                disposition = _finalize(
                    db,
                    settings,
                    payment_id=payment_id,
                    worker_id=worker_id,
                    attempt=attempt,
                    outcome="transport",
                    error_code=exc.code,
                    now=now_fn(),
                )
            except Exception:
                # Unexpected bug: never let one payment kill the pass. Roll
                # back whatever state the failed attempt left, then record a
                # retry with a fixed internal code (no exception text).
                db.rollback()
                logger.exception(
                    "reconciliation_attempt_crashed",
                    extra={
                        "payment_id": payment_id,
                        "gateway_order_id": gateway_order_id,
                        "attempt": attempt,
                        "worker_id": worker_id,
                    },
                )
                disposition = _finalize(
                    db,
                    settings,
                    payment_id=payment_id,
                    worker_id=worker_id,
                    attempt=attempt,
                    outcome="transport",
                    error_code=ERROR_INTERNAL,
                    now=now_fn(),
                )
            else:
                if settled is SettlementOutcome.VERIFIED:
                    disposition = _finalize(
                        db,
                        settings,
                        payment_id=payment_id,
                        worker_id=worker_id,
                        attempt=attempt,
                        outcome="verified",
                        error_code=None,
                        now=now_fn(),
                    )
                elif settled is SettlementOutcome.UNDER_REVIEW:
                    disposition = _finalize(
                        db,
                        settings,
                        payment_id=payment_id,
                        worker_id=worker_id,
                        attempt=attempt,
                        outcome="under_review",
                        error_code=None,
                        now=now_fn(),
                    )
                else:  # GATEWAY_NOT_PAID
                    disposition = _finalize(
                        db,
                        settings,
                        payment_id=payment_id,
                        worker_id=worker_id,
                        attempt=attempt,
                        outcome="gateway_not_paid",
                        error_code=ERROR_GATEWAY_NOT_PAID,
                        now=now_fn(),
                    )
            stats[disposition] = stats.get(disposition, 0) + 1
        finally:
            request_id_var.reset(token)
    return stats
