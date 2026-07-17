"""Request ID propagation and safe request logging.

Accepts an X-Request-ID header from a trusted reverse proxy (sanitized) or
generates one, stores it in a context variable for logs and audit events, and
returns it in the response. Request logging records method and path only —
never the query string, which may contain callback signatures.
"""

import logging
import re
import time
import uuid
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_setup import request_id_var

logger = logging.getLogger("app.http")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _incoming_request_id(scope: Scope) -> str | None:
    value = Headers(scope=scope).get("x-request-id")
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        status_holder: dict[str, Any] = {}
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "http_request",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status_code": status_holder.get("status"),
                    "duration_ms": duration_ms,
                },
            )
            request_id_var.reset(token)
