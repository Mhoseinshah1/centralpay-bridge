"""Admin bot service entrypoint: python -m app.adminbot"""

import asyncio
import logging
import signal

from app.adminbot.alerts import configure_alert_creation
from app.adminbot.runner import AdminBotService
from app.audit import record_event
from app.config import ConfigurationError, Settings, validate_admin_bot_settings
from app.db import create_session_factory
from app.logging_setup import configure_logging

logger = logging.getLogger("app.adminbot.main")


def main() -> int:
    settings = Settings()
    configure_logging(settings)
    try:
        admin_ids = validate_admin_bot_settings(settings)
    except ConfigurationError as exc:
        # Names the variable, never the value. The API and worker are
        # unaffected: only this service validates admin bot configuration.
        logger.error("admin_bot_config_invalid", extra={"reason": str(exc)})
        return 2

    session_factory = create_session_factory(settings.database_url)
    configure_alert_creation(settings)
    service = AdminBotService(settings, session_factory, admin_ids)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, service.stop_event.set)

    with session_factory() as db:
        record_event(
            db,
            payment_id=None,
            event_type="admin_bot_started",
            data={"administrators": len(admin_ids)},
        )
        db.commit()
    try:
        loop.run_until_complete(service.run())
    finally:
        with session_factory() as db:
            record_event(db, payment_id=None, event_type="admin_bot_stopped", data=None)
            db.commit()
        logger.info("admin_bot_stopped")
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
