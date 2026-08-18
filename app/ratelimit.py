"""Application-level rate limiting (compensating control for stock Caddy).

In-memory sliding windows, per process: with one API container this is a
global limit; running multiple API replicas multiplies the effective limit
by the replica count (documented; see RATE_LIMITING_ARCHITECTURE.md). The
global limiters below are keyed by nothing (one shared bucket); the
per-IP limiters are keyed by app.clientip.resolve_client_ip's result, kept
in a bounded LRU store so a distributed/spoofed-source flood cannot grow
memory without limit (task: "avoid high-cardinality unbounded storage").

Every check() / check_per_ip() call fails OPEN on an unexpected internal
exception: a bug in this compensating control must never itself become a
denial of service against legitimate payments or gateway callbacks (see
RATE_LIMITING_ARCHITECTURE.md's fail-open/closed decision).
"""

import logging
import threading
import time
from collections import OrderedDict, deque

from app.config import Settings

logger = logging.getLogger("app.ratelimit")


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def allow(self, now: float | None = None) -> bool:
        """Record one event; True while the window stays within the limit."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True

    def retry_after(self, now: float | None = None) -> float:
        """Seconds until the window has room again (>= 0).

        Meaningful right after allow() returned False for the same
        (approximately) `now`; if the window currently has room this
        returns 0.0. A separate call from allow() under the same lock
        name, not one atomic operation with it -- a benign race under
        concurrent access can shift the precise value by a window tick,
        which only affects Retry-After's precision (a UX hint), never
        the limiter's own accept/reject correctness.
        """
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._events) < self.limit:
                return 0.0
            return max(0.0, self.window_seconds - (now - self._events[0]))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class BoundedLimiterStore:
    """LRU-bounded map of key -> SlidingWindowLimiter.

    Once at capacity, the least-recently-used key's limiter is evicted
    (its budget resets) to make room for a new key. This is an accepted
    tradeoff for a compensating control -- never a correctness
    requirement -- that keeps memory bounded regardless of how many
    distinct keys (real or spoofed) are observed.
    """

    def __init__(self, *, limit: int, window_seconds: float, capacity: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._capacity = capacity
        self._store: OrderedDict[str, SlidingWindowLimiter] = OrderedDict()
        self._lock = threading.Lock()

    def _get_or_create(self, key: str) -> SlidingWindowLimiter:
        with self._lock:
            limiter = self._store.get(key)
            if limiter is None:
                if len(self._store) >= self._capacity:
                    self._store.popitem(last=False)  # evict least-recently-used
                limiter = SlidingWindowLimiter(self._limit, self._window_seconds)
                self._store[key] = limiter
            else:
                self._store.move_to_end(key)
            return limiter

    def allow(self, key: str, now: float | None = None) -> bool:
        return self._get_or_create(key).allow(now=now)

    def retry_after(self, key: str, now: float | None = None) -> float:
        return self._get_or_create(key).retry_after(now=now)

    def reset(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


class RateLimiters:
    """The abuse-control limiters, built from settings.

    Global limiters (unchanged from the pre-existing design) are the
    outer emergency ceiling; per-IP limiters are an additional, narrower
    layer so one source can no longer exhaust the shared budget for
    every other legitimate caller. See RATE_LIMITING_ARCHITECTURE.md.
    """

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.rate_limit_enabled
        # Authenticated create-payment bursts (legitimate bot traffic; the
        # default is far above normal shop volume).
        self.create = SlidingWindowLimiter(settings.rate_limit_create_per_minute, 60.0)
        # Stricter: repeated invalid API keys (credential guessing). No
        # per-IP layer: with a single shared inbound_api_key, per-IP and
        # per-credential collapse to the same dimension the global limiter
        # already covers, so a second layer here would be redundant.
        self.invalid_api_key = SlidingWindowLimiter(
            settings.rate_limit_invalid_key_per_10min, 600.0
        )
        # Callback signature failures (probing); the aggregated admin alert
        # fires independently of this limiter.
        self.invalid_signature = SlidingWindowLimiter(
            settings.rate_limit_invalid_signature_per_10min, 600.0
        )
        self.create_per_ip = BoundedLimiterStore(
            limit=settings.rate_limit_create_per_ip_per_minute,
            window_seconds=60.0,
            capacity=settings.rate_limit_ip_bucket_capacity,
        )
        self.invalid_signature_per_ip = BoundedLimiterStore(
            limit=settings.rate_limit_invalid_signature_per_ip_per_10min,
            window_seconds=600.0,
            capacity=settings.rate_limit_ip_bucket_capacity,
        )

    def check(self, limiter: SlidingWindowLimiter, event: str) -> bool:
        """Returns True when allowed. Emits a structured security event on
        rejection; fails OPEN and logs on an unexpected internal error --
        never raises."""
        if not self.enabled:
            return True
        try:
            allowed = limiter.allow()
        except Exception:
            logger.exception(
                "rate_limiter_check_failed", extra={"limiter": event, "scope": "global"}
            )
            return True
        if allowed:
            return True
        logger.warning(
            "rate_limited",
            extra={
                "limiter": event,
                "scope": "global",
                "limit": limiter.limit,
                "window_seconds": limiter.window_seconds,
            },
        )
        return False

    def check_per_ip(self, store: BoundedLimiterStore, client_ip: str, event: str) -> bool:
        """Same contract as check(), keyed by client_ip within `store`."""
        if not self.enabled:
            return True
        try:
            allowed = store.allow(client_ip)
        except Exception:
            logger.exception(
                "rate_limiter_check_failed", extra={"limiter": event, "scope": "per_ip"}
            )
            return True
        if allowed:
            return True
        logger.warning(
            "rate_limited",
            extra={
                "limiter": event,
                "scope": "per_ip",
                "limit": store._limit,
                "window_seconds": store._window_seconds,
            },
        )
        return False

    def retry_after(self, limiter: SlidingWindowLimiter) -> float:
        """Safe Retry-After for a just-rejected global check(); never raises."""
        try:
            return limiter.retry_after()
        except Exception:
            logger.exception("rate_limiter_retry_after_failed")
            return limiter.window_seconds

    def retry_after_per_ip(self, store: BoundedLimiterStore, client_ip: str) -> float:
        """Safe Retry-After for a just-rejected check_per_ip(); never raises."""
        try:
            return store.retry_after(client_ip)
        except Exception:
            logger.exception("rate_limiter_retry_after_failed")
            return store._window_seconds
