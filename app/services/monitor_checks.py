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
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError
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

# centralpay_getlink_failed has no "ordinary outcome" variant: get_link()
# either returns a redirect URL or raises CentralPayError (see
# app.services.payments._create_payment_link / app.centralpay.get_link) --
# there is no code path where a payer's own choice produces this event, so
# every instance already is a genuine gateway/transport failure. No further
# filtering needed.
#
# centralpay_verify_failed is different: app.services.verification.
# verify_and_settle records it for TWO semantically different outcomes
# under the SAME event type, distinguished by data.stage --
# stage="transport" is a real CentralPayError (connection/protocol/HTTP
# failure calling verify.php), always an infrastructure signal.
#
# stage="gateway" means CentralPay answered with gateway_success=False, but
# that itself covers TWO different cases -- see app.centralpay.
# gateway_reason_code / CentralPayClient.verify, whose fixed-vocabulary
# `failure_reason` is recorded verbatim as data.reason:
#   - "gateway_rejected" (GATEWAY_REJECTED): body.success is explicitly
#     false, or body.status matches a known failure value -- CentralPay
#     unambiguously said "this payment wasn't successful". The payer simply
#     didn't complete or abandoned it -- an ordinary, expected,
#     high-frequency business outcome, never a gateway outage, no matter how
#     many payers it happens to at once.
#   - "gateway_error_field" (GATEWAY_ERROR_FIELD), "gateway_response_invalid"
#     (GATEWAY_RESPONSE_INVALID), "gateway_missing_data"
#     (GATEWAY_MISSING_DATA): CentralPay's response carried a dedicated
#     "error" field, or had NEITHER a clear success NOR a clear failure
#     marker at all -- unlike an explicit per-payment rejection, these are
#     CentralPay's verify API itself behaving abnormally (a service/
#     protocol-level error, or a malformed/incomplete body), genuine
#     infrastructure signals that must keep counting -- otherwise a
#     systemic verify-API outage that returns HTTP 200 with an error field
#     or a broken body for every payment would produce zero burst
#     incidents.
# Any other/missing reason for stage="gateway" is ambiguous and excluded,
# same as a row with no stage value at all.
_GATEWAY_FAILURE_TRANSPORT_STAGE = "transport"
_GATEWAY_FAILURE_GATEWAY_STAGE = "gateway"
_GATEWAY_FAILURE_AMBIGUOUS_REASONS = (
    "gateway_error_field",
    "gateway_response_invalid",
    "gateway_missing_data",
)
_GATEWAY_FAILURE_RELIABLE_PREDICATE = or_(
    PaymentEvent.event_type == "centralpay_getlink_failed",
    and_(
        PaymentEvent.event_type == "centralpay_verify_failed",
        or_(
            PaymentEvent.data["stage"].as_string() == _GATEWAY_FAILURE_TRANSPORT_STAGE,
            and_(
                PaymentEvent.data["stage"].as_string() == _GATEWAY_FAILURE_GATEWAY_STAGE,
                PaymentEvent.data["reason"].as_string().in_(_GATEWAY_FAILURE_AMBIGUOUS_REASONS),
            ),
        ),
    ),
)
_BOT_FAILURE_EVENT_TYPE = "bot_notification_failed"
_BACKUP_GLOB = "centralpay-*.dump"
# scripts/backup.sh's write_manifest() always writes exactly these keys,
# in this order, for a successfully validated backup -- including
# "validation" LAST, so a manifest truncated partway through writing (a
# crash/kill between lines) is missing it even though every key written
# before it is present. Any manifest missing one is either truncated,
# corrupted, or was never produced by that tooling at all.
_BACKUP_MANIFEST_REQUIRED_KEYS = ("backup_file", "sha256", "size_bytes", "validation")
_BACKUP_MANIFEST_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# A real write_manifest() sidecar is well under 300 bytes; this is generous
# headroom, not a realistic size. This check runs every monitoring pass
# specifically for backups it may already suspect are malformed/corrupted
# -- an accidentally (or deliberately) huge file placed at this path must
# never be read into memory in full.
_BACKUP_MANIFEST_MAX_BYTES = 4096

