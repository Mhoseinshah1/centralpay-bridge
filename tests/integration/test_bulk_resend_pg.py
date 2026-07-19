"""Admin bulk resend (/resend_failed) on real PostgreSQL.

Covers eligibility selection, financial/gateway-fact preservation, the
attempt-counter policy, worker hand-off, real FOR UPDATE SKIP LOCKED
concurrency (no mocking of the database's locking behavior), audit events,
safe-mode refusal, and the secret policy.

Deterministic: injected clock, no sleeps (the only waits are on real database
row locks and thread joins).
"""

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.adminbot.auth import UpdateContext
from app.adminbot.commands import BULK_RESEND_SAFE_MODE_MESSAGE, CommandHandlers
from app.bot import BotNotifier
from app.models import Base, Payment, PaymentEvent, PaymentStatus
from app.services.bulk_resend import (
    BulkResendResult,
    preview_bulk_resend,
    requeue_failed_deliveries,
)
from app.services.notification import claim_next_due, run_worker_pass
from tests.conftest import TEST_ADMIN_ID

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in (
            "admin_alerts",
            "worker_heartbeats",
            "payment_events",
            "payments",
            "fee_policies",
            "alembic_version",
        ):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    Base.metadata.create_all(pg_engine)
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def idem_settings(settings):
    return settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})


_ORDER_SEQ = iter(range(1, 10_000_000))


def insert_payment(
    session_factory,
    *,
    order_id,
    status=PaymentStatus.MANUAL_REVIEW.value,
    reason="retry_limit_reached",
    gateway_verified=True,
    review_resolved=False,
    claimed=False,
    amount=10000,
    fee_rate_bps=0,
    fee_amount=0,
    reference_id=None,
    attempts=6,
    manual_review_at=T0,
) -> int:
    gid = 100_000_000 + next(_ORDER_SEQ)
    with session_factory() as db:
        payment = Payment(
            bot_order_id=order_id,
            gateway_order_id=gid,
            gateway_user_id=4242,
            amount=amount,
            fee_rate_bps=fee_rate_bps,
            fee_amount=fee_amount,
            payable_amount=amount + fee_amount,
            status=status,
            reference_id=reference_id if reference_id is not None else f"REF-{order_id}",
            gateway_verified_at=T0 - timedelta(hours=1) if gateway_verified else None,
            bot_notify_reason=reason,
            bot_notify_attempts=attempts,
            manual_review_at=manual_review_at,
            review_resolved_at=(T0 if review_resolved else None),
            notification_claimed_at=(T0 if claimed else None),
            notification_claimed_by=("worker-x" if claimed else None),
        )
        db.add(payment)
        db.commit()
        return payment.id


def get_payment(session_factory, payment_id) -> Payment:
    with session_factory() as db:
        return db.get(Payment, payment_id)


def event_count(session_factory, event_type, payment_id=None) -> int:
    with session_factory() as db:
        query = select(func.count(PaymentEvent.id)).where(PaymentEvent.event_type == event_type)
        if payment_id is not None:
            query = query.where(PaymentEvent.payment_id == payment_id)
        return db.execute(query).scalar_one()


def requeue(session_factory, *, user_id=TEST_ADMIN_ID, now=T0):
    with session_factory() as db:
        return requeue_failed_deliveries(db, telegram_user_id=user_id, now=now)


# --- eligibility -------------------------------------------------------------


@pytest.mark.parametrize("reason", ["retry_limit_reached", "bot_timeout_ambiguous"])
def test_eligible_reason_is_selected_and_requeued(pg_session_factory, reason):
    pid = insert_payment(pg_session_factory, order_id=f"elig-{reason}", reason=reason)
    result = requeue(pg_session_factory)
    assert result.requeued_count == 1
    row = get_payment(pg_session_factory, pid)
    assert row.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert row.bot_notify_reason == reason  # preserved until the next attempt
    assert event_count(pg_session_factory, "admin_bulk_resend_requested", pid) == 1


@pytest.mark.parametrize(
    "kwargs,label",
    [
        ({"reason": "verify_payable_amount_mismatch"}, "amount_mismatch"),
        ({"reason": "verify_user_id_mismatch"}, "user_mismatch"),
        ({"reason": "verify_invalid_reference_id"}, "invalid_reference"),
        ({"reason": "bot_http_400"}, "bot_4xx"),
        ({"reason": "bot_invalid_configuration"}, "config_failure"),
        ({"gateway_verified": False}, "not_verified"),
        ({"review_resolved": True}, "resolved"),
        ({"claimed": True}, "active_claim"),
    ],
)
def test_ineligible_rows_are_never_selected(pg_session_factory, kwargs, label):
    pid = insert_payment(pg_session_factory, order_id=f"inelig-{label}", **kwargs)
    result = requeue(pg_session_factory)
    assert result.requeued_count == 0
    row = get_payment(pg_session_factory, pid)
    assert row.status == kwargs.get("status", PaymentStatus.MANUAL_REVIEW.value)
    assert event_count(pg_session_factory, "admin_bulk_resend_requested", pid) == 0
    # A non-verified row keeps its NULL verification fact.
    if kwargs.get("gateway_verified") is False:
        assert row.gateway_verified_at is None


