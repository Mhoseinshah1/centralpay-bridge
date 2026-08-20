"""Admin bot /monitor command: authorization and safe, secret-free output."""

from datetime import UTC, datetime

import httpx
import pytest

from app.adminbot.auth import GENERIC_DENIAL, UpdateContext
from app.adminbot.commands import CommandHandlers
from app.models import WorkerHeartbeat
from tests.conftest import TEST_ADMIN_ID, TEST_ADMIN_ID_2

pytestmark = pytest.mark.usefixtures("app")

ADMIN_IDS = (TEST_ADMIN_ID, TEST_ADMIN_ID_2)


@pytest.fixture
def monitor_settings(admin_settings, tmp_path):
    dump = tmp_path / "centralpay-20260101-000000.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / (dump.name + ".ok")).touch()
    return admin_settings.model_copy(
        update={
            "centralpay_backup_dir": str(tmp_path),
            "monitor_disk_min_free_bytes": 1,
            "reconciliation_enabled": False,
        }
    )


class _FakeReadyResponse:
    status_code = 200

    def json(self):
        return {"status": "ready", "database": "ok"}


class _FakeReadyClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url):
        return _FakeReadyResponse()


@pytest.fixture
def handlers(session_factory, monitor_settings, monkeypatch):
    # Never a real outbound request in this test file: public_base_url
    # points at a non-routable test domain.
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: _FakeReadyClient())
    with session_factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_name="notification-worker",
                instance_id="admin-monitor-test",
                last_heartbeat_at=datetime.now(UTC),
            )
        )
        db.commit()
    return CommandHandlers(
        session_factory,
        monitor_settings,
        ADMIN_IDS,
        api_probe=lambda: {"live": True, "ready": True},
    )


def admin_ctx() -> UpdateContext:
    return UpdateContext(user_id=TEST_ADMIN_ID, chat_id=TEST_ADMIN_ID, chat_type="private")


def unauthorized_ctx() -> UpdateContext:
    return UpdateContext(user_id=999999999, chat_id=999999999, chat_type="private")


def _all_secrets(monitor_settings):
    return [
        monitor_settings.inbound_api_key,
        monitor_settings.callback_hmac_secret,
        monitor_settings.centralpay_getlink_api_key,
        monitor_settings.centralpay_verify_api_key,
        monitor_settings.bot_notify_token,
        monitor_settings.admin_bot_token,
        monitor_settings.database_url,
    ]


def test_monitor_unauthorized_user_rejected(handlers):
    [reply] = handlers.handle(unauthorized_ctx(), "monitor", [])
    assert reply == GENERIC_DENIAL
    assert "پایش" not in reply


def test_monitor_authorized_admin_works(handlers):
    [reply] = handlers.handle(admin_ctx(), "monitor", [])
    assert "پایش" in reply
    assert "وضعیت کلی" in reply
    assert "public_ready" not in reply  # rendered with a Persian label, not the raw key


def test_monitor_output_contains_no_secrets(handlers, monitor_settings):
    [reply] = handlers.handle(admin_ctx(), "monitor", [])
    for secret in _all_secrets(monitor_settings):
        assert secret not in reply


def test_monitor_reports_overall_status(handlers):
    [reply] = handlers.handle(admin_ctx(), "monitor", [])
    assert "OK" in reply or "WARNING" in reply or "CRITICAL" in reply


def test_monitor_group_chat_rejected(handlers):
    group_ctx = UpdateContext(user_id=TEST_ADMIN_ID, chat_id=-100123, chat_type="group")
    [reply] = handlers.handle(group_ctx, "monitor", [])
    assert reply == GENERIC_DENIAL
