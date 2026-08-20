"""Unit tests for app.services.monitor_checks (SQLite; no network)."""

import itertools
import os
import shutil
import time
from collections import namedtuple
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

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
            db, settings, worker_name="notification-worker", now=now
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
            db, settings, worker_name="notification-worker", now=now
        )
    assert result.status == "critical"
    assert result.reason == "heartbeat_stale"


def test_worker_heartbeat_missing(session_factory, settings):
    with session_factory() as db:
        result = monitor_checks.check_worker_heartbeat(
            db, settings, worker_name="notification-worker", now=datetime.now(UTC)
        )
    assert result.status == "critical"
    assert result.reason == "no_heartbeat_recorded"


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


def test_reconciliation_disabled_is_healthy(session_factory, settings):
    disabled = settings.model_copy(update={"reconciliation_enabled": False})
    with session_factory() as db:
        result = monitor_checks.check_reconciliation(db, disabled, now=datetime.now(UTC))
    assert result.status == "ok"
    assert result.reason == "disabled"


# --- backup ------------------------------------------------------------


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
    backup_settings = settings.model_copy(update={"centralpay_backup_dir": str(tmp_path)})
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "ok"


def test_backup_stale(settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    ok_file = tmp_path / (dump.name + ".ok")
    ok_file.touch()
    old_mtime = time.time() - 100
    os.utime(dump, (old_mtime, old_mtime))
    os.utime(ok_file, (old_mtime, old_mtime))
    backup_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_backup_warning_age_seconds": 10,
            "monitor_backup_critical_age_seconds": 3600,
        }
    )
    result = monitor_checks.check_backup(backup_settings, now=datetime.now(UTC))
    assert result.status == "warning"


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
            record_event(db, payment_id=payment.id, event_type="centralpay_verify_failed", data={})
        db.commit()
        result = monitor_checks.check_gateway_failure_burst(db, low, now=datetime.now(UTC))
    assert result.status == "ok"  # one affected payment, below the warning threshold of 2
    assert result.details["affected_payments"] == 1


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
