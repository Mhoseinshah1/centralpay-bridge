"""Server-side reconciliation of stuck link_created payments.

Covers the full contract: selection (staleness, feature flag, status
exclusivity), settlement through the SAME shared verification path as the
callback (all financial checks and manual_review behavior preserved), the
two-stage AGE-based retry schedule (fast every 10 s while the link is under
the fast window old — default 900 s, the 15-minute CentralPay link lifetime —
then every 5 minutes, anchored on the real link age so worker downtime never
restarts the fast window), the two-tier reserved-quota-with-spillover
selection fairness between the active (<fast window) and expiring (fast
window-max age) tiers, the hard reconciliation-lifetime cutoff at max age
(payments past it are excluded from selection but never mutated), attempt
exhaustion, callback/reconciliation idempotency in both orders, per-payment
crash isolation, and single bot-notification queueing. CentralPay is faked at
the httpx transport layer via the shared stub — the real client code runs.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.centralpay import CentralPayClient
from app.models import Payment, PaymentEvent, PaymentStatus
from app.services.reconciliation import (
    ERROR_GATEWAY_NOT_PAID,
    ERROR_INTERNAL,
    reconciliation_backoff_seconds,
    reconciliation_retry_delay_seconds,
    run_reconciliation_pass,
)
from tests.conftest import (
    as_utc,
    create_order,
    event_types,
    get_events,
    get_payment,
    valid_callback_path,
    verify_ok_response,
)

WORKER = "recon-test-worker-1"


def _client_for(settings, stub) -> CentralPayClient:
    return CentralPayClient(
        base_url=settings.centralpay_base_url,
        getlink_api_key=settings.centralpay_getlink_api_key,
        verify_api_key=settings.centralpay_verify_api_key,
        timeout_seconds=settings.centralpay_timeout_seconds,
        transport=httpx.MockTransport(stub.handler),
    )


def _age_payment(session_factory, bot_order_id: str, *, seconds: int) -> None:
    """Rewind the link-issuance clock so the payment counts as stale."""
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == bot_order_id)
        ).scalar_one()
        past = datetime.now(UTC) - timedelta(seconds=seconds)
        payment.callback_token_issued_at = past
        db.commit()


def _make_stale_link(client, settings, session_factory, *, order_id, amount=10000):
    assert create_order(client, settings, order_id=order_id, amount=amount).status_code == 200
    _age_payment(session_factory, order_id, seconds=settings.reconciliation_min_age_seconds + 5)
    return get_payment(session_factory, order_id)


def _make_old_link(client, settings, session_factory, *, order_id, age_seconds, amount=10000):
    """A link_created payment whose link age is exactly ``age_seconds`` —
    for exercising the active/expiring tier split and the hard age cutoff."""
    assert create_order(client, settings, order_id=order_id, amount=amount).status_code == 200
    _age_payment(session_factory, order_id, seconds=age_seconds)
    return get_payment(session_factory, order_id)


def _run_pass(session_factory, settings, stub, **kwargs):
    gateway = _client_for(settings, stub)
    try:
        with session_factory() as db:
            return run_reconciliation_pass(
                db, gateway, settings, worker_id=WORKER, **kwargs
            )
    finally:
        gateway.close()


def _notification_queued_count(session_factory, payment_id) -> int:
    with session_factory() as db:
        return len(
            db.execute(
                select(PaymentEvent).where(
                    PaymentEvent.payment_id == payment_id,
                    PaymentEvent.event_type == "bot_notification_queued",
                )
            ).all()
        )


# --- selection ----------------------------------------------------------------


def test_stale_link_created_is_selected_and_verified(
    client, settings, session_factory, stub
):
    payment = _make_stale_link(client, settings, session_factory, order_id="rec-1")
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id="REF-rec-1"
    )
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    assert stats["verified"] == 1

    settled = get_payment(session_factory, "rec-1")
    assert settled.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert settled.gateway_verified_at is not None
    assert settled.reference_id == "REF-rec-1"
    assert settled.reconciliation_attempts == 1
    assert settled.reconciliation_next_at is None
    assert settled.reconciliation_claimed_at is None
    assert settled.reconciliation_last_error_code is None
    types = event_types(get_events(session_factory, settled.id))
    assert "gateway_payment_verified" in types
    assert "reconciliation_verified" in types
    assert _notification_queued_count(session_factory, settled.id) == 1


def test_fresh_link_created_is_skipped(client, settings, session_factory, stub):
    assert create_order(client, settings, order_id="rec-fresh").status_code == 200
    stub.verify_requests.clear()
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 0
    assert stub.verify_requests == []  # the gateway was never contacted
    assert get_payment(session_factory, "rec-fresh").status == PaymentStatus.LINK_CREATED.value


def test_slow_getlink_does_not_consume_the_grace_period(
    client, settings, session_factory, stub, monkeypatch
):
    """A getLink call slower than the minimum age must not make the freshly
    returned link immediately eligible for reconciliation.

    The gateway "takes" min_age + 2 virtual seconds to answer getLink: the
    issuance timestamp must be stamped when getLink RETURNS, so the payment
    becomes due exactly min_age seconds after that moment — never earlier.
    Simulated with a virtual clock (no real sleeping): the payment module's
    clock and the reconciliation pass share one offset that the wrapped
    getLink call advances, exactly as wall time would during a slow request.
    """
    delay = settings.reconciliation_min_age_seconds + 2
    clock = {"offset": timedelta(0)}

    class _ShiftedDateTime:
        """datetime shim for app.services.payments: real time + virtual offset."""

        @staticmethod
        def now(tz=None):
            return datetime.now(tz) + clock["offset"]

    monkeypatch.setattr("app.services.payments.datetime", _ShiftedDateTime)

    real_get_link = CentralPayClient.get_link

    def slow_get_link(self, *args, **kwargs):
        result = real_get_link(self, *args, **kwargs)
        clock["offset"] += timedelta(seconds=delay)  # the gateway "took" this long
        return result

    monkeypatch.setattr(CentralPayClient, "get_link", slow_get_link)

    assert create_order(client, settings, order_id="rec-slow", amount=10000).status_code == 200
    link_returned_at = _ShiftedDateTime.now(UTC)
    payment = get_payment(session_factory, "rec-slow")
    issued_at = as_utc(payment.callback_token_issued_at)
    # The grace anchor starts when getLink succeeded, not when it was sent.
    assert issued_at >= link_returned_at - timedelta(seconds=1)

    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id="REF-rec-slow"
    )
    stub.verify_requests.clear()

    # Immediately after the URL is returned the payment must NOT be selected,
    # even though the creation request started min_age + 2 seconds ago.
    stats = _run_pass(session_factory, settings, stub, now_fn=lambda: link_returned_at)
    assert stats["processed"] == 0
    assert stub.verify_requests == []
    assert get_payment(session_factory, "rec-slow").status == PaymentStatus.LINK_CREATED.value

    # Still not selected one second before the grace period ends...
    almost_due = issued_at + timedelta(
        seconds=settings.reconciliation_min_age_seconds - 1
    )
    stats = _run_pass(session_factory, settings, stub, now_fn=lambda: almost_due)
    assert stats["processed"] == 0
    assert stub.verify_requests == []

    # ...and eligible exactly min_age seconds after the post-getLink stamp.
    due = issued_at + timedelta(seconds=settings.reconciliation_min_age_seconds)
    stats = _run_pass(session_factory, settings, stub, now_fn=lambda: due)
    assert stats["processed"] == 1
    assert stats["verified"] == 1
    assert len(stub.verify_requests) == 1
    assert (
        get_payment(session_factory, "rec-slow").status
        == PaymentStatus.BOT_NOTIFY_PENDING.value
    )


def test_disabled_feature_is_a_noop(client, settings, session_factory, stub):
    disabled = settings.model_copy(update={"reconciliation_enabled": False})
    _make_stale_link(client, settings, session_factory, order_id="rec-off")
    stub.verify_requests.clear()
    stats = _run_pass(session_factory, disabled, stub)
    assert stats["processed"] == 0
    assert stub.verify_requests == []
    assert get_payment(session_factory, "rec-off").status == PaymentStatus.LINK_CREATED.value


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.CREATED.value,
        PaymentStatus.GETLINK_FAILED.value,
        PaymentStatus.GATEWAY_VERIFIED.value,
        PaymentStatus.BOT_NOTIFY_PENDING.value,
        PaymentStatus.BOT_NOTIFY_ACCEPTED.value,
        PaymentStatus.MANUAL_REVIEW.value,
    ],
)
def test_non_link_created_statuses_are_never_selected(
    client, settings, session_factory, stub, status
):
    """Verified, notification, manual_review, and pre-link states are
    excluded by the status predicate alone — never auto-processed."""
    _make_stale_link(client, settings, session_factory, order_id="rec-status")
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "rec-status")
        ).scalar_one()
        payment.status = status
        if status in (
            PaymentStatus.GATEWAY_VERIFIED.value,
            PaymentStatus.BOT_NOTIFY_PENDING.value,
            PaymentStatus.BOT_NOTIFY_ACCEPTED.value,
        ):
            payment.gateway_verified_at = datetime.now(UTC)
        db.commit()
    stub.verify_requests.clear()
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 0
    assert stub.verify_requests == []


# --- retry scheduling and backoff ---------------------------------------------


def test_unpaid_result_schedules_bounded_retry(client, settings, session_factory, stub):
    _make_stale_link(client, settings, session_factory, order_id="rec-unpaid")
    # The stub's default verify_result is a gateway error response ->
    # gateway_success False -> "not paid yet".
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    assert stats["retry_scheduled"] == 1

    payment = get_payment(session_factory, "rec-unpaid")
    assert payment.status == PaymentStatus.LINK_CREATED.value  # never failed
    assert payment.gateway_verified_at is None
    assert payment.reconciliation_attempts == 1
    assert payment.reconciliation_last_error_code == ERROR_GATEWAY_NOT_PAID
    # Young link (age < fast window): the next check is one FAST interval out.
    expected_delay = settings.reconciliation_fast_interval_seconds
    assert payment.reconciliation_next_at is not None
    remaining = (as_utc(payment.reconciliation_next_at) - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= expected_delay + 1
    types = event_types(get_events(session_factory, payment.id))
    assert "reconciliation_gateway_not_paid" in types
    assert "reconciliation_retry_scheduled" in types
    # Routine polling of an unpaid link is the EXPECTED state: it records the
    # distinct non-alerting event, never the alert-mapped callback one.
    assert "centralpay_verify_not_paid" in types
    assert "centralpay_verify_failed" not in types
    assert "manual_review_required" not in types
    assert _notification_queued_count(session_factory, payment.id) == 0


def test_transport_failure_schedules_retry_and_never_crashes(
    client, settings, session_factory, stub
):
    _make_stale_link(client, settings, session_factory, order_id="rec-net")
    stub.verify_result = httpx.ConnectError("connection refused")
    stats = _run_pass(session_factory, settings, stub)  # must not raise
    assert stats["processed"] == 1
    assert stats["retry_scheduled"] == 1

    payment = get_payment(session_factory, "rec-net")
    assert payment.status == PaymentStatus.LINK_CREATED.value
    assert payment.reconciliation_attempts == 1
    assert payment.reconciliation_last_error_code == "centralpay_connection_error"
    assert payment.reconciliation_next_at is not None
    types = event_types(get_events(session_factory, payment.id))
    assert "reconciliation_transport_failed" in types
    assert "reconciliation_retry_scheduled" in types


def _aged_payment(age_seconds, *, use_created_at=False):
    """An in-memory Payment whose link is ``age_seconds`` old (negative =
    issued in the future, i.e. clock skew)."""
    now = datetime.now(UTC)
    issued = now - timedelta(seconds=age_seconds)
    payment = Payment(
        bot_order_id="delay-x",
        gateway_order_id=1,
        gateway_user_id=1,
        amount=1,
        payable_amount=1,
        status=PaymentStatus.LINK_CREATED.value,
    )
    if use_created_at:
        payment.callback_token_issued_at = None
        payment.created_at = issued
    else:
        payment.callback_token_issued_at = issued
    return payment, now


def test_two_stage_delay_fast_before_the_window(settings):
    """Link age below the fast window (default 900 s = the 15-minute
    CentralPay link lifetime): retry in 10 seconds."""
    window = settings.reconciliation_fast_window_seconds
    for age in (0, 15, 300, window - 1):
        payment, now = _aged_payment(age)
        assert (
            reconciliation_retry_delay_seconds(settings, payment=payment, now=now)
            == settings.reconciliation_fast_interval_seconds
            == 10
        )


def test_two_stage_delay_slow_at_and_after_the_boundary(settings):
    """At EXACTLY the window boundary — and any age beyond it — the slow
    300-second interval applies."""
    window = settings.reconciliation_fast_window_seconds
    for age in (window, window + 1, window * 2, 86_400):
        payment, now = _aged_payment(age)
        assert (
            reconciliation_retry_delay_seconds(settings, payment=payment, now=now)
            == settings.reconciliation_slow_interval_seconds
            == 300
        )


def test_two_stage_delay_is_age_based_not_attempt_based(settings):
    """Worker downtime: an old payment with a LOW attempt count still uses
    the slow interval — the fast window never restarts."""
    payment, now = _aged_payment(20 * 60)  # 20 minutes old
    payment.reconciliation_attempts = 1  # the worker was offline
    assert (
        reconciliation_retry_delay_seconds(settings, payment=payment, now=now) == 300
    )


def test_two_stage_delay_clamps_future_timestamps_to_fast(settings):
    """Clock skew making the link look issued in the future clamps the age
    to zero: fast interval, never a negative-age artifact."""
    payment, now = _aged_payment(-120)  # "issued" 2 minutes in the future
    assert reconciliation_retry_delay_seconds(settings, payment=payment, now=now) == 10


def test_two_stage_delay_falls_back_to_created_at(settings):
    """Without a callback_token_issued_at, created_at anchors the age."""
    young, now = _aged_payment(5, use_created_at=True)
    assert reconciliation_retry_delay_seconds(settings, payment=young, now=now) == 10
    old, now = _aged_payment(
        settings.reconciliation_fast_window_seconds + 100, use_created_at=True
    )
    assert reconciliation_retry_delay_seconds(settings, payment=old, now=now) == 300


def test_two_stage_delay_handles_naive_timestamps(settings):
    """SQLite hands back naive UTC datetimes; both anchors are normalized."""
    payment, now = _aged_payment(settings.reconciliation_fast_window_seconds + 100)
    assert payment.callback_token_issued_at is not None
    payment.callback_token_issued_at = payment.callback_token_issued_at.replace(
        tzinfo=None
    )
    assert (
        reconciliation_retry_delay_seconds(
            settings, payment=payment, now=now.replace(tzinfo=None)
        )
        == 300
    )


def test_deprecated_exponential_helper_remains_bounded(settings):
    """The RETIRED exponential helper is kept only as a deprecated utility
    (its settings stay accepted for env compatibility); production
    reconciliation never calls it. Its bound still holds."""
    initial = settings.reconciliation_initial_backoff_seconds
    maximum = settings.reconciliation_max_backoff_seconds
    assert reconciliation_backoff_seconds(settings, 1) == initial
    assert reconciliation_backoff_seconds(settings, 80) == maximum


def test_retry_not_due_until_next_at(client, settings, session_factory, stub):
    _make_stale_link(client, settings, session_factory, order_id="rec-wait")
    assert _run_pass(session_factory, settings, stub)["processed"] == 1  # schedules retry
    stub.verify_requests.clear()
    # Immediately after: the retry is in the future, so nothing is due.
    assert _run_pass(session_factory, settings, stub)["processed"] == 0
    assert stub.verify_requests == []
    # Once the clock passes next_at, it is selected again.
    payment = get_payment(session_factory, "rec-wait")
    later = payment.reconciliation_next_at
    assert later is not None
    future = (later if later.tzinfo else later.replace(tzinfo=UTC)) + timedelta(seconds=1)
    stats = _run_pass(session_factory, settings, stub, now_fn=lambda: future)
    assert stats["processed"] == 1
    assert get_payment(session_factory, "rec-wait").reconciliation_attempts == 2


def test_max_attempts_exhausts_without_state_change(
    client, settings, session_factory, stub
):
    _make_stale_link(client, settings, session_factory, order_id="rec-exh")
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "rec-exh")
        ).scalar_one()
        payment.reconciliation_attempts = settings.reconciliation_max_attempts - 1
        db.commit()
        payment_id = payment.id

    stats = _run_pass(session_factory, settings, stub)  # final attempt, unpaid
    assert stats["processed"] == 1
    assert stats["exhausted"] == 1

    payment = get_payment(session_factory, "rec-exh")
    assert payment.status == PaymentStatus.LINK_CREATED.value  # not paid, not failed
    assert payment.reconciliation_attempts == settings.reconciliation_max_attempts
    assert payment.reconciliation_next_at is None
    types = event_types(get_events(session_factory, payment_id))
    assert "reconciliation_exhausted" in types

    # Exhausted payments are never selected again.
    stub.verify_requests.clear()
    assert _run_pass(session_factory, settings, stub)["processed"] == 0
    assert stub.verify_requests == []


def test_unpaid_reconciliation_never_creates_admin_alerts(
    app, client, settings, session_factory, stub, alert_policy
):
    """Review finding: with admin error alerts enabled (production default),
    routine "not paid yet" reconciliation checks must NOT create admin alert
    rows - otherwise every in-progress payment floods the admin outbox. Only
    the distinct centralpay_verify_not_paid event is recorded, which the
    alert mapper ignores. (A CALLBACK reporting unpaid keeps alerting - that
    path is unchanged.)"""
    from tests.conftest import get_alerts

    _make_stale_link(client, settings, session_factory, order_id="rec-alert")
    stats = _run_pass(session_factory, alert_policy, stub)  # default stub: unpaid
    assert stats["retry_scheduled"] == 1
    assert get_alerts(session_factory) == []  # no alert rows at all


def test_claim_gap_is_closed_by_provisional_schedule(
    client, settings, session_factory, stub, monkeypatch
):
    """Review finding: the shared settlement path commits (releasing the row
    lock) BEFORE retry scheduling is finalized. The claim transaction must
    therefore already carry a provisional future next_at, so the committed
    gap-state is never due and a second worker cannot fire an immediate
    duplicate verify."""
    from app.services.verification import verify_and_settle as real_settle

    seen: list[object] = []

    def capturing(db, gateway, payment, *, settings=None, source="callback"):
        # State at the moment the shared path will commit: the provisional
        # schedule must already be on the row, inside the claim transaction.
        seen.append(payment.reconciliation_next_at)
        return real_settle(db, gateway, payment, settings=settings, source=source)

    monkeypatch.setattr("app.services.reconciliation.verify_and_settle", capturing)
    _make_stale_link(client, settings, session_factory, order_id="rec-gap")
    before = datetime.now(UTC)
    assert _run_pass(session_factory, settings, stub)["processed"] == 1  # unpaid path
    [provisional] = seen
    assert provisional is not None
    assert as_utc(provisional) > before - timedelta(seconds=2)
    # The provisional schedule uses the SAME two-stage helper: this link is
    # young (age < fast window), so it sits one FAST interval out — never an
    # exponential value, never "due now".
    assert as_utc(provisional) <= before + timedelta(
        seconds=settings.reconciliation_fast_interval_seconds + 3
    )
    # And the finalized schedule still stands after the pass.
    payment = get_payment(session_factory, "rec-gap")
    assert payment.reconciliation_next_at is not None


def test_old_payment_after_worker_downtime_uses_slow_interval(
    client, settings, session_factory, stub
):
    """End-to-end downtime scenario: a 20-minute-old link with ONE recorded
    attempt (the worker was offline) schedules its next check ~300 s out —
    the fast stage never restarts."""
    _make_stale_link(client, settings, session_factory, order_id="rec-down")
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "rec-down")
        ).scalar_one()
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(minutes=20)
        payment.reconciliation_attempts = 1  # low attempt count
        db.commit()

    stats = _run_pass(session_factory, settings, stub)  # default stub: unpaid
    assert stats["retry_scheduled"] == 1
    payment = get_payment(session_factory, "rec-down")
    assert payment.reconciliation_attempts == 2
    assert payment.reconciliation_next_at is not None
    remaining = (as_utc(payment.reconciliation_next_at) - datetime.now(UTC)).total_seconds()
    slow = settings.reconciliation_slow_interval_seconds
    assert slow - 10 < remaining <= slow + 1  # ~300 s, NOT the 10 s fast stage


# --- two-tier fairness (active vs. expiring) and the hard age cutoff --------


def test_fresh_payment_not_delayed_by_backlog(client, settings, session_factory, stub):
    """A newly-due (<15 min) payment must be checked in this very pass even
    with a historical backlog far exceeding the batch size — the production
    starvation bug ("young first" ORDER BY sorting new rows LAST) that this
    scheduling fix corrects."""
    batch_size = settings.reconciliation_batch_size
    for i in range(batch_size * 2):
        _make_old_link(
            client, settings, session_factory,
            order_id=f"rec-backlog-{i}",
            age_seconds=settings.reconciliation_fast_window_seconds + 60,
        )
    _make_stale_link(client, settings, session_factory, order_id="rec-fresh-priority")
    _run_pass(session_factory, settings, stub, batch_size=batch_size)
    checked = get_payment(session_factory, "rec-fresh-priority")
    assert checked.reconciliation_attempts == 1
    assert checked.reconciliation_last_at is not None


def test_expiring_tier_gets_reserved_capacity_under_continuous_fresh_traffic(
    client, settings, session_factory, stub
):
    """Even when the active tier alone can fill the whole batch every pass,
    the expiring (15 min-2 h) tier must still get its reserved slot(s) —
    it must never be permanently starved."""
    batch_size = settings.reconciliation_batch_size
    assert settings.reconciliation_slow_tier_reserved_slots >= 1
    for i in range(batch_size * 2):  # far more active-tier due rows than one batch
        _make_stale_link(client, settings, session_factory, order_id=f"rec-active-{i}")
    _make_old_link(
        client, settings, session_factory,
        order_id="rec-expiring-1",
        age_seconds=settings.reconciliation_fast_window_seconds + 60,
    )
    _run_pass(session_factory, settings, stub, batch_size=batch_size)
    checked = get_payment(session_factory, "rec-expiring-1")
    assert checked.reconciliation_attempts == 1  # got its reserved slot this pass


def test_expiring_tier_reserved_slot_survives_pass_time_budget_exhaustion(
    client, settings, session_factory, stub
):
    """The reserved expiring-tier slot(s) must be attempted at the HEAD of
    the pass, not the tail: ``run_reconciliation_pass`` only checks its
    wall-clock time budget before STARTING a new claim, never mid-verify, so
    a tail-positioned reservation is reachable only if every earlier slot's
    verify call finishes inside the shrinking remaining budget. Under
    sustained, slow active-tier traffic that budget can be exhausted long
    before the pass ever reaches a tail slot, permanently starving the
    expiring tier despite the documented guarantee. Reproduce that
    precondition here — enough due active-tier rows to fill the whole batch,
    each verify call artificially slow, and a time budget tight enough that
    the pass is cut off well before batch_size claims complete — and assert
    the expiring payment still gets its attempt because its reserved slot
    runs FIRST, unaffected by how much budget later active-tier claims burn."""
    batch_size = settings.reconciliation_batch_size
    assert settings.reconciliation_slow_tier_reserved_slots >= 1
    for i in range(batch_size):  # enough active-tier rows to fill the batch alone
        _make_stale_link(client, settings, session_factory, order_id=f"rec-slow-active-{i}")
    _make_old_link(
        client, settings, session_factory,
        order_id="rec-slow-expiring",
        age_seconds=settings.reconciliation_fast_window_seconds + 60,
    )
    stub.verify_delay_seconds = 0.15
    stats = _run_pass(
        session_factory, settings, stub, batch_size=batch_size, time_budget_seconds=0.4
    )
    # The tight budget really did cut the pass short before batch_size claims
    # completed — otherwise this test would not exercise the starvation bug.
    assert stats["processed"] < batch_size
    checked = get_payment(session_factory, "rec-slow-expiring")
    assert checked.reconciliation_attempts == 1


def test_active_tier_priority_slot_survives_pass_time_budget_exhaustion(
    client, settings, session_factory, stub
):
    """Mirror of the previous test: the ACTIVE tier's priority slot 0 must
    also survive an EXPIRING-tier verify call that alone exhausts the pass's
    wall-clock budget. Before the mandatory-prefix fix, slot 0 always
    preferred the expiring tier outright, so a sustained expiring backlog
    combined with slow verify calls could exhaust the whole budget on slot 0
    alone and leave the active tier — the still-payable, highest-priority
    tier — with ZERO claims, pass after pass. Reproduce that precondition:
    a sustained expiring-tier backlog (more due rows than one batch), one due
    active-tier payment, a verify call slower than the whole time budget by
    itself, and assert the active payment still gets attempted because slot
    0 (active-first) and the reserved expiring-first slot(s) that follow it
    are a mandatory prefix that runs regardless of budget exhaustion."""
    batch_size = settings.reconciliation_batch_size
    assert settings.reconciliation_slow_tier_reserved_slots >= 1
    for i in range(batch_size):  # sustained expiring-tier backlog
        _make_old_link(
            client, settings, session_factory,
            order_id=f"rec-slow-expiring-{i}",
            age_seconds=settings.reconciliation_fast_window_seconds + 60,
        )
    _make_stale_link(client, settings, session_factory, order_id="rec-slow-active")
    stub.verify_delay_seconds = 0.5  # a single verify call alone exceeds the budget below
    stats = _run_pass(
        session_factory, settings, stub, batch_size=batch_size, time_budget_seconds=0.3
    )
    # The tight budget really did cut the pass short before batch_size claims
    # completed — otherwise this test would not exercise the starvation bug.
    assert stats["processed"] < batch_size
    checked = get_payment(session_factory, "rec-slow-active")
    assert checked.reconciliation_attempts == 1


def test_spillover_from_active_tier_to_expiring_tier(client, settings, session_factory, stub):
    """When the active tier has fewer due rows than the batch, the unused
    capacity spills to the expiring tier instead of going idle."""
    batch_size = settings.reconciliation_batch_size
    _make_stale_link(client, settings, session_factory, order_id="rec-active-only")
    old_ids = [f"rec-old-spill-{i}" for i in range(batch_size - 1)]
    for order_id in old_ids:
        _make_old_link(
            client, settings, session_factory, order_id=order_id,
            age_seconds=settings.reconciliation_fast_window_seconds + 60,
        )
    stats = _run_pass(session_factory, settings, stub, batch_size=batch_size)
    assert stats["processed"] == batch_size  # 1 active + (batch_size - 1) expiring
    for order_id in old_ids:
        assert get_payment(session_factory, order_id).reconciliation_attempts == 1


def test_spillover_from_expiring_tier_to_active_tier(client, settings, session_factory, stub):
    """When the expiring tier is empty, its reserved capacity goes entirely
    to the active tier instead of going unused."""
    batch_size = settings.reconciliation_batch_size
    active_ids = [f"rec-active-spill-{i}" for i in range(batch_size)]
    for order_id in active_ids:
        _make_stale_link(client, settings, session_factory, order_id=order_id)
    stats = _run_pass(session_factory, settings, stub, batch_size=batch_size)
    assert stats["processed"] == batch_size  # no expiring rows due: all slots go active
    for order_id in active_ids:
        assert get_payment(session_factory, order_id).reconciliation_attempts == 1


def test_processed_never_exceeds_batch_size_across_tiers(client, settings, session_factory, stub):
    """With more than batch_size due rows in BOTH tiers combined, total
    processed for the pass is still bounded by batch_size exactly."""
    batch_size = settings.reconciliation_batch_size
    for i in range(batch_size):
        _make_stale_link(client, settings, session_factory, order_id=f"rec-cap-active-{i}")
    for i in range(batch_size):
        _make_old_link(
            client, settings, session_factory, order_id=f"rec-cap-old-{i}",
            age_seconds=settings.reconciliation_fast_window_seconds + 60,
        )
    stats = _run_pass(session_factory, settings, stub, batch_size=batch_size)
    assert stats["processed"] == batch_size


def test_selection_boundary_just_below_fast_window(client, settings, session_factory, stub):
    """Age = fast_window - 1: still the ACTIVE tier — selected, fast retry."""
    window = settings.reconciliation_fast_window_seconds
    _make_old_link(
        client, settings, session_factory, order_id="rec-b-below", age_seconds=window - 1
    )
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    payment = get_payment(session_factory, "rec-b-below")
    remaining = (as_utc(payment.reconciliation_next_at) - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= settings.reconciliation_fast_interval_seconds + 2


def test_selection_boundary_exactly_at_fast_window(client, settings, session_factory, stub):
    """Age == fast_window exactly: the active tier is a strict less-than, so
    this is EXPIRING-tier — still selected, slow retry."""
    window = settings.reconciliation_fast_window_seconds
    _make_old_link(client, settings, session_factory, order_id="rec-b-at", age_seconds=window)
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    payment = get_payment(session_factory, "rec-b-at")
    remaining = (as_utc(payment.reconciliation_next_at) - datetime.now(UTC)).total_seconds()
    slow = settings.reconciliation_slow_interval_seconds
    assert slow - 5 < remaining <= slow + 2


def test_selection_boundary_just_above_fast_window(client, settings, session_factory, stub):
    """Age = fast_window + 1: EXPIRING tier — still selected, slow retry."""
    window = settings.reconciliation_fast_window_seconds
    _make_old_link(
        client, settings, session_factory, order_id="rec-b-above", age_seconds=window + 1
    )
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    payment = get_payment(session_factory, "rec-b-above")
    remaining = (as_utc(payment.reconciliation_next_at) - datetime.now(UTC)).total_seconds()
    slow = settings.reconciliation_slow_interval_seconds
    assert slow - 5 < remaining <= slow + 2


def test_selection_boundary_just_below_max_age(client, settings, session_factory, stub):
    """Age = max_age - 1: still inside the expiring tier — selected."""
    max_age = settings.reconciliation_max_age_seconds
    _make_old_link(
        client, settings, session_factory, order_id="rec-hb-below", age_seconds=max_age - 1
    )
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    assert get_payment(session_factory, "rec-hb-below").reconciliation_attempts == 1


def test_selection_boundary_exactly_at_max_age_excluded(client, settings, session_factory, stub):
    """Age == max_age exactly: the hard cutoff excludes it — never selected,
    never mutated."""
    max_age = settings.reconciliation_max_age_seconds
    _make_old_link(client, settings, session_factory, order_id="rec-hb-at", age_seconds=max_age)
    before = get_payment(session_factory, "rec-hb-at")
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 0
    after = get_payment(session_factory, "rec-hb-at")
    assert after.reconciliation_attempts == before.reconciliation_attempts == 0
    assert after.status == PaymentStatus.LINK_CREATED.value


def test_selection_boundary_just_above_max_age_excluded(client, settings, session_factory, stub):
    """Age = max_age + 1: excluded — never selected."""
    max_age = settings.reconciliation_max_age_seconds
    _make_old_link(
        client, settings, session_factory, order_id="rec-hb-above", age_seconds=max_age + 1
    )
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 0


def test_payment_older_than_max_age_is_never_mutated(client, settings, session_factory, stub):
    """A payment well past the 2-hour hard cutoff is preserved byte-for-byte
    across multiple passes — never deleted, never marked paid or failed,
    never touched at all. It stays link_created for audit/operator
    inspection, exactly as the automatic-reconciliation-stops policy
    requires."""
    ancient = _make_old_link(
        client, settings, session_factory, order_id="rec-ancient",
        age_seconds=settings.reconciliation_max_age_seconds * 5,
    )

    def snapshot():
        with session_factory() as db:
            return db.execute(
                select(
                    Payment.id,
                    Payment.status,
                    Payment.amount,
                    Payment.fee_amount,
                    Payment.payable_amount,
                    Payment.reference_id,
                    Payment.gateway_verified_at,
                    Payment.reconciliation_attempts,
                    Payment.reconciliation_next_at,
                    Payment.reconciliation_last_at,
                    Payment.reconciliation_last_error_code,
                    Payment.reconciliation_claimed_at,
                    Payment.reconciliation_claimed_by,
                    Payment.updated_at,
                ).where(Payment.id == ancient.id)
            ).one()

    before = snapshot()
    for _ in range(3):
        stats = _run_pass(session_factory, settings, stub)
        assert stats["processed"] == 0
    assert snapshot() == before
    assert get_payment(session_factory, "rec-ancient").status == PaymentStatus.LINK_CREATED.value


def test_max_attempts_respected_in_expiring_tier(client, settings, session_factory, stub):
    """reconciliation_max_attempts remains a secondary safety guard inside
    the expiring tier too, not just the active tier."""
    _make_old_link(
        client, settings, session_factory, order_id="rec-old-exh",
        age_seconds=settings.reconciliation_fast_window_seconds + 60,
    )
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "rec-old-exh")
        ).scalar_one()
        payment.reconciliation_attempts = settings.reconciliation_max_attempts - 1
        db.commit()

    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    assert stats["exhausted"] == 1
    payment = get_payment(session_factory, "rec-old-exh")
    assert payment.reconciliation_attempts == settings.reconciliation_max_attempts
    assert payment.reconciliation_next_at is None
    assert payment.status == PaymentStatus.LINK_CREATED.value

    stub.verify_requests.clear()
    assert _run_pass(session_factory, settings, stub)["processed"] == 0
    assert stub.verify_requests == []


def test_reconciliation_max_age_must_exceed_fast_window(settings):
    # model_copy() does not re-run validators, so the invariant is checked
    # via a fresh construction from the full field set instead.
    with pytest.raises(ValueError, match="RECONCILIATION_MAX_AGE_SECONDS"):
        type(settings)(
            **{
                **settings.model_dump(),
                "reconciliation_max_age_seconds": settings.reconciliation_fast_window_seconds,
            }
        )


def test_reconciliation_slow_tier_reserved_slots_must_be_below_batch_size(settings):
    with pytest.raises(ValueError, match="RECONCILIATION_SLOW_TIER_RESERVED_SLOTS"):
        type(settings)(
            **{
                **settings.model_dump(),
                "reconciliation_slow_tier_reserved_slots": settings.reconciliation_batch_size,
            }
        )


def test_verified_payment_is_never_verified_again(
    client, settings, session_factory, stub
):
    """After reconciliation settles a payment, reconciliation_next_at is NULL
    and a later pass sends NO verify request for it."""
    payment = _make_stale_link(client, settings, session_factory, order_id="rec-done")
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id="REF-rec-done"
    )
    assert _run_pass(session_factory, settings, stub)["verified"] == 1
    settled = get_payment(session_factory, "rec-done")
    assert settled.gateway_verified_at is not None
    assert settled.reconciliation_next_at is None

    stub.verify_requests.clear()
    later = datetime.now(UTC) + timedelta(hours=1)
    stats = _run_pass(session_factory, settings, stub, now_fn=lambda: later)
    assert stats["processed"] == 0
    assert stub.verify_requests == []  # never verified again
    assert _notification_queued_count(session_factory, settled.id) == 1


# --- financial mismatches keep the existing manual_review behavior ------------


@pytest.mark.parametrize(
    "verify_kwargs,expected_event",
    [
        ({"amount": 999}, "verify_payable_amount_mismatch"),
        ({"amount": 10000, "user_id": 424299}, "verify_user_id_mismatch"),
        ({"amount": 10000, "reference_id": None}, "verify_missing_reference_id"),
        ({"amount": 10000, "reference_id": "x" * 300}, "verify_invalid_reference_id"),
    ],
)
def test_financial_mismatches_move_to_manual_review(
    client, settings, session_factory, stub, verify_kwargs, expected_event
):
    payment = _make_stale_link(client, settings, session_factory, order_id="rec-mm")
    kwargs = dict(verify_kwargs)
    kwargs.setdefault("user_id", payment.gateway_user_id)
    stub.verify_result = verify_ok_response(**kwargs)
    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 1
    assert stats["under_review"] == 1

    reviewed = get_payment(session_factory, "rec-mm")
    assert reviewed.status == PaymentStatus.MANUAL_REVIEW.value
    assert reviewed.gateway_verified_at is None
    types = event_types(get_events(session_factory, reviewed.id))
    assert expected_event in types
    assert "manual_review_required" in types
    assert _notification_queued_count(session_factory, reviewed.id) == 0  # never notified

    # manual_review is never auto-processed afterwards.
    stub.verify_requests.clear()
    assert _run_pass(session_factory, settings, stub)["processed"] == 0
    assert stub.verify_requests == []


def test_duplicate_reference_id_moves_to_manual_review(
    client, settings, session_factory, stub
):
    # First payment settles normally (via reconciliation) and owns the ref.
    first = _make_stale_link(client, settings, session_factory, order_id="rec-ref-a")
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=first.gateway_user_id, reference_id="REF-dup"
    )
    assert _run_pass(session_factory, settings, stub)["verified"] == 1

    # Second payment reports the SAME referenceId -> collision -> review.
    second = _make_stale_link(client, settings, session_factory, order_id="rec-ref-b")
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=second.gateway_user_id, reference_id="REF-dup"
    )
    stats = _run_pass(session_factory, settings, stub)
    assert stats["under_review"] == 1
    reviewed = get_payment(session_factory, "rec-ref-b")
    assert reviewed.status == PaymentStatus.MANUAL_REVIEW.value
    assert reviewed.reference_id is None  # never overwritten
    types = event_types(get_events(session_factory, reviewed.id))
    assert "reference_id_collision" in types


# --- callback/reconciliation idempotency --------------------------------------


def test_callback_verified_payment_is_not_reconciled(
    client, settings, session_factory, stub
):
    payment = _make_stale_link(client, settings, session_factory, order_id="rec-cb1")
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id="REF-cb1"
    )
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    stub.verify_requests.clear()

    stats = _run_pass(session_factory, settings, stub)
    assert stats["processed"] == 0  # already settled: not even selected
    assert stub.verify_requests == []
    assert _notification_queued_count(session_factory, payment.id) == 1


def test_callback_after_reconciliation_is_duplicate(
    client, settings, session_factory, stub
):
    payment = _make_stale_link(client, settings, session_factory, order_id="rec-cb2")
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id="REF-cb2"
    )
    assert _run_pass(session_factory, settings, stub)["verified"] == 1
    verify_calls = len(stub.verify_requests)

    # The payer's browser finally arrives with the REAL signed callback URL
    # and one-time token: the normal duplicate path answers, verify is never
    # called again, and the notification stays queued exactly once.
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert len(stub.verify_requests) == verify_calls
    types = event_types(get_events(session_factory, payment.id))
    assert "duplicate_callback_ignored" in types
    assert _notification_queued_count(session_factory, payment.id) == 1
    assert get_payment(session_factory, "rec-cb2").status == (
        PaymentStatus.BOT_NOTIFY_PENDING.value
    )


# --- crash isolation ----------------------------------------------------------


def test_one_payment_exception_does_not_stop_the_pass(
    client, settings, session_factory, stub, monkeypatch
):
    first = _make_stale_link(client, settings, session_factory, order_id="rec-boom")
    second = _make_stale_link(client, settings, session_factory, order_id="rec-ok")
    # Make created_at ordering deterministic: rec-boom is older.
    with session_factory() as db:
        boom = db.execute(select(Payment).where(Payment.bot_order_id == "rec-boom")).scalar_one()
        boom.created_at = datetime.now(UTC) - timedelta(hours=2)
        db.commit()

    from app.services.verification import verify_and_settle as real_settle

    boom_gateway_order_id = first.gateway_order_id

    def exploding(db, gateway, payment, *, settings=None, source="callback"):
        if payment.gateway_order_id == boom_gateway_order_id:
            raise RuntimeError("unexpected bug")
        return real_settle(db, gateway, payment, settings=settings, source=source)

    monkeypatch.setattr("app.services.reconciliation.verify_and_settle", exploding)
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=second.gateway_user_id, reference_id="REF-ok"
    )

    stats = _run_pass(session_factory, settings, stub)  # must not raise
    assert stats["processed"] == 2
    assert stats["verified"] == 1  # the healthy payment settled
    assert stats["retry_scheduled"] == 1  # the crashed one retries later

    crashed = get_payment(session_factory, "rec-boom")
    assert crashed.status == PaymentStatus.LINK_CREATED.value
    assert crashed.reconciliation_attempts == 1
    assert crashed.reconciliation_last_error_code == ERROR_INTERNAL
    assert crashed.reconciliation_next_at is not None
    assert get_payment(session_factory, "rec-ok").status == (
        PaymentStatus.BOT_NOTIFY_PENDING.value
    )


def test_batch_size_bounds_the_pass(client, settings, session_factory, stub):
    for i in range(3):
        _make_stale_link(client, settings, session_factory, order_id=f"rec-batch-{i}")
    stats = _run_pass(session_factory, settings, stub, batch_size=2)
    assert stats["processed"] == 2


def test_reconciled_payment_delivers_notification_once(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """End-to-end: reconciliation settles, the notification worker delivers,
    and the bot receives exactly one unchanged payload."""
    from tests.conftest import run_pass as run_notification_pass

    payment = _make_stale_link(client, settings, session_factory, order_id="rec-e2e")
    stub.verify_result = verify_ok_response(
        amount=10000, user_id=payment.gateway_user_id, reference_id="REF-e2e"
    )
    assert _run_pass(session_factory, settings, stub)["verified"] == 1

    result = run_notification_pass(session_factory, notifier, settings)
    assert result["processed"] == 1
    [request] = bot_stub.requests
    assert request == {"order_id": "rec-e2e", "actions": "custom_payment_verify"}
    assert get_payment(session_factory, "rec-e2e").status == (
        PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    )


# --- dedicated worker thread lifecycle ----------------------------------------


def test_reconciliation_thread_loop_starts_and_stops_cleanly(settings, session_factory):
    """The dedicated thread body runs passes on its interval with its own
    client/sessions and exits promptly when the stop event is set."""
    import threading
    import time as _time

    from app.worker import reconciliation_loop

    fast = settings.model_copy(update={"reconciliation_interval_seconds": 0.05})
    stop = threading.Event()
    thread = threading.Thread(
        target=reconciliation_loop,
        args=(fast, session_factory),
        kwargs={"worker_id": "loop-test", "stop": stop},
        daemon=True,
    )
    thread.start()
    _time.sleep(0.3)  # several empty passes (no due payments, no gateway I/O)
    assert thread.is_alive()
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    # Review finding: the heartbeat row must be its OWN instance (the upsert
    # keys on instance_id alone), so it can never shadow the notification
    # worker's row and make /health report that worker missing.
    from app.models import WorkerHeartbeat

    with session_factory() as db:
        [row] = db.execute(select(WorkerHeartbeat)).scalars().all()
    assert row.worker_name == "reconciliation-worker"
    assert row.instance_id == "loop-test-reconciliation"


def test_reconciliation_thread_survives_pass_exceptions(settings):
    """A failing pass (here: the database is down) only logs and waits for the
    next interval — the thread never dies."""
    import threading
    import time as _time

    from sqlalchemy.orm import Session

    from app.worker import reconciliation_loop

    calls: list[int] = []

    def bad_factory() -> Session:
        calls.append(1)
        raise RuntimeError("database unavailable")

    fast = settings.model_copy(update={"reconciliation_interval_seconds": 0.02})
    stop = threading.Event()
    thread = threading.Thread(
        target=reconciliation_loop,
        args=(fast, bad_factory),
        kwargs={"worker_id": "loop-crash-test", "stop": stop},
        daemon=True,
    )
    thread.start()
    _time.sleep(0.3)
    assert thread.is_alive()  # still looping despite every pass failing
    assert len(calls) >= 2  # it kept retrying
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()


# --- heartbeat identity (one process, two loops, two rows) --------------------


def test_one_process_keeps_two_heartbeat_rows_with_correct_names(session_factory):
    """Regression: both loops of ONE worker process heartbeat under their own
    stable instance ids, so one process creates and refreshes TWO rows — the
    startup race can no longer let one loop own (and permanently label) the
    other's row."""
    from datetime import timedelta as _td

    from sqlalchemy import select as _select

    from app.models import WorkerHeartbeat
    from app.services.heartbeat import record_worker_heartbeat
    from app.worker import heartbeat_instance_id

    base = "host-1234-abc123"  # the shared base worker id (logs/claims)
    t0 = datetime.now(UTC)

    def beat(name, loop, now):
        with session_factory() as db:
            record_worker_heartbeat(
                db,
                worker_name=name,
                instance_id=heartbeat_instance_id(base, loop),
                now=now,
                cycle_completed=True,
            )

    # Worst-case startup order (the old bug): reconciliation wins the race.
    beat("reconciliation-worker", "reconciliation", t0)
    beat("notification-worker", "notification", t0)
    # Both loops refresh later.
    t1 = t0 + _td(seconds=30)
    beat("reconciliation-worker", "reconciliation", t1)
    beat("notification-worker", "notification", t1)

    with session_factory() as db:
        rows = db.execute(
            _select(WorkerHeartbeat).order_by(WorkerHeartbeat.instance_id)
        ).scalars().all()
        by_instance = {row.instance_id: row for row in rows}
    assert len(rows) == 2  # exactly two rows — refreshes never created more
    notification = by_instance[f"{base}-notification"]
    reconciliation = by_instance[f"{base}-reconciliation"]
    assert notification.worker_name == "notification-worker"
    assert reconciliation.worker_name == "reconciliation-worker"
    # Both were refreshed, not recreated or cross-relabeled.
    assert as_utc(notification.last_heartbeat_at) == t1
    assert as_utc(reconciliation.last_heartbeat_at) == t1


