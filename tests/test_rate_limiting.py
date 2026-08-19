"""Rate limiting & abuse protection: end-to-end and object-level coverage.

See RATE_LIMITING_ARCHITECTURE.md for the full design. Existing coverage
in tests/test_phase5_hardening.py (the pre-existing global limiters) and
tests/test_callback_hardening.py (unbounded-memory regressions) is left
untouched; this file covers everything added in this PR: per-IP layering,
Retry-After, idempotency-aware ordering, fail-open behavior, config
validation, and concurrency.
"""

import json
import logging
import threading
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.models import CentralPayPayerIdentity, Payment, PaymentEvent, PaymentStatus
from app.ratelimit import BoundedLimiterStore, RateLimiters, SlidingWindowLimiter
from app.services.payer_identity import IDENTITY_TYPE_ORDER_FALLBACK
from tests.conftest import (
    DEFAULT_GATEWAY_USER_ID,
    DEFAULT_TELEGRAM_USER_ID,
    create_order,
    get_events,
    get_payment,
)


def _identity_row_count(session_factory) -> int:
    with session_factory() as db:
        return db.execute(select(func.count(CentralPayPayerIdentity.id))).scalar_one()

# --- object-level: SlidingWindowLimiter / BoundedLimiterStore ---------------


def test_below_limit_requests_succeed():
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60.0)
    assert [limiter.allow(now=100.0) for _ in range(4)] == [True, True, True, True]


def test_exact_boundary_succeeds_then_fails():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60.0)
    results = [limiter.allow(now=100.0) for _ in range(4)]
    assert results == [True, True, True, False]


def test_window_reset_behavior():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60.0)
    assert limiter.allow(now=100.0) is True
    assert limiter.allow(now=100.0) is True
    assert limiter.allow(now=100.0) is False
    # Still inside the window: no recovery yet.
    assert limiter.allow(now=159.9) is False
    # Outside the window: the earliest event has expired, budget recovers.
    assert limiter.allow(now=160.1) is True


def test_retry_after_is_zero_with_room_and_positive_when_full():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60.0)
    assert limiter.retry_after(now=100.0) == 0.0
    limiter.allow(now=100.0)
    assert limiter.retry_after(now=100.0) == 0.0  # one slot still free
    limiter.allow(now=110.0)
    # Full: the oldest event (t=100) frees up at t=160 -> 50s remain at t=110.
    assert limiter.retry_after(now=110.0) == pytest.approx(50.0)
    assert limiter.retry_after(now=161.0) == 0.0  # oldest has since expired


def test_bounded_store_isolates_distinct_keys():
    store = BoundedLimiterStore(limit=2, window_seconds=60.0, capacity=10)
    assert store.allow("ip-a", now=100.0) is True
    assert store.allow("ip-a", now=100.0) is True
    assert store.allow("ip-a", now=100.0) is False  # ip-a exhausted
    assert store.allow("ip-b", now=100.0) is True  # ip-b unaffected
    assert store.allow("ip-b", now=100.0) is True


def test_bounded_store_evicts_least_recently_used_beyond_capacity():
    store = BoundedLimiterStore(limit=10, window_seconds=60.0, capacity=2)
    store.allow("ip-a", now=100.0)
    store.allow("ip-b", now=100.0)
    assert len(store) == 2
    store.allow("ip-c", now=100.0)  # evicts ip-a (least recently used)
    assert len(store) == 2
    # ip-a's budget was reset by eviction -- a fresh limiter, not carried
    # forward state. This is the documented, accepted tradeoff (§6).
    assert store.retry_after("ip-a", now=100.0) == 0.0


def test_bounded_store_memory_bounded_under_high_cardinality_flood():
    """Task: 'avoid high-cardinality unbounded storage' -- thousands of
    distinct (e.g. spoofed) keys must never grow the store past capacity."""
    store = BoundedLimiterStore(limit=5, window_seconds=60.0, capacity=100)
    for i in range(50_000):
        store.allow(f"203.0.113.{i % 65536}.{i}", now=100.0 + i * 0.0001)
    assert len(store) <= 100


