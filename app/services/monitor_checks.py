"""Read-only monitoring checks for app.monitor.

Every check here is a plain, bounded, side-effect-free read (except
check_public_ready, which makes one outbound HTTPS request to the
operator-configured public URL and mutates nothing locally). Nothing here
claims, locks, retries, or writes a Payment/PaymentEvent row, creates a
backup, or repairs a sequence. Checks reuse the SAME query primitives the
CLI and admin bot already trust (app.adminbot.queries,
app.services.reconciliation_status, app.services.stuck_payments,
app.ops.run_db_checks) rather than re-deriving their own SQL, so this module
can never quietly disagree with what those surfaces already report.
"""

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adminbot import queries
from app.config import Settings
from app.models import PaymentEvent
from app.services.notification import NowFn, utcnow
from app.services.reconciliation_status import build_reconciliation_status_snapshot

logger = logging.getLogger("app.services.monitor_checks")

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_CRITICAL = "critical"

# Ordering for "worst status wins" aggregation.
_SEVERITY_RANK = {STATUS_OK: 0, STATUS_WARNING: 1, STATUS_CRITICAL: 2}

_GATEWAY_FAILURE_EVENT_TYPES = ("centralpay_getlink_failed", "centralpay_verify_failed")
_BOT_FAILURE_EVENT_TYPE = "bot_notification_failed"
_BACKUP_GLOB = "centralpay-*.dump"

# Check keys that always run every cheap cycle (everything except
# db_integrity, which app.monitor gates on its own slower cadence).
DB_INTEGRITY_CHECK_KEY = "db_integrity"


@dataclass(frozen=True)
class CheckResult:
    key: str
    status: str  # ok | warning | critical
    reason: str
    # Safe, bounded, non-secret metadata only — the exact same contract as
    # AdminAlert.payload / MonitorIncident.details. Never a customer order
    # id, card fragment, redirect URL, signature, or credential.
    details: dict[str, Any] = field(default_factory=dict)


def worse(a: str, b: str) -> str:
    """The more severe of two statuses (critical > warning > ok)."""
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


