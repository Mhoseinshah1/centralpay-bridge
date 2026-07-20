"""Application factory.

Use ``app.asgi:app`` as the uvicorn target; importing this module has no side
effects, which keeps tests and tooling free to build apps with their own
settings.
"""

import logging
import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import callback, health, payments
from app.centralpay import CentralPayClient
from app.config import Settings
from app.db import create_session_factory
from app.exceptions import BridgeError
from app.logging_setup import configure_logging
from app.middleware import RequestContextMiddleware
from app.version import APP_VERSION

logger = logging.getLogger("app.main")


async def _bridge_error_handler(request: Request, exc: BridgeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # FastAPI's default handler echoes submitted input back in the error body,
    # which could reflect credentials. Return field locations and messages only.
    errors = [
        {"loc": [str(part) for part in error.get("loc", [])], "msg": error.get("msg", "")}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "Invalid request"},
                 "detail": errors},
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Internal server error"}},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)
    # Enables admin alert outbox rows (a no-op when the admin bot is
    # disabled). The API never contacts Telegram; a Telegram outage can
    # never affect payment processing.
    from app.adminbot.alerts import configure_alert_creation

    configure_alert_creation(settings)

    app = FastAPI(
        title="CentralPay Bridge",
        version=APP_VERSION,
        # API docs are disabled: this service exposes a payment API, not a
        # browsable surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.session_factory = create_session_factory(settings.database_url)
    from app.ratelimit import RateLimiters

    app.state.rate_limiters = RateLimiters(settings)
    app.state.centralpay = CentralPayClient(
        base_url=settings.centralpay_base_url,
        getlink_api_key=settings.centralpay_getlink_api_key,
        verify_api_key=settings.centralpay_verify_api_key,
        timeout_seconds=settings.centralpay_timeout_seconds,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(BridgeError, _bridge_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_error_handler)

    app.include_router(health.router)
    app.include_router(payments.router)
    app.include_router(callback.router)
    # Repository-local font assets for the payer success page (Vazirmatn,
    # OFL — see app/static/fonts/). StaticFiles serves only regular files
    # under this fixed directory: no directory listing, no traversal, and
    # nothing outside app/static is exposed. The woff2 MIME type is
    # registered explicitly: python:slim images ship no /etc/mime.types,
    # where guess_type would otherwise fall back to application/octet-stream.
    mimetypes.add_type("font/woff2", ".woff2")
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )
    return app