def test_concurrent_requests_cannot_bypass_sliding_window_limiter():
    """Task: 'N simultaneous requests at threshold; N+1 must be rejected
    exactly as expected.' Real OS threads, no mocked time, proving the
    internal lock actually serializes concurrent access rather than
    allowing a race to admit more than `limit` callers."""
    limit = 20
    concurrency = 60
    limiter = SlidingWindowLimiter(limit=limit, window_seconds=60.0)
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(concurrency)

    def worker() -> None:
        barrier.wait()
        outcome = limiter.allow()
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == limit
    assert results.count(False) == concurrency - limit


def test_concurrent_requests_cannot_bypass_bounded_store_for_one_key():
    limit = 15
    concurrency = 50
    store = BoundedLimiterStore(limit=limit, window_seconds=60.0, capacity=1000)
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(concurrency)

    def worker() -> None:
        barrier.wait()
        outcome = store.allow("shared-ip")
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == limit


def test_concurrent_different_keys_do_not_interfere():
    """Race idea: 'different-client isolation' under real concurrency."""
    store = BoundedLimiterStore(limit=3, window_seconds=60.0, capacity=1000)
    outcomes: dict[str, list[bool]] = {"ip-a": [], "ip-b": []}
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(key: str) -> None:
        barrier.wait()
        outcome = store.allow(key)
        with lock:
            outcomes[key].append(outcome)

    threads = [
        threading.Thread(target=worker, args=(("ip-a", "ip-b")[i % 2],)) for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(outcomes["ip-a"]) == 3
    assert sum(outcomes["ip-b"]) == 3


# --- RateLimiters: fail-open behavior ----------------------------------------


def test_check_fails_open_on_internal_error(settings, caplog):
    limiters = RateLimiters(settings)

    class Boom(SlidingWindowLimiter):
        def allow(self, now: float | None = None) -> bool:
            raise RuntimeError("simulated limiter backend fault")

    broken = Boom(limit=1, window_seconds=60.0)
    with caplog.at_level(logging.ERROR, logger="app.ratelimit"):
        assert limiters.check(broken, "create_payment") is True
    assert any(r.message == "rate_limiter_check_failed" for r in caplog.records)


def test_check_per_ip_fails_open_on_internal_error(settings, caplog):
    limiters = RateLimiters(settings)

    class BoomStore(BoundedLimiterStore):
        def allow(self, key: str, now: float | None = None) -> bool:
            raise RuntimeError("simulated limiter backend fault")

    broken = BoomStore(limit=1, window_seconds=60.0, capacity=10)
    with caplog.at_level(logging.ERROR, logger="app.ratelimit"):
        assert limiters.check_per_ip(broken, "203.0.113.9", "create_payment") is True
    assert any(r.message == "rate_limiter_check_failed" for r in caplog.records)


def test_retry_after_helpers_fail_safe_on_internal_error(settings, caplog):
    limiters = RateLimiters(settings)

    class Boom(SlidingWindowLimiter):
        def retry_after(self, now: float | None = None) -> float:
            raise RuntimeError("simulated fault")

    broken = Boom(limit=1, window_seconds=42.0)
    with caplog.at_level(logging.ERROR, logger="app.ratelimit"):
        assert limiters.retry_after(broken) == 42.0  # falls back to the window


def test_disabled_rate_limiting_allows_unlimited_requests(settings):
    disabled = settings.model_copy(update={"rate_limit_enabled": False})
    limiters = RateLimiters(disabled)
    tiny = SlidingWindowLimiter(limit=1, window_seconds=60.0)
    assert all(limiters.check(tiny, "create_payment") for _ in range(10))


# --- config validation --------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "rate_limit_create_per_minute",
        "rate_limit_invalid_key_per_10min",
        "rate_limit_invalid_signature_per_10min",
        "rate_limit_create_per_ip_per_minute",
        "rate_limit_invalid_signature_per_ip_per_10min",
        "rate_limit_ip_bucket_capacity",
    ],
)
@pytest.mark.parametrize("bad_value", [0, -1])
def test_config_rejects_nonpositive_rate_limit_fields(settings, field, bad_value):
    from pydantic import ValidationError

    data = settings.model_dump()
    data[field] = bad_value
    with pytest.raises(ValidationError, match=r"(?i)greater than"):
        type(settings).model_validate(data)


# --- HTTP-level: per-IP layering, Retry-After, isolation ---------------------


