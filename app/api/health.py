"""Liveness and readiness endpoints."""

import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.version import APP_VERSION

logger = logging.getLogger("app.api.health")

router = APIRouter()


@router.get("/health/live")
def health_live() -> dict[str, str]:
    # Version is non-sensitive and helps operators confirm deployed code.
    return {"status": "alive", "version": APP_VERSION}


@router.get("/health/details")
def health_details(request: Request, response: Response) -> dict[str, object]:
    """Machine-readable operational detail for internal consumers.

    NOT proxied by Caddy (only the four public routes are) — reachable
    solely on the internal Docker network. Contains no secrets.
    """
    from app.adminbot import queries
    from app.config import Settings

    settings: Settings = request.app.state.settings
    session = request.app.state.session_factory()
    try:
        db_ok = queries.database_ok(session)
        heartbeat_age = queries.worker_heartbeat_age_seconds(session) if db_ok else None
        backup = queries.latest_backup_alert(session, "backup_succeeded") if db_ok else None
        details: dict[str, object] = {
            "status": "ready" if db_ok else "degraded",
            "version": APP_VERSION,
            "build_sha": settings.git_commit_sha or None,
            "environment": settings.environment,
            "database": "ok" if db_ok else "error",
            "migration_revision": queries.migration_revision(session) if db_ok else None,
            "worker_heartbeat_age_seconds": (
                int(heartbeat_age) if heartbeat_age is not None else None
            ),
            "pending_notifications": (
                queries.count_by_status(session, "bot_notify_pending") if db_ok else None
            ),
            "manual_review": (
                queries.count_by_status(session, "manual_review") if db_ok else None
            ),
            "alert_queue": queries.alert_queue_stats(session) if db_ok else None,
            "last_backup_at": (
                backup.created_at.isoformat() if backup is not None else None
            ),
        }
    except Exception:
        logger.exception("health_details_failed")
        response.status_code = 503
        return {"status": "error", "version": APP_VERSION}
    finally:
        session.close()
    if not db_ok:
        response.status_code = 503
    return details


@router.get("/health/ready")
def health_ready(request: Request, response: Response) -> dict[str, str]:
    session = request.app.state.session_factory()
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("service_unhealthy")
        response.status_code = 503
        return {"status": "unavailable", "database": "error"}
    finally:
        session.close()
    return {"status": "ready", "database": "ok"}
