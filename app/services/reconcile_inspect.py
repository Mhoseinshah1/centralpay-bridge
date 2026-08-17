"""Strictly read-only single-payment reconciliation inspection.

Backs ``centralpay reconcile ORDER_ID [--verify] [--confirm-aged-out]``
(see ``app.cli``). This module NEVER writes to the database: no payment
attribute is ever assigned, no ``PaymentEvent`` is ever created, no
notification is ever queued, and ``db.commit()`` is never called anywhere
in this file. It also never invokes any mutating settlement or
callback-processing function from ``app.services.verification`` or
``app.services.reconciliation`` — those are the mutating settlement paths
this module deliberately stays independent of.
``--verify`` is allowed to make exactly one ``CentralPayClient.verify()``
HTTP call and reports what settlement WOULD conclude; it never applies that
outcome. This is diagnostic gateway verification with no LOCAL database
mutation; the gateway's OWN verify-after-verify/idempotency semantics have
never been confirmed against production CentralPay (release blocker B2 --
see STAGING_VALIDATION.md) and must not be assumed read-only or safe to
call repeatedly just because this module makes no local change. For that
reason the call is gated behind ``settings.centralpay_diagnostic_verify_
enabled`` (default ``False``, checked in ``app.cli._cmd_reconcile`` before
any lock or HTTP request) -- enable only after that staging validation
closes B2.

Age-boundary predicates are never hand-rederived: every tier/aged-out/
exhausted check below queries the exact shared condition tuples imported
from ``app.services.reconciliation`` (``active_tier_age_conditions``,
``expiring_tier_age_conditions``, ``aged_out_age_condition``,
``active_tier_due_conditions``, ``expiring_tier_due_conditions``,
``reconciliation_exhausted_conditions``), scoped down to this one
payment's id — so this view can never quietly disagree with what the
reconciliation worker itself would do, the same reuse pattern
``app.services.reconciliation_status`` and ``app.services.stuck_payments``
already follow. This module defines NO local age-boundary math of its
own beyond the ``--verify`` safety gate's deliberately broader status
scope (see ``_verify_aged_out_conditions`` below) — even that reuses the
shared ``aged_out_age_condition`` expression, never a separately
computed cutoff.

Consistency: :func:`build_local_snapshot` issues exactly ONE structured
``SELECT`` per call, returning the ``Payment`` row together with every
tier/due/exhausted/aged-out boolean computed from that SAME row read. At
PostgreSQL READ COMMITTED isolation (the project's isolation level; see
AGENTS.md) a single statement is evaluated against one consistent
snapshot, so this never combines a ``Payment`` field read by one query
with a classification flag computed by a later, separately-timed query —
the failure mode a concurrent worker/callback UPDATE between several
separate SELECTs could otherwise produce. ``for_update=True`` takes the
exact ``SELECT ... FOR UPDATE`` row-lock discipline the mutating settlement
path in ``app.services.verification`` requires its caller to hold — used
ONLY by ``--verify`` (see ``app.cli._cmd_reconcile``), which
must reload the row and its eligibility flags under that lock, check
refusal AFTER the lock is held, and hold the lock across its own
diagnostic gateway call, so a concurrent settlement can never land between
this module's eligibility check and the gateway query. The caller must
also capture ``now`` AFTER the row lock is actually acquired (not before
any wait behind a concurrent transaction) and pass that same fresh
timestamp into this call, so the aged-out gate reflects time at lock
acquisition, not time at lock request. Default
(non-``--verify``) inspection always calls this with ``for_update=False``:
no lock is ever taken.
"""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.centralpay import VerifyResult
from app.config import Settings
from app.models import Payment, PaymentStatus
from app.services.reconciliation import (
    active_tier_age_conditions,
    active_tier_due_conditions,
    aged_out_age_condition,
    aged_out_conditions,
    expiring_tier_age_conditions,
    expiring_tier_due_conditions,
    reconciliation_exhausted_conditions,
)
from app.services.verification import VERIFIED_STATUSES

# Fixed diagnostic-only reason vocabulary. Deliberately distinct from the
# real audit event type strings app.services.verification writes
# (verify_missing_reference_id, verify_payable_amount_mismatch, ...) so a
# grep/search for the real audit trail can never accidentally match this
# module's PREDICTED-only output.
REASON_MISSING_REFERENCE_ID = "missing_reference_id"
REASON_INVALID_REFERENCE_ID = "invalid_reference_id"
REASON_PAYABLE_AMOUNT_MISMATCH = "payable_amount_mismatch"
REASON_USER_ID_MISMATCH = "user_id_mismatch"
REASON_REFERENCE_ID_COLLISION = "reference_id_collision"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _verify_aged_out_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """Broader than ``app.services.reconciliation.aged_out_conditions``:
    covers EVERY unverified payment regardless of status (not only
    ``link_created`` rows) — the ``--verify`` safety gate must refuse for
    ANY old, unverified payment, since a stale ``created``/``getlink_failed``
    anomaly should not be able to dodge the aged-out refusal just by never
    having reached ``link_created``. Reuses the EXACT same age-boundary
    expression (``aged_out_age_condition``) the link_created-scoped
    ``aged_out_conditions`` uses — only the ``status`` scope differs, never
    a separately computed cutoff.
    """
    return (
        Payment.gateway_verified_at.is_(None),
        aged_out_age_condition(settings, now=now),
    )


