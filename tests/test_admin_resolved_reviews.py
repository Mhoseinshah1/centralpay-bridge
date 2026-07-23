"""Open vs resolved manual reviews in the admin bot.

``centralpay review resolve`` stamps review_resolved_at / review_resolution
WITHOUT changing Payment.status (manual_review stays as history). The bot's
default worklists (/manual_review, /status count, /stuck, the retry_limit
section of /retry_queue) must show only OPEN reviews; resolved ones move to
the read-only /resolved_reviews history and the /payment detail view. No
read path may mutate financial facts.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.adminbot.auth import GENERIC_DENIAL, UpdateContext
from app.adminbot.commands import CommandHandlers
from app.models import Payment, PaymentStatus
from tests.conftest import TEST_ADMIN_ID, TEST_ADMIN_ID_2, make_verified_pending

pytestmark = pytest.mark.usefixtures("app")

ADMIN_IDS = (TEST_ADMIN_ID, TEST_ADMIN_ID_2)


@pytest.fixture
def handlers(session_factory, admin_settings):
    return CommandHandlers(
        session_factory,
        admin_settings,
        ADMIN_IDS,
        api_probe=lambda: {"live": True, "ready": True},
    )


def admin_ctx():
    return UpdateContext(
        user_id=TEST_ADMIN_ID, chat_id=TEST_ADMIN_ID, chat_type="private"
    )


def make_review(
    client,
    settings,
    session_factory,
    stub,
    *,
    order_id,
    reason="retry_limit_reached",
):
    """A gateway-verified payment parked in manual_review (open)."""
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id=order_id
    )
    with session_factory() as db:
        row = db.get(Payment, payment.id)
        row.status = PaymentStatus.MANUAL_REVIEW.value
        row.bot_notify_reason = reason
        row.manual_review_at = datetime(2026, 1, 1, tzinfo=UTC)
        db.commit()
    return payment


def resolve_review(
    session_factory,
    payment_id,
    *,
    resolution="confirmed_by_bot_operator",
    resolved_at=None,
):
    """Exactly what `centralpay review resolve` records: review metadata only,
    never the status and never a financial fact."""
    with session_factory() as db:
        row = db.get(Payment, payment_id)
        row.review_acknowledged_at = row.review_acknowledged_at or datetime.now(UTC)
        row.review_resolved_at = resolved_at or datetime.now(UTC)
        row.review_resolution = resolution
        db.commit()


def test_unresolved_review_appears_in_manual_review(
    handlers, client, settings, session_factory, stub
):
    make_review(client, settings, session_factory, stub, order_id="rev-open")
    text = "\n".join(handlers.handle(admin_ctx(), "manual_review", []))
    assert "rev-open" in text


def test_resolved_review_hidden_from_manual_review(
    handlers, client, settings, session_factory, stub
):
    open_payment = make_review(client, settings, session_factory, stub, order_id="rev-o")
    done_payment = make_review(client, settings, session_factory, stub, order_id="rev-d")
    resolve_review(session_factory, done_payment.id)
    text = "\n".join(handlers.handle(admin_ctx(), "manual_review", []))
    assert open_payment.bot_order_id in text
    assert done_payment.bot_order_id not in text
    # Status remains manual_review: resolution is metadata, not a state change.
    with session_factory() as db:
        assert db.get(Payment, done_payment.id).status == "manual_review"


def test_status_counts_only_open_reviews(
    handlers, client, settings, session_factory, stub
):
    make_review(client, settings, session_factory, stub, order_id="rev-c1")
    make_review(client, settings, session_factory, stub, order_id="rev-c2")
    resolved = make_review(client, settings, session_factory, stub, order_id="rev-c3")
    resolve_review(session_factory, resolved.id)
    text = "\n".join(handlers.handle(admin_ctx(), "status", []))
    assert "بررسی دستی: 2" in text


def test_stuck_excludes_resolved_reviews(
    handlers, client, settings, session_factory, stub
):
    make_review(client, settings, session_factory, stub, order_id="rev-s-open")
    resolved = make_review(client, settings, session_factory, stub, order_id="rev-s-done")
    resolve_review(session_factory, resolved.id)
    text = "\n".join(handlers.handle(admin_ctx(), "stuck", []))
    assert "rev-s-open" in text
    assert "rev-s-done" not in text


def test_retry_queue_excludes_resolved_retry_limit(
    handlers, client, settings, session_factory, stub
):
    make_review(
        client, settings, session_factory, stub,
        order_id="rev-q-open", reason="retry_limit_reached",
    )
    resolved = make_review(
        client, settings, session_factory, stub,
        order_id="rev-q-done", reason="retry_limit_reached",
    )
    resolve_review(session_factory, resolved.id)
    text = "\n".join(handlers.handle(admin_ctx(), "retry_queue", []))
    assert "rev-q-open" in text
    assert "rev-q-done" not in text
    assert "پایان تلاش‌ها (1)" in text


def test_resolved_reviews_shows_only_resolved_with_metadata(
    handlers, client, settings, session_factory, stub
):
    make_review(client, settings, session_factory, stub, order_id="rev-r-open")
    older = make_review(client, settings, session_factory, stub, order_id="rev-r-old")
    newer = make_review(client, settings, session_factory, stub, order_id="rev-r-new")
    now = datetime.now(UTC)
    resolve_review(
        session_factory, older.id,
        resolution="confirmed_by_bot_operator", resolved_at=now - timedelta(hours=2),
    )
    resolve_review(
        session_factory, newer.id,
        resolution="charge_reconciled_manually", resolved_at=now,
    )
    text = "\n".join(handlers.handle(admin_ctx(), "resolved_reviews", []))
    assert "rev-r-open" not in text  # open reviews never appear here
    # Every required field for each entry.
    assert "rev-r-old" in text and "rev-r-new" in text
    assert str(newer.gateway_order_id) in text
    assert "10,000" in text  # amount
    assert "retry_limit_reached" in text  # bot_notify_reason
    assert "confirmed_by_bot_operator" in text  # review_resolution
    assert "charge_reconciled_manually" in text
    assert "زمان تعیین‌تکلیف" in text  # review_resolved_at label
    # Newest resolution first.
    assert text.index("rev-r-new") < text.index("rev-r-old")


def test_resolved_reviews_default_and_max_limits(
    handlers, client, settings, session_factory, stub
):
    for index in range(55):
        payment = make_review(
            client, settings, session_factory, stub, order_id=f"rev-n-{index:02d}"
        )
        resolve_review(session_factory, payment.id)
    default_text = "\n".join(handlers.handle(admin_ctx(), "resolved_reviews", []))
    assert "بررسی‌های تعیین‌تکلیف‌شده (10)" in default_text  # default n
    explicit_text = "\n".join(handlers.handle(admin_ctx(), "resolved_reviews", ["3"]))
    assert "بررسی‌های تعیین‌تکلیف‌شده (3)" in explicit_text
    capped_text = "\n".join(handlers.handle(admin_ctx(), "resolved_reviews", ["500"]))
    assert "بررسی‌های تعیین‌تکلیف‌شده (50)" in capped_text  # hard maximum


def test_resolved_reviews_requires_authorization(handlers):
    outsider = UpdateContext(user_id=999999999, chat_id=999999999, chat_type="private")
    assert handlers.handle(outsider, "resolved_reviews", []) == [GENERIC_DENIAL]


def test_payment_shows_resolution_metadata(
    handlers, client, settings, session_factory, stub
):
    payment = make_review(client, settings, session_factory, stub, order_id="rev-p")
    resolve_review(session_factory, payment.id, resolution="confirmed_by_bot_operator")
    text = "\n".join(handlers.handle(admin_ctx(), "payment", ["rev-p"]))
    assert "تعیین‌تکلیف بررسی" in text  # resolution time label
    assert "confirmed_by_bot_operator" in text  # resolution type


def test_daily_report_counts_only_open_reviews(
    client, settings, session_factory, stub
):
    from app.adminbot.queries import daily_report_payload

    open_payment = make_review(client, settings, session_factory, stub, order_id="rev-dr-o")
    resolved = make_review(client, settings, session_factory, stub, order_id="rev-dr-d")
    resolve_review(session_factory, resolved.id)

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
                    Payment.review_resolved_at,
                    Payment.review_resolution,
                    Payment.updated_at,
                ).order_by(Payment.id)
            ).all()

    before = snapshot()
    with session_factory() as db:
        payload = daily_report_payload(db, report_date="2026-07-23")
    # The unresolved review is counted; the resolved one is excluded.
    assert payload["manual_review"] == 1
    # Both rows keep manual_review as historical status; nothing was mutated.
    assert snapshot() == before
    with session_factory() as db:
        assert db.get(Payment, open_payment.id).status == "manual_review"
        assert db.get(Payment, resolved.id).status == "manual_review"


def test_review_read_paths_do_not_mutate_financial_fields(
    handlers, client, settings, session_factory, stub
):
    open_payment = make_review(client, settings, session_factory, stub, order_id="rev-f1")
    resolved = make_review(client, settings, session_factory, stub, order_id="rev-f2")
    resolve_review(session_factory, resolved.id)

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
                    Payment.review_resolved_at,
                    Payment.review_resolution,
                    Payment.updated_at,
                ).order_by(Payment.id)
            ).all()

    before = snapshot()
    for command, args in (
        ("manual_review", []),
        ("status", []),
        ("stuck", []),
        ("retry_queue", []),
        ("resolved_reviews", []),
        ("payment", [open_payment.bot_order_id]),
        ("payment", [resolved.bot_order_id]),
    ):
        handlers.handle(admin_ctx(), command, args)
    assert snapshot() == before
