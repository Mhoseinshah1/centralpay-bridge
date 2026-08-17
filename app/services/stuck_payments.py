"""Read-only, unified categorization of payments an operator should look at.

Single source of truth for the CLI's ``centralpay stuck`` and the admin
bot's ``/stuck``, so the two surfaces can never disagree about what "stuck"
means. Every query here is a plain, lock-free SELECT: nothing is ever
claimed, mutated, retried, or committed. Age boundaries and the link-age
anchor are exactly the values/expression reconciliation itself uses (see
``app.services.reconciliation.link_age_anchor`` and ``Settings``), never
re-derived, so this view can never quietly drift from what the worker is
actually doing.

Three categories, in the fixed operator-priority order ``StuckOverview``
exposes:

* NEEDS_ATTENTION — open manual review; stale/failed bot notification
  (reused verbatim from ``app.adminbot.queries.stuck_payments``); a
  ``link_created`` payment whose reconciliation attempts are exhausted; a
  payment sitting in a status nothing ever automatically revisits
  (``created``, ``getlink_failed``, or the never-actually-persisted
  ``gateway_verified``) past a short grace period. A payment that is simply
  mid-flight — still being polled, gateway not yet paid — is deliberately
  NOT attention-worthy: that is the expected, routine steady state
  (``app.services.verification`` documents this explicitly), so promoting
  it here would bury real problems in noise.
* WAITING_GATEWAY — ``link_created``, unverified, younger than the hard
  reconciliation lifetime, not exhausted: ordinary in-flight polling
  (active or expiring tier, whether never yet checked or checked and not
  yet paid).
* EXPIRED — ``link_created``, unverified, at or past
  ``reconciliation_max_age_seconds``: reconciliation itself already
  excludes these from selection (see ``reconciliation.py``); kept here only
  for audit visibility, never retried.
"""

import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

# app.adminbot.queries is a plain, Telegram-agnostic set of read-only SQL
# helpers over app.models (no bot/formatting concerns) — reusing it here
# keeps the stale-notification-claim/manual-review detection in exactly one
# place instead of re-deriving it.
from app.adminbot import queries
from app.config import Settings
from app.models import Payment, PaymentStatus
from app.services.reconciliation import (
    ERROR_GATEWAY_NOT_PAID,
    link_age_anchor,
    reconciliation_exhausted_conditions,
)

NowFn = Callable[[], datetime]

# created/getlink_failed rows are never revisited automatically: only the
# original create-payment call ever attempts getLink, and reconciliation
# only ever selects link_created (see reconciliation.py's module docstring).
# Past this age they are a genuine anomaly, not just an in-flight request.
UNEXPECTED_STATE_GRACE_SECONDS = 60

# Defensive cap on rows materialized per bucket — independent of the
# caller-facing --limit/display truncation, which happens further down in
# the CLI/bot renderers. `StuckOverview.total_counts` is computed with plain
# COUNT queries and stays exact regardless of this cap, EXCEPT for the
# reused manual-review/bot-notification bucket (see `_reused_needs_attention`)
# where an exact count would require re-deriving `queries.stuck_payments`'s
# stale-claim logic; in the extreme case of more than this many simultaneous
# stuck deliveries, the count saturates at this cap rather than over-reading.
_QUERY_CAP = 200

