"""app.services.stuck_payments: the single categorizer shared by
`centralpay stuck` and the admin bot's `/stuck`.

Covers: which bucket each payment state lands in (including the explicit
decision that a normal, non-exhausted link_created retry — even one the
gateway has already reported "not paid" — is NOT attention-worthy), the
exhausted-vs-expired priority ordering, the unexpected-status catch-all and
its grace period, reuse of the existing manual-review/bot-notification
detection, fixed category ordering, and the hard read-only guarantee.
"""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.models import Payment, PaymentEvent, PaymentStatus
from app.services.stuck_payments import (
    UNEXPECTED_STATE_GRACE_SECONDS,
    StuckCategory,
    stuck_payments_overview,
)
from tests.conftest import create_order, get_payment, make_verified_pending, run_pass


def _age_link(session_factory, order_id: str, *, seconds: int) -> None:
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(seconds=seconds)
        db.commit()


def _age_created(session_factory, order_id: str, *, seconds: int) -> None:
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = datetime.now(UTC) - timedelta(seconds=seconds)
        db.commit()


def _set_reconciliation_state(session_factory, order_id: str, **fields) -> None:
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        for key, value in fields.items():
            setattr(payment, key, value)
        db.commit()


def _category_of(overview, order_id: str) -> StuckCategory | None:
    for entry in overview.ordered():
        if entry.payment.bot_order_id == order_id:
            return entry.category
    return None


def _entry_for(overview, order_id: str):
    for entry in overview.ordered():
        if entry.payment.bot_order_id == order_id:
            return entry
    return None


# --- link_created buckets ---------------------------------------------------


def test_fresh_link_created_is_waiting_gateway(client, settings, session_factory, stub):
    assert create_order(client, settings, order_id="fresh-1").status_code == 200
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert _category_of(overview, "fresh-1") == StuckCategory.WAITING_GATEWAY
    entry = _entry_for(overview, "fresh-1")
    assert entry.gateway_state == "pending"


def test_not_yet_exhausted_gateway_not_paid_is_still_waiting_not_attention(
    client, settings, session_factory, stub
):
    """The explicit product decision: an ordinary in-flight expiring-tier
    retry the gateway has already reported "not paid" is routine polling,
    never "needs attention" — only exhaustion (or another hard signal)
    promotes a link_created payment out of waiting_gateway."""
    assert create_order(client, settings, order_id="notpaid-1").status_code == 200
    past_fast_window = settings.reconciliation_fast_window_seconds + 60
    _age_link(session_factory, "notpaid-1", seconds=past_fast_window)
    _set_reconciliation_state(
        session_factory,
        "notpaid-1",
        reconciliation_attempts=4,
        reconciliation_last_error_code="gateway_not_paid",
        reconciliation_next_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert _category_of(overview, "notpaid-1") == StuckCategory.WAITING_GATEWAY
    entry = _entry_for(overview, "notpaid-1")
    assert entry.gateway_state == "not_paid"


def test_exhausted_reconciliation_is_needs_attention(client, settings, session_factory, stub):
    assert create_order(client, settings, order_id="exhausted-1").status_code == 200
    past_fast_window = settings.reconciliation_fast_window_seconds + 60
    _age_link(session_factory, "exhausted-1", seconds=past_fast_window)
    _set_reconciliation_state(
        session_factory,
        "exhausted-1",
        reconciliation_attempts=settings.reconciliation_max_attempts,
        reconciliation_last_error_code="gateway_not_paid",
        reconciliation_next_at=None,
    )
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert _category_of(overview, "exhausted-1") == StuckCategory.NEEDS_ATTENTION
    entry = _entry_for(overview, "exhausted-1")
    assert entry.reason == "reconciliation_exhausted:gateway_not_paid"


def test_expired_link_is_expired_category(client, settings, session_factory, stub):
    assert create_order(client, settings, order_id="expired-1").status_code == 200
    _age_link(session_factory, "expired-1", seconds=settings.reconciliation_max_age_seconds + 60)
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert _category_of(overview, "expired-1") == StuckCategory.EXPIRED


def test_expired_wins_over_exhausted(client, settings, session_factory, stub):
    """A payment that was exhausted AND is now past max_age is reported as
    EXPIRED — the more specific, terminal signal — not NEEDS_ATTENTION."""
    assert create_order(client, settings, order_id="both-1").status_code == 200
    _age_link(session_factory, "both-1", seconds=settings.reconciliation_max_age_seconds + 60)
    _set_reconciliation_state(
        session_factory,
        "both-1",
        reconciliation_attempts=settings.reconciliation_max_attempts,
        reconciliation_last_error_code="gateway_not_paid",
        reconciliation_next_at=None,
    )
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert _category_of(overview, "both-1") == StuckCategory.EXPIRED


# --- unexpected states -------------------------------------------------------


def test_getlink_failed_past_grace_period_is_needs_attention(
    client, settings, session_factory, stub
):
    stub.getlink_result = httpx.Response(200, json={"status": "error", "message": "down"})
    response = create_order(client, settings, order_id="glf-old")
    assert response.status_code == 502
    payment = get_payment(session_factory, "glf-old")
    assert payment.status == PaymentStatus.GETLINK_FAILED.value
    _age_created(session_factory, "glf-old", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 10)

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    entry = _entry_for(overview, "glf-old")
    assert entry is not None
    assert entry.category == StuckCategory.NEEDS_ATTENTION
    assert entry.reason == "unexpected_status:getlink_failed"


def test_getlink_failed_within_grace_period_is_not_reported(
    client, settings, session_factory, stub
):
    stub.getlink_result = httpx.Response(200, json={"status": "error", "message": "down"})
    assert create_order(client, settings, order_id="glf-fresh").status_code == 502

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert _category_of(overview, "glf-fresh") is None


def test_orphaned_created_status_past_grace_period_is_needs_attention(
    settings, session_factory
):
    """A payment that never even reached getLink (e.g. a crash between
    insert and the synchronous getLink call) is never auto-retried by
    anything — reconciliation only ever selects link_created."""
    stale_created_at = datetime.now(UTC) - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS + 5)
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="orphan-created-1",
                gateway_order_id=990001,
                gateway_user_id=555001,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.CREATED.value,
                created_at=stale_created_at,
            )
        )
        db.commit()
        overview = stuck_payments_overview(db, settings)
    entry = _entry_for(overview, "orphan-created-1")
    assert entry is not None
    assert entry.category == StuckCategory.NEEDS_ATTENTION
    assert entry.reason == "unexpected_status:created"


