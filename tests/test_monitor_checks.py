"""Unit tests for app.services.monitor_checks (SQLite; no network)."""

import itertools
import os
import shutil
import time
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker

from app.adminbot import queries
from app.audit import record_event
from app.models import Payment, WorkerHeartbeat
from app.services import monitor_checks

_counter = itertools.count(1)


def _seed_alembic_version(session_factory, revision: str = "test_revision") -> None:
    """The SQLite unit-test schema (Base.metadata.create_all) has no
    alembic_version table, so db-check's alembic_revision check always fails
    on this fixture regardless of payment data (see
    tests/test_db_check_details.py, which seeds the same fake row)."""
    with session_factory() as db:
        db.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        db.execute(text("DELETE FROM alembic_version"))
        db.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": revision}
        )
        db.commit()


def _make_payment(session_factory, *, status: str, amount: int = 10000, **kwargs) -> Payment:
    n = next(_counter)
    gateway_verified_at = kwargs.pop("gateway_verified_at", None)
    if status in ("bot_notify_pending", "bot_notify_accepted") and gateway_verified_at is None:
        gateway_verified_at = datetime.now(UTC)
    payment = Payment(
        bot_order_id=f"mon-{n}",
        gateway_order_id=900_000 + n,
        gateway_user_id=1000 + n,
        amount=amount,
        fee_rate_bps=0,
        fee_amount=0,
        payable_amount=amount,
        status=status,
        gateway_verified_at=gateway_verified_at,
        **kwargs,
    )
    with session_factory() as db:
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("invalid json")
        return self._json_body


class _FakeClient:
    def __init__(self, *, response=None, exc=None):
        self._response = response
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url):
        if self._exc is not None:
            raise self._exc
        return self._response


def _patch_httpx_client(monkeypatch, client) -> None:
    # Patches the real httpx module (the same object app.services.monitor_checks
    # imported), never a re-export through monitor_checks itself.
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)


# --- public_ready ------------------------------------------------------


def test_public_ready_healthy(settings, monkeypatch):
    _patch_httpx_client(
        monkeypatch, _FakeClient(response=_FakeResponse(200, {"status": "ready", "database": "ok"}))
    )
    result = monitor_checks.check_public_ready(settings)
    assert result.status == "ok"
    assert "latency_ms" in result.details


def test_public_ready_timeout(settings, monkeypatch):
    _patch_httpx_client(monkeypatch, _FakeClient(exc=httpx.TimeoutException("timeout")))
    result = monitor_checks.check_public_ready(settings)
    assert result.status == "critical"
    assert result.reason == "timeout"


def test_public_ready_connection_failed(settings, monkeypatch):
    _patch_httpx_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("boom")))
    result = monitor_checks.check_public_ready(settings)
    assert result.status == "critical"
    assert result.reason == "connection_failed"


def test_public_ready_unexpected_status(settings, monkeypatch):
    _patch_httpx_client(monkeypatch, _FakeClient(response=_FakeResponse(503)))
    result = monitor_checks.check_public_ready(settings)
    assert result.status == "critical"
    assert result.reason == "unexpected_status"


def test_public_ready_malformed_response(settings, monkeypatch):
    _patch_httpx_client(monkeypatch, _FakeClient(response=_FakeResponse(200, None)))
    result = monitor_checks.check_public_ready(settings)
    assert result.status == "critical"
    assert result.reason == "malformed_response"


def test_public_ready_unhealthy_response(settings, monkeypatch):
    _patch_httpx_client(
        monkeypatch, _FakeClient(response=_FakeResponse(200, {"status": "degraded"}))
    )
    result = monitor_checks.check_public_ready(settings)
    assert result.status == "critical"
    assert result.reason == "unhealthy_response"


# --- database ------------------------------------------------------------


def test_database_ok(session_factory):
    with session_factory() as db:
        result = monitor_checks.check_database(db)
    assert result.status == "ok"


def test_database_unavailable(session_factory, monkeypatch):
    monkeypatch.setattr(queries, "database_ok", lambda db: False)
    with session_factory() as db:
        result = monitor_checks.check_database(db)
    assert result.status == "critical"


# --- worker heartbeat ------------------------------------------------------


def test_worker_heartbeat_fresh(session_factory, settings):
    now = datetime.now(UTC)
    with session_factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_name="notification-worker", instance_id="w1", last_heartbeat_at=now
            )
        )
        db.commit()
        result = monitor_checks.check_worker_heartbeat(
            db,
            settings,
            worker_name="notification-worker",
            poll_interval_seconds=settings.bot_notify_worker_interval_seconds,
            now=now,
        )
    assert result.status == "ok"


def test_worker_heartbeat_stale(session_factory, settings):
    now = datetime.now(UTC)
    old = now - timedelta(seconds=settings.monitor_worker_heartbeat_critical_seconds + 10)
    with session_factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_name="notification-worker", instance_id="w1", last_heartbeat_at=old
            )
        )
        db.commit()
        result = monitor_checks.check_worker_heartbeat(
            db,
            settings,
            worker_name="notification-worker",
            poll_interval_seconds=settings.bot_notify_worker_interval_seconds,
            now=now,
        )
    assert result.status == "critical"
    assert result.reason == "heartbeat_stale"


def test_worker_heartbeat_cutoffs_scale_with_a_slower_poll_interval(session_factory, settings):
    """A worker configured with a longer-than-default polling interval must
    not be falsely reported stale on every single cycle just because the
    FIXED default cutoffs (60s/180s) are shorter than how long it
    legitimately sleeps between successful passes."""
    now = datetime.now(UTC)
    # 200s old: well past the fixed 60s/180s cutoffs, but well within one
    # normal cycle of a worker polling every 300s.
    old = now - timedelta(seconds=200)
    with session_factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_name="notification-worker", instance_id="w1", last_heartbeat_at=old
            )
        )
        db.commit()
        result = monitor_checks.check_worker_heartbeat(
            db,
            settings,
            worker_name="notification-worker",
            poll_interval_seconds=300.0,
            now=now,
        )
    assert result.status == "ok"


def test_worker_heartbeat_missing(session_factory, settings):
    with session_factory() as db:
        result = monitor_checks.check_worker_heartbeat(
            db,
            settings,
            worker_name="notification-worker",
            poll_interval_seconds=settings.bot_notify_worker_interval_seconds,
            now=datetime.now(UTC),
        )
    assert result.status == "critical"
    assert result.reason == "no_heartbeat_recorded"