def test_create_per_ip_limit_boundary_over_http(app, client, settings):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=3, window_seconds=60.0, capacity=10)
    codes = [
        create_order(client, settings, order_id=f"rl-ip-{i}").status_code for i in range(5)
    ]
    assert codes[:3] == [200, 200, 200]
    assert set(codes[3:]) == {429}


def test_create_per_ip_429_includes_retry_after_header(app, client, settings):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=1, window_seconds=45.0, capacity=10)
    create_order(client, settings, order_id="rl-ra-1")
    response = create_order(client, settings, order_id="rl-ra-2")
    assert response.status_code == 429
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None
    assert 0 < int(retry_after) <= 45


def test_429_response_envelope_matches_project_error_shape(app, client, settings):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    response = create_order(client, settings, order_id="rl-envelope")
    assert response.status_code == 429
    payload = response.json()
    assert payload == {"error": {"code": "rate_limited", "message": "Too many requests"}}


def test_success_responses_never_carry_a_retry_after_header(client, settings):
    response = create_order(client, settings, order_id="rl-no-header")
    assert response.status_code == 200
    assert "Retry-After" not in response.headers


def test_different_ips_do_not_share_the_create_bucket(app, client, settings):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=1, window_seconds=60.0, capacity=10)
    first = client.post(
        "/api/custom-payment",
        json={"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "rl-ip-a"},
        headers={"X-Forwarded-For": "198.51.100.10"},
    )
    second = client.post(
        "/api/custom-payment",
        json={"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "rl-ip-b"},
        headers={"X-Forwarded-For": "198.51.100.11"},
    )
    assert first.status_code == 200
    assert second.status_code == 200  # a DIFFERENT IP has its own budget


def test_ipv4_and_ipv6_callers_are_isolated_from_each_other(app, client, settings):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=1, window_seconds=60.0, capacity=10)
    v4 = client.post(
        "/api/custom-payment",
        json={"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "rl-v4"},
        headers={"X-Forwarded-For": "198.51.100.20"},
    )
    v6 = client.post(
        "/api/custom-payment",
        json={"api_key": settings.inbound_api_key, "amount": 10000, "order_id": "rl-v6"},
        headers={"X-Forwarded-For": "2001:db8::42"},
    )
    assert v4.status_code == 200
    assert v6.status_code == 200


def test_spoofed_forwarding_header_cannot_bypass_the_global_ceiling(app, client, settings):
    """Fragmenting requests across many fake X-Forwarded-For values must
    never defeat the GLOBAL emergency ceiling -- only the per-IP layer is
    keyed by the (trusted-but-attacker-choosable-when-behind-a-leaked-key)
    header; the global limiter counts every request regardless."""
    app.state.rate_limiters.create = SlidingWindowLimiter(limit=5, window_seconds=60.0)
    codes = []
    for i in range(8):
        response = client.post(
            "/api/custom-payment",
            json={
                "api_key": settings.inbound_api_key,
                "amount": 10000,
                "order_id": f"rl-spoof-{i}",
            },
            headers={"X-Forwarded-For": f"10.{i}.{i}.{i}"},  # a fresh fake IP every time
        )
        codes.append(response.status_code)
    assert codes[:5] == [200] * 5
    assert set(codes[5:]) == {429}  # the global ceiling still catches it


def test_create_and_invalid_signature_limiters_are_isolated(app, client, settings):
    """Different endpoint classes must not share a bucket: exhausting the
    create-per-IP limiter must not affect the invalid-signature-per-IP
    limiter for the exact same caller."""
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    exhausted = create_order(client, settings, order_id="rl-cross-1")
    assert exhausted.status_code == 429

    # invalid_signature_per_ip is untouched -- the callback path must
    # still evaluate signature validity normally (403), never 429, for
    # the exact same caller whose create budget is fully exhausted.
    response = client.get(
        "/api/centralpay/callback?orderId=1&ct=" + "a" * 32 + "&sig=" + "0" * 64
    )
    assert response.status_code == 403


# --- health endpoints always available ---------------------------------------


def test_health_endpoints_available_while_every_limiter_is_saturated(app, client, settings):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.invalid_api_key = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.invalid_signature = SlidingWindowLimiter(
        limit=0, window_seconds=60.0
    )
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    app.state.rate_limiters.invalid_signature_per_ip = _Store(
        limit=0, window_seconds=60.0, capacity=10
    )
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


# --- no side effects on a rejected request -----------------------------------


def test_no_db_mutation_on_rejected_new_order_create(app, client, settings, session_factory):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    with session_factory() as db:
        before = db.execute(select(func.count(Payment.id))).scalar_one()
    response = create_order(client, settings, order_id="rl-no-mutation")
    assert response.status_code == 429
    with session_factory() as db:
        after = db.execute(select(func.count(Payment.id))).scalar_one()
    assert after == before


def test_no_gateway_call_on_rejected_new_order_create(app, client, settings, stub):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    before = len(stub.getlink_requests)
    response = create_order(client, settings, order_id="rl-no-gateway-call")
    assert response.status_code == 429
    assert len(stub.getlink_requests) == before


def test_no_events_recorded_for_a_rejected_new_order(app, client, settings, session_factory):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    with session_factory() as db:
        before = db.execute(select(func.count(PaymentEvent.id))).scalar_one()
    response = create_order(client, settings, order_id="rl-no-events")
    assert response.status_code == 429
    with session_factory() as db:
        after = db.execute(select(func.count(PaymentEvent.id))).scalar_one()
    assert after == before


def test_no_verify_call_on_rejected_invalid_signature(app, client, settings, stub):
    app.state.rate_limiters.invalid_signature = SlidingWindowLimiter(
        limit=0, window_seconds=60.0
    )
    before = len(stub.verify_requests)
    response = client.get(
        "/api/centralpay/callback?orderId=1&ct=" + "a" * 32 + "&sig=" + "0" * 64
    )
    assert response.status_code == 429
    assert len(stub.verify_requests) == before


# --- idempotency interaction --------------------------------------------------


def test_idempotent_retry_bypasses_the_create_limiter_entirely(
    app, client, settings, stub
):
    """The critical proof: once an order has a live link, the create
    limiter (global AND per-IP) can be fully exhausted and the retry must
    still succeed with the SAME cached link -- never a 429, never a
    second gateway call."""
    from app.ratelimit import BoundedLimiterStore as _Store

    first = create_order(client, settings, order_id="rl-idem-1")
    assert first.status_code == 200
    original_url = first.json()["url"]
    getlink_calls_after_first = len(stub.getlink_requests)

    # Now fully exhaust BOTH create limiters -- a brand new order would
    # be rejected immediately.
    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    brand_new = create_order(client, settings, order_id="rl-idem-brand-new")
    assert brand_new.status_code == 429

    # The EXISTING order's retry is unaffected by the same exhausted limiters.
    retry = create_order(client, settings, order_id="rl-idem-1")
    assert retry.status_code == 200
    assert retry.json()["url"] == original_url
    assert len(stub.getlink_requests) == getlink_calls_after_first  # no new gateway call


def test_idempotent_retry_with_mismatched_amount_does_not_bypass_the_limiter(
    app, client, settings
):
    """Regression (see the GETLINK_FAILED tests below for the full story):
    "a row exists" is NOT sufficient for the create-limiter exemption. A
    retry with a DIFFERENT amount is not the safe cached replay -- it is
    exactly the kind of request that would make create_payment() do real
    work (record a duplicate_order_amount_mismatch event and commit it), so
    it must consume limiter budget exactly like a genuinely new order."""
    from app.ratelimit import BoundedLimiterStore as _Store

    original = create_order(client, settings, order_id="rl-idem-mismatch", amount=10000)
    assert original.status_code == 200
    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    conflicting = create_order(
        client, settings, order_id="rl-idem-mismatch", amount=99999
    )
    assert conflicting.status_code == 429  # the limiter gates it -- never reaches create_payment()


# --- regression: "row exists" is not "safe cached replay" -------------------
#
# app/api/payments.py originally skipped the create limiter for ANY existing
# bot_order_id row. create_payment() retries link creation -- a real
# get_link() gateway call, a fresh gateway_order_id -- for a row stuck in
# GETLINK_FAILED, so that blanket exemption let a caller replay one failed
# order id forever and hit the real gateway on every attempt while the
# create limiter was fully exhausted, defeating it for exactly the traffic
# shape (retries against one order id) it exists to bound. Fixed by
# find_safe_replay_redirect_url() (app/services/payments.py): the limiter is
# skipped ONLY for a request that is provably a zero-work, zero-gateway-call
# LINK_CREATED cached replay with matching amount and identity.


def test_getlink_failed_row_does_not_bypass_the_create_limiter(
    app, client, settings, stub, session_factory
):
    """THE REGRESSION, closed: a row existing is NOT sufficient for the
    create-limiter exemption. Proves the retry is rejected (429, not a
    second gateway attempt); stays rejected across REPEATED attempts while
    the limiter is exhausted; and the rejection is a true no-op (no new
    gateway call, no status/gateway_order_id/last_error change, no new
    audit events)."""
    from app.ratelimit import BoundedLimiterStore as _Store

    stub.getlink_result = httpx.ConnectError("connection refused")
    assert create_order(client, settings, order_id="rl-getlink-failed").status_code == 502
    before = get_payment(session_factory, "rl-getlink-failed")
    assert before.status == PaymentStatus.GETLINK_FAILED.value
    events_before = len(get_events(session_factory, before.id))
    getlink_calls_before_retries = len(stub.getlink_requests)

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)

    for _ in range(3):  # repeated retries must all stay rejected, not just the first
        retry = create_order(client, settings, order_id="rl-getlink-failed")
        assert retry.status_code == 429

    assert len(stub.getlink_requests) == getlink_calls_before_retries  # zero new gateway calls
    after = get_payment(session_factory, "rl-getlink-failed")
    assert after.gateway_order_id == before.gateway_order_id  # never re-allocated
    assert after.status == PaymentStatus.GETLINK_FAILED.value  # never retried
    assert after.last_error == before.last_error
    assert len(get_events(session_factory, before.id)) == events_before  # no amplification


def test_created_no_live_link_row_does_not_bypass_the_create_limiter(
    app, client, settings, session_factory
):
    """A row can exist in CREATED status with no live link yet (the process
    died between _ensure_payment_row committing and get_link() completing).
    It is not a safe cached replay either and must consume limiter budget
    on retry."""
    from app.ratelimit import BoundedLimiterStore as _Store

    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="rl-created-no-link",
                gateway_order_id=900_001,
                gateway_user_id=DEFAULT_GATEWAY_USER_ID,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.CREATED.value,
            )
        )
        db.commit()

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    response = create_order(client, settings, order_id="rl-created-no-link", amount=10000)
    assert response.status_code == 429


