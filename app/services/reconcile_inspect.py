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
HTTP call (read-only on the gateway side too, per app.centralpay) and
reports what settlement WOULD conclude; it never applies that outcome.

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
"""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
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


def _condition_holds(db: Session, payment_id: int, conditions: tuple[Any, ...]) -> bool:
    """Does this ONE payment currently satisfy a shared condition tuple?

    Filters the exact, unmodified condition tuple down to a single row —
    never re-expresses the boundary math — so the answer can never drift
    from what the real claim query (or the read-only snapshot builders)
    would compute for the same row.
    """
    return (
        db.execute(
            select(Payment.id).where(Payment.id == payment_id, *conditions)
        ).scalar_one_or_none()
        is not None
    )


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
    reconciliation predicates. Never mutated; never derived from a gateway
    call."""

    now: datetime
    link_age_seconds: float
    is_link_created_unverified: bool
    # "active" | "expiring" | "aged_out" | None (not a link_created,
    # unverified payment -- reconciliation age tiers do not apply to it).
    age_bucket: str | None
    active_tier_due: bool
    expiring_tier_due: bool
    auto_reconciliation_due: bool
    attempts_exhausted: bool
    # Broader safety-gate flag used by --verify: True for ANY unverified
    # payment (any status) whose link is at least reconciliation_max_age_
    # seconds old, not only link_created rows -- see
    # _verify_aged_out_conditions.
    verify_aged_out: bool


def build_local_snapshot(
    db: Session, settings: Settings, payment: Payment, *, now: datetime
) -> LocalSnapshot:
    anchor = payment.callback_token_issued_at or payment.created_at
    link_age_seconds = (now - _as_utc(anchor)).total_seconds()
    is_link_created_unverified = (
        payment.status == PaymentStatus.LINK_CREATED.value and payment.gateway_verified_at is None
    )
    verify_aged_out = _condition_holds(
        db, payment.id, _verify_aged_out_conditions(settings, now=now)
    )

    active_tier_due = False
    expiring_tier_due = False
    attempts_exhausted = False
    age_bucket: str | None = None
    if is_link_created_unverified:
        active_tier_due = _condition_holds(
            db, payment.id, active_tier_due_conditions(settings, now=now)
        )
        expiring_tier_due = _condition_holds(
            db, payment.id, expiring_tier_due_conditions(settings, now=now)
        )
        attempts_exhausted = _condition_holds(
            db, payment.id, reconciliation_exhausted_conditions(settings, now=now)
        )
        if _condition_holds(db, payment.id, aged_out_conditions(settings, now=now)):
            age_bucket = "aged_out"
        elif _condition_holds(db, payment.id, active_tier_age_conditions(settings, now=now)):
            age_bucket = "active"
        elif _condition_holds(db, payment.id, expiring_tier_age_conditions(settings, now=now)):
            age_bucket = "expiring"

    return LocalSnapshot(
        now=now,
        link_age_seconds=link_age_seconds,
        is_link_created_unverified=is_link_created_unverified,
        age_bucket=age_bucket,
        active_tier_due=active_tier_due,
        expiring_tier_due=expiring_tier_due,
        auto_reconciliation_due=active_tier_due or expiring_tier_due,
        attempts_exhausted=attempts_exhausted,
        verify_aged_out=verify_aged_out,
    )


class VerifyRefusal(enum.StrEnum):
    """Why ``--verify`` declined to contact the gateway at all."""

    ALREADY_VERIFIED = "already_gateway_verified"
    MANUAL_REVIEW_OWNED = "manual_review_owned"
    AGED_OUT = "aged_out"


def determine_verify_refusal(
    payment: Payment, local: LocalSnapshot, *, confirm_aged_out: bool
) -> VerifyRefusal | None:
    """Should ``--verify`` refuse to call the gateway for this payment?

    Order matters. ``manual_review`` is checked FIRST, before
    ``gateway_verified_at``: a gateway-verified payment can legitimately
    still be sitting in manual_review (e.g. a delivery-failure review that
    never touched the financial/verification facts), and in that case the
    operationally important fact is that an administrator already owns the
    review -- not that it happens to also be gateway-verified. Either way
    the command makes ZERO gateway calls. Aged-out is checked last: only a
    payment that is neither already-verified nor manual_review needs
    --confirm-aged-out, and neither of the first two reasons has (or
    needs) a confirmation override.
    """
    if payment.status == PaymentStatus.MANUAL_REVIEW.value:
        return VerifyRefusal.MANUAL_REVIEW_OWNED
    if payment.gateway_verified_at is not None:
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