def test_worker_heartbeat_fresh_but_last_cycle_failed(session_factory, settings):
    """The loop is alive (a heartbeat was just written) but its most recent
    pass raised -- record_worker_heartbeat only clears last_error_code on a
    SUCCESSFUL cycle, so a fresh-but-failing worker must not report "ok"
    forever just because age alone looks healthy."""
    now = datetime.now(UTC)
    with session_factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_name="notification-worker",
                instance_id="w1",
                last_heartbeat_at=now,
                last_error_code="RuntimeError",
            )
        )
        db.commit()
        result = monitor_checks.check_worker_heartbeat(
            db,
            settings,
            worker_name="notification-worker",
            poll_interval_seconds=settings.bot_notify_worker_interval_seconds,
            now=now,
        )
    assert result.status == "warning"
    assert result.reason == "last_cycle_failed"


# --- notification backlog ---------------------------------------------------


def test_notification_backlog_healthy(session_factory, settings):
    with session_factory() as db:
        result = monitor_checks.check_notification_backlog(db, settings, now=datetime.now(UTC))
    assert result.status == "ok"


def test_notification_backlog_warning_threshold(session_factory, settings):
    low = settings.model_copy(
        update={"monitor_notification_warning_count": 1, "monitor_notification_critical_count": 5}
    )
    _make_payment(session_factory, status="bot_notify_pending")
    with session_factory() as db:
        result = monitor_checks.check_notification_backlog(db, low, now=datetime.now(UTC))
    assert result.status == "warning"


def test_notification_backlog_critical_threshold(session_factory, settings):
    low = settings.model_copy(
        update={"monitor_notification_warning_count": 1, "monitor_notification_critical_count": 1}
    )
    _make_payment(session_factory, status="bot_notify_pending")
    with session_factory() as db:
        result = monitor_checks.check_notification_backlog(db, low, now=datetime.now(UTC))
    assert result.status == "critical"


def test_notification_backlog_old_single_notification(session_factory, settings):
    tight = settings.model_copy(
        update={
            "monitor_notification_max_age_seconds": 10,
            "monitor_notification_warning_count": 100,
        }
    )
    old_time = datetime.now(UTC) - timedelta(hours=1)
    _make_payment(session_factory, status="bot_notify_pending", gateway_verified_at=old_time)
    with session_factory() as db:
        result = monitor_checks.check_notification_backlog(db, tight, now=datetime.now(UTC))
    assert result.status == "warning"
    assert result.details["count"] == 1


# --- manual review -----------------------------------------------------------


def test_manual_review_unresolved_counted(session_factory, settings):
    low = settings.model_copy(update={"monitor_manual_review_warning_count": 1})
    _make_payment(
        session_factory,
        status="manual_review",
        manual_review_at=datetime.now(UTC),
        bot_notify_reason="retry_limit_reached",
    )
    with session_factory() as db:
        result = monitor_checks.check_manual_review(db, low, now=datetime.now(UTC))
    assert result.status == "warning"
    assert result.details["count"] == 1
    assert result.details["reasons"] == {"retry_limit_reached": 1}


def test_manual_review_resolved_excluded(session_factory, settings):
    low = settings.model_copy(update={"monitor_manual_review_warning_count": 1})
    now = datetime.now(UTC)
    _make_payment(
        session_factory,
        status="manual_review",
        manual_review_at=now,
        review_resolved_at=now,
        bot_notify_reason="retry_limit_reached",
    )
    with session_factory() as db:
        result = monitor_checks.check_manual_review(db, low, now=now)
    assert result.status == "ok"
    assert result.details["count"] == 0


# --- reconciliation ------------------------------------------------------


def test_reconciliation_exhaustion_detected(session_factory, settings):
    now = datetime.now(UTC)
    recon = settings.model_copy(update={"reconciliation_max_attempts": 3})
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        callback_token_issued_at=now - timedelta(seconds=100),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.reason == "reconciliation_exhausted"


def test_reconciliation_exhausted_stays_critical_after_aging_out(session_factory, settings):
    """An exhausted payment must not silently "recover" just because more
    time also passed and it crossed the age boundary too -- it is now
    MORE stuck (permanently excluded from automatic reconciliation, since
    aged-out rows are dropped from every due tier), not less, and must
    stay a critical incident until an operator resolves it."""
    now = datetime.now(UTC)
    recon = settings.model_copy(update={"reconciliation_max_attempts": 3})
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        callback_token_issued_at=now
        - timedelta(seconds=recon.reconciliation_max_age_seconds + 100),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.reason == "reconciliation_exhausted"


def test_reconciliation_gateway_not_paid_is_not_an_incident(session_factory, settings):
    now = datetime.now(UTC)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=1,
        reconciliation_next_at=now + timedelta(seconds=10),
        reconciliation_last_error_code="gateway_not_paid",
        callback_token_issued_at=now - timedelta(seconds=30),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, settings, now=now)
    assert result.status == "ok"


def test_reconciliation_old_link_alone_is_not_a_backlog(session_factory, settings):
    """A payer who simply hasn't paid in a while must never trip the
    backlog warning on its own -- only actual failure to drain the queue
    (a due row waiting long past when it became eligible) should. This
    payment's LINK is old (past 80% of the old link-age threshold this
    check used to key off) but it just became due THIS instant, so it has
    not been waiting at all."""
    now = datetime.now(UTC)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=1,
        reconciliation_next_at=now,
        callback_token_issued_at=now - timedelta(minutes=100),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, settings, now=now)
    assert result.status == "ok"


def test_reconciliation_stalled_due_row_is_a_backlog(session_factory, settings):
    """A due row that has been waiting since well before it became
    eligible -- the worker is falling behind its own schedule -- IS a
    real backlog, independent of how old the underlying link happens to
    be."""
    now = datetime.now(UTC)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=1,
        reconciliation_next_at=now - timedelta(minutes=30),
        callback_token_issued_at=now - timedelta(minutes=60),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, settings, now=now)
    assert result.status == "warning"
    assert result.reason == "backlog_aging"


def test_reconciliation_disabled_is_healthy(session_factory, settings):
    disabled = settings.model_copy(update={"reconciliation_enabled": False})
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, disabled, now=datetime.now(UTC))
    assert result.status == "ok"
    assert result.reason == "disabled"


# --- reconciliation exhaustion: bounded recency (permanent-CRITICAL fix) -