def test_idempotent_retry_with_mismatched_identity_does_not_bypass_the_limiter(
    app, client, settings
):
    """A retry claiming a DIFFERENT Telegram user for an existing linked
    order is the customer-mismatch case create_payment() itself refuses
    (incident 2026-07) -- it is not a safe exemption either."""
    from app.ratelimit import BoundedLimiterStore as _Store

    original = create_order(
        client, settings, order_id="rl-idem-identity-mismatch",
        telegram_user_id=DEFAULT_TELEGRAM_USER_ID,
    )
    assert original.status_code == 200
    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    conflicting = create_order(
        client, settings, order_id="rl-idem-identity-mismatch",
        telegram_user_id=DEFAULT_TELEGRAM_USER_ID + 1,
    )
    assert conflicting.status_code == 429


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        (PaymentStatus.GATEWAY_VERIFIED.value, {"gateway_verified_at": datetime.now(UTC)}),
        (PaymentStatus.MANUAL_REVIEW.value, {}),
    ],
)
def test_verified_or_under_review_rows_do_not_bypass_the_create_limiter(
    app, client, settings, session_factory, status, extra
):
    """Documented policy: ONLY a live LINK_CREATED replay is exempt.
    create_payment() answers an already-verified or under-review order
    read-only too (it just raises), but this PR does not extend the
    exemption to those states -- the smallest correct rule is the one
    stated by the task: same order, same amount, same identity, live
    LINK_CREATED/redirect_url, zero gateway call, zero mutation. Nothing
    is gained by special-casing these read-only-refusal states, and it
    would widen the surface find_safe_replay_redirect_url() has to keep in
    sync with create_payment()."""
    from app.ratelimit import BoundedLimiterStore as _Store

    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="rl-non-cache-state",
                gateway_order_id=900_002,
                gateway_user_id=DEFAULT_GATEWAY_USER_ID,
                amount=10000,
                payable_amount=10000,
                status=status,
                **extra,
            )
        )
        db.commit()

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    response = create_order(client, settings, order_id="rl-non-cache-state", amount=10000)
    assert response.status_code == 429


