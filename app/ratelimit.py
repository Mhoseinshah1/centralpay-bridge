"""Application-level rate limiting (compensating control for stock Caddy).

In-memory sliding windows, per process: with one API container this is a
global limit; running multiple API replicas multiplies the effective limit
by the replica count (documented). Keys are deliberately coarse — the only
trusted network source is Caddy on the internal network, and spoofable
headers like X-Forwarded-For are never consulted.
"""

import logging
import threading
import time
from collections import deque

from app.config import Settings

logger = logging.getLogger("app.ratelimit")


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, now: float | None = None) -> bool:
        """Record one event; True while the window stays within the limit."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class RateLimiters:
    """The three abuse-control limiters, built from settings."""

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.rate_limit_enabled
        # Authenticated create-payment bursts (legitimate bot traffic; the
        # default is far above normal shop volume).
        self.create = SlidingWindowLimiter(settings.rate_limit_create_per_minute, 60.0)
        # Stricter: repeated invalid API keys (credential guessing).
        self.invalid_api_key = SlidingWindowLimiter(
            settings.rate_limit_invalid_key_per_10min, 600.0
        )
        # Callback signature failures (probing); the aggregated admin alert
        # fires independently of this limiter.
        self.invalid_signature = SlidingWindowLimiter(
            settings.rate_limit_invalid_signature_per_10min, 600.0
        )

    def check(self, limiter: SlidingWindowLimiter, event: str) -> bool:
        """Returns True when allowed. Emits a structured security event on
        the first rejections; never raises."""
        if not self.enabled:
            return True
        if limiter.allow():
            return True
        logger.warning(
            "rate_limited",
            extra={"limiter": event, "limit": limiter.limit,
                   "window_seconds": limiter.window_seconds},
        )
        return False
