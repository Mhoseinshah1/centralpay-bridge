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
  attempted) or due, with fewer than ``RECONCILIATION_MAX_ATTEMPTS`` attempts,
  and younger than ``RECONCILIATION_MAX_AGE_SECONDS`` (the hard reconciliation
  lifetime — older payments are excluded from selection entirely, never
  deleted or mutated, preserved for audit/operator inspection). Split into two
  age tiers — ACTIVE (age < fast window) and EXPIRING (fast window <= age <
  max age) — each ordered oldest-due-first; every pass has a small MANDATORY
  fairness prefix at its HEAD — slot 0 prefers the active tier, the next
  ``RECONCILIATION_SLOW_TIER_RESERVED_SLOTS`` slot(s) prefer the expiring
  tier — with unused capacity from either tier spilling to the other, so a
  historical backlog can never delay a newly-created payment by more than a
  few seconds, and sustained fresh traffic can never permanently starve the
  expiring tier. The prefix runs before the pass's wall-clock time budget can
  stop it — even a single verify call slow enough to exhaust the whole
  budget cannot skip the rest of the prefix — so BOTH tiers get a real
  opportunity every pass regardless of gateway latency (see
  ``run_reconciliation_pass``). NOTHING else is ever selected — verified,
  notification, and ``manual_review`` states are excluded by the status
  predicate alone.
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
  AGE-based schedule (see reconciliation_retry_delay_seconds — fast while the
  link is under the fast window old, slow afterwards, by default) and NEVER
  move the payment to a failed or manual state; financial mismatches keep the
  existing manual_review behavior; attempt exhaustion (default 1000 attempts)
  or reaching the max-age hard limit stops the polling while leaving the
  payment in ``link_created`` for operators. Reconciliation stops immediately
  once the payment is verified, leaves link_created, or moves to
  manual_review.
* Privacy: events and logs carry only payment_id, gateway_order_id, attempt,
  worker_id, and fixed internal reason codes — never tokens, signatures, API
  keys, card numbers, raw gateway responses, or raw Telegram ids.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, not_, or_, select
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

    * age <  ``reconciliation_fast_window_seconds`` (default 900 s): retry in
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


def link_age_anchor() -> Any:
    """The timestamp reconciliation ages a ``link_created`` payment from.

    Falls back to ``created_at`` for the (rare) payment that never reached a
    successful ``getLink`` call. Public so every read of "how old is this
    link" — the claim queries below, and read-only reporting such as the
    stuck-payments overview — uses the exact same expression and can never
    quietly disagree about what "age" means.
    """
    return func.coalesce(Payment.callback_token_issued_at, Payment.created_at)


def aged_out_age_condition(settings: Settings, *, now: datetime) -> Any:
    """THE single boolean AGE expression for "aged out": link age >=
    ``reconciliation_max_age_seconds``. Not a full condition tuple — just
    the raw boundary comparison against :func:`link_age_anchor`, for
    callers to combine with whatever ``status``/``gateway_verified_at``
    scope they need (see :func:`aged_out_conditions` for the ``link_created``
    -scoped default every read-only bucket view uses, and
    ``app.services.reconcile_inspect`` for the broader, status-unscoped
    variant the ``--verify`` safety gate needs).

    Every aged-out/not-aged-out check in this codebase evaluates exactly
    this expression — never a locally re-derived
    ``now - timedelta(seconds=reconciliation_max_age_seconds)`` cutoff —
    so none of them can quietly drift apart.
    """
    cutoff = now - timedelta(seconds=settings.reconciliation_max_age_seconds)
    return link_age_anchor() <= cutoff


def tier_age_conditions(
    settings: Settings,
    *,
    now: datetime,
    age_floor: timedelta | None,
    age_ceiling: timedelta | None,
) -> tuple[Any, ...]:
    """Pure age/status/gateway_verified BOUNDARY for a ``link_created``
    payment — no due/attempt predicates. ``age_floor``/``age_ceiling`` of
    ``None`` means "no lower bound"/"no upper bound" respectively.

    THE single authoritative shape every link_created age tier in this
    codebase is built from: :func:`tier_due_conditions` adds the
    due/attempt predicates on top of exactly this, and
    :func:`active_tier_age_conditions` / :func:`expiring_tier_age_conditions`
    call this with the ACTIVE/EXPIRING tiers' fixed bounds — so the claim
    path, the due-predicate reporting, and the pure age-bucket reporting
    (``app.services.reconciliation_status``, ``app.services.stuck_payments``,
    ``app.services.reconcile_inspect``) all evaluate the exact same boundary
    math, never a locally re-derived one.
    """
    link_age_anchor_expr = link_age_anchor()
    conditions: list[Any] = [
        # ONLY stuck link_created rows: verified / notification /
        # manual_review / created / getlink_failed states never match.
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),  # belt-and-braces
    ]
    if age_floor is not None:
        conditions.append(link_age_anchor_expr <= now - age_floor)  # age >= age_floor
    if age_ceiling is not None:
        conditions.append(link_age_anchor_expr > now - age_ceiling)  # age < age_ceiling
    return tuple(conditions)