# --- regression #2: identity resolution can mutate even when
# _reconcile_identity() would end up "reuse" ----------------------------------
#
# find_safe_replay_redirect_url() originally rejected the exemption only for
# a stored telegram_user row given a DIFFERENT Telegram id. But
# create_payment() calls resolve_payer_identity() for the REQUESTED identity
# BEFORE the row lock and BEFORE _reconcile_identity() ever runs -- and that
# call can create and commit a brand new CentralPayPayerIdentity row (plus a
# centralpay_payer_identity_created audit event) even when the eventual
# reconciliation decision is harmless "reuse". A stored order_fallback row
# retried with an arbitrary NEW Telegram id -- or a stored telegram_user row
# retried with NO Telegram id -- both hit that write path while still being
# wrongly treated as a safe, limiter-exempt replay. Fixed: the exemption now
# requires the requested identity shape to exactly match the stored one.


def test_order_fallback_replay_with_no_telegram_id_still_bypasses_the_limiter(
    app, client, settings, stub, session_factory
):
    """The true safe case for an order_fallback row: a bare retry (no
    Telegram id) re-derives the exact identity_key this payment's mapping
    was already resolved under -- genuinely zero gateway calls, zero new
    identity rows, zero new events, even under an exhausted limiter."""
    from app.ratelimit import BoundedLimiterStore as _Store

    first = create_order(client, settings, order_id="rl-fallback-safe", telegram_user_id=None)
    assert first.status_code == 200
    original_url = first.json()["url"]
    payment = get_payment(session_factory, "rl-fallback-safe")
    assert payment.payer_identity_type == IDENTITY_TYPE_ORDER_FALLBACK
    getlink_calls_before = len(stub.getlink_requests)
    identities_before = _identity_row_count(session_factory)
    events_before = len(get_events(session_factory))

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)

    retry = create_order(client, settings, order_id="rl-fallback-safe", telegram_user_id=None)
    assert retry.status_code == 200
    assert retry.json()["url"] == original_url
    assert len(stub.getlink_requests) == getlink_calls_before
    assert _identity_row_count(session_factory) == identities_before
    assert len(get_events(session_factory)) == events_before