def _round_or_none(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def check_public_ready(settings: Settings) -> CheckResult:
    """GET {PUBLIC_BASE_URL}/health/ready over the real internet — proving
    genuine external reachability, unlike the admin bot's internal-only
    probe. PUBLIC_BASE_URL is fixed, validated operator configuration
    (app.config.normalize_public_base_url): never user input, so this can
    never become an SSRF vector. Redirects are never followed."""
    url = f"{settings.public_base_url}/health/ready"
    timeout = httpx.Timeout(
        connect=settings.monitor_public_ready_connect_timeout_seconds,
        read=settings.monitor_public_ready_read_timeout_seconds,
        write=settings.monitor_public_ready_connect_timeout_seconds,
        pool=settings.monitor_public_ready_connect_timeout_seconds,
    )
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.get(url)
    except httpx.TimeoutException:
        return CheckResult("public_ready", STATUS_CRITICAL, "timeout", {"url": url})
    except httpx.TransportError as exc:
        # Covers connection refused/reset, DNS failure, and TLS/certificate
        # errors — httpx raises all of these as TransportError subclasses.
        return CheckResult(
            "public_ready",
            STATUS_CRITICAL,
            "connection_failed",
            {"url": url, "error": type(exc).__name__},
        )
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    if response.status_code != 200:
        return CheckResult(
            "public_ready",
            STATUS_CRITICAL,
            "unexpected_status",
            {"url": url, "status_code": response.status_code, "latency_ms": latency_ms},
        )
    try:
        body = response.json()
    except ValueError:
        return CheckResult(
            "public_ready",
            STATUS_CRITICAL,
            "malformed_response",
            {"url": url, "latency_ms": latency_ms},
        )
    if not isinstance(body, dict) or body.get("status") != "ready" or body.get("database") != "ok":
        return CheckResult(
            "public_ready",
            STATUS_CRITICAL,
            "unhealthy_response",
            {"url": url, "latency_ms": latency_ms},
        )
    return CheckResult(
        "public_ready", STATUS_OK, "healthy", {"url": url, "latency_ms": latency_ms}
    )


def check_database(db: Session) -> CheckResult:
    if queries.database_ok(db):
        return CheckResult("database", STATUS_OK, "healthy", {})
    return CheckResult("database", STATUS_CRITICAL, "query_failed", {})


def check_worker_heartbeat(
    db: Session, settings: Settings, *, worker_name: str, now: datetime
) -> CheckResult:
    key = f"worker_heartbeat:{worker_name}"
    heartbeat = queries.latest_worker_heartbeat(db, worker_name=worker_name)
    if heartbeat is None:
        return CheckResult(
            key, STATUS_CRITICAL, "no_heartbeat_recorded", {"worker_name": worker_name}
        )
    age_seconds = (now - _as_utc(heartbeat.last_heartbeat_at)).total_seconds()
    details = {
        "worker_name": worker_name,
        "age_seconds": round(age_seconds, 1),
        "last_error_code": heartbeat.last_error_code,
    }
    if age_seconds >= settings.monitor_worker_heartbeat_critical_seconds:
        return CheckResult(key, STATUS_CRITICAL, "heartbeat_stale", details)
    if age_seconds >= settings.monitor_worker_heartbeat_warning_seconds:
        return CheckResult(key, STATUS_WARNING, "heartbeat_aging", details)
    if heartbeat.last_error_code is not None:
        # The loop is alive (fresh heartbeat) but its most recent pass
        # failed -- record_worker_heartbeat clears last_error_code to None
        # on every SUCCESSFUL cycle, so a set value here means the latest
        # attempt did not complete cleanly. Age alone would report this
        # worker "ok" forever if every pass keeps failing.
        return CheckResult(key, STATUS_WARNING, "last_cycle_failed", details)
    return CheckResult(key, STATUS_OK, "healthy", details)


def check_notification_backlog(db: Session, settings: Settings, *, now: datetime) -> CheckResult:
    """ACTIVE notification work only: payments currently in
    bot_notify_pending. Excludes manual_review (own check below) and every
    resolved/accepted historical row."""
    count = queries.count_by_status(db, "bot_notify_pending")
    oldest_age = queries.oldest_pending_notification_age_seconds(db, now=now)
    details = {"count": count, "oldest_age_seconds": _round_or_none(oldest_age)}
    if count >= settings.monitor_notification_critical_count:
        return CheckResult("notification_backlog", STATUS_CRITICAL, "backlog_critical", details)
    if count >= settings.monitor_notification_warning_count or (
        oldest_age is not None and oldest_age >= settings.monitor_notification_max_age_seconds
    ):
        return CheckResult("notification_backlog", STATUS_WARNING, "backlog_warning", details)
    return CheckResult("notification_backlog", STATUS_OK, "healthy", details)


def check_manual_review(db: Session, settings: Settings, *, now: datetime) -> CheckResult:
    """Genuinely unresolved manual review only (queries.count_open_manual_
    reviews) — a row an operator has already resolved via `centralpay
    review resolve` is never counted, even though it keeps status=
    manual_review as history."""
    count = queries.count_open_manual_reviews(db)
    oldest_age = queries.oldest_open_manual_review_age_seconds(db, now=now)
    buckets = queries.open_manual_review_reason_buckets(db)
    details = {
        "count": count,
        "oldest_age_seconds": _round_or_none(oldest_age),
        "reasons": buckets,
    }
    if count >= settings.monitor_manual_review_critical_count:
        return CheckResult("manual_review", STATUS_CRITICAL, "backlog_critical", details)
    if count >= settings.monitor_manual_review_warning_count or (
        oldest_age is not None and oldest_age >= settings.monitor_manual_review_max_age_seconds
    ):
        return CheckResult("manual_review", STATUS_WARNING, "backlog_warning", details)
    return CheckResult("manual_review", STATUS_OK, "healthy", details)


def check_reconciliation(db: Session, settings: Settings, *, now: datetime) -> CheckResult:
    """Abnormal reconciliation conditions only. Ordinary gateway_not_paid +
    reconciliation_retry_scheduled activity (a payer simply hasn't paid
    yet) is never itself an incident — it never appears here. Worker
    liveness is reported by the separate worker_heartbeat:reconciliation-
    worker check, not duplicated here."""
    if not settings.reconciliation_enabled:
        return CheckResult("reconciliation", STATUS_OK, "disabled", {"enabled": False})
    snapshot = build_reconciliation_status_snapshot(db, settings, now_fn=lambda: now)
    exhausted = snapshot.queue.exhausted_not_aged_out
    oldest_due = snapshot.queue.oldest_due_age_seconds
    details = {
        "exhausted_not_aged_out": exhausted,
        "oldest_due_age_seconds": _round_or_none(oldest_due),
    }
    if exhausted > 0:
        return CheckResult(
            "reconciliation", STATUS_CRITICAL, "reconciliation_exhausted", details
        )
    # Backlog approaching the hard reconciliation lifetime without being
    # drained — "cannot be drained" per the roadmap, not "users haven't
    # paid yet."
    warn_threshold = settings.reconciliation_max_age_seconds * 0.8
    if oldest_due is not None and oldest_due >= warn_threshold:
        return CheckResult("reconciliation", STATUS_WARNING, "backlog_aging", details)
    return CheckResult("reconciliation", STATUS_OK, "healthy", details)


def check_backup(settings: Settings, *, now: datetime) -> CheckResult:
    """Newest backup with a validated .ok sidecar, under the SAME
    bind-mounted, read-only backup directory scripts/backup.sh writes to.
    Never creates, validates the CONTENTS of, or deletes a backup — purely
    a bounded directory listing + stat."""
    backup_dir = Path(settings.centralpay_backup_dir)
    try:
        candidates = [
            p
            for p in backup_dir.glob(_BACKUP_GLOB)
            if p.is_file() and p.with_name(p.name + ".ok").exists()
        ]
    except OSError as exc:
        return CheckResult(
            "backup",
            STATUS_CRITICAL,
            "backup_dir_unreadable",
            {"backup_dir": str(backup_dir), "error": type(exc).__name__},
        )
    if not candidates:
        return CheckResult(
            "backup", STATUS_CRITICAL, "no_valid_backup_found", {"backup_dir": str(backup_dir)}
        )
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    age_seconds = now.timestamp() - newest.stat().st_mtime
    details = {"newest_backup": newest.name, "age_seconds": round(age_seconds, 1)}
    if age_seconds >= settings.monitor_backup_critical_age_seconds:
        return CheckResult("backup", STATUS_CRITICAL, "backup_stale", details)
    if age_seconds >= settings.monitor_backup_warning_age_seconds:
        return CheckResult("backup", STATUS_WARNING, "backup_aging", details)
    return CheckResult("backup", STATUS_OK, "healthy", details)


def check_disk(settings: Settings) -> CheckResult:
    """Disk usage of the filesystem backing the backup directory. On this
    project's single-host Compose topology, PostgreSQL data, backups, and
    the application runtime normally share one host filesystem, so this one
    check covers all of them (see docker-compose.yml's monitor service —
    the same read-only bind mount the backup-freshness check above uses)."""
    path = settings.centralpay_backup_dir
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return CheckResult(
            "disk_space",
            STATUS_CRITICAL,
            "path_unreadable",
            {"path": path, "error": type(exc).__name__},
        )
    free_percent = (usage.free / usage.total * 100) if usage.total else 0.0
    details = {
        "path": path,
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "free_percent": round(free_percent, 1),
    }
    if (
        usage.free <= settings.monitor_disk_min_free_bytes
        or free_percent <= settings.monitor_disk_critical_percent
    ):
        return CheckResult("disk_space", STATUS_CRITICAL, "low_disk_space", details)
    if free_percent <= settings.monitor_disk_warning_percent:
        return CheckResult("disk_space", STATUS_WARNING, "low_disk_space", details)
    return CheckResult("disk_space", STATUS_OK, "healthy", details)


def check_db_integrity(db: Session) -> CheckResult:
    """Reuses app.ops.run_db_checks verbatim (repair_sequences=False,
    details=False) — never a second copy of the integrity SQL. Strictly
    read-only: with repair_sequences=False, run_db_checks never issues a
    write."""
    from app.ops import run_db_checks  # local import to avoid a cycle (app.ops -> app.cli)

    report = run_db_checks(db, repair_sequences=False, details=False)
    details = {"failures": report.get("failures", [])}
    if report.get("status") == "ok":
        return CheckResult(DB_INTEGRITY_CHECK_KEY, STATUS_OK, "healthy", details)
    return CheckResult(
        DB_INTEGRITY_CHECK_KEY, STATUS_CRITICAL, "integrity_failures_found", details
    )


def _affected_payments_in_window(
    db: Session, *, event_types: tuple[str, ...], window_seconds: int, now: datetime
) -> int:
    """COUNT(DISTINCT payment_id) of PaymentEvent rows of the given types in
    the trailing window — AFFECTED PAYMENTS, never raw attempt counts (a
    single payment retried 6 times must count once, not six times)."""
    cutoff = now - timedelta(seconds=window_seconds)
    return db.execute(
        select(func.count(func.distinct(PaymentEvent.payment_id))).where(
            PaymentEvent.event_type.in_(event_types),
            PaymentEvent.payment_id.is_not(None),
            PaymentEvent.created_at >= cutoff,
        )
    ).scalar_one()


def check_gateway_failure_burst(db: Session, settings: Settings, *, now: datetime) -> CheckResult:
    """centralpay_getlink_failed / centralpay_verify_failed only — genuine
    transport/protocol/server failures. Never gateway_not_paid /
    reconciliation_gateway_not_paid, which are ordinary "not paid yet"
    outcomes recorded under different event types entirely (see
    app.services.reconciliation / app.services.verification) and are
    deliberately excluded here."""
    window = settings.monitor_gateway_failure_window_seconds
    affected = _affected_payments_in_window(
        db, event_types=_GATEWAY_FAILURE_EVENT_TYPES, window_seconds=window, now=now
    )
    details = {"window_seconds": window, "affected_payments": affected}
    if affected >= settings.monitor_gateway_failure_critical_count:
        return CheckResult(
            "gateway_failure_burst", STATUS_CRITICAL, "gateway_failure_burst", details
        )
    if affected >= settings.monitor_gateway_failure_warning_count:
        return CheckResult(
            "gateway_failure_burst", STATUS_WARNING, "gateway_failure_burst", details
        )
    return CheckResult("gateway_failure_burst", STATUS_OK, "healthy", details)


def check_bot_failure_burst(db: Session, settings: Settings, *, now: datetime) -> CheckResult:
    """bot_notification_failed only (recorded once per failed DELIVERY
    ATTEMPT — see app.services.notification.record_attempt_result). A
    single stuck payment retried repeatedly is still ONE affected payment,
    never counted per-attempt."""
    window = settings.monitor_bot_failure_window_seconds
    affected = _affected_payments_in_window(
        db, event_types=(_BOT_FAILURE_EVENT_TYPE,), window_seconds=window, now=now
    )
    details = {"window_seconds": window, "affected_payments": affected}
    if affected >= settings.monitor_bot_failure_critical_count:
        return CheckResult("bot_failure_burst", STATUS_CRITICAL, "bot_failure_burst", details)
    if affected >= settings.monitor_bot_failure_warning_count:
        return CheckResult("bot_failure_burst", STATUS_WARNING, "bot_failure_burst", details)
    return CheckResult("bot_failure_burst", STATUS_OK, "healthy", details)


def run_all_checks(
    db: Session,
    settings: Settings,
    *,
    now_fn: NowFn = utcnow,
    include_db_integrity: bool = True,
) -> list[CheckResult]:
    """Run every cheap check, plus db_integrity when requested.

    app.monitor's background loop passes include_db_integrity=False on
    every cycle except its own slower cadence; the CLI/admin-bot `/monitor`
    surfaces (human-triggered, on demand) always pass True — see their
    call sites for why that split is safe."""
    now = now_fn()
    results = [
        check_public_ready(settings),
        check_database(db),
        check_worker_heartbeat(db, settings, worker_name="notification-worker", now=now),
        check_notification_backlog(db, settings, now=now),
        check_manual_review(db, settings, now=now),
        check_reconciliation(db, settings, now=now),
        check_backup(settings, now=now),
        check_disk(settings),
        check_gateway_failure_burst(db, settings, now=now),
        check_bot_failure_burst(db, settings, now=now),
    ]
    if settings.reconciliation_enabled:
        results.append(
            check_worker_heartbeat(db, settings, worker_name="reconciliation-worker", now=now)
        )
    if include_db_integrity:
        results.append(check_db_integrity(db))
    return results


def overall_status(results: list[CheckResult]) -> str:
    status = STATUS_OK
    for result in results:
        status = worse(status, result.status)
    return status