# --- financial + gateway-fact preservation (7-16, 29) ------------------------


def test_requeue_preserves_all_financial_and_gateway_facts(pg_session_factory):
    pid = insert_payment(
        pg_session_factory,
        order_id="preserve",
        amount=250000,
        fee_rate_bps=500,
        fee_amount=12500,
        reference_id="REF-keep-me",
        attempts=6,
        manual_review_at=T0 - timedelta(days=2),
    )
    before = get_payment(pg_session_factory, pid)
    snapshot = (
        before.amount,
        before.fee_rate_bps,
        before.fee_amount,
        before.payable_amount,
        before.reference_id,
        before.gateway_verified_at,
        before.bot_notify_attempts,
        before.manual_review_at,
    )
    requeue(pg_session_factory)
    row = get_payment(pg_session_factory, pid)
    # 7-12: financial + reference + verification facts unchanged.
    assert row.amount == snapshot[0] == 250000
    assert row.fee_rate_bps == snapshot[1] == 500
    assert row.fee_amount == snapshot[2] == 12500
    assert row.payable_amount == snapshot[3] == 262500
    assert row.reference_id == snapshot[4] == "REF-keep-me"
    assert row.gateway_verified_at == snapshot[5]
    # 13: attempt counter NOT reset.
    assert row.bot_notify_attempts == snapshot[6] == 6
    # 14: next_retry_at set to now.
    assert row.next_retry_at == T0
    # 15/16: claim cleared / stays NULL.
    assert row.notification_claimed_at is None
    assert row.notification_claimed_by is None
    # 29: manual_review_at preserved.
    assert row.manual_review_at == snapshot[7]
    # status moved to pending for the worker.
    assert row.status == PaymentStatus.BOT_NOTIFY_PENDING.value


# --- worker hand-off (17-20) -------------------------------------------------


