"""Operator-only, single-payment recovery for a ``link_created`` payment
whose link has aged out of automatic reconciliation.

Background: the reconciliation worker (``app.services.reconciliation``)
deliberately EXCLUDES ``link_created`` payments once their link age reaches
``settings.reconciliation_max_age_seconds`` -- they are never deleted or
mutated, just left for operator inspection (see
``app.services.reconciliation.aged_out_conditions``). This module is the
narrowly-scoped, explicit escape hatch for recovering exactly ONE such
payment, without re-enabling automatic polling for it and without inventing
any financial logic of its own.

CRITICAL INVARIANT: every financial fact (gateway success, referenceId
validity/uniqueness, payable-amount match, gateway_user_id match, mismatch ->
manual_review, atomic notification queueing) is decided by
:func:`app.services.verification.verify_and_settle` -- the SAME single
settlement path the browser callback and the reconciliation worker already
share. This module never duplicates any of that logic: it only decides
WHETHER to call ``verify_and_settle`` at all, under the same row-lock
discipline ``app.cli``'s ``reconcile --verify`` already uses (see
``app.services.reconcile_inspect``), and records a few fixed, sanitized
audit markers around that single call.

Eligibility (all must hold, re-checked fresh under the row lock at
--confirm time, never trusted from an earlier non-locking read):

* ``status == link_created``
* ``gateway_verified_at IS NULL``
* ``status`` is not any member of
  ``app.services.verification.VERIFIED_STATUSES``
* ``status != manual_review``
* link age >= ``settings.reconciliation_max_age_seconds``

This is exactly :func:`app.services.reconciliation.aged_out_conditions`
(the reconciliation worker's own aged-out tier) -- never a locally
re-derived age cutoff or status set. Reusing it also means this module's
notion of "eligible" can never silently disagree with what the worker
itself would have selected, had the payment not aged out.

--- Duplicate-downstream safety (no bot resend here; see the PR that added
this module) ---

Before settling, could this payment already have reached the customer bot
despite being locally unverified? Audited against the actual schema and
every status-mutating code path in this codebase (not guessed):

1. ``payments.gateway_verified_at`` is assigned in exactly ONE place in the
   entire codebase: ``app.services.verification._validate_and_apply_
   verification`` (the success branch of the single settlement path). It is
   never cleared/reset back to NULL anywhere.
2. ``payments.status`` is set to ``link_created`` in exactly ONE place:
   ``app.services.payments`` on a successful ``getLink`` call, i.e. payment
   CREATION. No code path ever moves a payment BACK to ``link_created``
   once it has left that status.
3. Every status transition that reaches ``bot_notify_pending`` or
   ``bot_notify_accepted`` -- ``app.services.notification.queue_
   notification`` (called only from inside
   ``_validate_and_apply_verification``, atomically with setting
   ``gateway_verified_at``), ``app.services.notification.record_attempt_
   result``'s ACCEPTED branch, the administrator ``review resend`` path
   (``app.ops``), and the bulk-resend worker path
   (``app.services.bulk_resend``) -- either runs strictly after
   ``gateway_verified_at`` was already set, or (the two resend paths)
   explicitly REQUIRES ``gateway_verified_at IS NOT NULL`` before touching
   status at all. The database also enforces this independently: the
   ``ck_payments_delivery_requires_verification`` CHECK constraint
   (migration 0005) forbids ``status IN ('bot_notify_pending',
   'bot_notify_accepted')`` while ``gateway_verified_at IS NULL``.

Together, (1)-(3) prove that a payment with ``status == link_created`` AND
``gateway_verified_at IS NULL`` -- exactly this module's eligibility gate --
has NEVER, through any application code path, been queued for or delivered
to the customer bot. There is therefore no separate "was this already sent
to the bot?" check to add here beyond the eligibility gate itself; adding
one would be inventing a check against a state this codebase's own
invariants prove unreachable. See
``tests/test_aged_out_recovery.py::test_duplicate_downstream_safety_invariant_*``
for the regression tests that pin (1) and (2), and
``tests/integration/test_aged_out_recovery_pg.py`` for a real-PostgreSQL
proof that (3)'s CHECK constraint actually rejects the anomalous row this
whole argument depends on being impossible.

This module makes NO gateway HTTP request and NO persistent database write
of its own beyond the audit events explicitly listed below and whatever
``verify_and_settle`` itself does under its own documented contract.
"""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.centralpay import CentralPayClient
from app.config import Settings
from app.exceptions import CentralPayConnectionError, CentralPayError
from app.models import Payment, PaymentStatus
from app.services.reconcile_inspect import LocalSnapshot, build_local_snapshot
from app.services.verification import SettlementOutcome, verify_and_settle