def test_reconciliation_ancient_historical_backlog_does_not_stay_critical(
    session_factory, settings
):
    """A payment that exhausted retries long ago (both outside the recent
    window AND aged out) must NOT keep an otherwise-healthy system
    permanently critical -- it becomes a purely historical/informational
    count, never a CRITICAL driver. This is the production incident: 2126
    ancient exhausted rows must not poison current health forever."""
    now = datetime.now(UTC)
    recon = settings.model_copy(update={"reconciliation_max_attempts": 3})
    ancient = now - timedelta(days=30)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=ancient,
        callback_token_issued_at=ancient,
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "ok"
    assert result.details["exhausted_not_aged_out"] == 0
    assert result.details["exhausted_recent"] == 0
    assert result.details["exhausted_historical_total"] == 1


def test_reconciliation_new_exhaustion_triggers_critical(session_factory, settings):
    now = datetime.now(UTC)
    recon = settings.model_copy(update={"reconciliation_max_attempts": 3})
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=now,
        callback_token_issued_at=now - timedelta(seconds=100),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.reason == "reconciliation_exhausted"
    assert result.details["exhausted_recent"] == 1


def test_reconciliation_recent_exhaustion_stays_critical_just_inside_window(
    session_factory, settings
):
    """Exact boundary: last attempt 1 second INSIDE the recent window (and
    the link has ALSO aged out) must still alarm."""
    now = datetime.now(UTC)
    recon = settings.model_copy(
        update={
            "reconciliation_max_attempts": 3,
            "monitor_reconciliation_exhausted_recent_window_seconds": 3600,
        }
    )
    last_at = now - timedelta(seconds=3599)
    aged_out_at = now - timedelta(seconds=recon.reconciliation_max_age_seconds + 100)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=last_at,
        callback_token_issued_at=aged_out_at,
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.details["exhausted_not_aged_out"] == 0
    assert result.details["exhausted_recent"] == 1


def test_reconciliation_exhaustion_recovers_just_outside_window(session_factory, settings):
    """Exact boundary: last attempt 1 second OUTSIDE the recent window (and
    the link has aged out, and no other exhausted/actionable rows exist)
    must recover to ok -- this is the "bounded operational period" the
    monitor design requires."""
    now = datetime.now(UTC)
    recon = settings.model_copy(
        update={
            "reconciliation_max_attempts": 3,
            "monitor_reconciliation_exhausted_recent_window_seconds": 3600,
        }
    )
    last_at = now - timedelta(seconds=3601)
    aged_out_at = now - timedelta(seconds=recon.reconciliation_max_age_seconds + 100)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=last_at,
        callback_token_issued_at=aged_out_at,
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "ok"
    assert result.details["exhausted_recent"] == 0
    assert result.details["exhausted_historical_total"] == 1


def test_reconciliation_actionable_exhausted_alarms_even_outside_window(
    session_factory, settings
):
    """exhausted_not_aged_out (still within the reconciliation lifetime)
    always alarms, even if an operator configures a very short recent
    window that would otherwise exclude it -- an actionable, currently
    still-payable exhausted payment must never be silenced by this
    setting."""
    now = datetime.now(UTC)
    recon = settings.model_copy(
        update={
            "reconciliation_max_attempts": 3,
            "monitor_reconciliation_exhausted_recent_window_seconds": 1,
        }
    )
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=now - timedelta(seconds=100),
        callback_token_issued_at=now - timedelta(seconds=100),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.details["exhausted_not_aged_out"] == 1


def test_reconciliation_exhausted_null_last_at_treated_as_recent(session_factory, settings):
    """A directly-constructed exhausted row with no reconciliation_last_at
    recorded (never produced by the real claim path -- attempts and
    last_at are always written together -- but possible from a test or a
    future data-repair script) must be treated as recent, never silently
    excluded: an unknown attempt time must never resolve a genuinely
    current incident in the operator's favor by default."""
    now = datetime.now(UTC)
    recon = settings.model_copy(update={"reconciliation_max_attempts": 3})
    aged_out_at = now - timedelta(seconds=recon.reconciliation_max_age_seconds + 100)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=None,
        callback_token_issued_at=aged_out_at,
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.details["exhausted_recent"] == 1


def test_reconciliation_healthy_queue_and_historical_backlog_resolves(session_factory, settings):
    """A healthy live queue (no due/actionable/recent-exhausted rows) stays
    `ok` even in the presence of an old historical exhausted backlog and an
    unrelated healthy in-flight payment."""
    now = datetime.now(UTC)
    recon = settings.model_copy(update={"reconciliation_max_attempts": 3})
    ancient = now - timedelta(days=45)
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=ancient,
        callback_token_issued_at=ancient,
    )
    _make_payment(
        session_factory,
        status="link_created",
        reconciliation_attempts=1,
        reconciliation_next_at=now + timedelta(seconds=10),
        callback_token_issued_at=now - timedelta(seconds=5),
    )
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "ok"
    assert result.details["exhausted_historical_total"] == 1


# --- backup ------------------------------------------------------------


def _write_manifest(dump: Path, **overrides) -> Path:
    """Writes scripts/backup.sh's write_manifest() sidecar format (plain
    key=value lines) for `dump`. Callers override individual fields to
    exercise malformed/inconsistent manifests; the default fields describe
    a fully valid, matching manifest."""
    fields = {
        "backup_file": dump.name,
        "sha256": "a" * 64,
        "size_bytes": str(dump.stat().st_size),
        "created_at": "2026-01-01T00:00:00Z",
        "app_version": "0.6.0",
        "postgres_version": "16.0",
        "alembic_revision": "0012",
        "validation": "passed",
    }
    fields.update(overrides)
    manifest = dump.with_name(dump.name + ".manifest")
    manifest.write_text("\n".join(f"{key}={value}" for key, value in fields.items()) + "\n")
    return manifest


def test_backup_missing(settings, tmp_path):
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.reason == "no_valid_backup_found"


def test_backup_ignores_unvalidated_dump(settings, tmp_path):
    (tmp_path / "centralpay-20260101-000000.dump").write_bytes(b"PGDMP")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.reason == "no_valid_backup_found"


def test_backup_healthy(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump)
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "ok"


def test_backup_stale(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    ok_file = tmp_path / (dump.name + ".ok")
    ok_file.touch()
    manifest = _write_manifest(dump)
    old_mtime = time.time() - 100
    os.utime(dump, (old_mtime, old_mtime))
    os.utime(ok_file, (old_mtime, old_mtime))
    os.utime(manifest, (old_mtime, old_mtime))
    backup_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_backup_warning_age_seconds": 10,
            "monitor_backup_critical_age_seconds": 3600,
        }
    )
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "warning"
    assert result.reason == "backup_aging"


# --- backup manifest validation ---------------------------------------