# --- reused manual_review / bot-notification detection -----------------------


def test_manual_review_and_bot_notify_failure_reused_verbatim(
    client, settings, session_factory, stub, notifier, bot_stub
):
    make_verified_pending(client, settings, session_factory, stub, order_id="reused-attn")
    bot_stub.result = httpx.ReadTimeout("t")
    run_pass(session_factory, notifier, settings)

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    entry = _entry_for(overview, "reused-attn")
    assert entry is not None
    assert entry.category == StuckCategory.NEEDS_ATTENTION
    assert "bot_timeout_ambiguous" in entry.reason


# --- ordering + counts -------------------------------------------------------


def test_ordered_priority_is_attention_then_waiting_then_expired(
    client, settings, session_factory, stub
):
    assert create_order(client, settings, order_id="order-waiting").status_code == 200
    assert create_order(client, settings, order_id="order-expired").status_code == 200
    past_max_age = settings.reconciliation_max_age_seconds + 60
    _age_link(session_factory, "order-expired", seconds=past_max_age)
    assert create_order(client, settings, order_id="order-attn").status_code == 200
    past_fast_window = settings.reconciliation_fast_window_seconds + 60
    _age_link(session_factory, "order-attn", seconds=past_fast_window)
    _set_reconciliation_state(
        session_factory,
        "order-attn",
        reconciliation_attempts=settings.reconciliation_max_attempts,
        reconciliation_next_at=None,
    )

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    categories = [entry.category for entry in overview.ordered()]
    first_attention = categories.index(StuckCategory.NEEDS_ATTENTION)
    first_waiting = categories.index(StuckCategory.WAITING_GATEWAY)
    first_expired = categories.index(StuckCategory.EXPIRED)
    assert first_attention < first_waiting < first_expired


def test_total_counts_match_returned_entries_under_the_cap(
    client, settings, session_factory, stub
):
    assert create_order(client, settings, order_id="count-1").status_code == 200
    assert create_order(client, settings, order_id="count-2").status_code == 200
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert overview.total_counts["waiting_gateway"] == len(overview.waiting_gateway)
    assert overview.total_counts["needs_attention"] == len(overview.needs_attention)
    assert overview.total_counts["expired"] == len(overview.expired)


# --- read-only guarantee -----------------------------------------------------


def test_stuck_overview_never_mutates_or_writes_events(
    client, settings, session_factory, stub, notifier, bot_stub
):
    assert create_order(client, settings, order_id="ro-waiting").status_code == 200
    assert create_order(client, settings, order_id="ro-expired").status_code == 200
    _age_link(session_factory, "ro-expired", seconds=settings.reconciliation_max_age_seconds + 60)
    make_verified_pending(client, settings, session_factory, stub, order_id="ro-attn")
    bot_stub.result = httpx.ReadTimeout("t")
    run_pass(session_factory, notifier, settings)

    with session_factory() as db:
        before_payments = db.execute(
            select(
                Payment.id,
                Payment.status,
                Payment.reconciliation_attempts,
                Payment.reconciliation_next_at,
                Payment.reconciliation_claimed_at,
                Payment.reconciliation_claimed_by,
                Payment.updated_at,
            ).order_by(Payment.id)
        ).all()
        before_event_count = db.execute(select(func.count(PaymentEvent.id))).scalar_one()

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert overview.ordered()  # sanity: the fixture actually produced entries

    with session_factory() as db:
        after_payments = db.execute(
            select(
                Payment.id,
                Payment.status,
                Payment.reconciliation_attempts,
                Payment.reconciliation_next_at,
                Payment.reconciliation_claimed_at,
                Payment.reconciliation_claimed_by,
                Payment.updated_at,
            ).order_by(Payment.id)
        ).all()
        after_event_count = db.execute(select(func.count(PaymentEvent.id))).scalar_one()

    assert after_payments == before_payments
    assert after_event_count == before_event_count
