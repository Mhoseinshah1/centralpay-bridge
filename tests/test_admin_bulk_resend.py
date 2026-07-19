"""Admin bot /resend_failed: preview, idempotent-mode gate, authorization,
HTML-safety, and delivery-only (no network) execution.

Concurrency and full financial-invariant coverage live in the PostgreSQL
integration suite (tests/integration/test_bulk_resend_pg.py); these unit tests
exercise the handler surface on the in-memory engine.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.adminbot.auth import GENERIC_DENIAL, UpdateContext
from app.adminbot.commands import BULK_RESEND_SAFE_MODE_MESSAGE, CommandHandlers
from app.models import Payment, PaymentEvent, PaymentStatus
from tests.conftest import TEST_ADMIN_ID, TEST_ADMIN_ID_2, make_verified_pending

pytestmark = pytest.mark.usefixtures("app")

ADMIN_IDS = (TEST_ADMIN_ID, TEST_ADMIN_ID_2)


def _throwing_probe() -> dict[str, bool]:
    raise AssertionError("/resend_failed must never probe the API or the network")


@pytest.fixture
def idem_settings(admin_settings):
    return admin_settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})


@pytest.fixture
def idem_handlers(session_factory, idem_settings):
    return CommandHandlers(session_factory, idem_settings, ADMIN_IDS, api_probe=_throwing_probe)


@pytest.fixture
def safe_handlers(session_factory, admin_settings):
    # admin_settings inherits bot_notify_retry_mode="safe" from the base settings.
    return CommandHandlers(session_factory, admin_settings, ADMIN_IDS, api_probe=_throwing_probe)


def admin_ctx():
    return UpdateContext(user_id=TEST_ADMIN_ID, chat_id=TEST_ADMIN_ID, chat_type="private")


def make_eligible(
    client, settings, session_factory, stub, *, order_id, amount=10000, reason="retry_limit_reached"
):
    """A gateway-verified payment moved to manual_review for a delivery reason."""
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id=order_id, amount=amount
    )
    with session_factory() as db:
        row = db.get(Payment, payment.id)
        row.status = PaymentStatus.MANUAL_REVIEW.value
        row.bot_notify_reason = reason
        row.manual_review_at = datetime(2026, 1, 1, tzinfo=UTC)
        row.bot_notify_attempts = 6
        row.next_retry_at = None
        row.notification_claimed_at = None
        row.notification_claimed_by = None
        db.commit()
    return payment


def _count_status(session_factory, status):
    with session_factory() as db:
        return db.execute(
            select(func.count(Payment.id)).where(Payment.status == status)
        ).scalar_one()


def _events_of_type(session_factory, event_type):
    with session_factory() as db:
        return db.execute(
            select(func.count(PaymentEvent.id)).where(PaymentEvent.event_type == event_type)
        ).scalar_one()


# 1. Preview-only does not modify the DB.
def test_preview_without_confirm_does_not_modify_db(
    idem_handlers, client, settings, session_factory, stub
):
    make_eligible(client, settings, session_factory, stub, order_id="br-prev")
    before_events = _events_of_type(session_factory, "admin_bulk_resend_requested")
    replies = idem_handlers.handle(admin_ctx(), "resend_failed", [])
    text = "\n".join(replies)
    assert "پیش‌نمایش" in text
    assert "هنوز هیچ ارسال شبکه‌ای انجام نشده است" in text
    assert "/resend_failed confirm" in text
    # Still in manual_review; no requeue event, no requeue.
    assert _count_status(session_factory, PaymentStatus.MANUAL_REVIEW.value) == 1
    assert _count_status(session_factory, PaymentStatus.BOT_NOTIFY_PENDING.value) == 0
    assert _events_of_type(session_factory, "admin_bulk_resend_requested") == before_events


# 2. Confirm rejected in safe mode with the fixed message; no mutation.
def test_confirm_rejected_in_safe_mode(safe_handlers, client, settings, session_factory, stub):
    make_eligible(client, settings, session_factory, stub, order_id="br-safe")
    [reply] = safe_handlers.handle(admin_ctx(), "resend_failed", ["confirm"])
    assert reply == BULK_RESEND_SAFE_MODE_MESSAGE
    assert _count_status(session_factory, PaymentStatus.MANUAL_REVIEW.value) == 1
    assert _count_status(session_factory, PaymentStatus.BOT_NOTIFY_PENDING.value) == 0
    assert _events_of_type(session_factory, "admin_bulk_resend_requested") == 0
    assert _events_of_type(session_factory, "admin_bulk_resend_completed") == 0


# also: preview rejected in safe mode.
def test_preview_rejected_in_safe_mode(safe_handlers, client, settings, session_factory, stub):
    make_eligible(client, settings, session_factory, stub, order_id="br-safe2")
    [reply] = safe_handlers.handle(admin_ctx(), "resend_failed", [])
    assert reply == BULK_RESEND_SAFE_MODE_MESSAGE


# 3. Unauthorized users learn nothing.
def test_unauthorized_user_gets_no_information(
    idem_handlers, client, settings, session_factory, stub
):
    make_eligible(client, settings, session_factory, stub, order_id="br-unauth", amount=777777)
    outsider = UpdateContext(user_id=999999, chat_id=999999, chat_type="private")
    for args in ([], ["confirm"]):
        [reply] = idem_handlers.handle(outsider, "resend_failed", args)
        assert reply == GENERIC_DENIAL
        assert "777,777" not in reply
        assert "br-unauth" not in reply
        assert "پرداخت" not in reply and "واجد شرایط" not in reply
    # Nothing was requeued by the unauthorized attempts.
    assert _count_status(session_factory, PaymentStatus.BOT_NOTIFY_PENDING.value) == 0


# 4. Group chats are rejected.
def test_group_chat_is_rejected(idem_handlers, client, settings, session_factory, stub):
    make_eligible(client, settings, session_factory, stub, order_id="br-group")
    group = UpdateContext(user_id=TEST_ADMIN_ID, chat_id=-100, chat_type="group")
    [reply] = idem_handlers.handle(group, "resend_failed", ["confirm"])
    assert reply == GENERIC_DENIAL
    assert _count_status(session_factory, PaymentStatus.BOT_NOTIFY_PENDING.value) == 0


# 5. /help and /start display the new command.
def test_help_and_start_show_new_command(idem_handlers):
    help_text = "\n".join(idem_handlers.handle(admin_ctx(), "help", []))
    assert "/resend_failed — پیش‌نمایش ارسال مجدد موارد تحویل‌نشده" in help_text
    assert "/resend_failed confirm — بازگرداندن گروهی به صف، فقط در حالت idempotent" in help_text
    start_text = "\n".join(idem_handlers.handle(admin_ctx(), "start", []))
    assert "/resend_failed" in start_text


# 6. Responses are HTML-safe.
def test_responses_are_html_safe(idem_handlers, client, settings, session_factory, stub):
    make_eligible(client, settings, session_factory, stub, order_id="<b>x&y</b>")
    preview = "\n".join(idem_handlers.handle(admin_ctx(), "resend_failed", []))
    assert "<b>x&y</b>" not in preview
    assert "&lt;b&gt;x&amp;y&lt;/b&gt;" in preview


# 7. Responses remain below the configured message size limit.
def test_responses_below_message_size_limit(
    idem_handlers, idem_settings, client, settings, session_factory, stub
):
    for i in range(25):
        make_eligible(client, settings, session_factory, stub, order_id=f"br-size-{i}")
    for args in ([], ["confirm"]):
        replies = idem_handlers.handle(admin_ctx(), "resend_failed", args)
        for reply in replies:
            assert len(reply) <= idem_settings.admin_bot_max_message_length


# 8. Preview displays at most 20 order_ids.
def test_preview_shows_at_most_20_order_ids(idem_handlers, client, settings, session_factory, stub):
    for i in range(25):
        make_eligible(client, settings, session_factory, stub, order_id=f"brid-{i:02d}")
    preview = "\n".join(idem_handlers.handle(admin_ctx(), "resend_failed", []))
    shown = sum(1 for i in range(25) if f"brid-{i:02d}" in preview)
    assert shown == 20
    # The full count is still reported.
    assert "پرداخت‌های واجد شرایط: 25" in preview


# 9. Preview includes total original invoice amount.
def test_preview_includes_total_original_amount(
    idem_handlers, client, settings, session_factory, stub
):
    make_eligible(client, settings, session_factory, stub, order_id="br-amt-1", amount=250000)
    make_eligible(client, settings, session_factory, stub, order_id="br-amt-2", amount=350000)
    preview = "\n".join(idem_handlers.handle(admin_ctx(), "resend_failed", []))
    assert "مبلغ اصلی مجموع: 600,000 تومان" in preview


# 10. Execution performs no network request and requeues in the DB only.
def test_execution_performs_no_network_request(
    idem_handlers, client, settings, session_factory, stub
):
    import app.services.bulk_resend as bulk_resend_module

    make_eligible(client, settings, session_factory, stub, order_id="br-net", amount=123000)
    # The throwing api_probe proves _probe_api is never called on this path.
    replies = idem_handlers.handle(admin_ctx(), "resend_failed", ["confirm"])
    text = "\n".join(replies)
    assert "دوباره وارد صف ارسال شد" in text
    assert "ارسال واقعی توسط Worker انجام می‌شود" in text
    # Delivery-only wording; never a claim that credit was applied.
    assert "واریز" not in text and "اعتبار" not in text
    # The service itself has no HTTP client at all.
    source = bulk_resend_module.__file__
    with open(source) as fh:
        assert "httpx" not in fh.read()
    # DB-only effect: the row is now pending for the worker.
    assert _count_status(session_factory, PaymentStatus.BOT_NOTIFY_PENDING.value) == 1
