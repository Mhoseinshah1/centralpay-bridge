"""Bot notification worker.

Run locally:

    python -m app.worker

Polls for due bot_notify_pending payments, claims them with row locking
(FOR UPDATE SKIP LOCKED), delivers the bot notification outside any database
transaction, and records the classified result. Multiple workers can run
concurrently; stale claims from crashed workers are recovered on every pass.

Server-side payment RECONCILIATION (recovering link_created payments whose
browser callback never arrived) runs in a DEDICATED THREAD inside the same
process — never inline in the notification loop. The thread owns its own
CentralPay HTTP client and opens its own short-lived database sessions
(SQLAlchemy sessions and HTTP clients are never shared across threads), so a
slow or timing-out CentralPay verify call can never delay a bot
notification: the notification loop's cadence is completely independent.
The thread is exception-isolated (a failed pass logs and retries next
interval) and shuts down cleanly on SIGTERM/SIGINT via the shared stop
event; an in-flight verify call cannot be interrupted, so shutdown waits a
bounded time for it and then lets process exit proceed.
"""

import logging
import os
import signal
import socket
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from sqlalchemy.orm import Session

from app.adminbot.alerts import configure_alert_creation
from app.bot import BotNotifier
from app.centralpay import CentralPayClient
from app.config import ConfigurationError, Settings, validate_bot_notification_settings
from app.db import create_session_factory
from app.logging_setup import configure_logging
from app.services.heartbeat import record_worker_heartbeat
from app.services.notification import run_worker_pass, utcnow
from app.services.reconciliation import run_reconciliation_pass

logger = logging.getLogger("app.worker")


def build_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def reconciliation_loop(
    settings: Settings,
    session_factory: Callable[[], Session],
    *,
    worker_id: str,
    stop: threading.Event,
) -> None:
    """Body of the dedicated reconciliation thread.

    Owns a thread-local CentralPay client for its whole lifetime and opens a
    fresh session per pass — nothing here is ever shared with the
    notification loop's thread. Every pass is wrapped so one failure only
    logs and waits for the next interval; the loop exits when ``stop`` is
    set. All row-lock/idempotency guarantees live in
    ``run_reconciliation_pass`` itself and are unchanged by threading.
    """
    client = CentralPayClient(
        base_url=settings.centralpay_base_url,
        getlink_api_key=settings.centralpay_getlink_api_key,
        verify_api_key=settings.centralpay_verify_api_key,
        timeout_seconds=settings.centralpay_timeout_seconds,
    )
    logger.info(
        "reconciliation_thread_started",
        extra={
            "worker_id": worker_id,
            "interval_seconds": settings.reconciliation_interval_seconds,
            "batch_size": settings.reconciliation_batch_size,
        },
    )
    try:
        while not stop.is_set():
            cycle_completed = False
            error_code: str | None = None
            try:
                session = session_factory()
                try:
                    stats = run_reconciliation_pass(
                        session, client, settings, worker_id=worker_id
                    )
                finally:
                    session.close()
                cycle_completed = True
                if stats["processed"]:
                    logger.info(
                        "reconciliation_pass_completed",
                        extra={"worker_id": worker_id, **stats},
                    )
            except Exception as exc:
                error_code = type(exc).__name__
                logger.exception("reconciliation_pass_failed")
            # Operational visibility heartbeat, mirroring the notification
            # worker's; best-effort — heartbeat problems never stop the loop.
            try:
                hb_session = session_factory()
                try:
                    record_worker_heartbeat(
                        hb_session,
                        worker_name="reconciliation-worker",
                        instance_id=worker_id,
                        now=utcnow(),
                        cycle_completed=cycle_completed,
                        error_code=error_code,
                    )
                finally:
                    hb_session.close()
            except Exception:
                logger.warning("reconciliation_db_heartbeat_failed")
            stop.wait(settings.reconciliation_interval_seconds)
    finally:
        client.close()
        logger.info("reconciliation_thread_stopped", extra={"worker_id": worker_id})


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
            "reconciliation_enabled": settings.reconciliation_enabled,
        },
    )
    # Dedicated reconciliation thread: notification delivery below never
    # waits on a CentralPay verify call. Daemonized so a verify hung past
    # its timeout can never block process exit; shutdown still joins with a
    # bounded grace period for the common clean case.
    reconciliation_thread: threading.Thread | None = None
    if settings.reconciliation_enabled:
        reconciliation_thread = threading.Thread(
            target=reconciliation_loop,
            args=(settings, session_factory),
            kwargs={"worker_id": worker_id, "stop": stop},
            name="reconciliation",
            daemon=True,
        )
        reconciliation_thread.start()

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

    if reconciliation_thread is not None:
        # Clean shutdown: the loop observes `stop` between payments. An
        # in-flight verify cannot be interrupted, so wait at most its HTTP
        # timeout plus slack, then proceed (daemon thread; a crash here loses
        # only bookkeeping — settlement is transactional).
        reconciliation_thread.join(timeout=settings.centralpay_timeout_seconds + 5.0)
        if reconciliation_thread.is_alive():
            logger.warning(
                "reconciliation_thread_join_timeout", extra={"worker_id": worker_id}
            )
    notifier.close()
    logger.info("worker_stopped", extra={"worker_id": worker_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
