"""app.services.reconciliation_status: the single read-only snapshot behind
`centralpay reconciliation status`.

Covers: exact age-boundary behavior (proving it mirrors the reconciliation
worker's own tiers, not a hardcoded 15m/2h), the due-vs-raw-bucket
distinction (min_age/attempts/next_at gate "due" but never the raw age
buckets), the link-age-anchor preference/fallback, enabled/disabled and
heartbeat presence/freshness/staleness — including the case a fresh
heartbeat must NOT be reported as "healthy" while the last pass actually
failed — the oldest-due-age MAX (not min) semantics, the windowed/grouped
recent-stats query, custom Settings values proving nothing is hardcoded, and
the hard read-only guarantee.
"""

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models import Payment, PaymentEvent, PaymentStatus
from app.services.heartbeat import record_worker_heartbeat
from app.services.reconciliation_status import (
    CONFIG_SOURCE_UNCONFIRMED,
    RECONCILIATION_WORKER_NAME,
    build_reconciliation_status_snapshot,
)
from tests.conftest import as_utc

FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_order_ids = itertools.count(700001)


def _now_fn() -> datetime:
    return FIXED_NOW


def _age(seconds: float) -> datetime:
    return FIXED_NOW - timedelta(seconds=seconds)


def _make_payment(session_factory, *, bot_order_id: str, **fields) -> None:
    defaults = {
        "gateway_order_id": next(_order_ids),
        "gateway_user_id": 888001,
        "amount": 10000,
        "payable_amount": 10000,
        "status": PaymentStatus.LINK_CREATED.value,
        "created_at": FIXED_NOW,
        "reconciliation_attempts": 0,
    }
    defaults.update(fields)
    with session_factory() as db:
        db.add(Payment(bot_order_id=bot_order_id, **defaults))
        db.commit()


def _snapshot(session_factory, settings, **kwargs):
    with session_factory() as db:
        return build_reconciliation_status_snapshot(db, settings, now_fn=_now_fn, **kwargs)


def _beat(
    session_factory, *, instance_id: str, now: datetime, cycle_completed: bool, error_code=None
):
    with session_factory() as db:
        record_worker_heartbeat(
            db,
            worker_name=RECONCILIATION_WORKER_NAME,
            instance_id=instance_id,
            now=now,
            cycle_completed=cycle_completed,
            error_code=error_code,
        )


# --- payment buckets: exact age boundaries -----------------------------------