def test_backup_missing_manifest_is_not_healthy(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    # deliberately no .manifest sidecar written
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.reason == "backup_manifest_invalid"
    assert result.details["manifest_issue"] == "manifest_missing"


def test_backup_malformed_manifest_is_not_healthy(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    (tmp_path / (dump.name + ".manifest")).write_text("this is not a key=value sidecar at all\n")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_malformed"


def test_backup_manifest_missing_required_key_is_not_healthy(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    # A truncated manifest -- present, well-formed key=value lines, but
    # missing sha256/size_bytes entirely (never something backup.sh's own
    # write_manifest() would produce for a real backup).
    (tmp_path / (dump.name + ".manifest")).write_text(f"backup_file={dump.name}\n")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_malformed"


def test_backup_manifest_truncated_before_validation_marker_is_not_healthy(settings, tmp_path):
    """write_manifest() writes "validation=passed" LAST -- a manifest
    truncated partway through writing (a crash/kill between lines) can
    have every OTHER field present and consistent, but be missing that
    final marker. That must still be rejected: it's incomplete evidence
    that the archive actually passed validation, not just an unrelated
    missing field."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    fields = {
        "backup_file": dump.name,
        "sha256": "a" * 64,
        "size_bytes": str(dump.stat().st_size),
        "created_at": "2026-01-01T00:00:00Z",
        "app_version": "0.6.0",
        "postgres_version": "16.0",
        "alembic_revision": "0012",
        # "validation=passed" deliberately omitted -- truncated write.
    }
    (tmp_path / (dump.name + ".manifest")).write_text(
        "\n".join(f"{key}={value}" for key, value in fields.items()) + "\n"
    )
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_malformed"


def test_backup_manifest_wrong_dump_filename_is_not_healthy(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump, backup_file="centralpay-99990101-000000.dump")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_filename_mismatch"


def test_backup_manifest_path_traversal_payload_never_touches_the_filesystem(settings, tmp_path):
    """backup_file is only ever compared as a plain string against the
    already-known dump filename -- never used to construct a Path or open
    anything. A traversal-shaped value must be rejected as a plain
    mismatch, exactly like any other wrong filename, and must never cause
    any file outside the backup directory to be read."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump, backup_file="../../../../etc/passwd")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_filename_mismatch"


def test_backup_manifest_duplicate_key_is_not_healthy(settings, tmp_path):
    """scripts/centralpay's OWN restore-side check
    (`grep -E '^sha256=' | head -1`) reads only the FIRST sha256= line; a
    manifest with two conflicting sha256 lines (corruption, or a forged
    second value) must be rejected outright rather than silently resolved
    by picking either the first or the last -- disagreeing with restore's
    own choice would let this check claim recoverability for an archive
    restore itself would refuse."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    good_sha = "a" * 64
    lines = [
        f"backup_file={dump.name}",
        "sha256=0000000000000000000000000000000000000000000000000000000000bad",
        f"sha256={good_sha}",  # duplicate key -- forged/corrupted second value
        f"size_bytes={dump.stat().st_size}",
        "validation=passed",
    ]
    (tmp_path / (dump.name + ".manifest")).write_text("\n".join(lines) + "\n")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_malformed"


def test_backup_manifest_checksum_trailing_whitespace_is_not_healthy(settings, tmp_path):
    """scripts/centralpay's restore-side extraction
    (`cut -d= -f2`, no trimming) would keep trailing whitespace as part of
    the expected checksum, which a real sha256sum output never has --
    restore would reject this backup with a checksum mismatch. This check
    must never be more lenient than that: a value's surrounding whitespace
    is never stripped, so the same trailing whitespace fails the 64-hex
    shape check here too, instead of being silently cleaned up and
    reported healthy."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump, sha256=("a" * 64) + "  ")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_checksum_shape_invalid"


def test_backup_manifest_crlf_checksum_line_not_healthy(settings, tmp_path):
    """str.splitlines() treats a bare "\\r" (as left behind by a CRLF line
    ending) as its own line boundary and silently discards it -- unlike
    scripts/centralpay's restore-side extraction (grep/cut), which does no
    such normalization and would see the checksum value WITH its trailing
    "\\r" attached, never matching a real sha256sum output. A
    CRLF-corrupted checksum line (e.g. the manifest edited on Windows)
    must fail here too, not be quietly normalized into a passing shape."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    lines = [
        f"backup_file={dump.name}",
        f"sha256={'a' * 64}\r",  # CRLF: a trailing \r left before the \n
        f"size_bytes={dump.stat().st_size}",
        "validation=passed",
    ]
    (tmp_path / (dump.name + ".manifest")).write_bytes(("\n".join(lines) + "\n").encode())
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_checksum_shape_invalid"


def test_backup_symlinked_manifest_is_not_healthy(settings, tmp_path):
    """scripts/centralpay's restore path explicitly requires
    `! -L "$manifest"` and treats a symlinked manifest as equivalent to no
    integrity proof at all. Path.is_file()/read_text() FOLLOW symlinks, so
    without an explicit is_symlink() check, a manifest that is actually a
    symlink to an otherwise-valid file would be certified healthy here
    while restore itself would refuse to trust it."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    real_manifest = tmp_path / "real.manifest"
    _write_manifest(dump)  # writes dump.name + ".manifest" with valid fields
    canonical_manifest = tmp_path / (dump.name + ".manifest")
    canonical_manifest.rename(real_manifest)
    canonical_manifest.symlink_to(real_manifest)
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_missing"


def test_backup_manifest_size_mismatch_is_not_healthy(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump, size_bytes=str(dump.stat().st_size + 999))
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_size_mismatch"


def test_backup_manifest_malformed_checksum_shape_is_not_healthy(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump, sha256="not-a-valid-sha256-digest")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_checksum_shape_invalid"


def test_backup_stale_but_structurally_valid_manifest_preserves_age_semantics(settings, tmp_path):
    """A stale backup with a fully valid, consistent manifest still follows
    the existing age-based warning/critical semantics -- manifest
    validation and staleness are independent conditions."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    ok_file = tmp_path / (dump.name + ".ok")
    ok_file.touch()
    manifest = _write_manifest(dump)
    old_mtime = time.time() - 100_000
    os.utime(dump, (old_mtime, old_mtime))
    os.utime(ok_file, (old_mtime, old_mtime))
    os.utime(manifest, (old_mtime, old_mtime))
    backup_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_backup_warning_age_seconds": 10,
            "monitor_backup_critical_age_seconds": 3600,
        }
    )
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.reason == "backup_stale"  # a staleness reason, not a manifest one


def test_backup_check_never_reads_dump_contents(settings, tmp_path, monkeypatch):
    """Metadata-only proof: check_backup must never read the dump file's
    own bytes, only .stat() it and parse its small .manifest sidecar. A
    guard on Path.read_bytes raises if the check ever tries to read the
    (here, deliberately huge) dump file itself."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    with open(dump, "wb") as handle:
        handle.seek(200 * 1024 * 1024)  # 200MB sparse file: near-instant, no real disk usage
        handle.write(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump, size_bytes=str(dump.stat().st_size))

    real_read_bytes = Path.read_bytes

    def _guarded_read_bytes(self, *args, **kwargs):
        if self.suffix == ".dump":
            raise AssertionError(f"check_backup must never read dump file bytes: {self}")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _guarded_read_bytes)
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "ok"


def test_backup_oversized_manifest_is_not_healthy(settings, tmp_path):
    """A real write_manifest() sidecar is well under 300 bytes. A manifest
    file far larger than that -- corrupted, accidentally replaced, or
    deliberately huge -- must be rejected as malformed rather than
    accepted after being read in full."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    manifest = tmp_path / (dump.name + ".manifest")
    # Otherwise well-formed content, just padded far past the size bound.
    manifest.write_text(f"backup_file={dump.name}\n" + ("x" * 10_000) + "\n")
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_malformed"


def test_backup_manifest_parse_never_reads_more_than_the_size_bound(
    settings, tmp_path, monkeypatch
):
    """Metadata-only proof for the manifest sidecar itself: even when the
    file on disk is deliberately huge (here, a 200MB sparse file), the
    parser must never read more than a few KB of it into memory. A guard
    on the underlying file object's .read() raises if ever asked to read
    past the documented bound."""
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    manifest = tmp_path / (dump.name + ".manifest")
    with open(manifest, "wb") as handle:
        handle.write(f"backup_file={dump.name}\n".encode())
        handle.seek(200 * 1024 * 1024)  # 200MB sparse file: near-instant, no real disk usage
        handle.write(b"x")

    # _io.BufferedReader is an immutable C type -- its .read method can't be
    # monkeypatched directly. Instead wrap Path.open itself so every read
    # against the manifest file goes through a size-bound guard.
    class _BoundedReadGuard:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            max_size = monitor_checks._BACKUP_MANIFEST_MAX_BYTES + 1
            if size is None or size < 0 or size > max_size:
                raise AssertionError(f"manifest read must be bounded, got size={size}")
            return self._handle.read(size)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self._handle.__exit__(*exc_info)

    real_open = Path.open

    def _guarded_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        return _BoundedReadGuard(handle) if self.suffix == ".manifest" else handle

    monkeypatch.setattr(Path, "open", _guarded_open)
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["manifest_issue"] == "manifest_malformed"


# --- disk space ------------------------------------------------------------

_Usage = namedtuple("_Usage", ["total", "used", "free"])


def test_disk_warning(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: _Usage(total=100, used=70, free=30)
    )
    disk_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_disk_warning_percent": 50.0,
            "monitor_disk_critical_percent": 10.0,
            "monitor_disk_min_free_bytes": 1,
        }
    )
    result = monitor_checks.check_disk(disk_settings)
    assert result.status == "warning"


def test_disk_critical_by_percent(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: _Usage(total=100, used=95, free=5)
    )
    disk_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_disk_warning_percent": 50.0,
            "monitor_disk_critical_percent": 10.0,
            "monitor_disk_min_free_bytes": 1,
        }
    )
    result = monitor_checks.check_disk(disk_settings)
    assert result.status == "critical"


def test_disk_critical_by_absolute_floor(settings, monkeypatch, tmp_path):
    # Free percent alone looks healthy, but the absolute floor is breached.
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: _Usage(total=10_000_000_000, used=1_000_000_000, free=9_000_000_000),
    )
    disk_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_disk_warning_percent": 5.0,
            "monitor_disk_critical_percent": 1.0,
            "monitor_disk_min_free_bytes": 10_000_000_000,  # larger than free
        }
    )
    result = monitor_checks.check_disk(disk_settings)
    assert result.status == "critical"


# --- db integrity ------------------------------------------------------------


def test_db_integrity_healthy(session_factory):
    _seed_alembic_version(session_factory)
    with session_factory() as db:
        result = monitor_checks.check_db_integrity(db)
    assert result.status == "ok"


def test_db_integrity_failure_detected(session_factory):
    _make_payment(session_factory, status="totally_bogus_status")
    with session_factory() as db:
        result = monitor_checks.check_db_integrity(db)
    assert result.status == "critical"
    assert "invalid_payment_status" in result.details["failures"]


# --- gateway / bot failure bursts -------------------------------------------


def test_gateway_failure_burst_counts_affected_payments_not_attempts(session_factory, settings):
    payment = _make_payment(session_factory, status="link_created")
    low = settings.model_copy(
        update={
            "monitor_gateway_failure_warning_count": 2,
            "monitor_gateway_failure_critical_count": 5,
        }
    )
    with session_factory() as db:
        for _ in range(3):
            record_event(
                db,
                payment_id=payment.id,
                event_type="centralpay_verify_failed",
                data={"stage": "transport"},
            )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "ok"  # one affected payment, below the warning threshold of 2
    assert result.details["affected_payments"] == 1


def test_gateway_failure_burst_excludes_verify_failed_explicit_rejection(
    session_factory, settings
):
    """centralpay_verify_failed stage="gateway" reason="gateway_rejected"
    (app.centralpay.GATEWAY_REJECTED) is CentralPay EXPLICITLY answering
    "not successful" for one specific payment during a callback
    (app.services.verification.verify_and_settle) -- an ordinary, expected
    outcome (the payer didn't complete/abandoned), never a gateway
    infrastructure failure. Several payers failing around the same time
    must never trip this burst check."""
    payments = [_make_payment(session_factory, status="link_created") for _ in range(5)]
    low = settings.model_copy(update={"monitor_gateway_failure_warning_count": 1})
    with session_factory() as db:
        for payment in payments:
            record_event(
                db,
                payment_id=payment.id,
                event_type="centralpay_verify_failed",
                data={"stage": "gateway", "reason": "gateway_rejected"},
            )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "ok"
    assert result.details["affected_payments"] == 0


def test_gateway_failure_burst_counts_verify_failed_gateway_stage_response_invalid(
    session_factory, settings
):
    """stage="gateway" reason="gateway_response_invalid"
    (app.centralpay.GATEWAY_RESPONSE_INVALID) means CentralPay's response
    had NEITHER a clear success NOR a clear failure marker -- unlike an
    explicit rejection, this is the verify API itself behaving abnormally,
    a genuine protocol-level infra signal that must still count. Otherwise
    a systemic verify-API outage returning HTTP 200 with a broken body for
    every payment would produce zero burst incidents."""
    payments = [_make_payment(session_factory, status="link_created") for _ in range(3)]
    low = settings.model_copy(
        update={
            "monitor_gateway_failure_warning_count": 1,
            "monitor_gateway_failure_critical_count": 3,
        }
    )
    with session_factory() as db:
        for payment in payments:
            record_event(
                db,
                payment_id=payment.id,
                event_type="centralpay_verify_failed",
                data={"stage": "gateway", "reason": "gateway_response_invalid"},
            )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["affected_payments"] == 3


def test_gateway_failure_burst_counts_verify_failed_gateway_stage_missing_data(
    session_factory, settings
):
    """stage="gateway" reason="gateway_missing_data"
    (app.centralpay.GATEWAY_MISSING_DATA) -- same reasoning as
    gateway_response_invalid above: a protocol-level anomaly, not an
    ordinary payer-declined outcome, so it must still count."""
    payment = _make_payment(session_factory, status="link_created")
    low = settings.model_copy(update={"monitor_gateway_failure_warning_count": 1})
    with session_factory() as db:
        record_event(
            db,
            payment_id=payment.id,
            event_type="centralpay_verify_failed",
            data={"stage": "gateway", "reason": "gateway_missing_data"},
        )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "warning"
    assert result.details["affected_payments"] == 1


def test_gateway_failure_burst_counts_verify_failed_gateway_stage_error_field(
    session_factory, settings
):
    """stage="gateway" reason="gateway_error_field"
    (app.centralpay.GATEWAY_ERROR_FIELD) -- a dedicated "error" field in
    CentralPay's response, distinct from an explicit success=false/failure
    status value (see app.centralpay.gateway_reason_code). A service/
    protocol-level error signal, not CentralPay answering that one specific
    payment wasn't successful, so it must still count -- otherwise a
    systemic outage where verify.php returns an error field for every
    payment would produce zero burst incidents."""
    payment = _make_payment(session_factory, status="link_created")
    low = settings.model_copy(update={"monitor_gateway_failure_warning_count": 1})
    with session_factory() as db:
        record_event(
            db,
            payment_id=payment.id,
            event_type="centralpay_verify_failed",
            data={"stage": "gateway", "reason": "gateway_error_field"},
        )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "warning"
    assert result.details["affected_payments"] == 1


def test_gateway_failure_burst_excludes_verify_failed_missing_stage(session_factory, settings):
    """An event shape with no stage at all is ambiguous -- excluded rather
    than risk a misleading alert, same as an unrecognized stage value."""
    payment = _make_payment(session_factory, status="link_created")
    low = settings.model_copy(update={"monitor_gateway_failure_warning_count": 1})
    with session_factory() as db:
        record_event(
            db, payment_id=payment.id, event_type="centralpay_verify_failed", data={}
        )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "ok"
    assert result.details["affected_payments"] == 0


def test_gateway_failure_burst_excludes_verify_failed_gateway_stage_missing_reason(
    session_factory, settings
):
    """stage="gateway" with no reason field at all (or an unrecognized one)
    is ambiguous -- excluded rather than risk a misleading alert, same as
    a missing stage."""
    payment = _make_payment(session_factory, status="link_created")
    low = settings.model_copy(update={"monitor_gateway_failure_warning_count": 1})
    with session_factory() as db:
        record_event(
            db,
            payment_id=payment.id,
            event_type="centralpay_verify_failed",
            data={"stage": "gateway"},
        )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "ok"
    assert result.details["affected_payments"] == 0


def test_gateway_failure_burst_counts_verify_failed_transport_stage(session_factory, settings):
    """A genuine CentralPayError raised from client.verify() -- recorded
    stage="transport" -- IS a reliable infrastructure signal and must
    count."""
    payment = _make_payment(session_factory, status="link_created")
    low = settings.model_copy(update={"monitor_gateway_failure_warning_count": 1})
    with session_factory() as db:
        record_event(
            db,
            payment_id=payment.id,
            event_type="centralpay_verify_failed",
            data={"stage": "transport", "error_code": "centralpay_connection_error"},
        )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "warning"
    assert result.details["affected_payments"] == 1


def test_gateway_failure_burst_counts_distinct_transport_failure_payments(
    session_factory, settings
):
    payments = [_make_payment(session_factory, status="link_created") for _ in range(3)]
    low = settings.model_copy(
        update={
            "monitor_gateway_failure_warning_count": 1,
            "monitor_gateway_failure_critical_count": 3,
        }
    )
    with session_factory() as db:
        for payment in payments:
            record_event(
                db,
                payment_id=payment.id,
                event_type="centralpay_verify_failed",
                data={"stage": "transport"},
            )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["affected_payments"] == 3


def test_gateway_failure_burst_counts_every_getlink_failed_error_shape(session_factory, settings):
    """centralpay_getlink_failed has no "ordinary outcome" variant --
    get_link() only ever returns a redirect URL or raises CentralPayError
    (app.centralpay.CentralPayClient.get_link) -- so every error_code shape
    it can carry (connection/rejected/invalid-response) is already a
    genuine gateway failure; unlike centralpay_verify_failed, it needs no
    stage-style filtering."""
    payments = [_make_payment(session_factory, status="link_created") for _ in range(3)]
    low = settings.model_copy(
        update={
            "monitor_gateway_failure_warning_count": 1,
            "monitor_gateway_failure_critical_count": 3,
        }
    )
    error_codes = [
        "centralpay_connection_error",
        "centralpay_rejected",
        "centralpay_invalid_response",
    ]
    with session_factory() as db:
        for payment, error_code in zip(payments, error_codes, strict=True):
            record_event(
                db,
                payment_id=payment.id,
                event_type="centralpay_getlink_failed",
                data={"error_code": error_code, "reason": "x"},
            )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["affected_payments"] == 3


def test_gateway_failure_burst_warning_and_critical(session_factory, settings):
    payments = [_make_payment(session_factory, status="link_created") for _ in range(3)]
    low = settings.model_copy(
        update={
            "monitor_gateway_failure_warning_count": 1,
            "monitor_gateway_failure_critical_count": 3,
        }
    )
    with session_factory() as db:
        for payment in payments:
            record_event(
                db, payment_id=payment.id, event_type="centralpay_getlink_failed", data={}
            )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["affected_payments"] == 3


def test_gateway_not_paid_never_counts_as_a_gateway_failure(session_factory, settings):
    payment = _make_payment(session_factory, status="link_created")
    low = settings.model_copy(update={"monitor_gateway_failure_warning_count": 1})
    with session_factory() as db:
        record_event(
            db,
            payment_id=payment.id,
            event_type="reconciliation_gateway_not_paid",
            data={},
        )
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "ok"
    assert result.details["affected_payments"] == 0


def test_bot_failure_burst_warning_and_critical(session_factory, settings):
    payments = [_make_payment(session_factory, status="link_created") for _ in range(3)]
    low = settings.model_copy(
        update={"monitor_bot_failure_warning_count": 1, "monitor_bot_failure_critical_count": 3}
    )
    with session_factory() as db:
        for payment in payments:
            record_event(
                db, payment_id=payment.id, event_type="bot_notification_failed", data={}
            )
        db.commit()
        result = monitor_checks.check_bot_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "critical"
    assert result.details["affected_payments"] == 3


# --- overall status / run_all_checks ---------------------------------------


def test_overall_status_worst_wins():
    results = [
        monitor_checks.CheckResult("a", "ok", "x"),
        monitor_checks.CheckResult("b", "warning", "y"),
        monitor_checks.CheckResult("c", "ok", "z"),
    ]
    assert monitor_checks.overall_status(results) == "warning"
    results.append(monitor_checks.CheckResult("d", "critical", "w"))
    assert monitor_checks.overall_status(results) == "critical"


def test_run_all_checks_entirely_healthy(session_factory, settings, monkeypatch, tmp_path):
    _patch_httpx_client(
        monkeypatch, _FakeClient(response=_FakeResponse(200, {"status": "ready", "database": "ok"}))
    )
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: _Usage(total=100, used=10, free=90)
    )
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump)
    healthy = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "reconciliation_enabled": False,
            "monitor_disk_min_free_bytes": 1,
        }
    )
    _seed_alembic_version(session_factory)
    with session_factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_name="notification-worker",
                instance_id="w1",
                last_heartbeat_at=datetime.now(UTC),
            )
        )
        db.commit()
        results = monitor_checks.run_all_checks(db, healthy)
    assert monitor_checks.overall_status(results) == "ok"
    keys = {result.key for result in results}
    assert keys == {
        "public_ready",
        "database",
        "worker_heartbeat:notification-worker",
        "notification_backlog",
        "manual_review",
        "reconciliation",
        "backup",
        "disk_space",
        "gateway_failure_burst",
        "bot_failure_burst",
        "db_integrity",
    }


def test_run_all_checks_skips_db_integrity_when_excluded(session_factory, settings, monkeypatch):
    _patch_httpx_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("down")))
    with session_factory() as db:
        results = monitor_checks.run_all_checks(
            db, settings, include_db_integrity=False
        )
    assert monitor_checks.DB_INTEGRITY_CHECK_KEY not in {r.key for r in results}


def test_run_all_checks_includes_reconciliation_worker_heartbeat_when_enabled(
    session_factory, settings, monkeypatch
):
    _patch_httpx_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("down")))
    enabled = settings.model_copy(update={"reconciliation_enabled": True})
    with session_factory() as db:
        results = monitor_checks.run_all_checks(db, enabled, include_db_integrity=False)
    keys = {r.key for r in results}
    assert "worker_heartbeat:notification-worker" in keys
    assert "worker_heartbeat:reconciliation-worker" in keys


def test_run_all_checks_omits_reconciliation_worker_heartbeat_when_disabled(
    session_factory, settings, monkeypatch
):
    _patch_httpx_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("down")))
    disabled = settings.model_copy(update={"reconciliation_enabled": False})
    with session_factory() as db:
        results = monitor_checks.run_all_checks(db, disabled, include_db_integrity=False)
    keys = {r.key for r in results}
    assert "worker_heartbeat:notification-worker" in keys
    assert "worker_heartbeat:reconciliation-worker" not in keys


def test_run_all_checks_includes_admin_bot_delivery_heartbeat_when_enabled(
    session_factory, settings, monkeypatch
):
    """The admin bot's own delivery loop has no other visibility to this
    dedicated monitor -- its container liveness heartbeat file lives in its
    own tmpfs, never a database row, unless run_all_checks observes it."""
    _patch_httpx_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("down")))
    enabled = settings.model_copy(update={"admin_bot_enabled": True})
    with session_factory() as db:
        results = monitor_checks.run_all_checks(db, enabled, include_db_integrity=False)
    keys = {r.key for r in results}
    assert "worker_heartbeat:admin-bot-delivery" in keys


def test_run_all_checks_omits_admin_bot_delivery_heartbeat_when_disabled(
    session_factory, settings, monkeypatch
):
    _patch_httpx_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("down")))
    disabled = settings.model_copy(update={"admin_bot_enabled": False})
    with session_factory() as db:
        results = monitor_checks.run_all_checks(db, disabled, include_db_integrity=False)
    keys = {r.key for r in results}
    assert "worker_heartbeat:admin-bot-delivery" not in keys


# --- graceful degradation during a real database outage --------------------

_DB_DEPENDENT_KEYS_ALL_ENABLED = (
    "worker_heartbeat:notification-worker",
    "worker_heartbeat:reconciliation-worker",
    "worker_heartbeat:admin-bot-delivery",
    "notification_backlog",
    "manual_review",
    "reconciliation",
    "gateway_failure_burst",
    "bot_failure_burst",
    "db_integrity",
)


def test_run_all_checks_degrades_gracefully_when_database_is_unreachable(
    settings, monkeypatch, tmp_path
):
    """A genuinely broken engine/session (bound to a database file whose
    parent directory does not exist -- every connection attempt raises a
    real sqlalchemy.exc.OperationalError, not a monkeypatched bool), the
    same shape of failure a fully unreachable PostgreSQL would produce.
    run_all_checks must still return a complete, structured result list:
    DB-independent checks run normally, every DB-dependent check gets a
    critical database_unavailable placeholder, and nothing raises."""
    _patch_httpx_client(
        monkeypatch, _FakeClient(response=_FakeResponse(200, {"status": "ready", "database": "ok"}))
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda path: _Usage(total=100, used=10, free=90))
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump)
    broken_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "reconciliation_enabled": True,
            "admin_bot_enabled": True,
            "monitor_disk_min_free_bytes": 1,
        }
    )

    unreachable_dir = tmp_path / "no-such-directory"  # never created
    broken_engine = create_engine(f"sqlite:///{unreachable_dir}/db.sqlite")
    broken_factory = sessionmaker(bind=broken_engine, expire_on_commit=False, autoflush=False)

    with broken_factory() as db:
        results = monitor_checks.run_all_checks(db, broken_settings, include_db_integrity=True)

    by_key = {r.key: r for r in results}
    assert by_key["public_ready"].status == "ok"
    assert by_key["backup"].status == "ok"
    assert by_key["disk_space"].status == "ok"
    assert by_key["database"].status == "critical"
    assert by_key["database"].reason == "query_failed"
    for key in _DB_DEPENDENT_KEYS_ALL_ENABLED:
        assert by_key[key].status == "critical", key
        assert by_key[key].reason == "database_unavailable", key
        assert by_key[key].details == {"dependency": "database"}


def test_run_all_checks_reconciliation_disabled_stays_healthy_during_outage(
    settings, monkeypatch, tmp_path
):
    """check_reconciliation short-circuits to ok/disabled WITHOUT ever
    touching `db` when reconciliation_enabled is False -- it is
    DB-INDEPENDENT in that case, so a database outage must never turn it
    into a fabricated database_unavailable placeholder for a feature
    that's simply turned off (which could otherwise be persisted and
    alerted on as a real incident once the database recovers)."""
    _patch_httpx_client(
        monkeypatch, _FakeClient(response=_FakeResponse(200, {"status": "ready", "database": "ok"}))
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda path: _Usage(total=100, used=10, free=90))
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    _write_manifest(dump)
    broken_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "reconciliation_enabled": False,
            "monitor_disk_min_free_bytes": 1,
        }
    )

    unreachable_dir = tmp_path / "no-such-directory"  # never created
    broken_engine = create_engine(f"sqlite:///{unreachable_dir}/db.sqlite")
    broken_factory = sessionmaker(bind=broken_engine, expire_on_commit=False, autoflush=False)

    with broken_factory() as db:
        results = monitor_checks.run_all_checks(db, broken_settings, include_db_integrity=False)

    by_key = {r.key: r for r in results}
    assert by_key["database"].status == "critical"
    assert by_key["reconciliation"].status == "ok"
    assert by_key["reconciliation"].reason == "disabled"
    assert "worker_heartbeat:reconciliation-worker" not in by_key


def test_run_all_checks_excludes_db_integrity_when_unreachable_and_excluded(settings, tmp_path):
    unreachable_dir = tmp_path / "no-such-directory"
    broken_engine = create_engine(f"sqlite:///{unreachable_dir}/db.sqlite")
    broken_factory = sessionmaker(bind=broken_engine, expire_on_commit=False, autoflush=False)
    with broken_factory() as db:
        results = monitor_checks.run_all_checks(db, settings, include_db_integrity=False)
    keys = {r.key for r in results}
    assert monitor_checks.DB_INTEGRITY_CHECK_KEY not in keys
    assert "database" in keys


def test_run_all_checks_degrades_gracefully_when_a_later_query_fails_mid_pass(
    session_factory, settings, monkeypatch
):
    """The initial `database` probe (SELECT 1) succeeds, but a LATER
    DB-dependent check's own query raises a real OperationalError with
    connection_invalidated=True -- SQLAlchemy's own signal (set by its
    dialect-level is_disconnect() check) that the connection itself died,
    not just that one statement was bad. Every check already run stays
    as-is, the failing check and everything after it in the list becomes
    database_unavailable, and nothing raises."""

    def _boom(db, status):
        raise OperationalError(
            "SELECT ...",
            {},
            Exception("server closed the connection unexpectedly"),
            connection_invalidated=True,
        )

    monkeypatch.setattr(queries, "count_by_status", _boom)

    with session_factory() as db:
        results = monitor_checks.run_all_checks(db, settings, include_db_integrity=True)

    by_key = {r.key: r for r in results}
    # database's own SELECT 1 never touched count_by_status -- it still
    # succeeded for real.
    assert by_key["database"].status == "ok"
    # The check immediately before notification_backlog in the DB-dependent
    # order ran normally (never itself queried count_by_status).
    assert by_key["worker_heartbeat:notification-worker"].reason != "database_unavailable"
    # notification_backlog is the one whose own query broke, and every
    # DB-dependent check scheduled after it never even attempted a query.
    remaining_keys = (
        "notification_backlog",
        "manual_review",
        "reconciliation",
        "gateway_failure_burst",
        "bot_failure_burst",
        "db_integrity",
    )
    for key in remaining_keys:
        assert by_key[key].status == "critical", key
        assert by_key[key].reason == "database_unavailable", key


def test_run_all_checks_does_not_mislabel_a_non_database_bug(
    session_factory, settings, monkeypatch
):
    """A bug in a check that has nothing to do with database connectivity
    (here: a plain ValueError, as if run_db_checks had a real defect) must
    propagate normally, not get silently absorbed and reported as a
    fabricated `database_unavailable` -- that would hide the actual
    failure behind a misleading "PostgreSQL is down" story. Only a
    DBAPIError with connection_invalidated=True triggers the
    outage-degradation path."""

    def _buggy(db, status):
        raise ValueError("not a database problem")

    monkeypatch.setattr(queries, "count_by_status", _buggy)

    with session_factory() as db, pytest.raises(ValueError, match="not a database problem"):
        monitor_checks.run_all_checks(db, settings, include_db_integrity=True)


def test_run_all_checks_does_not_mislabel_a_non_connectivity_dbapi_error(
    session_factory, settings, monkeypatch
):
    """A DBAPIError that is NOT a connectivity failure (connection_
    invalidated=False -- e.g. a real ProgrammingError from a bad query on
    an otherwise-live connection) must also propagate normally, not be
    treated as a PostgreSQL outage. Distinguishing this from the test
    above is the whole point: not every SQLAlchemyError means the database
    is unreachable."""

    def _buggy(db, status):
        raise ProgrammingError(
            "SELECT ...", {}, Exception("column does not exist"), connection_invalidated=False
        )

    monkeypatch.setattr(queries, "count_by_status", _buggy)

    with session_factory() as db, pytest.raises(ProgrammingError, match="column does not exist"):
        monitor_checks.run_all_checks(db, settings, include_db_integrity=True)


def test_run_all_checks_healthy_path_unaffected_by_outage_handling(
    session_factory, settings, monkeypatch
):
    """Belt-and-braces: a fully healthy database (the normal SQLite unit-
    test session) must never itself get a database_unavailable result --
    proves the new degrade-on-failure logic never fires on the happy
    path."""
    _seed_alembic_version(session_factory)
    _patch_httpx_client(
        monkeypatch, _FakeClient(response=_FakeResponse(200, {"status": "ready", "database": "ok"}))
    )
    with session_factory() as db:
        results = monitor_checks.run_all_checks(db, settings, include_db_integrity=True)
    assert all(r.reason != "database_unavailable" for r in results)
    assert next(r for r in results if r.key == "database").status == "ok"
