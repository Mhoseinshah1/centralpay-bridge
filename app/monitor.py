"""Monitoring daemon entrypoint.

Run locally:

    python -m app.monitor

Runs the cheap checks (app.services.monitor_checks.run_all_checks) every
MONITOR_INTERVAL_SECONDS and the one expensive check -- db_integrity,
reusing app.ops.run_db_checks verbatim -- on its own, far slower
MONITOR_DB_INTEGRITY_INTERVAL_SECONDS cadence.

A single synchronous loop: the next cycle only starts once the previous one
has fully returned, so a slow pass can never cause overlapping copies to
pile up (see run_forever). A crashing pass logs and retries at the next
tick -- it never brings the loop down. This process has no code path that
writes to payments/payment_events/admin_alerts financial state; it only
reads existing tables and appends MonitorIncident rows plus (via the
existing app.adminbot.alerts.create_alert outbox) admin_alerts rows, so a
monitor crash, hang, or misconfiguration can never affect payment
processing, the notification worker, or reconciliation.

Runs as a DEDICATED process/container, separate from app.worker, precisely
so a dead notification/reconciliation worker cannot also silence detection
of its own death (see docker-compose.yml's "monitor" service).
"""

import logging
import os
import signal
import socket
import threading
import time
import uuid
from pathlib import Path
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from app.config import ConfigurationError, Settings, validate_monitor_settings
from app.db import create_session_factory
from app.logging_setup import configure_logging
from app.services.heartbeat import record_worker_heartbeat
from app.services.monitor_checks import (
    DB_INTEGRITY_CHECK_KEY,
    DB_UNAVAILABLE_REASON,
    run_all_checks,
)
from app.services.monitor_incidents import record_check_result
from app.services.notification import utcnow

logger = logging.getLogger("app.monitor")

WORKER_NAME = "monitor"


def build_instance_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def run_one_pass(
    session_factory: sessionmaker[Session], settings: Settings, *, include_db_integrity: bool
) -> bool:
    """One full check-and-record cycle, in one session.

    The checks are all read-only and finish first; only then does each
    result get applied to incident state (which may commit). Mixing
    several small sequential commits on one session this way matches the
    rest of this codebase's read-then-record style (e.g. app.cli's status
    commands) -- there is no financial-transaction boundary here to keep
    atomic.

    Returns whether db_integrity ACTUALLY EXECUTED this pass -- always
    False when include_db_integrity is False, and also False when it was
    due but run_all_checks placeholdered it as database_unavailable (a
    transient outage during this pass, even one that recovers before the
    record_check_result calls below run and lets this whole function
    return normally). run_forever's cycles_since_integrity cadence must
    only reset on a genuine result, never merely because the pass as a
    whole didn't raise.
    """
    now = utcnow()
    with session_factory() as db:
        results = run_all_checks(
            db, settings, now_fn=lambda: now, include_db_integrity=include_db_integrity
        )
        for result in results:
            record_check_result(db, settings, result, now=now)
        if not include_db_integrity:
            return False
        return not any(
            r.key == DB_INTEGRITY_CHECK_KEY and r.reason == DB_UNAVAILABLE_REASON
            for r in results
        )


def run_forever(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    stop_event: threading.Event,
    instance_id: str,
) -> None:
    interval = settings.monitor_interval_seconds
    db_integrity_every = max(
        1, round(settings.monitor_db_integrity_interval_seconds / interval)
    )
    heartbeat_file = Path(settings.monitor_heartbeat_file)
    # Cycles since db_integrity last actually completed -- NOT a raw tick
    # counter. Starts due (>= db_integrity_every) so the very first pass
    # includes it. Advances by one on every ordinary tick, exactly like a
    # tick counter would, EXCEPT it is only reset back to zero once
    # db_integrity itself genuinely ran (run_one_pass's return value, not
    # merely "the pass didn't raise"): a database outage during a due
    # cycle degrades gracefully rather than raising (see run_all_checks),
    # so a pass that persisted a database_unavailable placeholder for
    # db_integrity still "completes" successfully -- without this
    # distinction, a transient outage that happens to recover before the
    # record_check_result calls finish would wrongly reset the cadence and
    # defer the real retry by a full db_integrity_every cycles. A pass
    # that failed before/during db_integrity (raised) also leaves it due,
    # so the very next tick retries it instead of waiting for the next
    # scheduled slot.
    cycles_since_integrity = db_integrity_every
    logger.info(
        "monitor_started",
        extra={
            "instance_id": instance_id,
            "interval_seconds": interval,
            "db_integrity_interval_seconds": settings.monitor_db_integrity_interval_seconds,
        },
    )
    while not stop_event.is_set():
        started = time.monotonic()
        include_db_integrity = cycles_since_integrity >= db_integrity_every
        cycle_completed = False
        db_integrity_completed = False
        error_code: str | None = None
        try:
            db_integrity_completed = run_one_pass(
                session_factory, settings, include_db_integrity=include_db_integrity
            )
            cycle_completed = True
            # Liveness heartbeat: container health checks verify this file
            # stays fresh (same convention as worker/admin-bot).
            try:
                heartbeat_file.touch()
            except OSError:
                logger.warning("monitor_heartbeat_write_failed")
        except Exception as exc:
            error_code = type(exc).__name__
            logger.exception("monitor_pass_failed")
        # Database heartbeat for operational visibility -- best-effort,
        # never stops the loop.
        try:
            with session_factory() as hb_session:
                record_worker_heartbeat(
                    hb_session,
                    worker_name=WORKER_NAME,
                    instance_id=instance_id,
                    now=utcnow(),
                    cycle_completed=cycle_completed,
                    error_code=error_code,
                )
        except Exception:
            logger.warning("monitor_db_heartbeat_failed")
        if db_integrity_completed:
            cycles_since_integrity = 0
        else:
            cycles_since_integrity += 1
        elapsed = time.monotonic() - started
        # Never a negative sleep: a pass that overran its interval starts
        # the next one immediately, but still strictly after this one
        # returned -- no concurrent copies.
        stop_event.wait(max(0.0, interval - elapsed))


def main() -> int:
    settings = Settings()
    configure_logging(settings)
    try:
        validate_monitor_settings(settings)
    except ConfigurationError as exc:
        # Names the variable, never the value. The API, worker, and admin
        # bot are unaffected: only this service validates monitor config.
        logger.error("monitor_configuration_invalid", extra={"reason": str(exc)})
        return 2

    session_factory = create_session_factory(settings.database_url)
    instance_id = build_instance_id()
    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    run_forever(settings, session_factory, stop_event=stop_event, instance_id=instance_id)
    logger.info("monitor_stopped", extra={"instance_id": instance_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
