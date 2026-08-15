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
from app.services.reconciliation import ERROR_GATEWAY_NOT_PAID, link_age_anchor

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


def _link_created_buckets(
    db: Session, settings: Settings, now: datetime
) -> tuple[list[StuckEntry], list[StuckEntry], list[StuckEntry], dict[str, int]]:
    anchor = link_age_anchor()
    expired_cutoff = now - timedelta(seconds=settings.reconciliation_max_age_seconds)
    base_conditions: tuple[Any, ...] = (
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),
    )
    not_expired = (*base_conditions, anchor > expired_cutoff)
    expired_conditions = (*base_conditions, anchor <= expired_cutoff)
    # "Exhausted" is the only way reconciliation_next_at can be NULL on a
    # still-link_created, not-yet-expired row with a nonzero attempt count
    # (see reconciliation.py::_finalize) — never re-derived from anything
    # but these two columns, and never touches the claim path.
    exhausted_conditions = (
        *not_expired,
        Payment.reconciliation_next_at.is_(None),
        Payment.reconciliation_attempts >= settings.reconciliation_max_attempts,
    )
    waiting_conditions = (
        *not_expired,
        or_(
            Payment.reconciliation_next_at.is_not(None),
            Payment.reconciliation_attempts < settings.reconciliation_max_attempts,
        ),
    )

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
    waiting = [
        StuckEntry(
            payment=payment,
            category=StuckCategory.WAITING_GATEWAY,
            reason="link_created",
            gateway_state=_gateway_state(payment),
        )
        for payment in rows(waiting_conditions)
    ]
    expired = [
        StuckEntry(
            payment=payment,
            category=StuckCategory.EXPIRED,
            reason="reconciliation_max_age_exceeded",
            gateway_state=_gateway_state(payment),
        )
        for payment in rows(expired_conditions)
    ]
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
