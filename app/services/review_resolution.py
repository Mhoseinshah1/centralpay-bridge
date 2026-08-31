"""Explicit-list BULK resolution of open manual reviews (operator CLI only).

Production case that motivated this: 15 unresolved manual reviews, every one
of them ``gateway_verified=true`` with ``bot_notify_reason=retry_limit_reached``
and a last downstream HTTP 500. The operator independently confirmed with the
downstream VPN bot's own records that all 15 orders had already been credited,
so the correct outcome was ``resolution=confirmed_by_bot_operator`` with NO
resend. Doing that required a shell loop over 15 single-item CLI invocations —
15 separate transactions, 15 chances to fat-finger an order id, no preview, and
no way to see the set's financial shape before acting on it.

This module adds the bulk path WITHOUT weakening the single-payment one. The
single-payment ``app.ops review resolve`` command is unchanged and remains the
tool for correcting or re-recording an individual resolution.

Safety contract
---------------
* **Explicit order ids only.** There is no "resolve all", no filter, no
  ``--reason``-driven selection, and no default set. The operator must name
  every payment. A duplicate is rejected rather than silently deduplicated,
  because a duplicate means the operator's list is not what they think it is.
  Duplicates are detected on the RESOLVED PAYMENT, not just on the raw string:
  one payment can be named both by its ``bot_order_id`` and by its numeric
  ``gateway_order_id``, and those two strings differ. Matching on strings
  alone, a two-alias batch would preview as two eligible reviews, lock and
  mutate one row, and then print two success lines above ``resolved 1`` —
  telling the operator the batch covered more payments than it did.
* **Preview first.** :func:`preview_bulk_resolution` performs no lock and no
  write. The CLI runs it and refuses unless the operator re-runs with the
  explicit confirmation flag.
* **Every row passes the SAME check, individually.** :func:`refuse_reason` is
  one pure function; the preview and the execute path both call it, and the
  execute path re-calls it under the row lock.
* **Financial-mismatch sets are rejected.** A resolution code that asserts the
  downstream bot already credited the order (``confirmed_by_bot_operator``,
  ``duplicate_notification_confirmed_safe``) may only be applied to
  gateway-verified payments — you cannot truthfully claim a bot credited an
  order CentralPay never verified. Separately, a set MIXING verified and
  unverified payments is rejected outright at the set level: one shared
  operator justification cannot honestly cover two different financial
  situations.
* **All-or-nothing.** If any row fails its re-check under the lock, the whole
  transaction is rolled back and NOTHING is resolved. There is no partial
  application to reason about and no half-finished batch to reconcile.
* **Audited per row.** Each resolved payment gets its own permanent
  ``manual_review_resolved`` event (the same event type the single-payment
  command records, marked ``bulk``), plus one batch-level
  ``manual_review_bulk_resolved`` event.
* **No network.** No gateway HTTP, no downstream-bot HTTP. This module imports
  no client. Resolution is a local, operational record only.
* **No financial mutation.** The only columns written are
  ``review_acknowledged_at``, ``review_resolved_at``, and
  ``review_resolution``. ``status`` is deliberately left as ``manual_review``
  (permanent history, exactly like the single-payment path), and no amount,
  fee snapshot, verification fact, reference id, or identity field is touched.

Concurrency
-----------
Rows are locked with a single ``SELECT ... WHERE id IN (...) ORDER BY id
FOR UPDATE`` — plain ``FOR UPDATE``, never ``SKIP LOCKED``: a row another
process holds must BLOCK and then be re-checked, not be silently skipped out
of an all-or-nothing batch. Rows are re-read with ``populate_existing=True``
so a stale SQLAlchemy identity-map copy from the preview can never satisfy a
guard the locked row would fail; under READ COMMITTED, PostgreSQL re-evaluates
a locked row against its newest committed version once the lock is granted, so
the re-check sees whatever a racing operator just did.

The ascending-``id`` ordering is intended to give concurrent invocations a
consistent lock-acquisition order. It is NOT claimed as a deadlock proof:
PostgreSQL takes row locks during the scan, and while an ``id IN (...) ORDER
BY id`` query normally plans as a primary-key index scan in that order, the
plan is not part of the contract. This is safe because a deadlock here fails
CLOSED — PostgreSQL aborts one transaction, the batch's single commit never
happens, and nothing is resolved, which is exactly the all-or-nothing outcome
an ineligible row already produces. The operator re-runs. Combined with
``MAX_BULK_SIZE`` and the fact that this is a rare, human-initiated command,
that is the right trade against a more complex locking protocol.
"""

import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.models import Payment, PaymentStatus
from app.services.payment_lookup import AmbiguousOrderIdError, find_payment_by_order_id

logger = logging.getLogger("app.services.review_resolution")