def _run_worker(pg_session_factory, idem_settings, *, status_code, worker_id="w1", now=T0):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    notifier = BotNotifier(
        url=idem_settings.bot_payment_notify_url,
        token=idem_settings.bot_notify_token,
        connect_timeout_seconds=2.0,
        read_timeout_seconds=2.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pg_session_factory() as db:
            return run_worker_pass(
                db, notifier, idem_settings, worker_id=worker_id, now_fn=lambda: now
            )
    finally:
        notifier.close()


def test_worker_claims_requeued_payment_with_next_attempt_number(pg_session_factory):
    pid = insert_payment(pg_session_factory, order_id="handoff", attempts=6)
    requeue(pg_session_factory)
    with pg_session_factory() as db:
        claimed = claim_next_due(db, worker_id="w-claim", now=T0)
    assert claimed is not None
    assert claimed.payment_id == pid
    # 18: the worker creates the NEXT attempt number (6 -> 7).
    assert claimed.attempt == 7


def test_requeued_2xx_moves_to_accepted(pg_session_factory, idem_settings):
    pid = insert_payment(pg_session_factory, order_id="accept", attempts=6)
    requeue(pg_session_factory)
    _run_worker(pg_session_factory, idem_settings, status_code=200)
    row = get_payment(pg_session_factory, pid)
    assert row.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert row.bot_notify_attempts == 7


def test_requeued_retryable_failure_returns_to_manual_review(pg_session_factory, idem_settings):
    # attempts=6 with max_attempts=6: the post-resend attempt (7) hits the
    # limit, so a retryable failure returns to manual_review — never an
    # unbounded automatic retry.
    assert idem_settings.bot_notify_max_attempts == 6
    pid = insert_payment(pg_session_factory, order_id="requeue-fail", attempts=6)
    requeue(pg_session_factory)
    _run_worker(pg_session_factory, idem_settings, status_code=500)
    row = get_payment(pg_session_factory, pid)
    assert row.status == PaymentStatus.MANUAL_REVIEW.value
    assert row.bot_notify_reason == "retry_limit_reached"


# --- concurrency: real FOR UPDATE SKIP LOCKED (21-23) ------------------------


def test_concurrent_executions_requeue_each_row_once(pg_session_factory):
    pids = [insert_payment(pg_session_factory, order_id=f"conc-{i}") for i in range(12)]
    barrier = threading.Barrier(2)
    results: dict[int, BulkResendResult] = {}

    def worker(n: int):
        barrier.wait()
        with pg_session_factory() as db:
            results[n] = requeue_failed_deliveries(
                db, telegram_user_id=1000 + n, now=T0, batch_size=3
            )

    threads = [threading.Thread(target=worker, args=(n,)) for n in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 21: every row requeued exactly once; the two executions partitioned the set.
    total_requeued = sum(r.requeued_count for r in results.values())
    assert total_requeued == 12
    for pid in pids:
        row = get_payment(pg_session_factory, pid)
        assert row.status == PaymentStatus.BOT_NOTIFY_PENDING.value
        # 22/23: exactly one per-payment event per row (never duplicated).
        assert event_count(pg_session_factory, "admin_bulk_resend_requested", pid) == 1
    # No row is requeued by both executions (skip-locked partitioning).
    assert all(r.requeued_count >= 0 for r in results.values())
    assert results[0].requeued_count + results[1].requeued_count == 12


# --- audit events (23, 24, 27) -----------------------------------------------


def test_single_execution_records_events_exactly_once(pg_session_factory):
    pids = [insert_payment(pg_session_factory, order_id=f"aud-{i}") for i in range(3)]
    result = requeue(pg_session_factory)
    assert result.requeued_count == 3
    for pid in pids:
        assert event_count(pg_session_factory, "admin_bulk_resend_requested", pid) == 1
    # exactly one batch event with correct counts.
    assert event_count(pg_session_factory, "admin_bulk_resend_completed") == 1
    with pg_session_factory() as db:
        batch = db.execute(
            select(PaymentEvent).where(PaymentEvent.event_type == "admin_bulk_resend_completed")
        ).scalar_one()
    assert batch.payment_id is None
    assert batch.data["selected_count"] == 3
    assert batch.data["requeued_count"] == 3
    assert batch.data["skipped_count"] == 0
    assert batch.data["telegram_user_id"] == TEST_ADMIN_ID
    # per-payment event carries the safe fields only.
    with pg_session_factory() as db:
        ev = db.execute(
            select(PaymentEvent)
            .where(PaymentEvent.event_type == "admin_bulk_resend_requested")
            .limit(1)
        ).scalar_one()
    assert set(ev.data) == {
        "telegram_user_id",
        "previous_reason",
        "previous_attempts",
        "command",
    }
    assert ev.data["command"] == "resend_failed"
    assert ev.data["previous_attempts"] == 6


def test_zero_eligible_returns_successful_zero_result(pg_session_factory):
    result = requeue(pg_session_factory)
    assert result.requeued_count == 0
    assert result.selected_count == 0
    assert result.skipped_count == 0
    assert event_count(pg_session_factory, "admin_bulk_resend_completed") == 1


# --- audit history preserved (30) --------------------------------------------


def test_existing_audit_history_is_preserved(pg_session_factory):
    pid = insert_payment(pg_session_factory, order_id="history")
    with pg_session_factory() as db:
        db.add(PaymentEvent(payment_id=pid, event_type="gateway_payment_verified", data={}))
        db.add(PaymentEvent(payment_id=pid, event_type="manual_review_required", data={}))
        db.commit()
    requeue(pg_session_factory)
    with pg_session_factory() as db:
        types = set(
            db.execute(
                select(PaymentEvent.event_type).where(PaymentEvent.payment_id == pid)
            ).scalars()
        )
    assert {
        "gateway_payment_verified",
        "manual_review_required",
        "admin_bulk_resend_requested",
    } <= types


# --- safe mode: zero mutations via the handler (28) --------------------------


def test_safe_mode_performs_zero_mutations(pg_session_factory, settings):
    safe_settings = settings.model_copy(update={"bot_notify_retry_mode": "safe"})
    pid = insert_payment(pg_session_factory, order_id="safe-mode")
    handlers = CommandHandlers(
        pg_session_factory,
        safe_settings,
        (TEST_ADMIN_ID,),
        api_probe=lambda: {"live": True, "ready": True},
    )
    ctx = UpdateContext(user_id=TEST_ADMIN_ID, chat_id=TEST_ADMIN_ID, chat_type="private")
    [reply] = handlers.handle(ctx, "resend_failed", ["confirm"])
    assert reply == BULK_RESEND_SAFE_MODE_MESSAGE
    row = get_payment(pg_session_factory, pid)
    assert row.status == PaymentStatus.MANUAL_REVIEW.value
    assert event_count(pg_session_factory, "admin_bulk_resend_requested") == 0
    assert event_count(pg_session_factory, "admin_bulk_resend_completed") == 0


# --- admin command performs no external HTTP (26) ----------------------------


def test_bulk_resend_service_has_no_http_client():
    import app.services.bulk_resend as module

    assert "httpx" not in Path(module.__file__).read_text()


# --- secret policy: admin-bot never receives BOT_NOTIFY_TOKEN (25) -----------


def test_admin_bot_service_does_not_receive_bot_notify_token():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["admin-bot"].get("environment", {})
    # An explicit empty override masks the shared env_file value.
    assert env.get("BOT_NOTIFY_TOKEN") == ""


# --- preview parity on real PostgreSQL ---------------------------------------


def test_preview_counts_and_amounts_on_postgres(pg_session_factory):
    insert_payment(pg_session_factory, order_id="pv-1", amount=250000)
    insert_payment(
        pg_session_factory, order_id="pv-2", amount=350000, reason="bot_timeout_ambiguous"
    )
    insert_payment(pg_session_factory, order_id="pv-ineligible", reason="verify_user_id_mismatch")
    with pg_session_factory() as db:
        preview = preview_bulk_resend(db)
    assert preview.count == 2
    assert preview.total_amount == 600000
    assert set(preview.order_ids) == {"pv-1", "pv-2"}
    # A preview never mutates.
    assert event_count(pg_session_factory, "admin_bulk_resend_requested") == 0