@dataclass(frozen=True)
class LocalSnapshot:
    """Read-only facts about one payment, computed against the shared
    reconciliation predicates from ONE consistent database read (see
    :func:`build_local_snapshot`). Never mutated; never derived from a
    gateway call."""

    now: datetime
    link_age_seconds: float
    is_link_created_unverified: bool
    # "active" | "expiring" | "aged_out" | None (not a link_created,
    # unverified payment -- reconciliation age tiers do not apply to it).
    age_bucket: str | None
    active_tier_due: bool
    expiring_tier_due: bool
    # schedule_due: whether the age/next_at/attempts predicates ALONE say
    # this payment is due for reconciliation -- independent of whether the
    # reconciliation worker is administratively enabled right now.
    schedule_due: bool
    # reconciliation_enabled: settings.reconciliation_enabled at the moment
    # this snapshot was built. Exposed so operators can see WHY
    # auto_reconciliation_due is false when the worker is disabled, rather
    # than that looking identical to "not due yet".
    reconciliation_enabled: bool
    # auto_reconciliation_due: whether automatic reconciliation can
    # ACTUALLY happen right now -- schedule_due AND the worker being
    # enabled. Never true while reconciliation_enabled is false, even if
    # schedule_due is true.
    auto_reconciliation_due: bool
    attempts_exhausted: bool
    # Broader safety-gate flag used by --verify: True for ANY unverified
    # payment (any status) whose link is at least reconciliation_max_age_
    # seconds old, not only link_created rows -- see
    # _verify_aged_out_conditions.
    verify_aged_out: bool
    # Whether local state denotes successful gateway verification: status in
    # VERIFIED_STATUSES OR gateway_verified_at is set -- the SAME rule
    # determine_verify_refusal uses for ALREADY_VERIFIED, computed once here
    # so a report can never say "gateway_verified: no" for a payment this
    # module is simultaneously refusing to re-verify as already verified.
    # Never re-derived from gateway_verified_at alone anywhere downstream.
    is_gateway_verified: bool


