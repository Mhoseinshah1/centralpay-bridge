"""`centralpay monitor check` / `centralpay monitor incidents` (app.cli)."""

import json
import shutil
from collections import namedtuple
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from app.cli import main as cli_main
from app.services.monitor_checks import CheckResult
from app.services.monitor_incidents import record_check_result


def _seed_alembic_version(session_factory, revision: str = "test_revision") -> None:
    with session_factory() as db:
        db.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        db.execute(text("DELETE FROM alembic_version"))
        db.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": revision}
        )
        db.commit()


@pytest.fixture
def cli_env(settings, session_factory, monkeypatch, tmp_path):
    """A monitor-ready environment for app.cli's `main()`: patches Settings()
    / create_session_factory() the same way app.cli.main() constructs them
    internally, and points at a real (empty, valid) backup directory."""
    import app.cli as cli_module

    _seed_alembic_version(session_factory)
    cli_settings = settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_disk_min_free_bytes": 1,
            "reconciliation_enabled": False,
        }
    )
    monkeypatch.setattr(cli_module, "Settings", lambda: cli_settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: session_factory)

    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()

    from app.models import WorkerHeartbeat

    with session_factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_name="notification-worker",
                instance_id="cli-test-worker",
                last_heartbeat_at=datetime.now(UTC),
            )
        )
        db.commit()

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "ready", "database": "ok"}

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: _FakeClient())

    Usage = namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(total=100, used=1, free=99))
    return cli_settings


def test_monitor_check_json_is_valid_and_stable(cli_env, capsys):
    exit_code = cli_main(["monitor", "check", "--json"])
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)  # exactly one JSON object, on one line
    assert payload["status"] == "ok"
    assert isinstance(payload["checks"], dict)
    assert payload["failures"] == []
    for key, check in payload["checks"].items():
        assert check["key"] == key
        assert check["status"] in ("ok", "warning", "critical")
        assert "reason" in check
        assert "details" in check


def test_monitor_check_json_reports_failures(cli_env, session_factory):
    # Force a real failure the check-set will surface: an invalid payment
    # status (db_integrity), independent of any mocked network state.
    with session_factory() as db:
        from app.models import Payment

        db.add(
            Payment(
                bot_order_id="cli-bad-1",
                gateway_order_id=777001,
                gateway_user_id=1,
                amount=1000,
                fee_rate_bps=0,
                fee_amount=0,
                payable_amount=1000,
                status="not_a_real_status",
            )
        )
        db.commit()
    exit_code = cli_main(["monitor", "check", "--json"])
    assert exit_code == 1


def test_monitor_check_human_output(cli_env, capsys):
    exit_code = cli_main(["monitor", "check"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "overall" in out.lower()
    assert "public_ready" in out


def test_monitor_incidents_json_empty_by_default(cli_env, capsys):
    exit_code = cli_main(["monitor", "incidents", "--json"])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == ""


def test_monitor_incidents_json_lists_open_incident(cli_env, session_factory, capsys):
    now = datetime.now(UTC)
    result = CheckResult("disk_space", "critical", "low_disk_space", {"free_percent": 1.0})
    with session_factory() as db:
        record_check_result(db, cli_env, result, now=now)
    exit_code = cli_main(["monitor", "incidents", "--json"])
    assert exit_code == 0
    lines = [line for line in capsys.readouterr().out.strip().splitlines() if line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["check_key"] == "disk_space"
    assert payload["status"] == "open"


def test_monitor_incidents_human_output_no_incidents(cli_env, capsys):
    exit_code = cli_main(["monitor", "incidents"])
    assert exit_code == 0
    assert "No open incidents" in capsys.readouterr().out
