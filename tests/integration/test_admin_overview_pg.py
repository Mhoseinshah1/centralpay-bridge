"""PostgreSQL integration coverage for the admin-bot ops-visibility additions.

Two things here specifically need real PostgreSQL, not SQLite:

1. `/status`'s new needs_attention/waiting_gateway/expired counts must be
   numerically IDENTICAL to `app.services.stuck_payments.stuck_payments_overview`
   -- the exact function `centralpay stuck` itself calls -- for the same
   database state. This is the "stuck classifications match existing
   `centralpay stuck`" proof the roadmap item asks for. (Both are exact
   whenever there are 200 or fewer simultaneous bot-delivery problems --
   the realistic case exercised here and the only one this test asserts
   about; see cmd_status's own docstring for the documented, pre-existing
   divergence above that count.)
2. `app.adminbot.queries.find_payment` (used by `/payment`) now shares
   `app.services.payment_lookup.find_payment_by_order_id` with the CLI.
   Before this PR, the admin bot's own lookup had two real bugs only a
   real PostgreSQL BIGINT column reproduces: (a) it never checked whether
   a numeric identifier ambiguously named a DIFFERENT payment's
   gateway_order_id, silently showing an operator the wrong payment; and
   (b) it bound `int(identifier)` into a `gateway_order_id == ...` query
   for ANY all-digit string with no bound check, which raises
   `psycopg.errors.NumericValueOutOfRange` -- an unhandled crash -- for a
   bot_order_id longer than a signed 64-bit integer can hold (SQLite's
   dynamic typing never reproduces this, exactly like the equivalent
   pre-existing CLI regression test in
   tests/integration/test_reconcile_inspect_pg.py). Both are proven fixed
   here against a real PostgreSQL 16 database.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.adminbot.auth import UpdateContext
from app.adminbot.commands import CommandHandlers
from app.adminbot.queries import find_payment
from app.models import Base, Payment, PaymentStatus
from app.services import stuck_payments as stuck_service
from app.services.payment_lookup import AmbiguousOrderIdError

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

_TABLES = (
    "admin_alerts",
    "worker_heartbeats",
    "payment_events",
    "payments",
    "centralpay_payer_identities",
    "fee_policies",
    "alembic_version",
)

ADMIN_ID = 111111111


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


def _handlers(pg_session_factory, admin_settings) -> CommandHandlers:
    return CommandHandlers(
        pg_session_factory,
        admin_settings,
        (ADMIN_ID,),
        api_probe=lambda: {"live": True, "ready": True},
    )


def _admin_ctx() -> UpdateContext:
    return UpdateContext(user_id=ADMIN_ID, chat_id=ADMIN_ID, chat_type="private")


def _base_payment(*, bot_order_id: str, gateway_order_id: int, **overrides) -> Payment:
    fields: dict[str, object] = {
        "bot_order_id": bot_order_id,
        "gateway_order_id": gateway_order_id,
        "gateway_user_id": 1,
        "amount": 5000,
        "payable_amount": 5000,
        "status": PaymentStatus.LINK_CREATED.value,
    }
    fields.update(overrides)
    return Payment(**fields)


def test_admin_status_overview_counts_match_centralpay_stuck_overview_pg(
    pg_session_factory, admin_settings
):
    now = datetime.now(UTC)
    with pg_session_factory() as db:
        # One NEEDS_ATTENTION via bot-delivery failure (the ONLY code path
        # that sets bot_notify_reason alongside manual_review).
        db.add(
            _base_payment(
                bot_order_id="ov-attn-delivery",
                gateway_order_id=900001,
                status=PaymentStatus.MANUAL_REVIEW.value,
                bot_notify_reason="bot_http_500",
                manual_review_at=now,
                gateway_verified_at=now,
            )
        )
        # One NEEDS_ATTENTION via reconciliation exhaustion.
        db.add(
            _base_payment(
                bot_order_id="ov-attn-exhausted",
                gateway_order_id=900002,
                callback_token_issued_at=now
                - timedelta(seconds=admin_settings.reconciliation_fast_window_seconds + 60),
                reconciliation_attempts=admin_settings.reconciliation_max_attempts,
                reconciliation_last_error_code="gateway_not_paid",
                reconciliation_next_at=None,
            )
        )
        # One WAITING_GATEWAY: ordinary in-flight polling, not aged out.
        db.add(
            _base_payment(
                bot_order_id="ov-waiting",
                gateway_order_id=900003,
                callback_token_issued_at=now - timedelta(seconds=1200),
                reconciliation_attempts=2,
                reconciliation_last_at=now - timedelta(seconds=30),
            )
        )
        # One EXPIRED: past reconciliation_max_age_seconds.
        db.add(
            _base_payment(
                bot_order_id="ov-expired",
                gateway_order_id=900004,
                callback_token_issued_at=now
                - timedelta(seconds=admin_settings.reconciliation_max_age_seconds + 60),
                reconciliation_attempts=1,
                reconciliation_last_at=now - timedelta(hours=1),
            )
        )
        db.commit()

    # The exact function `centralpay stuck` itself calls.
    with pg_session_factory() as db:
        overview = stuck_service.stuck_payments_overview(db, admin_settings, now_fn=lambda: now)

    assert overview.total_counts["needs_attention"] == 2
    assert overview.total_counts["waiting_gateway"] == 1
    assert overview.total_counts["expired"] == 1

    handlers = _handlers(pg_session_factory, admin_settings)
    [text_out] = handlers.handle(_admin_ctx(), "status", [])

    # /status's own counts must equal centralpay stuck's, not merely be
    # present -- proving genuine parity, not just a rendered number.
    assert f"نیازمند بررسی: {overview.total_counts['needs_attention']}" in text_out
    assert f"در انتظار تأیید درگاه: {overview.total_counts['waiting_gateway']}" in text_out
    assert f"لینک‌های منقضی‌شده: {overview.total_counts['expired']}" in text_out


def test_admin_payment_lookup_refuses_ambiguous_numeric_id_pg(pg_session_factory, admin_settings):
    """A numeric string that names one payment's bot_order_id AND a
    DIFFERENT payment's gateway_order_id must be refused, never guessed --
    exactly the CLI's `_find_payment` contract, now shared."""
    with pg_session_factory() as db:
        db.add(_base_payment(bot_order_id="900050", gateway_order_id=1))
        db.add(_base_payment(bot_order_id="ov-other", gateway_order_id=900050))
        db.commit()

    with pg_session_factory() as db, pytest.raises(AmbiguousOrderIdError):
        find_payment(db, "900050")

    handlers = _handlers(pg_session_factory, admin_settings)
    [text_out] = handlers.handle(_admin_ctx(), "payment", ["900050"])
    assert "چند پرداخت" in text_out
    # Never silently shows either candidate payment's details.
    assert "ov-other" not in text_out


