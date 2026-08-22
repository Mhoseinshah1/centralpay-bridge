"""AdminBotService.background_loop must heartbeat to the database too.

The admin bot's own container-liveness heartbeat FILE lives in its own
tmpfs -- invisible to the dedicated monitor, which only reads the database.
Without a WorkerHeartbeat row, a stuck/crashed alert-delivery loop would go
completely undetected by app.services.monitor_checks.run_all_checks's
admin-bot-delivery check.
"""

import asyncio

from sqlalchemy import select

from app.adminbot.runner import DELIVERY_WORKER_NAME, AdminBotService
from app.models import WorkerHeartbeat
from tests.conftest import TEST_ADMIN_ID


async def _run_one_iteration_and_stop(service: AdminBotService, session_factory) -> None:
    task = asyncio.ensure_future(service.background_loop())
    try:
        row = None
        for _ in range(50):  # up to ~5s
            await asyncio.sleep(0.1)
            with session_factory() as db:
                row = db.execute(
                    select(WorkerHeartbeat).where(
                        WorkerHeartbeat.worker_name == DELIVERY_WORKER_NAME
                    )
                ).scalar_one_or_none()
            if row is not None:
                break
        assert row is not None, "admin-bot-delivery heartbeat row was never written"
    finally:
        service.stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)


def test_background_loop_writes_admin_bot_delivery_heartbeat(
    session_factory, admin_settings, monkeypatch, tmp_path
):
    settings = admin_settings.model_copy(
        update={"admin_bot_heartbeat_file": str(tmp_path / "heartbeat")}
    )
    service = AdminBotService(settings, session_factory, (TEST_ADMIN_ID,))
    monkeypatch.setattr(service.monitor, "run_once", lambda: None)
    monkeypatch.setattr("app.adminbot.runner.maybe_queue_daily_report", lambda db, s: None)

    asyncio.run(_run_one_iteration_and_stop(service, session_factory))

    with session_factory() as db:
        heartbeat = db.execute(
            select(WorkerHeartbeat).where(WorkerHeartbeat.worker_name == DELIVERY_WORKER_NAME)
        ).scalar_one()
    assert heartbeat.instance_id == service.instance_id
    assert heartbeat.last_error_code is None
