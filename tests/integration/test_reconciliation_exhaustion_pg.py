"""PostgreSQL integration coverage for the bounded reconciliation-exhaustion
monitor signal (app.services.monitor_checks.check_reconciliation).

Production incident: `reconciliation_exhausted_ever_conditions` had no time
bound at all, so a historical backlog of payments that stopped retrying
months ago and have long since aged out kept the monitor permanently
CRITICAL even though the live system was healthy. The fix adds a bounded
"recent" population (`reconciliation_recently_exhausted_conditions`,
anchored on `reconciliation_last_at`) that the monitor actually alarms on,
while the unbounded historical total is kept only for operator context.

Only real PostgreSQL proves:

* real timezone-aware timestamp comparisons at exact second boundaries
  (SQLite's datetime handling is looser and can hide an off-by-one);
* the query plan the reconciliation-due index
  (`ix_payments_reconciliation_due`) actually produces at a realistic
  table size, never a full sequential scan of `payments`.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, Payment, PaymentStatus
from app.services import monitor_checks
from app.services.reconciliation import (
    reconciliation_actionable_exhausted_conditions,
    reconciliation_recently_exhausted_conditions,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

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


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


def _make_payment(session_factory, *, n: int, **kwargs) -> None:
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id=f"pgrec-{n}",
                gateway_order_id=800_000 + n,
                gateway_user_id=1000 + n,
                amount=10000,
                fee_rate_bps=0,
                fee_amount=0,
                payable_amount=10000,
                status=PaymentStatus.LINK_CREATED.value,
                **kwargs,
            )
        )
        db.commit()


def test_ancient_historical_exhaustion_recovers_under_real_postgres(
    pg_session_factory, settings
):
    """The exact production shape: an old, aged-out, long-untouched
    exhausted payment must not keep the check CRITICAL under real
    PostgreSQL timezone-aware timestamp semantics."""
    now = datetime.now(UTC)
    recon = settings.model_copy(update={"reconciliation_max_attempts": 3})
    ancient = now - timedelta(days=30)
    _make_payment(
        pg_session_factory,
        n=1,
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=ancient,
        callback_token_issued_at=ancient,
    )
    with pg_session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "ok"
    assert result.details["exhausted_recent"] == 0
    assert result.details["exhausted_historical_total"] == 1


def test_exact_recent_window_boundary_under_real_postgres(pg_session_factory, settings):
    """Two rows straddling the exact window boundary (aged out either way,
    so only the recency bound distinguishes them) must be classified
    correctly by real PostgreSQL's timestamp comparison -- no off-by-one
    from timezone or precision handling."""
    now = datetime.now(UTC)
    window = 3600
    recon = settings.model_copy(
        update={
            "reconciliation_max_attempts": 3,
            "monitor_reconciliation_exhausted_recent_window_seconds": window,
        }
    )
    aged_out_at = now - timedelta(seconds=recon.reconciliation_max_age_seconds + 100)
    _make_payment(
        pg_session_factory,
        n=1,
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=now - timedelta(seconds=window - 1),  # just inside
        callback_token_issued_at=aged_out_at,
    )
    _make_payment(
        pg_session_factory,
        n=2,
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=now - timedelta(seconds=window + 1),  # just outside
        callback_token_issued_at=aged_out_at,
    )
    with pg_session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.details["exhausted_recent"] == 1
    assert result.details["exhausted_historical_total"] == 2


def test_recovery_transition_after_window_elapses_under_real_postgres(
    pg_session_factory, settings
):
    """The SAME payment, evaluated at two different `now` values: critical
    right after exhaustion, then recovered once the recent window has
    fully elapsed and nothing else is due -- proving the signal is a
    genuine time-bounded transition, not a one-shot classification."""
    exhausted_at = datetime.now(UTC)
    window = 1800
    recon = settings.model_copy(
        update={
            "reconciliation_max_attempts": 3,
            "monitor_reconciliation_exhausted_recent_window_seconds": window,
        }
    )
    aged_out_at = exhausted_at - timedelta(seconds=recon.reconciliation_max_age_seconds + 100)
    _make_payment(
        pg_session_factory,
        n=1,
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=exhausted_at,
        callback_token_issued_at=aged_out_at,
    )
    with pg_session_factory() as db:
        just_after = monitor_checks.check_reconciliation(
            db, recon, now=exhausted_at + timedelta(seconds=1)
        )
    assert just_after.status == "critical"

    with pg_session_factory() as db:
        long_after = monitor_checks.check_reconciliation(
            db, recon, now=exhausted_at + timedelta(seconds=window + 1)
        )
    assert long_after.status == "ok"
    assert long_after.details["exhausted_historical_total"] == 1


def test_disjoint_actionable_populations_counted_correctly_under_real_postgres(
    pg_session_factory, settings
):
    """Two DISTINCT rows, one satisfying only exhausted_not_aged_out and one
    satisfying only exhausted_recent (neither population contains the
    other), must both count toward exhausted_actionable_total under real
    PostgreSQL timestamp semantics -- a naive max(exhausted_not_aged_out,
    exhausted_recent) would report 1 instead of the real 2."""
    now = datetime.now(UTC)
    window = 3600
    recon = settings.model_copy(
        update={
            "reconciliation_max_attempts": 3,
            "monitor_reconciliation_exhausted_recent_window_seconds": window,
        }
    )
    # Row A: still within the reconciliation lifetime (not aged out), last
    # attempt well outside the recent window.
    _make_payment(
        pg_session_factory,
        n=1,
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=now - timedelta(seconds=window * 2),
        callback_token_issued_at=now - timedelta(seconds=100),
    )
    # Row B: already aged out, last attempt well inside the recent window.
    _make_payment(
        pg_session_factory,
        n=2,
        reconciliation_attempts=3,
        reconciliation_next_at=None,
        reconciliation_last_at=now - timedelta(seconds=100),
        callback_token_issued_at=(
            now - timedelta(seconds=recon.reconciliation_max_age_seconds + 100)
        ),
    )
    with pg_session_factory() as db:
        result = monitor_checks.check_reconciliation(db, recon, now=now)
    assert result.status == "critical"
    assert result.details["exhausted_not_aged_out"] == 1
    assert result.details["exhausted_recent"] == 1
    assert result.details["exhausted_actionable_total"] == 2


def test_actionable_exhausted_query_uses_an_index_not_a_full_table_scan(
    pg_session_factory, settings
):
    """Same performance guarantee as the recently-exhausted query, for the
    new union query that computes exhausted_actionable_total: planned via
    an index, never a sequential scan of the full `payments` table, at a
    realistic table size."""
    now = datetime.now(UTC)
    with pg_session_factory() as db:
        db.execute(
            text(
                """
                INSERT INTO payments (
                    bot_order_id, gateway_order_id, gateway_user_id,
                    amount, fee_rate_bps, fee_amount, payable_amount,
                    status, reconciliation_attempts, reconciliation_next_at,
                    reconciliation_last_at, gateway_verified_at
                )
                SELECT
                    'pg-bulk-verified2-' || gs,
                    720000 + gs,
                    1,
                    10000, 0, 0, 10000,
                    'verified',
                    0, NULL, NULL,
                    :now
                FROM generate_series(1, 20000) AS gs
                """
            ),
            {"now": now},
        )
        db.execute(
            text(
                """
                INSERT INTO payments (
                    bot_order_id, gateway_order_id, gateway_user_id,
                    amount, fee_rate_bps, fee_amount, payable_amount,
                    status, reconciliation_attempts, reconciliation_next_at,
                    reconciliation_last_at, callback_token_issued_at
                )
                SELECT
                    'pg-bulk-exhausted2-' || gs,
                    770000 + gs,
                    1,
                    10000, 0, 0, 10000,
                    'link_created',
                    1000, NULL,
                    :ancient,
                    :ancient
                FROM generate_series(1, 200) AS gs
                """
            ),
            {"ancient": now - timedelta(days=60)},
        )
        db.commit()
        db.execute(text("ANALYZE payments"))
        db.commit()

    recon = settings.model_copy(update={"reconciliation_max_attempts": 1000})
    conditions = reconciliation_actionable_exhausted_conditions(
        recon,
        now=now,
        window_seconds=recon.monitor_reconciliation_exhausted_recent_window_seconds,
    )
    query = select(func.count(Payment.id)).where(*conditions)
    with pg_session_factory() as db:
        compiled = query.compile(
            dialect=db.bind.dialect, compile_kwargs={"literal_binds": True}
        )
        plan_rows = db.execute(text(f"EXPLAIN {compiled}")).fetchall()
    plan_text = "\n".join(row[0] for row in plan_rows)
    assert "Index Scan using ix_payments" in plan_text, plan_text
    assert "Seq Scan on payments" not in plan_text, plan_text


def test_recently_exhausted_query_uses_an_index_not_a_full_table_scan(pg_session_factory, settings):
    """At a realistic table size, the recently-exhausted count query must
    be planned via an index on `status` (either the plain column index or
    the composite `ix_payments_reconciliation_due`, whichever the planner's
    statistics prefer) -- never a sequential scan of the full `payments`
    table. No new index is introduced for this feature; this proves the
    existing ones are sufficient."""
    now = datetime.now(UTC)
    with pg_session_factory() as db:
        # Bulk-seed a realistic mix: mostly settled/unrelated rows (status
        # != link_created, so excluded by the index's leading column) plus a
        # modest population of actual link_created/exhausted rows -- large
        # enough for the planner's statistics to prefer the index over a
        # full scan.
        db.execute(
            text(
                """
                INSERT INTO payments (
                    bot_order_id, gateway_order_id, gateway_user_id,
                    amount, fee_rate_bps, fee_amount, payable_amount,
                    status, reconciliation_attempts, reconciliation_next_at,
                    reconciliation_last_at, gateway_verified_at
                )
                SELECT
                    'pg-bulk-verified-' || gs,
                    700000 + gs,
                    1,
                    10000, 0, 0, 10000,
                    'verified',
                    0, NULL, NULL,
                    :now
                FROM generate_series(1, 20000) AS gs
                """
            ),
            {"now": now},
        )
        db.execute(
            text(
                """
                INSERT INTO payments (
                    bot_order_id, gateway_order_id, gateway_user_id,
                    amount, fee_rate_bps, fee_amount, payable_amount,
                    status, reconciliation_attempts, reconciliation_next_at,
                    reconciliation_last_at, callback_token_issued_at
                )
                SELECT
                    'pg-bulk-exhausted-' || gs,
                    750000 + gs,
                    1,
                    10000, 0, 0, 10000,
                    'link_created',
                    1000, NULL,
                    :ancient,
                    :ancient
                FROM generate_series(1, 200) AS gs
                """
            ),
            {"ancient": now - timedelta(days=60)},
        )
        db.commit()
        db.execute(text("ANALYZE payments"))
        db.commit()

    recon = settings.model_copy(update={"reconciliation_max_attempts": 1000})
    conditions = reconciliation_recently_exhausted_conditions(
        recon,
        now=now,
        window_seconds=recon.monitor_reconciliation_exhausted_recent_window_seconds,
    )
    query = select(func.count(Payment.id)).where(*conditions)
    with pg_session_factory() as db:
        compiled = query.compile(
            dialect=db.bind.dialect, compile_kwargs={"literal_binds": True}
        )
        plan_rows = db.execute(text(f"EXPLAIN {compiled}")).fetchall()
    plan_text = "\n".join(row[0] for row in plan_rows)
    assert "Index Scan using ix_payments" in plan_text, plan_text
    assert "Seq Scan on payments" not in plan_text, plan_text