def active_tier_age_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """The ACTIVE bucket's age/status/gateway_verified boundary:
    ``link_created``, unverified, age < fast_window. NO
    ``reconciliation_min_age_seconds`` floor — that is a DUE-only
    refinement (see :func:`active_tier_due_conditions`), not a bucket
    boundary: a payment 2 seconds old is still in the ACTIVE age bucket
    even though the worker will not yet attempt it.

    THE single authoritative definition of where the active tier's age
    boundary sits — shared by the worker's claim (via
    :func:`active_tier_due_conditions`), ``reconciliation_status``'s
    ``PaymentBuckets.active``, and ``reconcile_inspect``'s ``age_bucket``.
    """
    return tier_age_conditions(
        settings,
        now=now,
        age_floor=None,
        age_ceiling=timedelta(seconds=settings.reconciliation_fast_window_seconds),
    )


def expiring_tier_age_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """The EXPIRING bucket's age/status/gateway_verified boundary:
    ``link_created``, unverified, fast_window <= age < max_age.

    THE single authoritative definition — shared by the worker's claim
    (via :func:`expiring_tier_due_conditions`), ``reconciliation_status``'s
    ``PaymentBuckets.expiring``, and ``reconcile_inspect``'s ``age_bucket``.
    """
    return tier_age_conditions(
        settings,
        now=now,
        age_floor=timedelta(seconds=settings.reconciliation_fast_window_seconds),
        age_ceiling=timedelta(seconds=settings.reconciliation_max_age_seconds),
    )


def aged_out_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """``link_created``, unverified, aged out (age >= max_age).

    THE single authoritative AGED_OUT boundary — shared by
    ``reconciliation_status``'s ``PaymentBuckets.aged_out`` and
    ``stuck_payments``'s ``EXPIRED`` category. (``reconcile_inspect``'s
    ``--verify`` safety gate needs a broader, status-unscoped variant —
    every unverified payment, not only ``link_created`` rows — built
    directly from :func:`aged_out_age_condition`; see that module.)
    """
    return (
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),
        aged_out_age_condition(settings, now=now),
    )


def tier_due_conditions(
    settings: Settings,
    *,
    now: datetime,
    age_floor: timedelta,
    age_ceiling: timedelta,
) -> tuple[Any, ...]:
    """Pure WHERE-condition builder: is a ``link_created`` payment due for
    reconciliation in the age tier ``[age_floor, age_ceiling)``? Builds on
    :func:`tier_age_conditions` — never re-expresses the boundary math —
    adding only the due/attempt predicates.

    Extracted from ``_claim_in_age_range`` so read-only reporting (e.g.
    ``app.services.reconciliation_status``) can mirror selection semantics
    EXACTLY — by construction, not by re-derivation — without ever calling
    the mutating claim path itself. Never locks, never orders, never limits;
    the claim path adds those on top of these same conditions.
    """
    return (
        *tier_age_conditions(settings, now=now, age_floor=age_floor, age_ceiling=age_ceiling),
        or_(
            Payment.reconciliation_next_at.is_(None),
            Payment.reconciliation_next_at <= now,
        ),
        Payment.reconciliation_attempts < settings.reconciliation_max_attempts,
    )


def active_tier_due_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """The <fast-window (default 15 min) tier's due predicate — read-only
    counterpart of ``_claim_active_tier``."""
    return tier_due_conditions(
        settings,
        now=now,
        age_floor=timedelta(seconds=settings.reconciliation_min_age_seconds),
        age_ceiling=timedelta(seconds=settings.reconciliation_fast_window_seconds),
    )