def test_active_expiring_boundary_is_exact(settings, session_factory):
    """Age EXACTLY equal to fast_window must land in 'expiring', not
    'active' — the task's documented boundary rule."""
    _make_payment(
        session_factory,
        bot_order_id="boundary-fast",
        callback_token_issued_at=_age(settings.reconciliation_fast_window_seconds),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.active == 0
    assert snapshot.buckets.expiring == 1
    assert snapshot.buckets.aged_out == 0
    # And it is due in the expiring tier (floor is inclusive), not active.
    assert snapshot.queue.active_due == 0
    assert snapshot.queue.expiring_due == 1


def test_just_below_fast_window_is_active(settings, session_factory):
    _make_payment(
        session_factory,
        bot_order_id="boundary-fast-minus",
        callback_token_issued_at=_age(settings.reconciliation_fast_window_seconds - 1),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.active == 1
    assert snapshot.buckets.expiring == 0


def test_max_age_boundary_is_exact_aged_out(settings, session_factory):
    """Age EXACTLY equal to max_age must land in 'aged_out', not
    'expiring' — and must never be due or exhausted (aged-out rows win)."""
    _make_payment(
        session_factory,
        bot_order_id="boundary-max",
        callback_token_issued_at=_age(settings.reconciliation_max_age_seconds),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.expiring == 0
    assert snapshot.buckets.aged_out == 1
    assert snapshot.queue.expiring_due == 0
    assert snapshot.queue.exhausted_not_aged_out == 0


def test_just_below_max_age_is_expiring(settings, session_factory):
    _make_payment(
        session_factory,
        bot_order_id="boundary-max-minus",
        callback_token_issued_at=_age(settings.reconciliation_max_age_seconds - 1),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.expiring == 1
    assert snapshot.buckets.aged_out == 0


# --- due predicates vs raw buckets -------------------------------------------


def test_min_age_gates_due_but_not_the_raw_bucket(settings, session_factory):
    """Younger than min_age: still counted as 'active' (age-only bucket),
    but NOT active_due (the due predicate respects min_age)."""
    assert settings.reconciliation_min_age_seconds > 1
    _make_payment(
        session_factory,
        bot_order_id="too-young",
        callback_token_issued_at=_age(1),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.active == 1
    assert snapshot.queue.active_due == 0


def test_attempts_at_max_excluded_from_due_and_counted_exhausted(settings, session_factory):
    custom = settings.model_copy(update={"reconciliation_max_attempts": 3})
    age = custom.reconciliation_fast_window_seconds + 60  # expiring tier
    _make_payment(
        session_factory,
        bot_order_id="exhausted-1",
        callback_token_issued_at=_age(age),
        reconciliation_attempts=3,
        reconciliation_next_at=None,
    )
    snapshot = _snapshot(session_factory, custom)
    assert snapshot.queue.expiring_due == 0
    assert snapshot.queue.exhausted_not_aged_out == 1
    # Still visible in the raw bucket (age-only view).
    assert snapshot.buckets.expiring == 1


def test_attempts_below_max_is_due_not_exhausted(settings, session_factory):
    custom = settings.model_copy(update={"reconciliation_max_attempts": 3})
    age = custom.reconciliation_fast_window_seconds + 60
    _make_payment(
        session_factory,
        bot_order_id="not-exhausted-1",
        callback_token_issued_at=_age(age),
        reconciliation_attempts=2,
        reconciliation_next_at=None,
    )
    snapshot = _snapshot(session_factory, custom)
    assert snapshot.queue.expiring_due == 1
    assert snapshot.queue.exhausted_not_aged_out == 0


def test_future_next_at_excludes_from_due_but_not_from_bucket_or_exhausted(
    settings, session_factory
):
    age = settings.reconciliation_min_age_seconds + 60  # active tier, past min_age
    _make_payment(
        session_factory,
        bot_order_id="future-retry",
        callback_token_issued_at=_age(age),
        reconciliation_attempts=1,
        reconciliation_next_at=FIXED_NOW + timedelta(hours=1),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.queue.active_due == 0
    assert snapshot.buckets.active == 1
    # next_at is NOT null, so this can never be "exhausted" regardless of
    # attempts.
    assert snapshot.queue.exhausted_not_aged_out == 0


def test_next_at_due_in_the_past_is_due(settings, session_factory):
    age = settings.reconciliation_min_age_seconds + 60
    _make_payment(
        session_factory,
        bot_order_id="past-retry",
        callback_token_issued_at=_age(age),
        reconciliation_attempts=1,
        reconciliation_next_at=FIXED_NOW - timedelta(minutes=1),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.queue.active_due == 1


# --- link-age anchor preference/fallback -------------------------------------


def test_callback_token_issued_at_preferred_over_created_at(settings, session_factory):
    """created_at alone would put this in 'active'; the REAL anchor
    (callback_token_issued_at) puts it in 'aged_out'."""
    _make_payment(
        session_factory,
        bot_order_id="anchor-preference",
        created_at=FIXED_NOW,
        callback_token_issued_at=_age(settings.reconciliation_max_age_seconds + 100),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.active == 0
    assert snapshot.buckets.aged_out == 1


def test_created_at_fallback_when_no_callback_token(settings, session_factory):
    age = settings.reconciliation_fast_window_seconds + 100  # expiring tier
    _make_payment(
        session_factory,
        bot_order_id="anchor-fallback",
        created_at=_age(age),
        callback_token_issued_at=None,
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.expiring == 1


# --- enabled/disabled + heartbeat --------------------------------------------


def test_enabled_flag_reflects_settings(settings, session_factory):
    enabled = _snapshot(
        session_factory, settings.model_copy(update={"reconciliation_enabled": True})
    )
    disabled = _snapshot(
        session_factory, settings.model_copy(update={"reconciliation_enabled": False})
    )
    assert enabled.runtime.enabled is True
    assert disabled.runtime.enabled is False


def test_disabled_heartbeat_is_not_applicable_even_with_a_heartbeat_row(settings, session_factory):
    _beat(session_factory, instance_id="disabled-test", now=_age(2), cycle_completed=True)
    disabled = settings.model_copy(update={"reconciliation_enabled": False})
    snapshot = _snapshot(session_factory, disabled)
    assert snapshot.runtime.heartbeat_fresh is None


def test_disabled_heartbeat_is_not_applicable_without_any_heartbeat_row(settings, session_factory):
    disabled = settings.model_copy(update={"reconciliation_enabled": False})
    snapshot = _snapshot(session_factory, disabled)
    assert snapshot.runtime.heartbeat_present is False
    assert snapshot.runtime.heartbeat_fresh is None


def test_enabled_with_no_heartbeat_is_unhealthy_not_na(settings, session_factory):
    enabled = settings.model_copy(update={"reconciliation_enabled": True})
    snapshot = _snapshot(session_factory, enabled)
    assert snapshot.runtime.heartbeat_present is False
    assert snapshot.runtime.heartbeat_fresh is False


def test_fresh_heartbeat_is_reported_fresh(settings, session_factory):
    _beat(session_factory, instance_id="fresh-test", now=_age(2), cycle_completed=True)
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.runtime.heartbeat_present is True
    assert snapshot.runtime.heartbeat_fresh is True
    assert snapshot.runtime.heartbeat_age_seconds == 2
    assert as_utc(snapshot.runtime.last_successful_cycle_at) == _age(2)
    assert snapshot.runtime.last_error_code is None


def test_stale_heartbeat_is_reported_stale(settings, session_factory):
    threshold = max(settings.reconciliation_interval_seconds * 6, 120)
    _beat(
        session_factory,
        instance_id="stale-test",
        now=_age(threshold + 100),
        cycle_completed=True,
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.runtime.heartbeat_present is True
    assert snapshot.runtime.heartbeat_fresh is False


def test_fresh_heartbeat_with_failing_passes_is_not_reported_as_healthy(
    settings, session_factory
):
    """Regression: the loop can keep ticking (fresh last_heartbeat_at) while
    every recent PASS fails. This must show up as an old
    last_successful_cycle_at + a populated last_error_code, NEVER collapsed
    into a single misleading 'healthy' flag."""
    instance_id = "flapping-test"
    _beat(session_factory, instance_id=instance_id, now=_age(7200), cycle_completed=True)
    _beat(
        session_factory,
        instance_id=instance_id,
        now=_age(1),
        cycle_completed=False,
        error_code="CentralPayError",
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.runtime.heartbeat_fresh is True  # the loop is ticking
    assert as_utc(snapshot.runtime.last_successful_cycle_at) == FIXED_NOW - timedelta(hours=2)
    assert snapshot.runtime.last_successful_cycle_age_seconds == 7200
    assert snapshot.runtime.last_error_code == "CentralPayError"


def test_config_source_defaults_to_unconfirmed_without_a_worker_heartbeat_file(
    settings, session_factory, tmp_path
):
    missing_path = tmp_path / "does-not-exist"
    custom = settings.model_copy(update={"worker_heartbeat_file": str(missing_path)})
    snapshot = _snapshot(session_factory, custom)
    assert snapshot.runtime.config_source == CONFIG_SOURCE_UNCONFIRMED


# --- oldest due age: MAX, not MIN (regression per explicit correction) ------


def test_oldest_due_age_overall_is_the_max_not_the_min(settings, session_factory):
    active_age = settings.reconciliation_min_age_seconds + 120
    expiring_age = settings.reconciliation_fast_window_seconds + 3000
    _make_payment(
        session_factory,
        bot_order_id="oldest-active",
        callback_token_issued_at=_age(active_age),
    )
    _make_payment(
        session_factory,
        bot_order_id="oldest-expiring",
        callback_token_issued_at=_age(expiring_age),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.queue.oldest_active_due_age_seconds == active_age
    assert snapshot.queue.oldest_expiring_due_age_seconds == expiring_age
    assert expiring_age > active_age  # sanity: the two really do differ
    # The overall oldest due age must be the LARGER (older) of the two, not
    # the smaller one — age is a duration, not a priority rank.
    assert snapshot.queue.oldest_due_age_seconds == expiring_age
    assert snapshot.queue.oldest_due_age_seconds != snapshot.queue.oldest_active_due_age_seconds


def test_oldest_due_age_overall_ignores_an_empty_tier(settings, session_factory):
    active_age = settings.reconciliation_min_age_seconds + 50
    _make_payment(
        session_factory,
        bot_order_id="only-active-due",
        callback_token_issued_at=_age(active_age),
    )
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.queue.oldest_expiring_due_age_seconds is None
    assert snapshot.queue.oldest_due_age_seconds == active_age


def test_oldest_due_age_overall_is_none_when_queue_is_empty(settings, session_factory):
    snapshot = _snapshot(session_factory, settings)
    assert snapshot.queue.oldest_active_due_age_seconds is None
    assert snapshot.queue.oldest_expiring_due_age_seconds is None
    assert snapshot.queue.oldest_due_age_seconds is None


# --- custom settings: proving nothing is hardcoded ---------------------------


def test_custom_fast_window_and_max_age_drive_the_boundaries(settings, session_factory):
    custom = settings.model_copy(
        update={
            "reconciliation_fast_window_seconds": 50,
            "reconciliation_max_age_seconds": 200,
        }
    )
    # Age 60s: under the DEFAULT fast window (900s) this would be "active",
    # but under the custom 50s fast window it must be "expiring".
    _make_payment(
        session_factory,
        bot_order_id="custom-window",
        callback_token_issued_at=_age(60),
    )
    snapshot = _snapshot(session_factory, custom)
    assert snapshot.buckets.active == 0
    assert snapshot.buckets.expiring == 1


def test_custom_min_age_drives_due_eligibility(settings, session_factory):
    custom = settings.model_copy(update={"reconciliation_min_age_seconds": 500})
    # Age 100s: under default min_age (10s) this is due; under the custom
    # 500s floor it must not be.
    _make_payment(
        session_factory,
        bot_order_id="custom-min-age",
        callback_token_issued_at=_age(100),
    )
    snapshot = _snapshot(session_factory, custom)
    assert snapshot.queue.active_due == 0
    assert snapshot.buckets.active == 1


# --- recent stats: grouped, windowed, correctly labeled ---------------------


def test_recent_stats_only_counts_within_window_and_fills_zeros(settings, session_factory):
    with session_factory() as db:
        payment = Payment(
            bot_order_id="stats-1",
            gateway_order_id=next(_order_ids),
            gateway_user_id=888002,
            amount=10000,
            payable_amount=10000,
            status=PaymentStatus.LINK_CREATED.value,
            created_at=FIXED_NOW,
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        payment_id = payment.id
        for event_type, age_hours in (
            ("reconciliation_verified", 1),
            ("reconciliation_retry_scheduled", 2),
            ("reconciliation_gateway_not_paid", 3),
            ("reconciliation_transport_failed", 4),
            ("reconciliation_exhausted", 5),
            ("reconciliation_verified", 25),  # outside the 24h window
            ("some_unrelated_event", 1),  # never counted
        ):
            db.add(
                PaymentEvent(
                    payment_id=payment_id,
                    event_type=event_type,
                    created_at=FIXED_NOW - timedelta(hours=age_hours),
                )
            )
        db.commit()

    snapshot = _snapshot(session_factory, settings)
    recent = snapshot.recent
    assert recent.window_hours == 24
    assert recent.verified == 1  # the 25h-old one is excluded
    assert recent.retry_scheduled == 1
    assert recent.gateway_not_paid == 1
    assert recent.transport_failed == 1
    assert recent.exhausted == 1


def test_recent_stats_default_zero_when_nothing_happened(settings, session_factory):
    snapshot = _snapshot(session_factory, settings)
    recent = snapshot.recent
    assert (
        recent.verified,
        recent.retry_scheduled,
        recent.gateway_not_paid,
        recent.transport_failed,
        recent.exhausted,
    ) == (0, 0, 0, 0, 0)


def test_recent_exhausted_event_does_not_prove_still_not_aged_out(settings, session_factory):
    """The exact distinction the CLI must never blur: recent.exhausted counts
    `reconciliation_exhausted` EVENTS raised within the window, which proves
    nothing about whether those payments are STILL not aged-out right now — a
    payment can be marked exhausted and later age out before this command
    runs. queue.exhausted_not_aged_out is a live, aged-out-excluded,
    current-state count and must stay 0 here while recent.exhausted is 1."""
    _make_payment(
        session_factory,
        bot_order_id="exhausted-then-aged-out",
        callback_token_issued_at=_age(settings.reconciliation_max_age_seconds + 60),
        reconciliation_attempts=settings.reconciliation_max_attempts,
    )
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "exhausted-then-aged-out")
        ).scalar_one()
        db.add(
            PaymentEvent(
                payment_id=payment.id,
                event_type="reconciliation_exhausted",
                created_at=_age(3600),
            )
        )
        db.commit()

    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.aged_out == 1
    assert snapshot.queue.exhausted_not_aged_out == 0  # aged-out wins, excluded
    assert snapshot.recent.exhausted == 1  # the event still happened in-window


# --- read-only guarantee -----------------------------------------------------


def test_snapshot_never_mutates_or_writes_events(settings, session_factory):
    _make_payment(
        session_factory,
        bot_order_id="ro-active",
        callback_token_issued_at=_age(settings.reconciliation_min_age_seconds + 60),
    )
    _make_payment(
        session_factory,
        bot_order_id="ro-expiring",
        callback_token_issued_at=_age(settings.reconciliation_fast_window_seconds + 60),
        reconciliation_attempts=settings.reconciliation_max_attempts,
        reconciliation_next_at=None,
    )
    _make_payment(
        session_factory,
        bot_order_id="ro-aged-out",
        callback_token_issued_at=_age(settings.reconciliation_max_age_seconds + 60),
    )
    _beat(session_factory, instance_id="ro-test", now=_age(2), cycle_completed=True)

    with session_factory() as db:
        before_payments = db.execute(
            select(
                Payment.id,
                Payment.status,
                Payment.reconciliation_attempts,
                Payment.reconciliation_next_at,
                Payment.reconciliation_claimed_at,
                Payment.reconciliation_claimed_by,
                Payment.reconciliation_last_at,
                Payment.updated_at,
            ).order_by(Payment.id)
        ).all()
        before_event_count = db.execute(select(func.count(PaymentEvent.id))).scalar_one()

    snapshot = _snapshot(session_factory, settings)
    assert snapshot.buckets.total_unverified == 3  # sanity: the fixture produced data

    with session_factory() as db:
        after_payments = db.execute(
            select(
                Payment.id,
                Payment.status,
                Payment.reconciliation_attempts,
                Payment.reconciliation_next_at,
                Payment.reconciliation_claimed_at,
                Payment.reconciliation_claimed_by,
                Payment.reconciliation_last_at,
                Payment.updated_at,
            ).order_by(Payment.id)
        ).all()
        after_event_count = db.execute(select(func.count(PaymentEvent.id))).scalar_one()

    assert after_payments == before_payments
    assert after_event_count == before_event_count
