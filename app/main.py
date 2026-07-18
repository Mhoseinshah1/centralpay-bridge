"""Application factory.

Use ``app.asgi:app`` as the uvicorn target; importing this module has no side
effects, which keeps tests and tooling free to build apps with their own
settings.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import callback, health, payments
from app.centralpay import CentralPayClient
from app.config import Settings
from app.db import create_session_factory
from app.exceptions import BridgeError
from app.logging_setup import configure_logging
from app.middleware import RequestContextMiddleware

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
        version="0.1.0",
        # API docs are disabled: this service exposes a payment API, not a
        # browsable surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.session_factory = create_session_factory(settings.database_url)
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
    return app
