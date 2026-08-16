"""Strictly read-only reconciliation operational status snapshot.

Single shared structured snapshot consumed by ``centralpay reconciliation
status`` (both the human and ``--json`` renderers in ``app.cli``) and,
later, any admin-bot/dashboard surface that wants the same data. Nothing
here ever claims, locks, or mutates a row, records an event, or calls the
gateway — see ``app.services.reconciliation`` for the mutating claim/
settlement path this module deliberately never calls.

Every age boundary and due predicate mirrors ``app.services.reconciliation``
EXACTLY, via the shared pure (non-mutating) condition builders there
(``active_tier_due_conditions`` / ``expiring_tier_due_conditions`` /
``reconciliation_exhausted_conditions``) and the shared ``link_age_anchor()``
expression — so this view can never quietly disagree with what the
reconciliation worker is actually doing (the same reuse pattern
``app.services.stuck_payments`` follows).
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adminbot.queries import latest_worker_heartbeat
from app.config import Settings
from app.models import Payment, PaymentEvent, PaymentStatus, WorkerHeartbeat
from app.services.reconciliation import (
    active_tier_due_conditions,
    expiring_tier_due_conditions,
    link_age_anchor,
    reconciliation_exhausted_conditions,
)

NowFn = Callable[[], datetime]

RECONCILIATION_WORKER_NAME = "reconciliation-worker"

# Same liveness-file contract as the worker container's own Docker
# healthcheck (docker-compose.yml, service "worker": fresh = age < 120s):
# touched by app.worker's notification loop after every completed pass, on
# that container's own tmpfs. A CLI process invoked via `compose exec
# worker` shares that filesystem and sees it fresh; one invoked via
# `compose exec api` never does (the api container never runs app.worker).
# Best-effort ONLY — never the sole safety mechanism. The actual guarantee
# is scripts/centralpay always routing `reconciliation status` to the
# worker container, never api; this is a second, independent, honest signal
# for whoever inspects the output (see build_reconciliation_status_snapshot).
_WORKER_HEARTBEAT_FILE_FRESH_SECONDS = 120

# config_source values. WORKER_CONTAINER is only ever returned when the
# worker liveness file was actually found fresh — never asserted blindly.
CONFIG_SOURCE_WORKER_CONTAINER = "worker_container_process_env"
CONFIG_SOURCE_UNCONFIRMED = "process_environment"

# Recent-statistics event types (see app.services.reconciliation._finalize).
# gateway_not_paid is informational/expected polling activity — the gateway
# simply has not confirmed payment yet — never an error. transport_failed
# and exhausted are the operator attention signals.
_STAT_VERIFIED = "reconciliation_verified"
_STAT_RETRY_SCHEDULED = "reconciliation_retry_scheduled"
_STAT_GATEWAY_NOT_PAID = "reconciliation_gateway_not_paid"
_STAT_TRANSPORT_FAILED = "reconciliation_transport_failed"
_STAT_EXHAUSTED = "reconciliation_exhausted"
_RECENT_STAT_EVENT_TYPES = (
    _STAT_VERIFIED,
    _STAT_RETRY_SCHEDULED,
    _STAT_GATEWAY_NOT_PAID,
    _STAT_TRANSPORT_FAILED,
    _STAT_EXHAUSTED,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return (now - _as_utc(value)).total_seconds()


def _oldest_age(*ages: float | None) -> float | None:
    """The OLDEST (largest) age among the given tiers.

    Age is a duration, not a rank: the LARGER value is the OLDER payment, so
    this is a max, never a min — a 3000s-old due payment is older than a
    120s-old one, regardless of which tier each sits in. Absent (None)
    tiers are ignored; the result is None only when every tier is empty.
    """
    present = [age for age in ages if age is not None]
    return max(present) if present else None


@dataclass(frozen=True)
class ReconciliationRuntime:
    enabled: bool
    # Where THIS process's Settings/config came from — see
    # _detect_config_source. Never asserted without evidence.
    config_source: str
    heartbeat_present: bool
    heartbeat_age_seconds: float | None
    # Whether the heartbeat itself is still being written recently — proves
    # only that the reconciliation LOOP is ticking, never that its most
    # recent pass succeeded (see last_successful_cycle_at/last_error_code
    # for that). None ("not applicable") exactly when reconciliation is
    # disabled; a fresh heartbeat with reconciliation now disabled would be
    # a stale artifact from before disabling, not evidence of anything.
    # When reconciliation IS enabled but no heartbeat row exists at all,
    # this is False (unhealthy), never N/A: the loop should be beating.
    heartbeat_fresh: bool | None
    # The last pass that completed WITHOUT an exception (cleared to None on
    # every subsequent successful pass; see app.services.heartbeat). A fresh
    # heartbeat combined with an old last_successful_cycle_at and a set
    # last_error_code means the loop is alive but its passes keep failing —
    # this module deliberately never collapses that into one "healthy" flag.
    last_successful_cycle_at: datetime | None
    last_successful_cycle_age_seconds: float | None
    last_error_code: str | None


@dataclass(frozen=True)
class ReconciliationConfig:
    min_age_seconds: int
    fast_window_seconds: int
    max_age_seconds: int
    fast_interval_seconds: int
    slow_interval_seconds: int
    scan_interval_seconds: float
    batch_size: int
    max_attempts: int
    slow_tier_reserved_slots: int


@dataclass(frozen=True)
class PaymentBuckets:
    """``status='link_created' AND gateway_verified_at IS NULL``, bucketed by
    link age ALONE (no ``reconciliation_next_at``/``attempts`` filter) — the
    full population reconciliation could ever consider, not just what is
    due right now (see ``QueueHealth`` for that)."""

    total_unverified: int
    active: int  # age < fast_window
    expiring: int  # fast_window <= age < max_age
    aged_out: int  # age >= max_age


@dataclass(frozen=True)
class QueueHealth:
    active_due: int
    expiring_due: int
    # Exhausted attempts within the reconciliation lifetime — explicitly
    # NOT aged-out rows (those are PaymentBuckets.aged_out, which always
    # takes priority for operator categorization; see
    # reconciliation.reconciliation_exhausted_conditions).
    exhausted_not_aged_out: int
    oldest_active_due_age_seconds: float | None
    oldest_expiring_due_age_seconds: float | None
    oldest_due_age_seconds: float | None


@dataclass(frozen=True)
class RecentStats:
    window_hours: int
    verified: int
    retry_scheduled: int
    gateway_not_paid: int
    transport_failed: int
    exhausted: int


@dataclass(frozen=True)
class ReconciliationStatusSnapshot:
    generated_at: datetime
    runtime: ReconciliationRuntime
    config: ReconciliationConfig
    buckets: PaymentBuckets
    queue: QueueHealth
    recent: RecentStats


def _detect_config_source(settings: Settings) -> str:
    """Best-effort, EVIDENCE-BASED detection of whether this process shares
    a container with the running worker loop — never a hardcoded claim.

    See the module-level comment on _WORKER_HEARTBEAT_FILE_FRESH_SECONDS.
    """
    try:
        mtime = os.path.getmtime(settings.worker_heartbeat_file)
    except OSError:
        return CONFIG_SOURCE_UNCONFIRMED
    if time.time() - mtime < _WORKER_HEARTBEAT_FILE_FRESH_SECONDS:
        return CONFIG_SOURCE_WORKER_CONTAINER
    return CONFIG_SOURCE_UNCONFIRMED


def _runtime(db: Session, settings: Settings, now: datetime) -> ReconciliationRuntime:
    heartbeat: WorkerHeartbeat | None = latest_worker_heartbeat(
        db, worker_name=RECONCILIATION_WORKER_NAME
    )
    heartbeat_present = heartbeat is not None
    heartbeat_age = (
        _age_seconds(heartbeat.last_heartbeat_at, now) if heartbeat is not None else None
    )
    if not settings.reconciliation_enabled:
        heartbeat_fresh = None
    elif heartbeat_present:
        threshold = max(settings.reconciliation_interval_seconds * 6, 120)
        heartbeat_fresh = heartbeat_age is not None and heartbeat_age < threshold
    else:
        heartbeat_fresh = False
    last_cycle_at = heartbeat.last_cycle_at if heartbeat is not None else None
    return ReconciliationRuntime(
        enabled=settings.reconciliation_enabled,
        config_source=_detect_config_source(settings),
        heartbeat_present=heartbeat_present,
        heartbeat_age_seconds=heartbeat_age,
        heartbeat_fresh=heartbeat_fresh,
        last_successful_cycle_at=last_cycle_at,
        last_successful_cycle_age_seconds=_age_seconds(last_cycle_at, now),
        last_error_code=heartbeat.last_error_code if heartbeat is not None else None,
    )


def _config(settings: Settings) -> ReconciliationConfig:
    return ReconciliationConfig(
        min_age_seconds=settings.reconciliation_min_age_seconds,
        fast_window_seconds=settings.reconciliation_fast_window_seconds,
        max_age_seconds=settings.reconciliation_max_age_seconds,
        fast_interval_seconds=settings.reconciliation_fast_interval_seconds,
        slow_interval_seconds=settings.reconciliation_slow_interval_seconds,
        scan_interval_seconds=settings.reconciliation_interval_seconds,
        batch_size=settings.reconciliation_batch_size,
        max_attempts=settings.reconciliation_max_attempts,
        slow_tier_reserved_slots=settings.reconciliation_slow_tier_reserved_slots,
    )


def _buckets(db: Session, settings: Settings, now: datetime) -> PaymentBuckets:
    anchor = link_age_anchor()
    base = (
        Payment.status == PaymentStatus.LINK_CREATED.value,
        Payment.gateway_verified_at.is_(None),
    )
    fast_cutoff = now - timedelta(seconds=settings.reconciliation_fast_window_seconds)
    max_cutoff = now - timedelta(seconds=settings.reconciliation_max_age_seconds)

    def count(*conditions: Any) -> int:
        return db.execute(select(func.count(Payment.id)).where(*base, *conditions)).scalar_one()

    active = count(anchor > fast_cutoff)
    expiring = count(anchor <= fast_cutoff, anchor > max_cutoff)
    aged_out = count(anchor <= max_cutoff)
    return PaymentBuckets(
        total_unverified=active + expiring + aged_out,
        active=active,
        expiring=expiring,
        aged_out=aged_out,
    )


def _queue(db: Session, settings: Settings, now: datetime) -> QueueHealth:
    anchor = link_age_anchor()

    def count(conditions: tuple[Any, ...]) -> int:
        return db.execute(select(func.count(Payment.id)).where(*conditions)).scalar_one()

    def oldest_age(conditions: tuple[Any, ...]) -> float | None:
        oldest_anchor = db.execute(select(func.min(anchor)).where(*conditions)).scalar_one()
        return _age_seconds(oldest_anchor, now)

    active_conditions = active_tier_due_conditions(settings, now=now)
    expiring_conditions = expiring_tier_due_conditions(settings, now=now)
    exhausted_conditions = reconciliation_exhausted_conditions(settings, now=now)

    oldest_active = oldest_age(active_conditions)
    oldest_expiring = oldest_age(expiring_conditions)
    return QueueHealth(
        active_due=count(active_conditions),
        expiring_due=count(expiring_conditions),
        exhausted_not_aged_out=count(exhausted_conditions),
        oldest_active_due_age_seconds=oldest_active,
        oldest_expiring_due_age_seconds=oldest_expiring,
        oldest_due_age_seconds=_oldest_age(oldest_active, oldest_expiring),
    )


def _recent_stats(db: Session, now: datetime, *, window_hours: int) -> RecentStats:
    """ONE grouped, window-bounded query (the app.adminbot.queries.
    errors_summary shape: ``created_at >= cutoff AND event_type IN (...)
    GROUP BY event_type``) rather than one round trip per event type. Bounded
    to the trailing window and a fixed, short event-type list — never a scan
    of the full (unbounded, growing) audit trail. This does not guarantee
    any particular PostgreSQL execution plan, only that the query itself is
    explicitly bounded on both dimensions.
    """
    cutoff = now - timedelta(hours=window_hours)
    rows = db.execute(
        select(PaymentEvent.event_type, func.count(PaymentEvent.id))
        .where(
            PaymentEvent.created_at >= cutoff,
            PaymentEvent.event_type.in_(_RECENT_STAT_EVENT_TYPES),
        )
        .group_by(PaymentEvent.event_type)
    ).tuples().all()
    counts: dict[str, int] = dict(rows)
    return RecentStats(
        window_hours=window_hours,
        verified=counts.get(_STAT_VERIFIED, 0),
        retry_scheduled=counts.get(_STAT_RETRY_SCHEDULED, 0),
        gateway_not_paid=counts.get(_STAT_GATEWAY_NOT_PAID, 0),
        transport_failed=counts.get(_STAT_TRANSPORT_FAILED, 0),
        exhausted=counts.get(_STAT_EXHAUSTED, 0),
    )


def build_reconciliation_status_snapshot(
    db: Session,
    settings: Settings,
    *,
    now_fn: NowFn = utcnow,
    stats_window_hours: int = 24,
) -> ReconciliationStatusSnapshot:
    """Build the full read-only reconciliation status snapshot.

    Never locks, never writes, never commits, never calls the gateway —
    safe to call at any time, including concurrently with the reconciliation
    worker. The single source both the human and ``--json`` CLI renderers
    format from — see ``app.cli``.
    """
    now = now_fn()
    return ReconciliationStatusSnapshot(
        generated_at=now,
        runtime=_runtime(db, settings, now),
        config=_config(settings),
        buckets=_buckets(db, settings, now),
        queue=_queue(db, settings, now),
        recent=_recent_stats(db, now, window_hours=stats_window_hours),
    )