# PaymentStatus values no current code path ever persists: verification
# always moves link_created straight to bot_notify_pending (see
# app.services.verification / app.services.notification.queue_notification).
# A row sitting in gateway_verified is itself evidence of a bug, exactly
# like an abandoned created/getlink_failed row.
_UNEXPECTED_STATUSES = (
    PaymentStatus.CREATED.value,
    PaymentStatus.GETLINK_FAILED.value,
    PaymentStatus.GATEWAY_VERIFIED.value,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:  # SQLite returns naive UTC datetimes
        return value.replace(tzinfo=UTC)
    return value


class StuckCategory(enum.StrEnum):
    NEEDS_ATTENTION = "needs_attention"
    WAITING_GATEWAY = "waiting_gateway"
    EXPIRED = "expired"


@dataclass(frozen=True)
class StuckEntry:
    payment: Payment
    category: StuckCategory
    # Exact machine-readable reason/state — never a generic label (mirrors
    # the existing invariant on app.adminbot.queries.StuckEntry.category).
    reason: str
    # "pending" | "not_paid" | a reconciliation error code | None when the
    # entry is not a link_created/reconciliation-tracked payment.
    gateway_state: str | None = None


@dataclass(frozen=True)
class StuckOverview:
    needs_attention: list[StuckEntry]
    waiting_gateway: list[StuckEntry]
    expired: list[StuckEntry]
    # Exact totals (see _QUERY_CAP note above), independent of how many
    # entries were actually materialized in the lists.
    total_counts: dict[str, int]

    def ordered(self) -> list[StuckEntry]:
        """Fixed operator priority order: attention, then waiting, then
        expired. Callers wanting a display cap should slice this list."""
        return [*self.needs_attention, *self.waiting_gateway, *self.expired]


def _gateway_state(payment: Payment) -> str:
    if payment.reconciliation_last_error_code == ERROR_GATEWAY_NOT_PAID:
        return "not_paid"
    if payment.reconciliation_attempts == 0:
        return "pending"
    return payment.reconciliation_last_error_code or "pending"


def _reused_needs_attention(db: Session, settings: Settings) -> list[StuckEntry]:
    """Manual review + bot-notification failures: existing logic, untouched."""
    entries = queries.stuck_payments(
        db,
        claim_timeout_seconds=settings.bot_notify_claim_timeout_seconds,
        limit=_QUERY_CAP,
    )
    return [
        StuckEntry(
            payment=entry.payment,
            category=StuckCategory.NEEDS_ATTENTION,
            reason=entry.category,
        )
        for entry in entries
    ]


def _waiting_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """A link_created, unverified payment younger than the hard
    reconciliation lifetime and not exhausted: ordinary in-flight polling.
    Shared by the overview's WAITING_GATEWAY bucket and
    ``waiting_snapshot``/``count_waiting`` so they can never disagree about
    the definition."""
    anchor = link_age_anchor()
    expired_cutoff = now - timedelta(seconds=settings.reconciliation_max_age_seconds)
    return (
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),
        anchor > expired_cutoff,
        or_(
            Payment.reconciliation_next_at.is_not(None),
            Payment.reconciliation_attempts < settings.reconciliation_max_attempts,
        ),
    )


def _expired_conditions(settings: Settings, *, now: datetime) -> tuple[Any, ...]:
    """A link_created, unverified payment at or past
    reconciliation_max_age_seconds. Shared by the overview's EXPIRED bucket
    and ``expired_snapshot``/``count_expired``."""
    anchor = link_age_anchor()
    expired_cutoff = now - timedelta(seconds=settings.reconciliation_max_age_seconds)
    return (
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),
        anchor <= expired_cutoff,
    )


def _waiting_entry(payment: Payment) -> StuckEntry:
    return StuckEntry(
        payment=payment,
        category=StuckCategory.WAITING_GATEWAY,
        reason="link_created",
        gateway_state=_gateway_state(payment),
    )


def _expired_entry(payment: Payment) -> StuckEntry:
    return StuckEntry(
        payment=payment,
        category=StuckCategory.EXPIRED,
        reason="reconciliation_max_age_exceeded",
        gateway_state=_gateway_state(payment),
    )


def count_waiting(db: Session, settings: Settings, *, now: datetime) -> int:
    """EXACT count of WAITING_GATEWAY payments (unbounded, no _QUERY_CAP).
    A plain COUNT, not a snapshot: `/stuck`'s header shows this as a bare
    number with no accompanying detail rows in that command, so there is no
    count/list pair to keep consistent here. When `/waiting` itself runs
    and renders entries, use `waiting_snapshot` instead — its total and
    rows come from one SQL statement."""
    return db.execute(
        select(func.count(Payment.id)).where(*_waiting_conditions(settings, now=now))
    ).scalar_one()


def count_expired(db: Session, settings: Settings, *, now: datetime) -> int:
    """EXACT count of EXPIRED payments (unbounded, no _QUERY_CAP). Same
    plain-COUNT rationale as `count_waiting`: use `expired_snapshot` when
    `/expired` itself renders entries."""
    return db.execute(
        select(func.count(Payment.id)).where(*_expired_conditions(settings, now=now))
    ).scalar_one()


