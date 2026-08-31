"""Attention resolution under real PostgreSQL: locking, races, migration 0013.

Proves what SQLite cannot (SQLite has no real `SELECT ... FOR UPDATE` and a
single writer):

* two operators resolving the SAME payment concurrently: exactly one wins,
  exactly one audit event, and the loser sees ALREADY_RESOLVED — the first
  actor/time/reason/note is never overwritten;
* a payment that becomes financially meaningful WHILE an operator is mid
  resolution is refused by the under-lock re-check, not resolved from the
  stale pre-lock read (the SQLAlchemy identity-map staleness case AGENTS.md
  calls out);
* the all-or-nothing bulk review path really is atomic across a batch;
* migration 0013 from the deployed production revision (0012): forward-safe,
  idempotent, preserves every historical row, invents no financial fact, and
  its downgrade is non-destructive by default;
* the `ck_payments_attention_resolution_consistent` CHECK is real in
  PostgreSQL — a half-populated resolution is impossible.

Run only when TEST_DATABASE_URL points at a disposable PostgreSQL database.
"""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Base, Payment, PaymentEvent, PaymentStatus
from app.services import attention, review_resolution
from tests.alembic_head import ALEMBIC_HEAD

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

_TABLES = (
    "monitor_incidents",
    "admin_alerts",
    "worker_heartbeats",
    "payment_events",
    "payments",
    "fee_policies",
    "centralpay_payer_identities",
    "alembic_version",
)

_ATTENTION_COLUMNS = {
    "attention_resolved_at",
    "attention_resolution",
    "attention_resolved_by",
    "attention_resolution_note",
}


def _drop_all(engine) -> None:
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    _drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def migrated_engine():
    """A database built by the REAL Alembic chain, not `create_all` — the only
    way to exercise migration 0013 as production will run it."""
    engine = create_engine(TEST_DATABASE_URL)
    _drop_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