def build_local_snapshot(
    db: Session, settings: Settings, payment_id: int, *, now: datetime, for_update: bool = False
) -> tuple[Payment, LocalSnapshot] | None:
    """Read exactly one payment and its full classification in ONE
    structured ``SELECT`` -- see the module docstring for why this matters.

    Returns ``(payment, snapshot)``, or ``None`` if no payment with this id
    exists. ``for_update=True`` takes a ``SELECT ... FOR UPDATE`` row lock
    (see the module docstring); the caller is responsible for never
    mutating or committing through it.
    """
    active_age_expr = and_(*active_tier_age_conditions(settings, now=now))
    expiring_age_expr = and_(*expiring_tier_age_conditions(settings, now=now))
    aged_out_expr = and_(*aged_out_conditions(settings, now=now))
    active_due_expr = and_(*active_tier_due_conditions(settings, now=now))
    expiring_due_expr = and_(*expiring_tier_due_conditions(settings, now=now))
    exhausted_expr = and_(*reconciliation_exhausted_conditions(settings, now=now))
    verify_aged_out_expr = and_(*_verify_aged_out_conditions(settings, now=now))

    stmt = (
        select(
            Payment,
            active_age_expr.label("active_age"),
            expiring_age_expr.label("expiring_age"),
            aged_out_expr.label("aged_out"),
            active_due_expr.label("active_due"),
            expiring_due_expr.label("expiring_due"),
            exhausted_expr.label("exhausted"),
            verify_aged_out_expr.label("verify_aged_out"),
        )
        .where(Payment.id == payment_id)
        # Without this, a Payment already present in this SAME session's
        # identity map (e.g. from an earlier, non-locking lookup such as
        # app.cli._find_payment) would have its ORM attributes silently left
        # UNREFRESHED by this query's result -- even though the query itself
        # reads current data -- defeating both the --verify row-lock reload
        # and the single-consistent-read guarantee this function exists to
        # provide.
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()

    row = db.execute(stmt).one_or_none()
    if row is None:
        return None
    payment = row[0]

    anchor = payment.callback_token_issued_at or payment.created_at
    link_age_seconds = (now - _as_utc(anchor)).total_seconds()
    is_link_created_unverified = (
        payment.status == PaymentStatus.LINK_CREATED.value and payment.gateway_verified_at is None
    )

    # Every boolean below already embeds the same status/gateway_verified_at
    # scope inside its shared condition tuple, so it is naturally False for
    # a payment that is not a link_created/unverified row -- never guarded
    # again here, which would risk drifting from the shared predicates.
    active_tier_due = bool(row.active_due)
    expiring_tier_due = bool(row.expiring_due)
    attempts_exhausted = bool(row.exhausted)
    age_bucket: str | None = None
    if row.aged_out:
        age_bucket = "aged_out"
    elif row.active_age:
        age_bucket = "active"
    elif row.expiring_age:
        age_bucket = "expiring"

    schedule_due = active_tier_due or expiring_tier_due
    local = LocalSnapshot(
        now=now,
        link_age_seconds=link_age_seconds,
        is_link_created_unverified=is_link_created_unverified,
        age_bucket=age_bucket,
        active_tier_due=active_tier_due,
        expiring_tier_due=expiring_tier_due,
        schedule_due=schedule_due,
        reconciliation_enabled=settings.reconciliation_enabled,
        auto_reconciliation_due=settings.reconciliation_enabled and schedule_due,
        attempts_exhausted=attempts_exhausted,
        verify_aged_out=bool(row.verify_aged_out),
        is_gateway_verified=(
            payment.status in VERIFIED_STATUSES or payment.gateway_verified_at is not None
        ),
    )
    return payment, local


class VerifyRefusal(enum.StrEnum):
    """Why ``--verify`` declined to contact the gateway at all."""

    # Configuration gate, not a payment-state fact: checked in app.cli
    # BEFORE any row lock or payment-state check below, and before any
    # network call. See settings.centralpay_diagnostic_verify_enabled.
    DIAGNOSTIC_VERIFY_DISABLED = "diagnostic_verify_not_enabled"
    ALREADY_VERIFIED = "already_gateway_verified"
    MANUAL_REVIEW_OWNED = "manual_review_owned"
    AGED_OUT = "aged_out"


def determine_verify_refusal(
    payment: Payment, local: LocalSnapshot, *, confirm_aged_out: bool
) -> VerifyRefusal | None:
    """Should ``--verify`` refuse to call the gateway for this payment?

    Order matters. ``manual_review`` is checked FIRST, before the
    already-verified check: a gateway-verified payment can legitimately
    still be sitting in manual_review (e.g. a delivery-failure review that
    never touched the financial/verification facts), and in that case the
    operationally important fact is that an administrator already owns the
    review -- not that it happens to also be gateway-verified. Either way
    the command makes ZERO gateway calls.

    The already-verified check uses ``local.is_gateway_verified``, computed
    once in :func:`build_local_snapshot` from ``VERIFIED_STATUSES`` (from
    ``app.services.verification`` -- the exact same statuses the callback
    route's settlement handler treats as "verification already happened, do
    not re-verify") OR ``gateway_verified_at is not None``. Reusing that
    single computed field (rather than re-deriving the same condition here)
    guarantees this refusal decision can never disagree with what a report
    built from the SAME snapshot shows as "gateway verified". Only a
    database CHECK constraint ties ``gateway_verified_at`` to the two
    bot-notify statuses today; ``gateway_verified`` carries no such
    constraint, so ``gateway_verified_at`` alone is not a reliable proxy for
    every status in ``VERIFIED_STATUSES``.

    Aged-out is checked last: only a payment that is neither
    already-verified nor manual_review needs --confirm-aged-out, and
    neither of the first two reasons has (or needs) a confirmation
    override.
    """
    if payment.status == PaymentStatus.MANUAL_REVIEW.value:
        return VerifyRefusal.MANUAL_REVIEW_OWNED
    if local.is_gateway_verified:
        return VerifyRefusal.ALREADY_VERIFIED
    if local.verify_aged_out and not confirm_aged_out:
        return VerifyRefusal.AGED_OUT
    return None


class VerifyAssessment(enum.StrEnum):
    """Diagnostic-only prediction of what settlement WOULD conclude.
    Never applied -- see the module docstring."""

    WOULD_VERIFY = "WOULD_VERIFY"
    WOULD_REQUIRE_MANUAL_REVIEW = "WOULD_REQUIRE_MANUAL_REVIEW"
    NOT_SUCCESSFUL = "NOT_SUCCESSFUL"


@dataclass(frozen=True)
class VerifyComparison:
    """Read-only comparison between one fresh VerifyResult and the local
    payment snapshot. Building this NEVER writes to the database -- the one
    query it may issue (reference_id collision) is a plain SELECT."""

    gateway_success: bool
    assessment: VerifyAssessment
    reason_code: str | None
    gateway_failure_reason: str | None
    reference_id_present: bool
    reference_id_valid: bool
    reported_reference_id: str | None
    amount_matches: bool | None
    expected_payable_amount: int
    reported_amount: int | None
    user_id_matches: bool | None
    reference_id_collision: bool
    field_errors: tuple[str, ...]


def evaluate_verify_result(db: Session, payment: Payment, result: VerifyResult) -> VerifyComparison:
    """Replicate the READ-ONLY equivalent of the checks
    ``app.services.verification._validate_and_apply_verification`` performs
    -- reference_id validity, payable-amount match, gateway_user_id match,
    reference_id uniqueness -- without ever calling that function and
    without ever writing anything. Never prints/returns the raw gateway
    user id or card number -- neither is even read here."""
    if not result.gateway_success:
        return VerifyComparison(
            gateway_success=False,
            assessment=VerifyAssessment.NOT_SUCCESSFUL,
            reason_code=None,
            gateway_failure_reason=result.failure_reason,
            reference_id_present=False,
            reference_id_valid=False,
            reported_reference_id=None,
            amount_matches=None,
            expected_payable_amount=payment.payable_amount,
            reported_amount=None,
            user_id_matches=None,
            reference_id_collision=False,
            field_errors=result.field_errors,
        )

    if result.reference_id is None:
        reason = (
            REASON_INVALID_REFERENCE_ID
            if result.reference_id_invalid
            else REASON_MISSING_REFERENCE_ID
        )
        return VerifyComparison(
            gateway_success=True,
            assessment=VerifyAssessment.WOULD_REQUIRE_MANUAL_REVIEW,
            reason_code=reason,
            gateway_failure_reason=None,
            reference_id_present=result.reference_id_invalid,
            reference_id_valid=False,
            reported_reference_id=None,
            amount_matches=None,
            expected_payable_amount=payment.payable_amount,
            reported_amount=result.amount,
            user_id_matches=None,
            reference_id_collision=False,
            field_errors=result.field_errors,
        )

    if result.amount != payment.payable_amount:
        return VerifyComparison(
            gateway_success=True,
            assessment=VerifyAssessment.WOULD_REQUIRE_MANUAL_REVIEW,
            reason_code=REASON_PAYABLE_AMOUNT_MISMATCH,
            gateway_failure_reason=None,
            reference_id_present=True,
            reference_id_valid=True,
            reported_reference_id=result.reference_id,
            amount_matches=False,
            expected_payable_amount=payment.payable_amount,
            reported_amount=result.amount,
            user_id_matches=None,
            reference_id_collision=False,
            field_errors=result.field_errors,
        )

    if result.user_id != payment.gateway_user_id:
        return VerifyComparison(
            gateway_success=True,
            assessment=VerifyAssessment.WOULD_REQUIRE_MANUAL_REVIEW,
            reason_code=REASON_USER_ID_MISMATCH,
            gateway_failure_reason=None,
            reference_id_present=True,
            reference_id_valid=True,
            reported_reference_id=result.reference_id,
            amount_matches=True,
            expected_payable_amount=payment.payable_amount,
            reported_amount=result.amount,
            user_id_matches=False,
            reference_id_collision=False,
            field_errors=result.field_errors,
        )

    # Read-only collision check -- the SAME query
    # _validate_and_apply_verification uses, never a write.
    collision = (
        db.execute(
            select(Payment.id)
            .where(Payment.reference_id == result.reference_id, Payment.id != payment.id)
            .limit(1)
        ).first()
        is not None
    )
    if collision:
        return VerifyComparison(
            gateway_success=True,
            assessment=VerifyAssessment.WOULD_REQUIRE_MANUAL_REVIEW,
            reason_code=REASON_REFERENCE_ID_COLLISION,
            gateway_failure_reason=None,
            reference_id_present=True,
            reference_id_valid=True,
            reported_reference_id=result.reference_id,
            amount_matches=True,
            expected_payable_amount=payment.payable_amount,
            reported_amount=result.amount,
            user_id_matches=True,
            reference_id_collision=True,
            field_errors=result.field_errors,
        )

    return VerifyComparison(
        gateway_success=True,
        assessment=VerifyAssessment.WOULD_VERIFY,
        reason_code=None,
        gateway_failure_reason=None,
        reference_id_present=True,
        reference_id_valid=True,
        reported_reference_id=result.reference_id,
        amount_matches=True,
        expected_payable_amount=payment.payable_amount,
        reported_amount=result.amount,
        user_id_matches=True,
        reference_id_collision=False,
        field_errors=result.field_errors,
    )