def expiring_tier_due_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """The fast-window-to-max-age (default 15 min-2 h) tier's due predicate —
    read-only counterpart of ``_claim_expiring_tier``."""
    return tier_due_conditions(
        settings,
        now=now,
        age_floor=timedelta(seconds=settings.reconciliation_fast_window_seconds),
        age_ceiling=timedelta(seconds=settings.reconciliation_max_age_seconds),
    )


def reconciliation_exhausted_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """A ``link_created`` payment whose reconciliation attempts are exhausted
    but which has NOT (yet) aged out — i.e. polling stopped at the attempts
    cap while the payment was still inside the reconciliation lifetime.
    Deliberately excludes aged-out rows: those are reported separately (see
    ``reconciliation_status.py``'s ``aged_out`` bucket and
    ``stuck_payments.py``'s ``EXPIRED`` category), which always takes
    priority over "exhausted" for operator-facing categorization. The
    "NOT aged out" half reuses :func:`aged_out_age_condition` (negated),
    never a locally re-derived cutoff.
    """
    return (
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),
        not_(aged_out_age_condition(settings, now=now)),  # NOT aged out
        Payment.reconciliation_next_at.is_(None),
        Payment.reconciliation_attempts >= settings.reconciliation_max_attempts,
    )


def reconciliation_exhausted_ever_conditions(
    settings: Settings, *, now: datetime
) -> tuple[Any, ...]:
    """Same population as :func:`reconciliation_exhausted_conditions`, minus
    its "NOT aged out" restriction: every ``link_created`` payment whose
    reconciliation attempts hit the cap, regardless of whether it has ALSO
    since aged out.

    Operator-facing bucket display (``reconciliation_status.py``,
    ``stuck_payments.py``) deliberately wants "exhausted" and "aged out" as
    mutually exclusive categories, which is exactly what
    :func:`reconciliation_exhausted_conditions` gives it. Monitoring needs
    the opposite: a payment reconciliation gave up on due to repeated
    failures must stay visible as a critical condition even after it also
    crosses the age boundary later -- otherwise the SAME stuck payment
    aging out would silently "resolve" the monitor's incident, reporting a
    false recovery for something that got MORE stuck, not less. See
    ``app.services.monitor_checks.check_reconciliation``, the sole
    consumer of this condition.
    """
    return (
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),
        Payment.reconciliation_next_at.is_(None),
        Payment.reconciliation_attempts >= settings.reconciliation_max_attempts,
    )