NOTE_MAX_LENGTH = 500
ACTOR_MAX_LENGTH = 128

# Hard cap on one batch. Large enough for the real operational case (15) with
# generous headroom, small enough that an operator cannot lock a big slice of
# the table in one transaction by pasting a runaway list.
MAX_BULK_SIZE = 100

# Resolution codes that ASSERT the downstream bot already processed the order.
# Applying one to a payment CentralPay never verified would record a claim the
# operator cannot possibly have verified, so it is refused.
RESOLUTIONS_REQUIRING_GATEWAY_VERIFIED: frozenset[str] = frozenset(
    {"confirmed_by_bot_operator", "duplicate_notification_confirmed_safe"}
)


class BulkReviewRefusal(enum.StrEnum):
    """Exactly why one row (or the whole set) may not be bulk-resolved."""

    NOT_FOUND = "not_found"
    AMBIGUOUS_ORDER_ID = "ambiguous_order_id"
    NOT_MANUAL_REVIEW = "not_manual_review"
    ALREADY_RESOLVED = "already_resolved"
    REQUIRES_GATEWAY_VERIFIED = "requires_gateway_verified"
    # Set-level.
    DUPLICATE_ORDER_ID = "duplicate_order_id"
    MIXED_VERIFICATION_SET = "mixed_verification_set"
    BATCH_TOO_LARGE = "batch_too_large"
    EMPTY_BATCH = "empty_batch"


REFUSAL_MESSAGE: Mapping[BulkReviewRefusal, str] = {
    BulkReviewRefusal.NOT_FOUND: "payment not found",
    BulkReviewRefusal.AMBIGUOUS_ORDER_ID: (
        "ambiguous order id (matches one payment's bot_order_id and a "
        "different payment's gateway_order_id)"
    ),
    BulkReviewRefusal.NOT_MANUAL_REVIEW: "not in manual_review (status={status})",
    BulkReviewRefusal.ALREADY_RESOLVED: (
        "already resolved ({resolution} at {resolved_at}); use the "
        "single-payment `review resolve` to re-record one deliberately"
    ),
    BulkReviewRefusal.REQUIRES_GATEWAY_VERIFIED: (
        "resolution asserts the downstream bot processed this order, but the "
        "payment was never gateway verified (gateway_verified_at is NULL)"
    ),
    BulkReviewRefusal.DUPLICATE_ORDER_ID: (
        "names a payment already listed in this batch (the same payment can "
        "be named by its bot_order_id and by its numeric gateway_order_id)"
    ),
    BulkReviewRefusal.MIXED_VERIFICATION_SET: (
        "the set mixes gateway-verified and never-verified payments; one "
        "shared justification cannot cover both. Resolve them separately."
    ),
    BulkReviewRefusal.BATCH_TOO_LARGE: (
        f"more than {MAX_BULK_SIZE} order ids in one batch"
    ),
    BulkReviewRefusal.EMPTY_BATCH: "no order ids given",
}


@dataclass(frozen=True)
class BulkReviewRow:
    """One row of the preview / result report."""

    order_id: str  # exactly as the operator typed it
    payment_id: int | None
    bot_order_id: str | None
    status: str | None
    gateway_verified: bool | None
    amount: int | None
    bot_notify_reason: str | None
    refusal: BulkReviewRefusal | None
    message: str | None


@dataclass(frozen=True)
class BulkReviewReport:
    rows: tuple[BulkReviewRow, ...]
    # Set-level refusal, evaluated only when every individual row passed.
    set_refusal: BulkReviewRefusal | None
    set_message: str | None
    resolution: str

    @property
    def eligible(self) -> bool:
        """True only when EVERY row passed and no set-level rule failed."""
        return self.set_refusal is None and all(row.refusal is None for row in self.rows)

    @property
    def blocked_rows(self) -> tuple[BulkReviewRow, ...]:
        return tuple(row for row in self.rows if row.refusal is not None)


@dataclass(frozen=True)
class BulkReviewResult:
    resolved: bool
    resolved_count: int
    report: BulkReviewReport


def _message(refusal: BulkReviewRefusal, payment: Payment | None = None) -> str:
    template = REFUSAL_MESSAGE[refusal]
    if payment is None:
        return template
    return template.format(
        status=payment.status,
        resolution=payment.review_resolution,
        resolved_at=(
            payment.review_resolved_at.isoformat() if payment.review_resolved_at else None
        ),
    )


