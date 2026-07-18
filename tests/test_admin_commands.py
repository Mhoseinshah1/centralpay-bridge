"""Admin bot commands: safe read-only output, bounded, secret-free."""

import pytest
from sqlalchemy import func, select

from app.adminbot.auth import UpdateContext
from app.adminbot.commands import RECENT_MAX, CommandHandlers
from app.models import Payment, PaymentEvent
from tests.conftest import (
    TEST_ADMIN_ID,
    TEST_ADMIN_ID_2,
    make_verified_pending,
    run_pass,
)

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


def _all_secrets(admin_settings):
    return [
        admin_settings.inbound_api_key,
        admin_settings.callback_hmac_secret,
        admin_settings.centralpay_getlink_api_key,
        admin_settings.centralpay_verify_api_key,
        admin_settings.bot_notify_token,
        admin_settings.admin_bot_token,
    ]


def test_status_returns_safe_health_data(handlers, admin_settings, session_factory):
    [reply] = handlers.handle(admin_ctx(), "status", [])
    assert "API" in reply
    assert "✅" in reply
    for secret in _all_secrets(admin_settings):
        assert secret not in reply
    # No connection strings or paths leak either.
    assert "postgresql" not in reply
    assert "/etc/centralpay-bridge" not in reply


def test_recent_enforces_maximum_count(
    handlers, client, settings, session_factory, stub
):
    for index in range(3):
        make_verified_pending(
            client, settings, session_factory, stub, order_id=f"adm-{index}"
        )
    replies = handlers.handle(admin_ctx(), "recent", ["500"])
    text = "\n".join(replies)
    # The bound is applied: title reflects at most RECENT_MAX.
    assert f"آخرین {min(3, RECENT_MAX)} پرداخت" in text
    assert "adm-0" in text and "adm-2" in text


def test_payment_lookup_by_bot_order_id_and_gateway_id(
    handlers, client, settings, session_factory, stub
):
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="adm-lookup"
    )
    by_bot = "\n".join(handlers.handle(admin_ctx(), "payment", ["adm-lookup"]))
    assert "adm-lookup" in by_bot
    assert str(payment.gateway_order_id) in by_bot
    by_gateway = "\n".join(
        handlers.handle(admin_ctx(), "payment", [str(payment.gateway_order_id)])
    )
    assert "adm-lookup" in by_gateway
    # Audit events are included, bounded to 10.
    assert "gateway_payment_verified" in by_gateway


def test_payment_output_contains_no_secrets(
    handlers, admin_settings, client, settings, session_factory, stub
):
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="adm-secret"
    )
    text = "\n".join(handlers.handle(admin_ctx(), "payment", ["adm-secret"]))
    for secret in _all_secrets(admin_settings):
        assert secret not in text
    # Full card number and redirect URL never appear; only last4 may.
    assert "6037991234567890" not in text
    assert payment.redirect_url is not None
    assert payment.redirect_url not in text
    assert "sig=" not in text


def test_manual_review_shows_exact_reason_codes(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    import httpx

    make_verified_pending(client, settings, session_factory, stub, order_id="adm-review")
    bot_stub.result = httpx.Response(422)
    run_pass(session_factory, notifier, settings)
    text = "\n".join(handlers.handle(admin_ctx(), "manual_review", []))
    assert "adm-review" in text
    assert "bot_http_422" in text  # the exact reason code, not a generic label


def test_stuck_uses_exact_categories(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    import httpx

    make_verified_pending(client, settings, session_factory, stub, order_id="adm-attn")
    bot_stub.result = httpx.ReadTimeout("t")
    run_pass(session_factory, notifier, settings)
    text = "\n".join(handlers.handle(admin_ctx(), "stuck", []))
    assert "adm-attn" in text
    # The exact reason code appears; never a generic "stuck" label.
    assert "bot_timeout_ambiguous" in text
    assert "stuck" not in text


def test_retry_queue_is_read_only(
    handlers, client, settings, session_factory, stub
):
    make_verified_pending(client, settings, session_factory, stub, order_id="adm-queue")
    with session_factory() as db:
        before_payments = db.execute(
            select(
                Payment.id, Payment.status, Payment.bot_notify_attempts, Payment.updated_at
            ).order_by(Payment.id)
        ).all()
        before_events = db.execute(select(func.count(PaymentEvent.id))).scalar_one()

    text = "\n".join(handlers.handle(admin_ctx(), "retry_queue", []))
    assert "adm-queue" in text

    with session_factory() as db:
        after_payments = db.execute(
            select(
                Payment.id, Payment.status, Payment.bot_notify_attempts, Payment.updated_at
            ).order_by(Payment.id)
        ).all()
        after_events = db.execute(select(func.count(PaymentEvent.id))).scalar_one()
    assert after_payments == before_payments
    # Only the command-audit events were appended; no payment mutations.
    assert after_events >= before_events


def test_backup_status_exposes_no_credentials(handlers, admin_settings):
    text = "\n".join(handlers.handle(admin_ctx(), "backup_status", []))
    for secret in _all_secrets(admin_settings):
        assert secret not in text
    assert "postgresql" not in text
    assert "/var/backups" not in text  # no path disclosure


def test_health_reports_components(handlers):
    text = "\n".join(handlers.handle(admin_ctx(), "health", []))
    assert "live" in text and "ready" in text
    assert "ضربان ورکر" in text


def test_start_includes_no_balance_credit_warning(handlers):
    text = "\n".join(handlers.handle(admin_ctx(), "start", []))
    assert "واریز قطعی اعتبار" in text
    assert "0.5.0-rc1" in text


def test_errors_summary_lists_reason_events(
    handlers, client, settings, session_factory, stub
):
    import httpx

    stub.getlink_result = httpx.ConnectError("refused")
    client.post(
        "/api/custom-payment",
        json={
            "api_key": settings.inbound_api_key,
            "amount": 10000,
            "order_id": "adm-err",
        },
    )
    text = "\n".join(handlers.handle(admin_ctx(), "errors", []))
    assert "centralpay_getlink_failed" in text


def test_version_command(handlers):
    text = "\n".join(handlers.handle(admin_ctx(), "version", []))
    assert "0.5.0-rc1" in text