# Check keys that always run every cheap cycle (everything except
# db_integrity, which app.monitor gates on its own slower cadence).
DB_INTEGRITY_CHECK_KEY = "db_integrity"
# CheckResult.reason for a DB-dependent check that could not run because
# check_database (or an earlier DB-dependent check this same pass) already
# found PostgreSQL unavailable -- never itself the result of a query.
# Public (not underscore-prefixed): app.monitor.run_one_pass compares
# against this to tell whether db_integrity actually executed this pass,
# as opposed to being placeholdered by an outage -- see its docstring.
DB_UNAVAILABLE_REASON = "database_unavailable"


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
    db: Session,
    settings: Settings,
    *,
    worker_name: str,
    poll_interval_seconds: float,
    now: datetime,
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
    # Effective cutoffs scale with THIS worker's own polling interval --
    # same reasoning as docker-compose.yml's monitor healthcheck and
    # reconciliation_status._runtime's heartbeat_fresh (both
    # max(interval * 6, floor)) -- so a successful worker configured with a
    # longer-than-default interval is never falsely reported stale on every
    # single cycle. Identical to the fixed configured values for every
    # worker's own default interval; only loosens when an operator
    # configures something slower.
    warning_cutoff = max(
        settings.monitor_worker_heartbeat_warning_seconds, poll_interval_seconds * 6
    )
    critical_cutoff = max(settings.monitor_worker_heartbeat_critical_seconds, warning_cutoff * 3)
    if age_seconds >= critical_cutoff:
        return CheckResult(key, STATUS_CRITICAL, "heartbeat_stale", details)
    if age_seconds >= warning_cutoff:
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
    worker check, not duplicated here.

    Exhaustion answers "is reconciliation unhealthy now, or has there been
    a recent unresolved operational exhaustion event" — never "has any
    payment in this database's history ever exhausted retries". A historical
    backlog of long-aged-out, long-untouched exhausted payments must not
    keep an otherwise-healthy system permanently critical; see
    ReconciliationStatusSnapshot.queue.exhausted_recent /
    exhausted_not_aged_out / exhausted_historical_total below."""
    if not settings.reconciliation_enabled:
        return CheckResult("reconciliation", STATUS_OK, "disabled", {"enabled": False})
    snapshot = build_reconciliation_status_snapshot(db, settings, now_fn=lambda: now)
    # Two populations drive CRITICAL, both bounded to "current/actionable",
    # never the unbounded all-time historical total:
    #   * exhausted_not_aged_out -- still within the reconciliation lifetime
    #     (age < max_age), so inherently bounded to at most max_age_seconds
    #     old; always alarms regardless of how recently it happened.
    #   * exhausted_recent -- aged-out-inclusive (a payment reconciliation
    #     already gave up on must never falsely "resolve" just because more
    #     time also passed and it crossed the age boundary too -- that made
    #     it MORE stuck, not less) but bounded to a recent operational
    #     window (monitor_reconciliation_exhausted_recent_window_seconds),
    #     so an old backlog whose last attempt was long ago eventually stops
    #     keeping an otherwise-healthy system permanently critical.
    # exhausted_historical_total (unbounded, all-time) is reported
    # separately for operator context only and never drives severity here.
    actionable_exhausted = snapshot.queue.exhausted_not_aged_out
    recent_exhausted = snapshot.queue.exhausted_recent
    oldest_overdue = snapshot.queue.oldest_overdue_seconds
    details = {
        "exhausted_not_aged_out": actionable_exhausted,
        "exhausted_recent": recent_exhausted,
        "exhausted_historical_total": snapshot.queue.exhausted_historical_total,
        "oldest_overdue_seconds": _round_or_none(oldest_overdue),
    }
    if actionable_exhausted > 0 or recent_exhausted > 0:
        return CheckResult(
            "reconciliation", STATUS_CRITICAL, "reconciliation_exhausted", details
        )
    # A due row waiting this long since it became eligible (NOT how old its
    # link is -- a payer simply not having paid yet never grows this value)
    # means the worker is falling behind its own schedule and the queue is
    # failing to drain, which is the actual "cannot be drained" condition
    # the roadmap asks for. Scaled off the slow tier's re-poll interval
    # (the larger of the two, so it dominates oldest_overdue_seconds) with
    # the same floor app.services.reconciliation_status._runtime already
    # uses for "is polling keeping up".
    warn_threshold = max(settings.reconciliation_slow_interval_seconds * 5, 300)
    if oldest_overdue is not None and oldest_overdue >= warn_threshold:
        return CheckResult("reconciliation", STATUS_WARNING, "backlog_aging", details)
    return CheckResult("reconciliation", STATUS_OK, "healthy", details)


def _parse_backup_manifest(path: Path) -> dict[str, str] | None:
    """Parse scripts/backup.sh's ``write_manifest`` sidecar: a plain
    line-oriented ``key=value`` text file. Returns None (never raises) for
    anything unreadable or not in that shape -- callers treat that
    identically to a missing manifest. Only ever reads this small (~200
    byte) sidecar, never the dump file itself.

    Deliberately as strict as scripts/centralpay's OWN restore-side
    manifest read (``grep -E '^sha256=' | head -1 | cut -d= -f2``, no
    trimming): a value's surrounding whitespace is never stripped here
    either, so a corrupted/tampered field that restore's raw extraction
    would choke on (e.g. a checksum with trailing whitespace) fails this
    check's shape validation too, rather than being silently cleaned up
    and reported healthy for an archive restore would actually refuse. A
    genuine ``write_manifest`` output never repeats a key; a duplicate is
    corruption or a forged second value, and -- since restore reads only
    the FIRST occurrence -- silently picking either the first or the last
    here could make this check disagree with what restore would actually
    accept. Rejected outright instead.

    Reads at most ``_BACKUP_MANIFEST_MAX_BYTES`` regardless of the file's
    actual size -- this check runs every monitoring pass specifically for
    backups it may already suspect are malformed, so an accidentally (or
    deliberately) huge file at this path must never be loaded into memory
    in full; it is simply rejected as malformed instead."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_BACKUP_MANIFEST_MAX_BYTES + 1)
        if len(raw) > _BACKUP_MANIFEST_MAX_BYTES:
            return None
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fields: dict[str, str] = {}
    # Split on "\n" only -- NOT str.splitlines(), which also treats a bare
    # "\r" and "\r\n" as line boundaries and silently discards them.
    # scripts/centralpay's restore-side extraction (grep/cut) does no such
    # normalization: a CRLF-corrupted manifest leaves the "\r" as part of
    # the value there, so a checksum with a trailing "\r" fails restore's
    # comparison. splitlines() would have hidden that same "\r" here,
    # letting this check certify a manifest restore would reject.
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            continue  # blank line: skipped, but never strips a real value
        key, sep, value = raw_line.partition("=")
        if not sep or key in fields:
            return None
        fields[key] = value
    return fields