def count_other_attention(db: Session, settings: Settings, *, now: datetime) -> int:
    """EXACT count of every current NEEDS_ATTENTION condition that is NOT a
    bot-delivery problem:

    * reconciliation-exhausted (a financial/reconciliation state)
    * unexpected-status rows (a system anomaly)
    * open manual-review rows caused by a FINANCIAL/verification mismatch
      (``app.adminbot.queries.count_non_delivery_manual_reviews`` — the
      exact complement of ``bot_delivery_snapshot``'s manual-review half,
      sharing the same ``_open_manual_review_conditions``/
      ``_bot_delivery_manual_review_conditions`` predicates so a row can
      never be counted in both this function and
      ``bot_delivery_snapshot``, and never dropped from both).

    Invariant this preserves: for the same (db, settings, now),
    ``queries.bot_delivery_snapshot(db, now=now).total
    + count_other_attention(db, settings, now=now)``
    equals the total number of rows any current NEEDS_ATTENTION condition
    would select — reconciliation-exhausted, unexpected-status, and EVERY
    open manual-review row (delivery or non-delivery), with the
    stale/old-bot_notify_pending rows folded into the bot-delivery half.
    Never approximated and never double-counted. This is a separate COUNT
    statement (not fused into `bot_delivery_snapshot`'s single statement):
    `/stuck` shows `other_total` as a bare summary number with no
    accompanying detail rows, so there is no count/list pair to keep
    consistent here the way there is for the bot-delivery, waiting, and
    expired totals."""
    exhausted_conditions = reconciliation_exhausted_conditions(settings, now=now)
    exhausted_total = db.execute(
        select(func.count(Payment.id)).where(*exhausted_conditions)
    ).scalar_one()
    cutoff = now - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS)
    unexpected_total = db.execute(
        select(func.count(Payment.id)).where(
            Payment.status.in_(_UNEXPECTED_STATUSES), Payment.created_at <= cutoff
        )
    ).scalar_one()
    non_delivery_manual_review_total = queries.count_non_delivery_manual_reviews(db)
    return exhausted_total + unexpected_total + non_delivery_manual_review_total


@dataclass(frozen=True)
class WaitingSnapshot:
    # EXACT total matching _waiting_conditions (unbounded — never reduced
    # by `limit`; see `waiting_snapshot`'s docstring for why).
    total: int
    entries: list[StuckEntry]


@dataclass(frozen=True)
class ExpiredSnapshot:
    # EXACT total matching _expired_conditions (unbounded — never reduced
    # by `limit`; see `expired_snapshot`'s docstring for why).
    total: int
    entries: list[StuckEntry]


