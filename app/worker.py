"""Bot notification worker.

Run locally:

    python -m app.worker

Polls for due bot_notify_pending payments, claims them with row locking
(FOR UPDATE SKIP LOCKED), delivers the bot notification outside any database
transaction, and records the classified result. Multiple workers can run
concurrently; stale claims from crashed workers are recovered on every pass.
"""

import logging
import os
import signal
import socket
import threading
import uuid
from pathlib import Path
from types import FrameType

from app.adminbot.alerts import configure_alert_creation
from app.bot import BotNotifier
from app.config import ConfigurationError, Settings, validate_bot_notification_settings
from app.db import create_session_factory
from app.logging_setup import configure_logging
from app.services.heartbeat import record_worker_heartbeat
from app.services.notification import run_worker_pass, utcnow

logger = logging.getLogger("app.worker")


def build_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def main() -> int:
    settings = Settings()
    configure_logging(settings)
    try:
        validate_bot_notification_settings(settings)
    except ConfigurationError as exc:
        # The message names the missing variable but never its value.
        logger.error("worker_configuration_invalid", extra={"reason": str(exc)})
        return 2

    session_factory = create_session_factory(settings.database_url)
    # Enables admin alert outbox rows for worker-side transitions; a no-op
    # when the admin bot is disabled. Telegram is never contacted here.
    configure_alert_creation(settings)
    notifier = BotNotifier(
        url=settings.bot_payment_notify_url,
        token=settings.bot_notify_token,
        connect_timeout_seconds=settings.bot_notify_connect_timeout_seconds,
        read_timeout_seconds=settings.bot_notify_read_timeout_seconds,
    )
    worker_id = build_worker_id()
    stop = threading.Event()

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "worker_started",
        extra={
            "worker_id": worker_id,
            "interval_seconds": settings.bot_notify_worker_interval_seconds,
            "retry_mode": settings.bot_notify_retry_mode,
            "max_attempts": settings.bot_notify_max_attempts,
        },
    )
    heartbeat = Path(settings.worker_heartbeat_file)
    while not stop.is_set():
        session = session_factory()
        cycle_completed = False
        error_code: str | None = None
        try:
            result = run_worker_pass(session, notifier, settings, worker_id=worker_id)
            cycle_completed = True
            if result["processed"] or result["recovered"]:
                logger.info("worker_pass_completed", extra={"worker_id": worker_id, **result})
            # Liveness heartbeat: container health checks verify this file
            # stays fresh. Only touched after a completed pass.
            try:
                heartbeat.touch()
            except OSError:
                logger.warning("worker_heartbeat_write_failed")
        except Exception as exc:
            error_code = type(exc).__name__
            logger.exception("worker_pass_failed")
        finally:
            session.close()
        # Database heartbeat for operational visibility (admin bot /health).
        # Best-effort: heartbeat problems never stop delivery.
        try:
            with session_factory() as hb_session:
                record_worker_heartbeat(
                    hb_session,
                    worker_name="notification-worker",
                    instance_id=worker_id,
                    now=utcnow(),
                    cycle_completed=cycle_completed,
                    error_code=error_code,
                )
        except Exception:
            logger.warning("worker_db_heartbeat_failed")
        stop.wait(settings.bot_notify_worker_interval_seconds)

    notifier.close()
    logger.info("worker_stopped", extra={"worker_id": worker_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