# Passed as verify_and_settle's `source` -- routes gateway_not_paid to the
# same non-alarming, informational event path reconciliation polling uses
# (`centralpay_verify_not_paid`, with this value embedded as `data.source`),
# rather than the callback path's warning-level anomaly event. A "not paid
# yet" answer for an aged-out payment an operator is deliberately checking
# is an expected possible outcome, not a bug.
RECOVERY_SOURCE = "aged_out_recovery"


class RecoveryRefusal(enum.StrEnum):
    """Why a recovery attempt refuses, checked in this fixed order (an
    administrator-owned review always outranks every other reason; an
    already-settled payment is reported precisely, not lumped in with
    "wrong status")."""

    MANUAL_REVIEW_OWNED = "manual_review_owned"
    ALREADY_VERIFIED = "already_gateway_verified"
    NOT_LINK_CREATED = "not_link_created"
    NOT_AGED_OUT = "not_aged_out"


def determine_recovery_refusal(payment: Payment, local: LocalSnapshot) -> RecoveryRefusal | None:
    """``None`` means eligible. Reuses ``local.is_gateway_verified`` (status
    in ``VERIFIED_STATUSES`` OR ``gateway_verified_at`` set -- computed once
    in :func:`app.services.reconcile_inspect.build_local_snapshot`) and
    ``local.age_bucket`` (computed from the exact same
    ``app.services.reconciliation.aged_out_conditions`` the worker itself
    selects on) rather than re-deriving either condition."""
    if payment.status == PaymentStatus.MANUAL_REVIEW.value:
        return RecoveryRefusal.MANUAL_REVIEW_OWNED
    if local.is_gateway_verified:
        return RecoveryRefusal.ALREADY_VERIFIED
    if payment.status != PaymentStatus.LINK_CREATED.value:
        return RecoveryRefusal.NOT_LINK_CREATED
    if local.age_bucket != "aged_out":
        return RecoveryRefusal.NOT_AGED_OUT
    return None


@dataclass(frozen=True)
class RecoverySnapshot:
    """Safe operational facts captured under the row lock, BEFORE any
    settlement attempt -- never the raw ORM ``Payment``, which
    ``verify_and_settle`` may mutate in place after this is captured (a
    caller rendering a report from a stale copy of this snapshot after a
    settlement would otherwise silently mix a pre-attempt status with a
    post-attempt one). Carries nothing secret."""

    bot_order_id: str
    gateway_order_id: int
    status: str
    gateway_verified: bool
    link_age_seconds: float
    aged_out: bool
    reconciliation_attempts: int


def _snapshot(payment: Payment, local: LocalSnapshot) -> RecoverySnapshot:
    return RecoverySnapshot(
        bot_order_id=payment.bot_order_id,
        gateway_order_id=payment.gateway_order_id,
        status=payment.status,
        gateway_verified=local.is_gateway_verified,
        link_age_seconds=local.link_age_seconds,
        aged_out=local.age_bucket == "aged_out",
        reconciliation_attempts=payment.reconciliation_attempts,
    )


def build_preview(
    db: Session, settings: Settings, payment_id: int, *, now: datetime
) -> tuple[RecoverySnapshot, RecoveryRefusal | None] | None:
    """Read-only preview: one non-locking, consistent read. Never takes a
    row lock, never writes, never records a ``PaymentEvent``. Returns
    ``None`` if no payment with this id exists."""
    snapshot = build_local_snapshot(db, settings, payment_id, now=now, for_update=False)
    if snapshot is None:
        return None
    payment, local = snapshot
    return _snapshot(payment, local), determine_recovery_refusal(payment, local)


class RecoveryOutcomeKind(enum.StrEnum):
    REFUSED = "refused"
    VERIFIED = "verified"
    GATEWAY_NOT_PAID = "gateway_not_paid"
    MANUAL_REVIEW = "manual_review"
    TRANSPORT_FAILED = "transport_failed"


@dataclass(frozen=True)
class RecoveryOutcome:
    kind: RecoveryOutcomeKind
    # Whether an HTTP request actually reached CentralPay -- mirrors
    # `reconcile --verify`'s identical semantics (see app.cli._cmd_reconcile).
    # False ONLY for REFUSED: eligibility failed before any gateway request
    # was even attempted. True for VERIFIED / GATEWAY_NOT_PAID /
    # MANUAL_REVIEW (a real, parsed gateway response) and for a
    # TRANSPORT_FAILED caused by a non-connection CentralPayError (a
    # non-200 status or an unparseable body PROVES the request was
    # transmitted and answered). None ONLY for a TRANSPORT_FAILED caused by
    # CentralPayConnectionError, where httpx cannot distinguish "never left
    # this process" from "sent, but the response was lost".
    gateway_request_performed: bool | None
    # True ONLY for the CentralPayConnectionError case above.
    delivery_uncertain: bool
    refusal: RecoveryRefusal | None = None
    transport_error_code: str | None = None