def refuse_reason(payment: Payment, *, resolution: str) -> BulkReviewRefusal | None:
    """THE per-row eligibility guard. Pure, so the preview and the locked
    execute path evaluate literally the same predicate.

    Stricter than the single-payment ``app.ops review resolve`` command in
    exactly one respect: an ALREADY-resolved review is refused here. Bulk
    resolution is a blanket action over a set the operator asserts is
    homogeneous, so silently re-stamping a review someone already decided —
    overwriting the earlier actor's resolution code — is not an outcome bulk
    should ever produce. Correcting one previously-recorded resolution stays
    available, deliberately, through the single-payment command.
    """
    if payment.status != PaymentStatus.MANUAL_REVIEW.value:
        return BulkReviewRefusal.NOT_MANUAL_REVIEW
    if payment.review_resolved_at is not None:
        return BulkReviewRefusal.ALREADY_RESOLVED
    if (
        resolution in RESOLUTIONS_REQUIRING_GATEWAY_VERIFIED
        and payment.gateway_verified_at is None
    ):
        return BulkReviewRefusal.REQUIRES_GATEWAY_VERIFIED
    return None


def _row_for(order_id: str, payment: Payment, *, resolution: str) -> BulkReviewRow:
    refusal = refuse_reason(payment, resolution=resolution)
    return BulkReviewRow(
        order_id=order_id,
        payment_id=payment.id,
        bot_order_id=payment.bot_order_id,
        status=payment.status,
        gateway_verified=payment.gateway_verified_at is not None,
        amount=payment.amount,
        bot_notify_reason=payment.bot_notify_reason,
        refusal=refusal,
        message=None if refusal is None else _message(refusal, payment),
    )


def _set_refusal(rows: Sequence[BulkReviewRow]) -> BulkReviewRefusal | None:
    """Set-level rules, evaluated only once every individual row passed.

    A batch that mixes gateway-verified and never-verified payments is
    rejected: the operator supplies ONE justification note and ONE resolution
    code for the whole set, and those two populations are materially different
    financial situations (one has a confirmed gateway payment behind it, the
    other does not). Forcing them into separate invocations keeps each recorded
    justification truthful about what it actually covers.
    """
    verified = {row.gateway_verified for row in rows}
    if len(verified) > 1:
        return BulkReviewRefusal.MIXED_VERIFICATION_SET
    return None


def build_report(
    db: Session, *, order_ids: Sequence[str], resolution: str
) -> BulkReviewReport:
    """Read-only evaluation of a candidate batch. No lock, no write.

    Duplicate ids are reported per-occurrence rather than deduplicated, and an
    empty or oversized batch fails at the set level.
    """
    if not order_ids:
        return BulkReviewReport(
            rows=(),
            set_refusal=BulkReviewRefusal.EMPTY_BATCH,
            set_message=_message(BulkReviewRefusal.EMPTY_BATCH),
            resolution=resolution,
        )
    if len(order_ids) > MAX_BULK_SIZE:
        return BulkReviewReport(
            rows=(),
            set_refusal=BulkReviewRefusal.BATCH_TOO_LARGE,
            set_message=_message(BulkReviewRefusal.BATCH_TOO_LARGE),
            resolution=resolution,
        )

    seen: set[str] = set()
    # Resolved payment ids, so two DIFFERENT strings naming the SAME payment
    # (bot_order_id and gateway_order_id) are still caught as a duplicate.
    seen_payment_ids: set[int] = set()
    rows: list[BulkReviewRow] = []
    for order_id in order_ids:
        if order_id in seen:
            rows.append(
                BulkReviewRow(
                    order_id=order_id,
                    payment_id=None,
                    bot_order_id=None,
                    status=None,
                    gateway_verified=None,
                    amount=None,
                    bot_notify_reason=None,
                    refusal=BulkReviewRefusal.DUPLICATE_ORDER_ID,
                    message=_message(BulkReviewRefusal.DUPLICATE_ORDER_ID),
                )
            )
            continue
        seen.add(order_id)
        try:
            payment = find_payment_by_order_id(db, order_id)
        except AmbiguousOrderIdError:
            rows.append(
                BulkReviewRow(
                    order_id=order_id,
                    payment_id=None,
                    bot_order_id=None,
                    status=None,
                    gateway_verified=None,
                    amount=None,
                    bot_notify_reason=None,
                    refusal=BulkReviewRefusal.AMBIGUOUS_ORDER_ID,
                    message=_message(BulkReviewRefusal.AMBIGUOUS_ORDER_ID),
                )
            )
            continue
        if payment is None:
            rows.append(
                BulkReviewRow(
                    order_id=order_id,
                    payment_id=None,
                    bot_order_id=None,
                    status=None,
                    gateway_verified=None,
                    amount=None,
                    bot_notify_reason=None,
                    refusal=BulkReviewRefusal.NOT_FOUND,
                    message=_message(BulkReviewRefusal.NOT_FOUND),
                )
            )
            continue
        if payment.id in seen_payment_ids:
            rows.append(
                BulkReviewRow(
                    order_id=order_id,
                    payment_id=payment.id,
                    bot_order_id=payment.bot_order_id,
                    status=payment.status,
                    gateway_verified=payment.gateway_verified_at is not None,
                    amount=payment.amount,
                    bot_notify_reason=payment.bot_notify_reason,
                    refusal=BulkReviewRefusal.DUPLICATE_ORDER_ID,
                    message=_message(BulkReviewRefusal.DUPLICATE_ORDER_ID),
                )
            )
            continue
        seen_payment_ids.add(payment.id)
        rows.append(_row_for(order_id, payment, resolution=resolution))

    set_refusal = (
        _set_refusal(rows) if all(row.refusal is None for row in rows) else None
    )
    return BulkReviewReport(
        rows=tuple(rows),
        set_refusal=set_refusal,
        set_message=None if set_refusal is None else _message(set_refusal),
        resolution=resolution,
    )