def _claim_in_age_range(
    db: Session,
    settings: Settings,
    *,
    worker_id: str,
    now: datetime,
    age_floor: timedelta,
    age_ceiling: timedelta,
) -> Payment | None:
    """Select and claim ONE due payment whose link age falls in
    ``[age_floor, age_ceiling)``, keeping its row lock.

    Shared by both reconciliation tiers (see ``run_reconciliation_pass``):
    the active tier bounds age to ``[min_age, fast_window)``, the expiring
    tier to ``[fast_window, max_age)``. A payment aged ``max_age`` or older
    therefore matches NEITHER tier and is never claimed by anything — that
    IS the enforcement of the hard reconciliation lifetime; no separate
    exclusion flag exists, and nothing about the row is ever written.

    The lock is intentionally held across the verify call (like the callback
    path) — that lock IS the double-settlement guard. SKIP LOCKED makes a
    second worker pick a different row instead of waiting.
    """
    due_conditions = tier_due_conditions(
        settings, now=now, age_floor=age_floor, age_ceiling=age_ceiling
    )
    payment = db.execute(
        select(Payment)
        .where(*due_conditions)
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


def _claim_active_tier(
    db: Session, settings: Settings, *, worker_id: str, now: datetime
) -> Payment | None:
    """The <fast-window (default 15 min) tier: highest reconciliation
    priority, the payment link is still payable."""
    return _claim_in_age_range(
        db,
        settings,
        worker_id=worker_id,
        now=now,
        age_floor=timedelta(seconds=settings.reconciliation_min_age_seconds),
        age_ceiling=timedelta(seconds=settings.reconciliation_fast_window_seconds),
    )


def _claim_expiring_tier(
    db: Session, settings: Settings, *, worker_id: str, now: datetime
) -> Payment | None:
    """The fast-window-to-max-age (default 15 min-2 h) safety tier: link has
    expired, but the payer may have paid near expiry or the gateway/callback
    may be lagging."""
    return _claim_in_age_range(
        db,
        settings,
        worker_id=worker_id,
        now=now,
        age_floor=timedelta(seconds=settings.reconciliation_fast_window_seconds),
        age_ceiling=timedelta(seconds=settings.reconciliation_max_age_seconds),
    )


def _claim_next_due(
    db: Session,
    settings: Settings,
    *,
    worker_id: str,
    now: datetime,
    slot_index: int,
) -> Payment | None:
    """Claim ONE due payment for this pass, using reserved-quota-with-
    spillover fairness between the two age tiers.

    Slot 0 of every pass (by processing order) tries the ACTIVE tier first;
    the next ``reconciliation_slow_tier_reserved_slots`` slot(s) — indices 1
    through ``reconciliation_slow_tier_reserved_slots`` inclusive — try the
    EXPIRING tier first; every remaining slot tries the ACTIVE tier first
    again. Either way, a slot whose preferred tier has nothing due
    immediately falls back to the other tier before giving up.

    Slots 0 through ``reconciliation_slow_tier_reserved_slots`` form the
    pass's MANDATORY fairness prefix (see ``run_reconciliation_pass``, which
    lets this prefix run even once the wall-clock budget is exhausted). Both
    tiers therefore get a real opportunity every pass that has due rows,
    regardless of how slow gateway verify calls are:
    * slot 0 guarantees the ACTIVE tier is tried BEFORE any verify call in
      the pass has consumed any budget, so a slow EXPIRING verify can never
      push active-tier payments — the ones still payable — out of a pass;
    * the following reserved slot(s) guarantee the EXPIRING tier is tried
      immediately after, before a slow ACTIVE verify can exhaust the budget
      first.
    Everything past the mandatory prefix stays budget-gated and strongly
    prefers the active tier, with capacity still spilling freely in both
    directions whenever a tier is empty.
    """
    prefer_expiring = 1 <= slot_index <= settings.reconciliation_slow_tier_reserved_slots
    if prefer_expiring:
        return _claim_expiring_tier(
            db, settings, worker_id=worker_id, now=now
        ) or _claim_active_tier(db, settings, worker_id=worker_id, now=now)
    return _claim_active_tier(
        db, settings, worker_id=worker_id, now=now
    ) or _claim_expiring_tier(db, settings, worker_id=worker_id, now=now)


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

    A per-payment failure never terminates the pass. Past the mandatory
    fairness prefix (below), the wall-clock budget bounds the pass LENGTH by
    refusing to START another claim once exceeded; it cannot interrupt an
    in-flight verify call, so a pass may overrun by up to one gateway
    timeout (or, during the mandatory prefix, by up to
    ``1 + reconciliation_slow_tier_reserved_slots`` gateway timeouts — see
    below). Bot-notification latency does not depend on this budget at all:
    the worker runs reconciliation in a DEDICATED THREAD (see app/worker.py),
    never inline in the notification loop.

    Fairness: slot 0 of every pass prefers the ACTIVE tier and the next
    ``reconciliation_slow_tier_reserved_slots`` slot(s) prefer the EXPIRING
    tier (see ``_claim_next_due``), each falling back to the other tier when
    its preference has nothing due — so a historical backlog can never delay
    fresh payments and sustained fresh traffic can never starve the expiring
    tier. These ``1 + reconciliation_slow_tier_reserved_slots`` slots are the
    pass's MANDATORY prefix and are allowed to run even once the wall-clock
    budget is exhausted, because the budget is only ever checked before
    STARTING a new claim, never mid-verify: without that carve-out, a single
    slow verify call in an early mandatory slot could exhaust the budget and
    silently skip a later one, defeating the guarantee for whichever tier
    lost the race. Only once the mandatory prefix is complete does the normal
    budget apply to further claims. Total claims this pass never exceed
    ``limit`` either way.

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
    # The mandatory fairness prefix (1 active-first slot + the expiring-first
    # reserved slots) always fits inside `limit`: reconciliation_slow_tier_
    # reserved_slots is validated to stay below reconciliation_batch_size, but
    # `limit` may be a smaller ad-hoc override (e.g. in tests), so clamp.
    mandatory_slots = min(1 + settings.reconciliation_slow_tier_reserved_slots, limit)
    started = time.monotonic()

    while stats["processed"] < limit and (
        stats["processed"] < mandatory_slots or (time.monotonic() - started) < budget
    ):
        token = request_id_var.set(f"rec-{uuid.uuid4().hex[:16]}")
        try:
            now = now_fn()
            payment = _claim_next_due(
                db, settings, worker_id=worker_id, now=now, slot_index=stats["processed"]
            )
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