def _backup_manifest_issue(dump: Path, dump_size: int) -> str | None:
    """None when dump's ``.manifest`` sidecar is present, well-formed, and
    consistent with the dump file itself; otherwise a short machine-
    readable reason.

    Deliberately cheap, metadata-only validation: parses the small text
    sidecar and compares it against the dump's own name/size (already
    known from a stat the caller already did). NEVER reads or hashes the
    dump's own bytes -- byte-level checksum verification is the canonical
    backup tooling's job (scripts/backup.sh's validate_archive at creation
    time, and restore-time verification), not this periodic monitor's; a
    multi-hundred-MB pg_dump hashed every MONITOR_INTERVAL_SECONDS would
    make the monitor itself a load problem. Only the checksum field's
    SHAPE is validated here, never its correctness against the actual
    bytes.

    Never constructs a filesystem path from manifest-supplied data (only a
    plain string comparison against the already-known dump filename) --
    no path traversal, no unsafe filename interpretation."""
    manifest_path = dump.with_name(dump.name + ".manifest")
    # is_symlink() checked separately (and first): Path.is_file() FOLLOWS
    # symlinks, so a manifest that is actually a symlink to some other
    # regular file would otherwise pass straight through. scripts/
    # centralpay's own restore-side check explicitly requires `! -L
    # "$manifest"` and treats a symlinked manifest as equivalent to no
    # manifest at all (falling back to the interactive LEGACY path) --
    # matched here rather than letting this check certify a manifest
    # restore itself would refuse to trust.
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return "manifest_missing"
    fields = _parse_backup_manifest(manifest_path)
    if fields is None or any(key not in fields for key in _BACKUP_MANIFEST_REQUIRED_KEYS):
        return "manifest_malformed"
    if fields["backup_file"] != dump.name:
        return "manifest_filename_mismatch"
    if not _BACKUP_MANIFEST_SHA256_RE.match(fields["sha256"]):
        return "manifest_checksum_shape_invalid"
    try:
        manifest_size = int(fields["size_bytes"])
    except ValueError:
        return "manifest_malformed"
    if manifest_size != dump_size:
        return "manifest_size_mismatch"
    if fields["validation"] != "passed":
        return "manifest_malformed"
    return None