def _alembic(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout + result.stderr


def _alembic_version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _column_names(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _make_payment(
    pg_session_factory,
    *,
    order_id: str,
    gateway_order_id: int,
    status: str = PaymentStatus.GETLINK_FAILED.value,
    amount: int = 230000,
    **fields,
) -> int:
    with pg_session_factory() as db:
        payment = Payment(
            bot_order_id=order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=55501234,
            amount=amount,
            payable_amount=amount,
            status=status,
            # Set exactly as the real create path does: the signed return URL
            # is hashed BEFORE getLink, so a getlink_failed row has one.
            callback_token_hash="b" * 64,
            callback_token_issued_at=datetime.now(UTC) - timedelta(days=30),
            created_at=datetime.now(UTC) - timedelta(days=30),
            **fields,
        )
        db.add(payment)
        db.commit()
        return payment.id


# --- concurrency ----------------------------------------------------------


def test_two_operators_resolving_the_same_payment_race_safely(pg_session_factory):
    """The row lock is the serialization point. Exactly one resolution wins;
    every loser is told ALREADY_RESOLVED and the winner's record is intact."""
    payment_id = _make_payment(
        pg_session_factory, order_id="race-1", gateway_order_id=910000001001
    )
    barrier = threading.Barrier(8)

    def worker(index: int):
        barrier.wait()
        with pg_session_factory() as db:
            return attention.resolve_attention(
                db,
                payment_id=payment_id,
                resolution="stale_getlink_failure",
                note=f"operator-{index}",
                actor=f"host-cli-{index}",
                now=datetime.now(UTC),
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(worker, range(8)))

    winners = [o for o in outcomes if o.resolved]
    losers = [o for o in outcomes if not o.resolved]
    assert len(winners) == 1
    assert len(losers) == 7
    assert all(o.refusal is attention.AttentionRefusal.ALREADY_RESOLVED for o in losers)

    with pg_session_factory() as db:
        payment = db.get(Payment, payment_id)
        # Exactly one durable record, and it is a real operator's.
        assert payment.attention_resolution == "stale_getlink_failure"
        assert payment.attention_resolution_note.startswith("operator-")
        assert payment.attention_resolved_by.startswith("host-cli-")
        # Exactly one audit event — no duplicate operator action recorded.
        assert (
            db.execute(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.payment_id == payment_id,
                    PaymentEvent.event_type == "payment_attention_resolved",
                )
            ).scalar_one()
            == 1
        )
        # Every loser saw the SAME winning record, not its own.
        assert {o.existing_resolved_by for o in losers} == {payment.attention_resolved_by}


def test_a_payment_verified_under_the_lock_is_refused_not_resolved(pg_session_factory):
    """Identity-map staleness + a real race: an operator reads an eligible
    payment, another transaction verifies it, and only THEN does the operator
    take the lock. `populate_existing=True` forces the re-read, so the guard
    sees the verified row and refuses."""
    payment_id = _make_payment(
        pg_session_factory, order_id="race-2", gateway_order_id=910000001002
    )

    operator = pg_session_factory()
    # Warm the operator session's identity map with the PRE-verification row,
    # exactly as a preview (`attention show`) would.
    stale = operator.get(Payment, payment_id)
    assert stale.gateway_verified_at is None
    operator.rollback()

    # A concurrent settlement commits in between.
    with pg_session_factory() as other:
        row = other.get(Payment, payment_id)
        row.gateway_verified_at = datetime.now(UTC)
        row.reference_id = "REF-RACE-2"
        other.commit()

    try:
        outcome = attention.resolve_attention(
            operator,
            payment_id=payment_id,
            resolution="stale_getlink_failure",
            note="stale read",
            actor="host-cli",
            now=datetime.now(UTC),
        )
    finally:
        operator.close()

    assert outcome.resolved is False
    assert outcome.refusal is attention.AttentionRefusal.GATEWAY_VERIFIED

    with pg_session_factory() as db:
        payment = db.get(Payment, payment_id)
        assert payment.attention_resolved_at is None
        assert payment.gateway_verified_at is not None
        assert payment.reference_id == "REF-RACE-2"


def test_bulk_review_resolution_is_atomic_across_the_batch(pg_session_factory):
    """All-or-nothing: one ineligible row in the middle of the batch leaves
    EVERY row unresolved, not a partially applied batch."""
    ids = [
        _make_payment(
            pg_session_factory,
            order_id=f"bulk-{i}",
            gateway_order_id=910000002000 + i,
            status=PaymentStatus.MANUAL_REVIEW.value,
            gateway_verified_at=datetime.now(UTC),
            reference_id=f"REF-BULK-{i}",
            manual_review_at=datetime.now(UTC),
            bot_notify_reason="retry_limit_reached",
            bot_notify_attempts=5,
        )
        for i in range(5)
    ]
    # Row 2 was already resolved by someone else.
    with pg_session_factory() as db:
        db.get(Payment, ids[2]).review_resolved_at = datetime.now(UTC)
        db.get(Payment, ids[2]).review_resolution = "confirmed_by_bot_operator"
        db.commit()

    with pg_session_factory() as db:
        result = review_resolution.resolve_reviews(
            db,
            order_ids=[f"bulk-{i}" for i in range(5)],
            resolution="confirmed_by_bot_operator",
            note="operator confirmed with the bot",
            actor="host-cli",
            now=datetime.now(UTC),
        )
    assert result.resolved is False
    assert result.resolved_count == 0
    blocked = {row.order_id: row.refusal for row in result.report.blocked_rows}
    assert blocked == {"bulk-2": review_resolution.BulkReviewRefusal.ALREADY_RESOLVED}

    with pg_session_factory() as db:
        # The four eligible rows are untouched, and no event was written.
        for i in (0, 1, 3, 4):
            assert db.get(Payment, ids[i]).review_resolved_at is None
        assert (
            db.execute(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.event_type == "manual_review_bulk_resolved"
                )
            ).scalar_one()
            == 0
        )