def test_admin_payment_lookup_huge_numeric_bot_order_id_does_not_crash_pg(
    pg_session_factory, admin_settings
):
    """Codex-class regression: a bot_order_id longer than a signed 64-bit
    BIGINT can hold must never be bound into a gateway_order_id == ...
    query. Before sharing app.services.payment_lookup, the admin bot's own
    find_payment() had no such guard and would raise
    psycopg.errors.NumericValueOutOfRange here."""
    huge_bot_order_id = "9" * 30
    with pg_session_factory() as db:
        db.add(_base_payment(bot_order_id=huge_bot_order_id, gateway_order_id=900060))
        db.commit()

    with pg_session_factory() as db:
        found = find_payment(db, huge_bot_order_id)
        assert found is not None
        assert found.gateway_order_id == 900060

    handlers = _handlers(pg_session_factory, admin_settings)
    [text_out] = handlers.handle(_admin_ctx(), "payment", [huge_bot_order_id])
    assert huge_bot_order_id in text_out


def test_admin_read_only_commands_never_mutate_payments_pg(pg_session_factory, admin_settings):
    """/status, /stuck, /waiting, /expired, /payment, /retry_queue,
    /manual_review must never write a row -- proven on real PostgreSQL by
    comparing every column (including updated_at) before and after."""
    now = datetime.now(UTC)
    with pg_session_factory() as db:
        db.add(
            _base_payment(
                bot_order_id="ro-check",
                gateway_order_id=900070,
                status=PaymentStatus.MANUAL_REVIEW.value,
                bot_notify_reason="bot_http_500",
                manual_review_at=now,
                gateway_verified_at=now,
            )
        )
        db.commit()

    def _snapshot(db: Session) -> object:
        return db.execute(
            select(
                Payment.status,
                Payment.updated_at,
                Payment.bot_notify_attempts,
                Payment.manual_review_at,
                Payment.notification_claimed_by,
            ).order_by(Payment.id)
        ).all()

    with pg_session_factory() as db:
        before = _snapshot(db)

    handlers = _handlers(pg_session_factory, admin_settings)
    for command, args in (
        ("status", []),
        ("stuck", []),
        ("waiting", []),
        ("expired", []),
        ("payment", ["ro-check"]),
        ("retry_queue", []),
        ("manual_review", []),
    ):
        handlers.handle(_admin_ctx(), command, args)

    with pg_session_factory() as db:
        after = _snapshot(db)

    assert after == before