def check_backup(settings: Settings, *, now: datetime) -> CheckResult:
    """Newest backup with a validated .ok sidecar, under the SAME
    bind-mounted, read-only backup directory scripts/backup.sh writes to.
    Never creates, validates the byte CONTENTS of, or deletes a backup --
    a bounded directory listing + stat, plus a cheap parse of the small
    .manifest metadata sidecar (see _backup_manifest_issue)."""
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
    try:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        newest_stat = newest.stat()
        manifest_issue = _backup_manifest_issue(newest, newest_stat.st_size)
    except OSError as exc:
        # A filesystem race (file removed between the glob() listing above
        # and this stat) or a permission error stat() doesn't ignore (e.g.
        # EACCES) -- still never allowed to raise out of a check that must
        # keep running during a database outage (see run_all_checks).
        return CheckResult(
            "backup",
            STATUS_CRITICAL,
            "backup_dir_unreadable",
            {"backup_dir": str(backup_dir), "error": type(exc).__name__},
        )
    if manifest_issue is not None:
        # Missing/malformed/inconsistent manifest metadata means the
        # recoverability evidence for the newest backup is incomplete --
        # never reported as healthy, regardless of the backup's age.
        return CheckResult(
            "backup",
            STATUS_CRITICAL,
            "backup_manifest_invalid",
            {"newest_backup": newest.name, "manifest_issue": manifest_issue},
        )
    age_seconds = now.timestamp() - newest_stat.st_mtime
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
    db: Session, *, predicate: Any, window_seconds: int, now: datetime
) -> int:
    """COUNT(DISTINCT payment_id) of PaymentEvent rows matching `predicate`
    in the trailing window — AFFECTED PAYMENTS, never raw attempt counts (a
    single payment retried 6 times must count once, not six times)."""
    cutoff = now - timedelta(seconds=window_seconds)
    return db.execute(
        select(func.count(func.distinct(PaymentEvent.payment_id))).where(
            predicate,
            PaymentEvent.payment_id.is_not(None),
            PaymentEvent.created_at >= cutoff,
        )
    ).scalar_one()