def test_bulk_review_resolution_commits_the_whole_eligible_batch(pg_session_factory):
    ids = [
        _make_payment(
            pg_session_factory,
            order_id=f"bulk-ok-{i}",
            gateway_order_id=910000003000 + i,
            status=PaymentStatus.MANUAL_REVIEW.value,
            gateway_verified_at=datetime.now(UTC),
            reference_id=f"REF-BULKOK-{i}",
            manual_review_at=datetime.now(UTC),
            bot_notify_reason="retry_limit_reached",
            bot_notify_attempts=5,
        )
        for i in range(15)  # exactly the production batch size
    ]

    with pg_session_factory() as db:
        result = review_resolution.resolve_reviews(
            db,
            order_ids=[f"bulk-ok-{i}" for i in range(15)],
            resolution="confirmed_by_bot_operator",
            note="bot operator confirmed all 15 were credited",
            actor="host-cli",
            now=datetime.now(UTC),
        )
    assert result.resolved is True
    assert result.resolved_count == 15

    with pg_session_factory() as db:
        for payment_id in ids:
            payment = db.get(Payment, payment_id)
            assert payment.review_resolved_at is not None
            assert payment.review_resolution == "confirmed_by_bot_operator"
            # Status stays as permanent history; financial facts untouched.
            assert payment.status == PaymentStatus.MANUAL_REVIEW.value
            assert payment.gateway_verified_at is not None
            assert payment.amount == 230000
        # One event per row plus one batch event.
        assert (
            db.execute(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.event_type == "manual_review_resolved"
                )
            ).scalar_one()
            == 15
        )
        assert (
            db.execute(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.event_type == "manual_review_bulk_resolved"
                )
            ).scalar_one()
            == 1
        )


def test_concurrent_bulk_batches_never_double_resolve(pg_session_factory):
    """Two operators submit overlapping batches at the same instant. Plain
    `FOR UPDATE` (never SKIP LOCKED) makes the second block, then re-check and
    refuse — never silently skip a row out of an all-or-nothing batch, and
    never write two resolutions for one payment."""
    for i in range(6):
        _make_payment(
            pg_session_factory,
            order_id=f"bulk-race-{i}",
            gateway_order_id=910000004000 + i,
            status=PaymentStatus.MANUAL_REVIEW.value,
            gateway_verified_at=datetime.now(UTC),
            reference_id=f"REF-BULKRACE-{i}",
            manual_review_at=datetime.now(UTC),
            bot_notify_reason="retry_limit_reached",
            bot_notify_attempts=5,
        )
    batches = [
        [f"bulk-race-{i}" for i in range(4)],  # 0,1,2,3
        [f"bulk-race-{i}" for i in range(2, 6)],  # 2,3,4,5 — overlaps
    ]
    barrier = threading.Barrier(2)

    def worker(batch):
        barrier.wait()
        with pg_session_factory() as db:
            return review_resolution.resolve_reviews(
                db,
                order_ids=batch,
                resolution="confirmed_by_bot_operator",
                note="concurrent",
                actor="host-cli",
                now=datetime.now(UTC),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, batches))

    # At most one batch can succeed: they share rows, and the loser's shared
    # rows are ALREADY_RESOLVED by the time it re-checks under the lock.
    assert sum(1 for r in results if r.resolved) <= 1

    with pg_session_factory() as db:
        for i in range(6):
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == f"bulk-race-{i}")
            ).scalar_one()
            events = db.execute(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.payment_id == payment.id,
                    PaymentEvent.event_type == "manual_review_resolved",
                )
            ).scalar_one()
            # Never two resolutions for one payment.
            assert events <= 1


# --- database constraint --------------------------------------------------


def test_a_half_populated_resolution_is_rejected_by_postgresql(pg_session_factory):
    """`ck_payments_attention_resolution_consistent`: a row can never claim to
    be resolved without recording by whom, when, on what grounds, and why."""
    payment_id = _make_payment(
        pg_session_factory, order_id="ck-1", gateway_order_id=910000005001
    )
    with pytest.raises(IntegrityError), pg_session_factory() as db:
        payment = db.get(Payment, payment_id)
        payment.attention_resolved_at = datetime.now(UTC)
        payment.attention_resolution = "stale_getlink_failure"
        # actor and note deliberately omitted
        db.commit()