def test_order_fallback_replay_with_new_telegram_id_does_not_bypass_the_limiter(
    app, client, settings, stub, session_factory
):
    """THE REGRESSION: a stored order_fallback row retried with an
    arbitrary, never-before-seen Telegram id must NOT be treated as a safe
    replay -- resolve_payer_identity() would create and commit a brand new
    identity row (plus an audit event) for that Telegram id before
    _reconcile_identity() gets a chance to harmlessly "reuse" the stored
    identity. Proves: rejected (429) under an exhausted limiter for THREE
    different Telegram ids, zero new identity rows, zero new events, zero
    gateway calls -- the mutation the bug allowed never happens."""
    from app.ratelimit import BoundedLimiterStore as _Store

    first = create_order(client, settings, order_id="rl-fallback-unsafe", telegram_user_id=None)
    assert first.status_code == 200
    getlink_calls_before = len(stub.getlink_requests)
    identities_before = _identity_row_count(session_factory)
    events_before = len(get_events(session_factory))

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)

    for candidate_id in (DEFAULT_TELEGRAM_USER_ID, DEFAULT_TELEGRAM_USER_ID + 1, 999_999_999):
        response = create_order(
            client, settings, order_id="rl-fallback-unsafe", telegram_user_id=candidate_id
        )
        assert response.status_code == 429

    assert len(stub.getlink_requests) == getlink_calls_before
    assert _identity_row_count(session_factory) == identities_before
    assert len(get_events(session_factory)) == events_before


