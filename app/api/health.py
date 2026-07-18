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