def check_gateway_failure_burst(db: Session, settings: Settings, *, now: datetime) -> CheckResult:
    """Reliable gateway/transport failures only — see
    _GATEWAY_FAILURE_RELIABLE_PREDICATE for exactly which event shapes
    count and why. Never gateway_not_paid / reconciliation_gateway_not_paid
    (different event types entirely) or a centralpay_verify_failed row
    whose stage shows CentralPay simply answered "not successful" for a
    specific payment — both are ordinary, expected outcomes, not gateway
    outages, and are deliberately excluded here."""
    window = settings.monitor_gateway_failure_window_seconds
    affected = _affected_payments_in_window(
        db, predicate=_GATEWAY_FAILURE_RELIABLE_PREDICATE, window_seconds=window, now=now
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
        db,
        predicate=PaymentEvent.event_type == _BOT_FAILURE_EVENT_TYPE,
        window_seconds=window,
        now=now,
    )
    details = {"window_seconds": window, "affected_payments": affected}
    if affected >= settings.monitor_bot_failure_critical_count:
        return CheckResult("bot_failure_burst", STATUS_CRITICAL, "bot_failure_burst", details)
    if affected >= settings.monitor_bot_failure_warning_count:
        return CheckResult("bot_failure_burst", STATUS_WARNING, "bot_failure_burst", details)
    return CheckResult("bot_failure_burst", STATUS_OK, "healthy", details)


def _db_unavailable_result(key: str) -> CheckResult:
    """Placeholder for a DB-dependent check that could not run this pass
    because PostgreSQL itself is unavailable. Reuses the existing
    STATUS_CRITICAL vocabulary rather than adding a new status value, so
    every existing caller (the CLI, the admin bot's /monitor command, and
    app.monitor's own incident-recording loop) keeps working against the
    same three-state CheckResult contract."""
    return CheckResult(key, STATUS_CRITICAL, DB_UNAVAILABLE_REASON, {"dependency": "database"})


def _rollback_after_db_failure(db: Session) -> None:
    # Best-effort: if the connection itself is gone, rollback() can raise
    # too. Either way, no further DB-dependent query is attempted this
    # pass -- this is defense in depth, not a requirement for correctness.
    try:
        db.rollback()
    except Exception:
        logger.warning("monitor_session_rollback_failed")