def test_a_verified_payment_may_still_carry_an_attention_resolution_row_shape(
    pg_session_factory,
):
    """The database must NOT forbid `attention_resolved_at` alongside
    `gateway_verified_at`.

    `app.services.attention` refuses to CREATE that combination, but a
    resolved payment can legitimately be settled afterwards by a late browser
    callback (`process_callback` does not gate on `link_created`). A
    constraint here would turn that settlement into an IntegrityError and fail
    a real customer payment. This test is the guard against re-adding one.
    """
    payment_id = _make_payment(
        pg_session_factory, order_id="inert-1", gateway_order_id=910000005002
    )
    with pg_session_factory() as db:
        attention.resolve_attention(
            db,
            payment_id=payment_id,
            resolution="stale_getlink_failure",
            note="closed",
            actor="host-cli",
            now=datetime.now(UTC),
        )
    # The settlement path's write must succeed against the real schema.
    with pg_session_factory() as db:
        payment = db.get(Payment, payment_id)
        payment.gateway_verified_at = datetime.now(UTC)
        payment.reference_id = "REF-INERT-1"
        payment.status = PaymentStatus.BOT_NOTIFY_PENDING.value
        db.commit()

    with pg_session_factory() as db:
        payment = db.get(Payment, payment_id)
        assert payment.gateway_verified_at is not None
        assert payment.attention_resolution == "stale_getlink_failure"


# --- migration 0013 -------------------------------------------------------


def test_migration_0013_from_production_0012(migrated_engine):
    """From the deployed production revision (0012): `alembic upgrade head`
    runs 0013; existing rows are preserved with NULL (== not resolved) and no
    financial fact is invented; re-upgrade is idempotent; the default
    downgrade is non-destructive."""
    _alembic("upgrade", "0012")
    assert _alembic_version(migrated_engine) == "0012"
    assert _ATTENTION_COLUMNS.isdisjoint(_column_names(migrated_engine, "payments"))

    # A production-shaped stale getlink_failed row written by the 0012 system.
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO payments"
                " (bot_order_id, gateway_order_id, gateway_user_id, amount,"
                "  fee_rate_bps, fee_amount, payable_amount, status,"
                "  callback_token_hash, created_at)"
                " VALUES ('12ca60ac8c', 536747157809, 55501234, 230000,"
                "  0, 0, 230000, 'getlink_failed', :h, :c)"
            ),
            {"h": "c" * 64, "c": datetime(2026, 8, 1, tzinfo=UTC)},
        )

    _alembic("upgrade", "head")
    assert _alembic_version(migrated_engine) == ALEMBIC_HEAD
    assert _column_names(migrated_engine, "payments") >= _ATTENTION_COLUMNS
    assert "ix_payments_attention_unresolved" in {
        i["name"] for i in sa.inspect(migrated_engine).get_indexes("payments")
    }

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT amount, payable_amount, fee_amount, status,"
                " gateway_verified_at, reference_id, attention_resolved_at,"
                " attention_resolution, attention_resolved_by,"
                " attention_resolution_note"
                " FROM payments WHERE bot_order_id = '12ca60ac8c'"
            )
        ).one()
    # Every financial fact preserved verbatim...
    assert row.amount == 230000
    assert row.payable_amount == 230000
    assert row.fee_amount == 0
    assert row.status == "getlink_failed"
    assert row.gateway_verified_at is None
    assert row.reference_id is None
    # ...and NOTHING invented: NULL means exactly "not resolved".
    assert row.attention_resolved_at is None
    assert row.attention_resolution is None
    assert row.attention_resolved_by is None
    assert row.attention_resolution_note is None

    # Recovery safety: re-running the upgrade over the existing schema no-ops.
    _alembic("stamp", "0012")
    _alembic("upgrade", "head")
    assert _alembic_version(migrated_engine) == ALEMBIC_HEAD
    assert _column_names(migrated_engine, "payments") >= _ATTENTION_COLUMNS