def test_admin_health_sees_fresh_notification_worker_with_both_loops_active(
    session_factory,
):
    """Regression: with both loops heartbeating (reconciliation first — the
    order that used to poison the shared row), admin health still finds a
    FRESH notification-worker heartbeat and never reports it missing/stale."""
    from app.adminbot.queries import latest_worker_heartbeat, worker_heartbeat_age_seconds
    from app.services.heartbeat import record_worker_heartbeat
    from app.worker import heartbeat_instance_id

    base = "host-5678-def456"
    now = datetime.now(UTC)
    with session_factory() as db:
        record_worker_heartbeat(
            db,
            worker_name="reconciliation-worker",
            instance_id=heartbeat_instance_id(base, "reconciliation"),
            now=now,
            cycle_completed=True,
        )
    with session_factory() as db:
        record_worker_heartbeat(
            db,
            worker_name="notification-worker",
            instance_id=heartbeat_instance_id(base, "notification"),
            now=now,
            cycle_completed=True,
        )

    with session_factory() as db:
        found = latest_worker_heartbeat(db)  # admin default: notification-worker
        assert found is not None
        assert found.worker_name == "notification-worker"
        assert found.instance_id == f"{base}-notification"
        age = worker_heartbeat_age_seconds(db)
    assert age is not None
    assert age < 60  # fresh — never reported stale/missing