def preview_bulk_resolution(
    db: Session, *, order_ids: Sequence[str], resolution: str
) -> BulkReviewReport:
    """Strictly read-only preview. Rolls back so no transaction is left open
    holding read locks while the operator reads the output."""
    report = build_report(db, order_ids=order_ids, resolution=resolution)
    db.rollback()
    return report


def resolve_reviews(
    db: Session,
    *,
    order_ids: Sequence[str],
    resolution: str,
    note: str,
    actor: str,
    now: datetime,
) -> BulkReviewResult:
    """All-or-nothing bulk resolution.

    Re-evaluates the batch, locks every candidate row in one statement
    (ascending id, plain ``FOR UPDATE``, ``populate_existing=True``), re-runs
    the FULL per-row and set-level guards against that freshly-locked state,
    and only then writes. Any failure at any point rolls the whole transaction
    back and resolves nothing.
    """
    report = build_report(db, order_ids=order_ids, resolution=resolution)
    if not report.eligible:
        db.rollback()
        return BulkReviewResult(resolved=False, resolved_count=0, report=report)

    payment_ids = sorted(row.payment_id for row in report.rows if row.payment_id is not None)
    locked = list(
        db.execute(
            select(Payment)
            .where(Payment.id.in_(payment_ids))
            .order_by(Payment.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars()
    )

    by_id = {payment.id: payment for payment in locked}
    # A row that vanished between the report and the lock (impossible today —
    # payments are never deleted — but never assumed) fails the batch closed.
    recheck_rows: list[BulkReviewRow] = []
    for row in report.rows:
        payment = by_id.get(row.payment_id) if row.payment_id is not None else None
        if payment is None:
            recheck_rows.append(
                BulkReviewRow(
                    order_id=row.order_id,
                    payment_id=row.payment_id,
                    bot_order_id=row.bot_order_id,
                    status=None,
                    gateway_verified=None,
                    amount=None,
                    bot_notify_reason=None,
                    refusal=BulkReviewRefusal.NOT_FOUND,
                    message=_message(BulkReviewRefusal.NOT_FOUND),
                )
            )
            continue
        recheck_rows.append(_row_for(row.order_id, payment, resolution=resolution))

    set_refusal = (
        _set_refusal(recheck_rows) if all(r.refusal is None for r in recheck_rows) else None
    )
    recheck = BulkReviewReport(
        rows=tuple(recheck_rows),
        set_refusal=set_refusal,
        set_message=None if set_refusal is None else _message(set_refusal),
        resolution=resolution,
    )
    if not recheck.eligible:
        db.rollback()
        return BulkReviewResult(resolved=False, resolved_count=0, report=recheck)

    safe_note = note[:NOTE_MAX_LENGTH]
    safe_actor = actor[:ACTOR_MAX_LENGTH]
    for payment in locked:
        # Operational review metadata ONLY, identical to the single-payment
        # path: status stays `manual_review` as permanent history, and no
        # financial or gateway fact is written.
        payment.review_acknowledged_at = payment.review_acknowledged_at or now
        payment.review_resolved_at = now
        payment.review_resolution = resolution
        record_event(
            db,
            payment_id=payment.id,
            event_type="manual_review_resolved",
            data={
                "resolution": resolution,
                "note": safe_note,
                "operator": safe_actor,
                "bulk": True,
                "batch_size": len(locked),
            },
        )
    record_event(
        db,
        payment_id=None,
        event_type="manual_review_bulk_resolved",
        level="warning",
        data={
            "resolution": resolution,
            "note": safe_note,
            "operator": safe_actor,
            "resolved_count": len(locked),
            # Bot order ids only: no amounts, no payer identity, no secrets.
            "order_ids": [payment.bot_order_id for payment in locked],
        },
    )
    db.commit()
    logger.warning(
        "manual_review_bulk_resolved",
        extra={
            "resolution": resolution,
            "operator": safe_actor,
            "resolved_count": len(locked),
        },
    )
    return BulkReviewResult(
        resolved=True, resolved_count=len(locked), report=recheck
    )
