"""Worker heartbeat records for operational visibility. No secrets stored."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat
from app.version import APP_VERSION

logger = logging.getLogger("app.services.heartbeat")


def record_worker_heartbeat(
    db: Session,
    *,
    worker_name: str,
    instance_id: str,
    now: datetime,
    cycle_completed: bool,
    error_code: str | None = None,
) -> None:
    """Upsert this instance's heartbeat row and commit. Best-effort caller."""
    heartbeat = db.execute(
        select(WorkerHeartbeat).where(WorkerHeartbeat.instance_id == instance_id)
    ).scalar_one_or_none()
    if heartbeat is None:
        heartbeat = WorkerHeartbeat(
            worker_name=worker_name,
            instance_id=instance_id,
            last_heartbeat_at=now,
            version=APP_VERSION,
        )
        db.add(heartbeat)
    heartbeat.last_heartbeat_at = now
    if cycle_completed:
        heartbeat.last_cycle_at = now
        heartbeat.last_error_code = None
    if error_code is not None:
        heartbeat.last_error_code = error_code[:64]
    heartbeat.version = APP_VERSION
    db.commit()
