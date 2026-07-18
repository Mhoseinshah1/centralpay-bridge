"""Dynamic percentage service fee.

Money model:

- ``payment.amount`` — the ORIGINAL bot invoice amount (what the bot
  requested and what the bot will credit). Never includes the fee.
- ``fee_rate_bps`` — fee percentage in basis points (10% = 1000 bps).
- ``fee_amount = (amount * fee_rate_bps + 5000) // 10000`` — deterministic
  integer arithmetic, ROUND HALF UP on the ten-thousandths boundary.
  Never floats.
- ``payable_amount = amount + fee_amount`` — what CentralPay charges the
  payer and what verify must report.

Policies are append-only rows in ``fee_policies``. Payments snapshot the
policy at creation; later policy changes never touch existing payments.
Selection is deterministic and time-based, so scheduled changes activate
at their exact ``effective_at`` with no restart, and every API/worker
replica observes changes through PostgreSQL.
"""

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.models import FeePolicy

logger = logging.getLogger("app.services.fees")

MAX_RATE_BPS = 10_000
_NOTE_MAX_LENGTH = 500

# Percent string accepted by operator tooling: 0..100 with at most two
# decimal places. No signs, no exponents, no separators, no whitespace.
# re.ASCII: without it \d (and int()) accept non-ASCII Unicode digits.
# \Z rather than $: $ would also match before a trailing newline.
_RATE_PATTERN = re.compile(r"^(\d{1,3})(?:\.(\d{1,2}))?\Z", re.ASCII)


def calculate_fee(amount: int, rate_bps: int) -> tuple[int, int]:
    """Return ``(fee_amount, payable_amount)`` for an original amount.

    fee_amount = (amount * rate_bps + 5000) // 10000  — round half up.
    Pure integers throughout; Python ints are unbounded so the
    intermediate product cannot overflow.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not 0 <= rate_bps <= MAX_RATE_BPS:
        raise ValueError("rate_bps must be within 0..10000")
    fee_amount = (amount * rate_bps + 5_000) // 10_000
    return fee_amount, amount + fee_amount


def parse_rate_percent(value: str) -> int:
    """Parse an operator-supplied percentage string into basis points.

    Accepts 0..100 with at most two decimals ("0", "10", "7.5", "2.25").
    Rejects signs, exponents, commas, whitespace, NaN/Infinity, more than
    two decimals, and anything above 100.
    """
    match = _RATE_PATTERN.match(value)
    if match is None:
        raise ValueError(
            "invalid fee rate: use 0..100 with at most two decimal places (e.g. 10 or 7.5)"
        )
    whole, decimals = match.group(1), match.group(2) or ""
    bps = int(whole) * 100 + int((decimals + "00")[:2])
    if bps > MAX_RATE_BPS:
        raise ValueError("fee rate cannot exceed 100")
    return bps


def format_rate_percent(rate_bps: int) -> str:
    whole, frac = divmod(rate_bps, 100)
    if frac == 0:
        return f"{whole}%"
    return f"{whole}.{frac:02d}".rstrip("0") + "%"


def select_effective_policy(db: Session, *, now: datetime | None = None) -> FeePolicy | None:
    """The policy governing NEW payments at ``now``.

    Deterministic: highest ``effective_at`` not after ``now``, then
    highest ``id``; cancelled policies are never selected. ``None`` means
    no policy exists yet (zero fee).
    """
    now = now or datetime.now(UTC)
    return db.execute(
        select(FeePolicy)
        .where(FeePolicy.effective_at <= now, FeePolicy.cancelled_at.is_(None))
        .order_by(FeePolicy.effective_at.desc(), FeePolicy.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def next_scheduled_policy(db: Session, *, now: datetime | None = None) -> FeePolicy | None:
    now = now or datetime.now(UTC)
    return db.execute(
        select(FeePolicy)
        .where(FeePolicy.effective_at > now, FeePolicy.cancelled_at.is_(None))
        .order_by(FeePolicy.effective_at.asc(), FeePolicy.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def _validate_note(note: str) -> str:
    note = note.strip()
    if not note:
        raise ValueError("a non-empty --note is required")
    if len(note) > _NOTE_MAX_LENGTH:
        raise ValueError(f"note must be at most {_NOTE_MAX_LENGTH} characters")
    return note


def create_policy(
    db: Session,
    *,
    rate_bps: int,
    effective_at: datetime,
    actor: str,
    note: str,
    scheduled: bool,
) -> FeePolicy:
    """Append a new fee policy row and its permanent audit event.

    The caller commits. Existing payments are never touched — the policy
    only governs orders created at or after ``effective_at``.
    """
    if not 0 <= rate_bps <= MAX_RATE_BPS:
        raise ValueError("rate_bps must be within 0..10000")
    if effective_at.tzinfo is None:
        raise ValueError("effective_at must carry an explicit timezone")
    note = _validate_note(note)
    policy = FeePolicy(
        rate_bps=rate_bps, effective_at=effective_at, created_by=actor, note=note
    )
    db.add(policy)
    db.flush()
    record_event(
        db,
        payment_id=None,
        event_type="fee_policy_scheduled" if scheduled else "fee_policy_created",
        data={
            "policy_id": policy.id,
            "rate_bps": rate_bps,
            "effective_at": effective_at.isoformat(),
            "actor": actor,
            "note": note,
        },
    )
    logger.info(
        "fee_policy_created",
        extra={
            "policy_id": policy.id,
            "rate_bps": rate_bps,
            "effective_at": effective_at.isoformat(),
            "scheduled": scheduled,
        },
    )
    return policy


def cancel_policy(
    db: Session, *, policy_id: int, actor: str, note: str, now: datetime | None = None
) -> FeePolicy:
    """Cancel a policy (normally a scheduled one). Append-only: the row
    stays in history with its cancellation metadata. The caller commits."""
    now = now or datetime.now(UTC)
    note = _validate_note(note)
    policy = db.execute(
        select(FeePolicy).where(FeePolicy.id == policy_id).with_for_update()
    ).scalar_one_or_none()
    if policy is None:
        raise ValueError(f"fee policy {policy_id} does not exist")
    if policy.cancelled_at is not None:
        raise ValueError(f"fee policy {policy_id} is already cancelled")
    policy.cancelled_at = now
    policy.cancelled_by = actor
    policy.cancellation_note = note
    record_event(
        db,
        payment_id=None,
        event_type="fee_policy_cancelled",
        data={
            "policy_id": policy.id,
            "rate_bps": policy.rate_bps,
            "effective_at": policy.effective_at.isoformat(),
            "actor": actor,
            "note": note,
        },
    )
    logger.info("fee_policy_cancelled", extra={"policy_id": policy.id, "actor": actor})
    return policy