def test_record_worker_heartbeat_never_silently_relabels(session_factory, caplog):
    """A heartbeat targeting an instance row owned by a DIFFERENT worker type
    is refused loudly: the row keeps its name AND its timestamp (refreshing it
    would fake the other worker's liveness), and a warning is logged."""
    import logging as _logging

    from sqlalchemy import select as _select

    from app.models import WorkerHeartbeat
    from app.services.heartbeat import record_worker_heartbeat

    t0 = datetime.now(UTC)
    with session_factory() as db:
        record_worker_heartbeat(
            db,
            worker_name="notification-worker",
            instance_id="collide-1",
            now=t0,
            cycle_completed=True,
        )
    with (
        caplog.at_level(_logging.WARNING, logger="app.services.heartbeat"),
        session_factory() as db,
    ):
        record_worker_heartbeat(
            db,
            worker_name="reconciliation-worker",  # wrong type, same instance
            instance_id="collide-1",
            now=t0 + timedelta(seconds=120),
            cycle_completed=True,
        )
    assert any(
        record.getMessage() == "worker_heartbeat_name_mismatch"
        for record in caplog.records
    )
    with session_factory() as db:
        [row] = db.execute(_select(WorkerHeartbeat)).scalars().all()
    assert row.worker_name == "notification-worker"  # never renamed
    assert as_utc(row.last_heartbeat_at) == t0  # never falsely refreshed