def execute_confirmed_recovery(
    db: Session,
    client: CentralPayClient,
    *,
    payment_id: int,
    settings: Settings,
) -> tuple[RecoverySnapshot, RecoveryOutcome] | None:
    """The ONLY mutating entry point in this module. Returns ``None`` if no
    payment with this id exists (defensive only -- payments are never
    deleted).

    Contract:

    1. Acquires ``SELECT ... FOR UPDATE`` on the payment row FIRST.
    2. Captures ``now`` and reloads the row's full eligibility snapshot only
       AFTER the lock is actually held (never before any wait behind a
       concurrent transaction) -- the exact discipline
       ``app.cli``'s ``reconcile --verify`` already uses (see
       ``app.services.reconcile_inspect``), closing races with the browser
       callback, the reconciliation worker, and a second concurrent
       recovery attempt.
    3. Re-evaluates eligibility under that fresh read. If no longer
       eligible, refuses with ZERO gateway requests.
    4. If still eligible, calls ``verify_and_settle`` EXACTLY ONCE, with
       the row lock held across the call (the same guarantee the callback
       and reconciliation paths already rely on) -- this module never
       calls ``CentralPayClient.verify`` directly, never re-implements any
       financial check, and never assigns a financial ``Payment`` field
       itself.

    The row lock is released the moment ``verify_and_settle`` (or this
    function's own refusal path) commits; the audit marker recorded after a
    settlement attempt runs in a fresh, lock-free transaction, since by
    then the financial outcome is already durably committed.
    """
    locked_id = db.execute(
        select(Payment.id).where(Payment.id == payment_id).with_for_update()
    ).scalar_one_or_none()
    if locked_id is None:
        return None

    # Captured AFTER the lock above is actually acquired -- never before any
    # wait behind a concurrent transaction holding the same row.
    now = datetime.now(UTC)
    reloaded = build_local_snapshot(db, settings, payment_id, now=now, for_update=True)
    if reloaded is None:
        return None
    payment, local = reloaded
    snapshot = _snapshot(payment, local)

    record_event(
        db,
        payment_id=payment.id,
        event_type="aged_out_recovery_requested",
        data={"gateway_order_id": payment.gateway_order_id},
    )

    refusal = determine_recovery_refusal(payment, local)
    if refusal is not None:
        record_event(
            db,
            payment_id=payment.id,
            event_type="aged_out_recovery_refused",
            level="warning",
            data={"gateway_order_id": payment.gateway_order_id, "reason": refusal.value},
        )
        db.commit()
        return snapshot, RecoveryOutcome(
            RecoveryOutcomeKind.REFUSED,
            gateway_request_performed=False,
            delivery_uncertain=False,
            refusal=refusal,
        )

    try:
        settled = verify_and_settle(db, client, payment, settings=settings, source=RECOVERY_SOURCE)
    except CentralPayError as exc:
        # A connection-level failure cannot be told apart from "sent, then
        # the response never arrived" -- httpx gives no way to know
        # whether bytes reached the gateway. Any OTHER CentralPayError (a
        # non-200 status or an unparseable body) PROVES the request was
        # transmitted and answered -- never "uncertain". Same distinction
        # `reconcile --verify` already makes (app.cli._cmd_reconcile).
        if isinstance(exc, CentralPayConnectionError):
            performed, delivery_uncertain = None, True
        else:
            performed, delivery_uncertain = True, False
        # verify_and_settle already recorded centralpay_verify_failed and
        # committed before raising -- this marker is a fresh, separate,
        # lock-free transaction purely to tag the attempt as ours.
        record_event(
            db,
            payment_id=payment.id,
            event_type="aged_out_recovery_transport_failed",
            level="error",
            data={"gateway_order_id": payment.gateway_order_id, "error_code": exc.code},
        )
        db.commit()
        return snapshot, RecoveryOutcome(
            RecoveryOutcomeKind.TRANSPORT_FAILED,
            gateway_request_performed=performed,
            delivery_uncertain=delivery_uncertain,
            transport_error_code=exc.code,
        )

    if settled is SettlementOutcome.VERIFIED:
        kind, event_type = RecoveryOutcomeKind.VERIFIED, "aged_out_recovery_verified"
    elif settled is SettlementOutcome.UNDER_REVIEW:
        kind, event_type = RecoveryOutcomeKind.MANUAL_REVIEW, "aged_out_recovery_manual_review"
    else:
        kind, event_type = RecoveryOutcomeKind.GATEWAY_NOT_PAID, "aged_out_recovery_not_paid"
    record_event(
        db,
        payment_id=payment.id,
        event_type=event_type,
        data={"gateway_order_id": payment.gateway_order_id},
    )
    db.commit()
    return snapshot, RecoveryOutcome(kind, gateway_request_performed=True, delivery_uncertain=False)
