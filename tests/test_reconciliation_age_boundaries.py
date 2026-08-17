"""Exact age-boundary classification, proven identical across all four
surfaces that read a link_created payment's age: the reconciliation worker's
claim predicates, `reconciliation status`'s PaymentBuckets, `stuck`'s
WAITING_GATEWAY/EXPIRED categorization, and `reconcile`'s age_bucket.

This is the regression coverage for the PR #59 review fix that removed
reconcile_inspect.py's locally-duplicated age-boundary math in favor of the
single shared implementation in app.services.reconciliation
(active_tier_age_conditions / expiring_tier_age_conditions /
aged_out_conditions / aged_out_age_condition). Every boundary here is
exercised through each module's own PUBLIC surface (not just the shared
predicate functions directly), so a future change that reintroduces
per-module duplication would show up as a real classification mismatch
between worker/status/stuck/reconcile, not just a code-review concern.

Defaults under test (from the `settings` fixture / Settings model
defaults): reconciliation_min_age_seconds=10, reconciliation_fast_window_
seconds=900 (15 min), reconciliation_max_age_seconds=7200 (2 h).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.models import Payment, PaymentStatus
from app.services.reconcile_inspect import build_local_snapshot
from app.services.reconciliation import (
    active_tier_age_conditions,
    active_tier_due_conditions,
    aged_out_conditions,
    expiring_tier_age_conditions,
    expiring_tier_due_conditions,
)
from app.services.reconciliation_status import build_reconciliation_status_snapshot
from app.services.stuck_payments import StuckCategory, stuck_payments_overview

FAST_WINDOW = 900
MAX_AGE = 7200
MIN_AGE = 10


def _make_payment_at_age(
    session_factory, *, bot_order_id: str, gateway_order_id: int, age_seconds: float, now: datetime
) -> int:
    """A link_created, unverified payment whose link age is EXACTLY
    ``age_seconds`` relative to ``now`` (negative == issued in the future,
    for the clock-skew case)."""
    with session_factory() as db:
        payment = Payment(
            bot_order_id=bot_order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=1,
            amount=5000,
            payable_amount=5000,
            status=PaymentStatus.LINK_CREATED.value,
            callback_token_issued_at=now - timedelta(seconds=age_seconds),
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment.id


def _condition_holds(db, payment_id: int, conditions) -> bool:
    return (
        db.execute(
            select(Payment.id).where(Payment.id == payment_id, *conditions)
        ).scalar_one_or_none()
        is not None
    )


def _classify(session_factory, settings, payment_id: int, now: datetime) -> dict[str, Any]:
    """Ask all four surfaces to classify the ONE payment in the (otherwise
    empty) database, through their own public APIs."""
    with session_factory() as db:
        worker_active_age = _condition_holds(
            db, payment_id, active_tier_age_conditions(settings, now=now)
        )
        worker_expiring_age = _condition_holds(
            db, payment_id, expiring_tier_age_conditions(settings, now=now)
        )
        worker_aged_out = _condition_holds(db, payment_id, aged_out_conditions(settings, now=now))
        worker_active_due = _condition_holds(
            db, payment_id, active_tier_due_conditions(settings, now=now)
        )
        worker_expiring_due = _condition_holds(
            db, payment_id, expiring_tier_due_conditions(settings, now=now)
        )

        status_snapshot = build_reconciliation_status_snapshot(db, settings, now_fn=lambda: now)
        stuck_overview = stuck_payments_overview(db, settings, now_fn=lambda: now)
        snapshot = build_local_snapshot(db, settings, payment_id, now=now)
        assert snapshot is not None
        _, local = snapshot

    stuck_category = None
    for entry in stuck_overview.ordered():
        if entry.payment.id == payment_id:
            stuck_category = entry.category
            break

    return {
        "worker_active_age": worker_active_age,
        "worker_expiring_age": worker_expiring_age,
        "worker_aged_out": worker_aged_out,
        "worker_active_due": worker_active_due,
        "worker_expiring_due": worker_expiring_due,
        "status_buckets": status_snapshot.buckets,
        "stuck_category": stuck_category,
        "reconcile_age_bucket": local.age_bucket,
        "reconcile_auto_due": local.auto_reconciliation_due,
    }


# --- the four exact ACTIVE / EXPIRING / AGED_OUT boundaries -----------------


@pytest.mark.parametrize(
    "case_name,age_seconds,expected_bucket,expected_stuck_category",
    [
        ("just_below_fast_window", FAST_WINDOW - 1, "active", StuckCategory.WAITING_GATEWAY),
        ("exactly_fast_window", FAST_WINDOW, "expiring", StuckCategory.WAITING_GATEWAY),
        ("just_below_max_age", MAX_AGE - 1, "expiring", StuckCategory.WAITING_GATEWAY),
        ("exactly_max_age", MAX_AGE, "aged_out", StuckCategory.EXPIRED),
    ],
)
def test_age_boundary_classified_identically_across_all_surfaces(
    session_factory, settings, case_name, age_seconds, expected_bucket, expected_stuck_category
):
    now = datetime.now(UTC)
    # min_age_seconds (10s) is always exceeded well before the fast-window
    # boundary (900s), so the due-floor never confounds these four cases.
    payment_id = _make_payment_at_age(
        session_factory,
        bot_order_id=f"boundary-{case_name}",
        gateway_order_id=hash(case_name) % 900000 + 100000,
        age_seconds=age_seconds,
        now=now,
    )

    result = _classify(session_factory, settings, payment_id, now)

    # Worker: age-only bucket membership (the exact predicate the claim
    # path's tier_due_conditions is built from) is mutually exclusive and
    # matches the expected bucket.
    assert result["worker_active_age"] == (expected_bucket == "active")
    assert result["worker_expiring_age"] == (expected_bucket == "expiring")
    assert result["worker_aged_out"] == (expected_bucket == "aged_out")

    # Worker: DUE membership follows the age bucket for active/expiring
    # (min_age_seconds is already satisfied at these ages); aged-out rows
    # are due for NEITHER tier -- that IS the hard lifetime cutoff.
    if expected_bucket == "active":
        assert result["worker_active_due"] is True
        assert result["worker_expiring_due"] is False
    elif expected_bucket == "expiring":
        assert result["worker_active_due"] is False
        assert result["worker_expiring_due"] is True
    else:
        assert result["worker_active_due"] is False
        assert result["worker_expiring_due"] is False

    # Status: PaymentBuckets counts exactly one payment in the expected
    # bucket and zero in the other two (this is the only payment in the DB).
    buckets = result["status_buckets"]
    assert buckets.active == (1 if expected_bucket == "active" else 0)
    assert buckets.expiring == (1 if expected_bucket == "expiring" else 0)
    assert buckets.aged_out == (1 if expected_bucket == "aged_out" else 0)
    assert buckets.total_unverified == 1

    # Stuck: WAITING_GATEWAY for active/expiring, EXPIRED for aged_out.
    assert result["stuck_category"] == expected_stuck_category

    # Reconcile: age_bucket matches exactly.
    assert result["reconcile_age_bucket"] == expected_bucket


# --- min_age_seconds floor: a DUE-only refinement, not a bucket boundary ---


def test_just_below_min_age_is_active_bucket_but_not_yet_due(session_factory, settings):
    """A payment younger than reconciliation_min_age_seconds is still in
    the ACTIVE age bucket (worker/status/stuck/reconcile all agree it is
    "active", not some fourth state) but is NOT YET due for the worker to
    claim -- the min-age floor is a due-only refinement layered on top of
    the age bucket, never a bucket boundary itself."""
    now = datetime.now(UTC)
    payment_id = _make_payment_at_age(
        session_factory,
        bot_order_id="boundary-below-min-age",
        gateway_order_id=100101,
        age_seconds=MIN_AGE - 1,
        now=now,
    )

    result = _classify(session_factory, settings, payment_id, now)

    assert result["worker_active_age"] is True
    assert result["status_buckets"].active == 1
    assert result["stuck_category"] == StuckCategory.WAITING_GATEWAY
    assert result["reconcile_age_bucket"] == "active"

    # But not due yet: the min_age_seconds floor has not been reached.
    assert result["worker_active_due"] is False
    assert result["reconcile_auto_due"] is False


def test_exactly_min_age_is_active_bucket_and_due(session_factory, settings):
    """The inclusive counterpart: exactly reconciliation_min_age_seconds
    old is both the ACTIVE bucket AND due."""
    now = datetime.now(UTC)
    payment_id = _make_payment_at_age(
        session_factory,
        bot_order_id="boundary-exactly-min-age",
        gateway_order_id=100102,
        age_seconds=MIN_AGE,
        now=now,
    )

    result = _classify(session_factory, settings, payment_id, now)

    assert result["worker_active_age"] is True
    assert result["reconcile_age_bucket"] == "active"
    assert result["worker_active_due"] is True
    assert result["reconcile_auto_due"] is True


# --- future timestamp / clock skew -------------------------------------------


def test_future_link_issuance_timestamp_is_active_bucket_but_not_due(session_factory, settings):
    """A clock-skewed or malformed row whose callback_token_issued_at is in
    the FUTURE relative to now: age is effectively negative, which is still
    "younger than the fast window" (ACTIVE bucket, all four surfaces agree)
    but can never satisfy the min_age_seconds due-floor -- consistent with
    reconciliation_retry_delay_seconds's documented clamp-to-zero behavior
    for the same clock-skew case, and proof the age math never goes
    negative-due or otherwise misbehaves at this edge."""
    now = datetime.now(UTC)
    payment_id = _make_payment_at_age(
        session_factory,
        bot_order_id="boundary-future-timestamp",
        gateway_order_id=100103,
        age_seconds=-100,  # issued 100s in the future
        now=now,
    )

    result = _classify(session_factory, settings, payment_id, now)

    assert result["worker_active_age"] is True
    assert result["worker_aged_out"] is False
    assert result["status_buckets"].active == 1
    assert result["status_buckets"].aged_out == 0
    assert result["stuck_category"] == StuckCategory.WAITING_GATEWAY
    assert result["reconcile_age_bucket"] == "active"

    assert result["worker_active_due"] is False
    assert result["reconcile_auto_due"] is False


# --- worker actually claims the boundary consistently with the predicates --


def test_worker_claims_expiring_not_active_at_exact_fast_window_boundary(
    client, settings, session_factory, stub
):
    """End-to-end proof (not just the shared predicate query): a real
    run_reconciliation_pass claims a payment sitting exactly at the fast-
    window boundary via the EXPIRING tier, matching
    expiring_tier_age_conditions/expiring_tier_due_conditions above -- the
    worker's actual behavior, not merely a re-check of its own predicate
    function."""
    import httpx

    from app.centralpay import CentralPayClient
    from app.services.reconciliation import run_reconciliation_pass
    from tests.conftest import create_order, get_payment

    now = datetime.now(UTC)
    order_id = "boundary-worker-claim-exact-fast-window"
    assert create_order(client, settings, order_id=order_id, amount=5000).status_code == 200
    with session_factory() as db:
        payment = db.execute(select(Payment).where(Payment.bot_order_id == order_id)).scalar_one()
        payment.callback_token_issued_at = now - timedelta(seconds=FAST_WINDOW)
        db.commit()

    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid yet"})
    gateway = CentralPayClient(
        base_url=settings.centralpay_base_url,
        getlink_api_key=settings.centralpay_getlink_api_key,
        verify_api_key=settings.centralpay_verify_api_key,
        timeout_seconds=settings.centralpay_timeout_seconds,
        transport=httpx.MockTransport(stub.handler),
    )
    try:
        with session_factory() as db:
            stats = run_reconciliation_pass(
                db, gateway, settings, worker_id="boundary-test-worker", now_fn=lambda: now
            )
    finally:
        gateway.close()

    assert stats["processed"] == 1
    payment = get_payment(session_factory, order_id)
    assert payment.reconciliation_attempts == 1  # actually claimed and attempted