def waiting_snapshot(
    db: Session, settings: Settings, *, now: datetime, limit: int
) -> WaitingSnapshot:
    """`/waiting`'s total AND its detail rows from ONE SQL statement, so a
    payment that stops waiting (gets verified, or its link expires)
    between what would otherwise be a separate COUNT and a separate
    SELECT can never make the two disagree. ``func.count().over()`` (a
    window function) computes the exact total over every row matching
    ``_waiting_conditions`` BEFORE ``LIMIT`` is applied — standard SQL
    window-function semantics — in the same statement that fetches the (at
    most ``limit``) rows.

    Rows are ordered longest-waiting (oldest link-age anchor) first — the
    operationally most urgent ordering: these payments have been polling
    the gateway without resolution the longest. A fresh, directly-queried
    statement (not a slice of the overview's capped list) so `/waiting N`
    is correct regardless of total row count.

    Two rows can share the exact same anchor timestamp (same
    ``callback_token_issued_at``/``created_at``), which the database is
    otherwise free to return in an unspecified order. Ties are broken by
    ascending ``Payment.id`` — the row created first among the tied rows
    sorts first — keeping the tie-break consistent with the primary
    "oldest first" ordering instead of leaving it storage-dependent."""
    anchor = link_age_anchor()
    total_col = func.count().over().label("total")
    stmt = (
        select(Payment, total_col)
        .where(*_waiting_conditions(settings, now=now))
        .order_by(anchor.asc(), Payment.id.asc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    total = rows[0].total if rows else 0
    entries = [_waiting_entry(payment) for payment, _total in rows]
    return WaitingSnapshot(total=total, entries=entries)


def expired_snapshot(
    db: Session, settings: Settings, *, now: datetime, limit: int
) -> ExpiredSnapshot:
    """`/expired`'s total AND its detail rows from ONE SQL statement — same
    window-function design as ``waiting_snapshot``, so a link that gets
    verified or reconciled between what would otherwise be a separate
    COUNT and a separate SELECT can never make the two disagree.

    Rows are ordered MOST RECENTLY expired (newest link-age anchor that is
    still past the cutoff) first. With potentially thousands of expired
    rows accumulating over the deployment's lifetime, the overview's
    ascending-and-capped ``expired`` list would only ever surface the most
    ancient legacy rows — never useful for an operator checking what JUST
    expired. A fresh, directly-queried, descending statement instead.

    Two rows can share the exact same anchor timestamp; ties are broken by
    descending ``Payment.id`` — the row created most recently among the
    tied rows sorts first — keeping the tie-break consistent with the
    primary "most recently expired first" ordering instead of leaving it
    storage-dependent."""
    anchor = link_age_anchor()
    total_col = func.count().over().label("total")
    stmt = (
        select(Payment, total_col)
        .where(*_expired_conditions(settings, now=now))
        .order_by(anchor.desc(), Payment.id.desc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    total = rows[0].total if rows else 0
    entries = [_expired_entry(payment) for payment, _total in rows]
    return ExpiredSnapshot(total=total, entries=entries)


def _link_created_buckets(
    db: Session, settings: Settings, now: datetime
) -> tuple[list[StuckEntry], list[StuckEntry], list[StuckEntry], dict[str, int]]:
    anchor = link_age_anchor()
    # "Exhausted" is the only way reconciliation_next_at can be NULL on a
    # still-link_created, not-yet-expired row with a nonzero attempt count
    # (see reconciliation.py::_finalize) — shared with reconciliation_status.py
    # via the same pure predicate builder, so the two read-only views can
    # never quietly disagree about what "exhausted" means; never touches the
    # claim path.
    exhausted_conditions = reconciliation_exhausted_conditions(settings, now=now)
    waiting_conditions = _waiting_conditions(settings, now=now)
    expired_conditions = _expired_conditions(settings, now=now)

    def count(conditions: tuple[Any, ...]) -> int:
        return db.execute(select(func.count(Payment.id)).where(*conditions)).scalar_one()

    def rows(conditions: tuple[Any, ...]) -> list[Payment]:
        return list(
            db.execute(
                select(Payment).where(*conditions).order_by(anchor.asc()).limit(_QUERY_CAP)
            ).scalars()
        )

    attention = [
        StuckEntry(
            payment=payment,
            category=StuckCategory.NEEDS_ATTENTION,
            reason=(
                "reconciliation_exhausted:"
                f"{payment.reconciliation_last_error_code or 'unknown'}"
            ),
            gateway_state=_gateway_state(payment),
        )
        for payment in rows(exhausted_conditions)
    ]
    waiting = [_waiting_entry(payment) for payment in rows(waiting_conditions)]
    expired = [_expired_entry(payment) for payment in rows(expired_conditions)]
    counts = {
        "reconciliation_exhausted": count(exhausted_conditions),
        "waiting_gateway": count(waiting_conditions),
        "expired": count(expired_conditions),
    }
    return attention, waiting, expired, counts


def _unexpected_status_entries(db: Session, now: datetime) -> tuple[list[StuckEntry], int]:
    cutoff = now - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS)
    conditions: tuple[Any, ...] = (
        Payment.status.in_(_UNEXPECTED_STATUSES),
        Payment.created_at <= cutoff,
    )
    total = db.execute(select(func.count(Payment.id)).where(*conditions)).scalar_one()
    payments = db.execute(
        select(Payment).where(*conditions).order_by(Payment.created_at.asc()).limit(_QUERY_CAP)
    ).scalars()
    entries = [
        StuckEntry(
            payment=payment,
            category=StuckCategory.NEEDS_ATTENTION,
            reason=f"unexpected_status:{payment.status}",
        )
        for payment in payments
    ]
    return entries, total


def stuck_payments_overview(
    db: Session, settings: Settings, *, now_fn: NowFn = utcnow
) -> StuckOverview:
    """Build the full categorized, read-only snapshot.

    Never locks, never writes, never commits — safe to call at any time,
    including concurrently with the reconciliation worker.
    """
    now = now_fn()
    reused_attention = _reused_needs_attention(db, settings)
    exhausted_attention, waiting, expired, link_counts = _link_created_buckets(db, settings, now)
    unexpected_attention, unexpected_total = _unexpected_status_entries(db, now)

    needs_attention = [*reused_attention, *exhausted_attention, *unexpected_attention]
    total_counts = {
        "needs_attention": (
            len(reused_attention) + link_counts["reconciliation_exhausted"] + unexpected_total
        ),
        "waiting_gateway": link_counts["waiting_gateway"],
        "expired": link_counts["expired"],
    }
    return StuckOverview(
        needs_attention=needs_attention,
        waiting_gateway=waiting,
        expired=expired,
        total_counts=total_counts,
    )