def test_migration_0013_downgrade_is_non_destructive_by_default(migrated_engine):
    """A code rollback must never force a schema downgrade, and must never
    silently destroy operator resolution history."""
    _alembic("upgrade", "head")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO payments"
                " (bot_order_id, gateway_order_id, gateway_user_id, amount,"
                "  fee_rate_bps, fee_amount, payable_amount, status, created_at,"
                "  attention_resolved_at, attention_resolution,"
                "  attention_resolved_by, attention_resolution_note)"
                " VALUES ('down-1', 536747157810, 55501234, 1000,"
                "  0, 0, 1000, 'getlink_failed', :c, :c,"
                "  'stale_getlink_failure', 'host-cli', 'closed')"
            ),
            {"c": datetime(2026, 8, 1, tzinfo=UTC)},
        )

    _alembic("downgrade", "0012")
    assert _alembic_version(migrated_engine) == "0012"
    # Columns and the recorded resolution both survive.
    assert _column_names(migrated_engine, "payments") >= _ATTENTION_COLUMNS
    with migrated_engine.connect() as conn:
        resolution = conn.execute(
            text("SELECT attention_resolution FROM payments WHERE bot_order_id = 'down-1'")
        ).scalar_one()
    assert resolution == "stale_getlink_failure"

    _alembic("upgrade", "head")
    assert _alembic_version(migrated_engine) == ALEMBIC_HEAD


def test_bulk_rolls_back_when_a_row_becomes_ineligible_before_the_lock(
    pg_session_factory, monkeypatch
):
    """Execution-time re-check under FOR UPDATE, on real PostgreSQL.

    `resolve_reviews` evaluates eligibility once to build its report and AGAIN
    against the freshly-locked rows. This proves the second evaluation is what
    actually decides: a row that is eligible when the report is built, and
    becomes a financial/verification review before the lock is taken, must
    reject the WHOLE batch and mutate nothing.

    The interleaving is made deterministic by wrapping `build_report` so that a
    SEPARATE session commits the change after the report is computed and before
    the locking statement runs — the same window a concurrent operator or
    worker would occupy.
    """
    ids = [
        _make_payment(
            pg_session_factory,
            order_id=f"race-elig-{index}",
            gateway_order_id=910000006000 + index,
            status=PaymentStatus.MANUAL_REVIEW.value,
            gateway_verified_at=datetime.now(UTC),
            reference_id=f"REF-RACEELIG-{index}",
            manual_review_at=datetime.now(UTC),
            bot_notify_reason="retry_limit_reached",
            bot_notify_attempts=5,
        )
        for index in range(3)
    ]

    real_build_report = review_resolution.build_report
    fired = {"done": False}

    def build_report_then_mutate(db, *, order_ids, resolution):
        report = real_build_report(db, order_ids=order_ids, resolution=resolution)
        if not fired["done"]:
            fired["done"] = True
            assert report.eligible, "the batch must start out fully eligible"
            # A concurrent transaction turns row 1 into a financial/verification
            # review (bot_notify_reason IS NULL) and commits.
            with pg_session_factory() as other:
                other.get(Payment, ids[1]).bot_notify_reason = None
                other.commit()
        return report

    monkeypatch.setattr(review_resolution, "build_report", build_report_then_mutate)

    with pg_session_factory() as db:
        result = review_resolution.resolve_reviews(
            db,
            order_ids=[f"race-elig-{index}" for index in range(3)],
            resolution="confirmed_by_bot_operator",
            note="operator confirmed",
            actor="host-cli",
            now=datetime.now(UTC),
        )

    assert result.resolved is False
    assert result.resolved_count == 0
    blocked = {row.order_id: row.refusal for row in result.report.blocked_rows}
    assert blocked == {
        "race-elig-1": (
            review_resolution.BulkReviewRefusal
            .FINANCIAL_REVIEW_REQUIRES_INDIVIDUAL_RESOLUTION
        )
    }

    with pg_session_factory() as db:
        # Every row — including the two that never became ineligible — is
        # untouched, and no event of either kind was written.
        for payment_id in ids:
            assert db.get(Payment, payment_id).review_resolved_at is None
        assert (
            db.execute(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.event_type.in_(
                        ("manual_review_resolved", "manual_review_bulk_resolved")
                    )
                )
            ).scalar_one()
            == 0
        )
