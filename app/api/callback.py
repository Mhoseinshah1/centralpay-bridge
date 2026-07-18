"""GET /api/centralpay/callback — signed return URL from CentralPay.

The HMAC signature is validated before any database or gateway processing.
The signature value and the full query string are never logged. Invalid
signatures never touch the database per request; only an aggregated storm
alert (threshold within a rolling window) writes a single alert row.
"""

import logging
import threading
import time as time_module
from collections import deque

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.api.deps import CentralPayDep, DbDep, SettingsDep
from app.api.pages import payment_status_page
from app.exceptions import InvalidCallbackSignatureError, RateLimitedError
from app.security import verify_callback_signature
from app.services.verification import process_callback

logger = logging.getLogger("app.api.callback")

router = APIRouter()


class SignatureFailureTracker:
    """In-memory counter for invalid callback signatures.

    Keeps the invalid-signature path free of per-request database work while
    still surfacing storms: crossing the threshold within the window reports
    once per window.
    """

    def __init__(self, threshold: int = 5, window_seconds: float = 600.0) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()
        self._lock = threading.Lock()
        # None means "never reported". A numeric sentinel like 0.0 would be
        # wrong: time.monotonic() has an arbitrary epoch, and on a freshly
        # booted machine (CI runners, newly provisioned servers) it can be
        # SMALLER than window_seconds, which would suppress the very first
        # storm report.
        self._last_reported: float | None = None

    def record(self, now: float | None = None) -> int | None:
        """Record one failure; returns the count when a storm should be
        reported, else None."""
        now = now if now is not None else time_module.monotonic()
        with self._lock:
            self._events.append(now)
            cutoff = now - self.window_seconds
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            count = len(self._events)
            if count >= self.threshold and (
                self._last_reported is None
                or now - self._last_reported >= self.window_seconds
            ):
                self._last_reported = now
                return count
        return None

    def reset(self) -> None:
        """Return to the initial state (used by tests)."""
        with self._lock:
            self._events.clear()
            self._last_reported = None


signature_failure_tracker = SignatureFailureTracker()


@router.get("/api/centralpay/callback", response_class=HTMLResponse)
def centralpay_callback(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    client: CentralPayDep,
    order_id: int = Query(alias="orderId"),
    ct: str = Query(min_length=1, max_length=64),
    sig: str = Query(min_length=1, max_length=128),
) -> HTMLResponse:
    # The signature binds orderId and the one-time callback token together;
    # both are validated cryptographically before any database work. The
    # token's durable consumption state is checked inside the row lock.
    if not verify_callback_signature(settings.callback_hmac_secret, order_id, ct, sig):
        logger.warning("callback_signature_invalid", extra={"gateway_order_id": order_id})
        limiters = request.app.state.rate_limiters
        rate_ok = limiters.check(limiters.invalid_signature, "invalid_callback_signature")
        storm_count = signature_failure_tracker.record()
        if storm_count is not None:
            # Best-effort aggregated alert; failure never affects the response.
            try:
                from app.adminbot.alerts import create_alert

                create_alert(
                    db,
                    alert_type="callback_signature_failures",
                    severity="warning",
                    deduplication_key="callback_signature_failures",
                    payload={"count": storm_count, "window_seconds": 600},
                )
                db.commit()
            except Exception:
                logger.exception("signature_storm_alert_failed")
        if not rate_ok:
            raise RateLimitedError()
        raise InvalidCallbackSignatureError()
    result = process_callback(
        db, client, gateway_order_id=order_id, callback_token=ct, settings=settings
    )
    # Once CentralPay verification has succeeded the payer always gets a
    # success page, even while bot delivery is pending or under review.
    return HTMLResponse(
        payment_status_page(
            result.status,
            result.bot_order_id,
            bot_username=settings.telegram_bot_username,
        )
    )