def test_telegram_user_replay_with_matching_telegram_id_still_bypasses_the_limiter(
    app, client, settings, stub, session_factory
):
    """The true safe case for a telegram_user row: the exact same Telegram
    id re-derives the exact identity_key this payment's mapping was already
    resolved under -- genuinely zero gateway calls, zero new identity rows."""
    from app.ratelimit import BoundedLimiterStore as _Store

    first = create_order(
        client, settings, order_id="rl-telegram-safe",
        telegram_user_id=DEFAULT_TELEGRAM_USER_ID,
    )
    assert first.status_code == 200
    original_url = first.json()["url"]
    getlink_calls_before = len(stub.getlink_requests)
    identities_before = _identity_row_count(session_factory)

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)

    retry = create_order(
        client, settings, order_id="rl-telegram-safe",
        telegram_user_id=DEFAULT_TELEGRAM_USER_ID,
    )
    assert retry.status_code == 200
    assert retry.json()["url"] == original_url
    assert len(stub.getlink_requests) == getlink_calls_before
    assert _identity_row_count(session_factory) == identities_before


def test_telegram_user_replay_with_missing_telegram_id_does_not_bypass_the_limiter(
    app, client, settings
):
    """A stored telegram_user row retried WITHOUT a Telegram id requests the
    order_fallback identity_key instead -- a DIFFERENT identity_key than
    what this payment's mapping was resolved under, so
    resolve_payer_identity() is not provably a no-write lookup. Must not be
    exempt, even though _reconcile_identity() would still "reuse" the
    stored Telegram identity."""
    from app.ratelimit import BoundedLimiterStore as _Store

    first = create_order(
        client, settings, order_id="rl-telegram-no-id",
        telegram_user_id=DEFAULT_TELEGRAM_USER_ID,
    )
    assert first.status_code == 200

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)

    retry = create_order(client, settings, order_id="rl-telegram-no-id", telegram_user_id=None)
    assert retry.status_code == 429


def test_legacy_untyped_linked_row_does_not_bypass_the_create_limiter(
    app, client, settings, session_factory
):
    """A pre-fix legacy row (no payer_identity_id, no stored identity type)
    is NOT provably zero-mutation: create_payment()'s retired-scheme lookup
    is a JOIN on payer_identity_id, which matches nothing for a NULL fk, so
    resolve_payer_identity() always runs and can write. Documented policy:
    NOT exempt, regardless of whether a Telegram id is supplied."""
    from app.ratelimit import BoundedLimiterStore as _Store

    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="rl-legacy-row",
                gateway_order_id=900_003,
                gateway_user_id=DEFAULT_GATEWAY_USER_ID,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.LINK_CREATED.value,
                redirect_url="https://centralpay.test.local/pay/legacy",
                # payer_identity_id / payer_identity_type left at their NULL
                # defaults -- a pre-fix legacy row.
            )
        )
        db.commit()

    app.state.rate_limiters.create = SlidingWindowLimiter(limit=0, window_seconds=60.0)
    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)

    response = create_order(client, settings, order_id="rl-legacy-row", amount=10000)
    assert response.status_code == 429


# --- secret / log redaction ---------------------------------------------------


def test_rate_limited_log_event_contains_no_secrets(app, client, settings, caplog):
    from app.ratelimit import BoundedLimiterStore as _Store

    app.state.rate_limiters.create_per_ip = _Store(limit=0, window_seconds=60.0, capacity=10)
    with caplog.at_level(logging.WARNING, logger="app.ratelimit"):
        create_order(client, settings, order_id="rl-log-secret")
    rate_limited_records = [r for r in caplog.records if r.message == "rate_limited"]
    assert rate_limited_records
    for record in rate_limited_records:
        serialized = json.dumps(record.__dict__, default=str)
        assert settings.inbound_api_key not in serialized
        assert settings.callback_hmac_secret not in serialized


def test_invalid_signature_rate_limit_log_never_leaks_signature_or_token(
    app, client, settings, caplog
):
    app.state.rate_limiters.invalid_signature = SlidingWindowLimiter(
        limit=0, window_seconds=60.0
    )
    secret_looking_ct = "deadbeef" * 4
    secret_looking_sig = "cafebabe" * 8
    with caplog.at_level(logging.WARNING):
        client.get(
            f"/api/centralpay/callback?orderId=1&ct={secret_looking_ct}&sig={secret_looking_sig}"
        )
    for record in caplog.records:
        text = record.getMessage() + json.dumps(getattr(record, "__dict__", {}), default=str)
        assert secret_looking_ct not in text
        assert secret_looking_sig not in text