def _build_db_dependent_checks(
    db: Session, settings: Settings, *, now: datetime, include_db_integrity: bool
) -> list[tuple[str, Callable[[], CheckResult]]]:
    """Every check that queries `db`, as (key, thunk) pairs in the order
    they should run. Building this list never itself issues a query --
    each thunk is a lambda, so run_all_checks can inspect the key set
    (e.g. to fill in database_unavailable placeholders) without running
    any of them."""
    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        (
            "worker_heartbeat:notification-worker",
            lambda: check_worker_heartbeat(
                db,
                settings,
                worker_name="notification-worker",
                poll_interval_seconds=settings.bot_notify_worker_interval_seconds,
                now=now,
            ),
        ),
        ("notification_backlog", lambda: check_notification_backlog(db, settings, now=now)),
        ("manual_review", lambda: check_manual_review(db, settings, now=now)),
        ("gateway_failure_burst", lambda: check_gateway_failure_burst(db, settings, now=now)),
        ("bot_failure_burst", lambda: check_bot_failure_burst(db, settings, now=now)),
    ]
    if settings.reconciliation_enabled:
        # check_reconciliation only queries `db` when reconciliation is
        # enabled -- disabled, it short-circuits to ok/disabled without
        # touching the session at all, so it belongs in the DB-INDEPENDENT
        # results built by run_all_checks instead (see there), never here.
        checks.append(("reconciliation", lambda: check_reconciliation(db, settings, now=now)))
        checks.append(
            (
                "worker_heartbeat:reconciliation-worker",
                lambda: check_worker_heartbeat(
                    db,
                    settings,
                    worker_name="reconciliation-worker",
                    poll_interval_seconds=settings.reconciliation_interval_seconds,
                    now=now,
                ),
            )
        )
    if settings.admin_bot_enabled:
        # The admin bot's own delivery loop (app.adminbot.runner) has no
        # OTHER visibility to this dedicated monitor -- its container
        # liveness heartbeat file lives in its own tmpfs, and it writes no
        # database row unless it heartbeats here. Enabled-only, exactly
        # like reconciliation-worker above: when the admin bot is
        # disabled, no delivery loop runs at all, so "no heartbeat" would
        # be a false critical rather than a real signal.
        checks.append(
            (
                "worker_heartbeat:admin-bot-delivery",
                lambda: check_worker_heartbeat(
                    db,
                    settings,
                    worker_name="admin-bot-delivery",
                    poll_interval_seconds=settings.admin_bot_alert_poll_interval_seconds,
                    now=now,
                ),
            )
        )
    if include_db_integrity:
        checks.append((DB_INTEGRITY_CHECK_KEY, lambda: check_db_integrity(db)))
    return checks


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
    call sites for why that split is safe.

    DB-INDEPENDENT checks (public_ready, backup, disk_space, and
    reconciliation specifically WHEN settings.reconciliation_enabled is
    False -- check_reconciliation short-circuits to ok/disabled without
    touching `db` in that case) always run, even when PostgreSQL itself is
    unavailable — they read one outbound HTTPS URL and the filesystem
    (or nothing at all), never this function's `db` session. Every other
    check is DB-DEPENDENT. If the initial `database` probe
    already reports critical, or if a later DB-dependent check raises a
    genuine connectivity failure mid-pass (a DBAPIError whose
    connection_invalidated flag is True — SQLAlchemy's own signal that the
    underlying connection itself is dead, e.g. a real disconnect/
    OperationalError, not just any query error), no further SQL is
    attempted on this session for the rest of this pass — every check that
    had to be skipped gets a critical/database_unavailable placeholder
    instead. Only that specific class of failure degrades this way: a
    query/schema-level defect on an otherwise-live connection (e.g. a
    ProgrammingError from a genuine bug) or a non-database bug (e.g. a
    ValueError) is deliberately NOT caught here and propagates normally,
    so it is never mislabeled as a fabricated PostgreSQL outage — see
    app.monitor.run_forever's own monitor_pass_failed handling for that
    case. A genuine PostgreSQL outage must never make this function raise:
    every caller (the CLI, the admin bot's /monitor command, and
    app.monitor's own loop) always gets back a complete, structured result
    list, even mid-outage."""
    now = now_fn()
    results = [check_public_ready(settings), check_backup(settings, now=now), check_disk(settings)]
    if not settings.reconciliation_enabled:
        results.append(check_reconciliation(db, settings, now=now))

    db_result = check_database(db)
    results.append(db_result)

    db_dependent = _build_db_dependent_checks(
        db, settings, now=now, include_db_integrity=include_db_integrity
    )

    if db_result.status != STATUS_OK:
        _rollback_after_db_failure(db)
        results.extend(_db_unavailable_result(key) for key, _ in db_dependent)
        return results

    for index, (key, thunk) in enumerate(db_dependent):
        try:
            results.append(thunk())
        except DBAPIError as exc:
            if not exc.connection_invalidated:
                # A real SQL/schema-level defect (e.g. ProgrammingError from
                # a bad query) on an otherwise-live connection -- NOT a
                # connectivity failure. Propagate normally so it is never
                # mislabeled a fabricated PostgreSQL outage; app.monitor's
                # own monitor_pass_failed handling (or the CLI/admin-bot
                # caller) sees the real exception.
                raise
            logger.warning(
                "monitor_check_query_failed", extra={"check": key, "error": type(exc).__name__}
            )
            _rollback_after_db_failure(db)
            results.append(_db_unavailable_result(key))
            results.extend(_db_unavailable_result(k) for k, _ in db_dependent[index + 1 :])
            break
    return results


def overall_status(results: list[CheckResult]) -> str:
    status = STATUS_OK
    for result in results:
        status = worse(status, result.status)
    return status
