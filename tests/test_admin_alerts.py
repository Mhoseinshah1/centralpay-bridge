"""Alert outbox: creation in payment transactions, dedup, delivery, retries."""

from datetime import UTC, datetime, timedelta

import httpx

from app.adminbot.alerts import create_alert
from app.adminbot.telegram import SendOutcome, classify_send_error
from app.models import AlertStatus
from tests.conftest import (
    TEST_ADMIN_ID,
    TEST_ADMIN_ID_2,
    FakeAlertSender,
    create_order,
    get_alerts,
    get_payment,
    make_verified_pending,
    run_alert_pass,
    run_pass,
    valid_callback_path,
    verify_ok_response,
)

FIXED_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def test_manual_review_creates_alert(
    alert_policy, client, settings, session_factory, stub, bot_stub, notifier
):
    make_verified_pending(client, settings, session_factory, stub, order_id="al-review")
    bot_stub.result = httpx.Response(422)
    run_pass(session_factory, notifier, settings)

    alerts = get_alerts(session_factory, "manual_review_required")
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert alerts[0].status == AlertStatus.PENDING.value
    assert alerts[0].payment_id == get_payment(session_factory, "al-review").id


def test_amount_mismatch_creates_high_severity_alert(
    alert_policy, client, settings, session_factory, stub
):
    assert create_order(client, settings, order_id="al-amount", amount=10000).status_code == 200
    payment = get_payment(session_factory, "al-amount")
    stub.verify_result = verify_ok_response(amount=999)
    client.get(valid_callback_path(stub, payment.gateway_order_id))

    alerts = get_alerts(session_factory, "verify_amount_mismatch")
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    # Never deduplicated: financial-integrity alerts have no dedup key.
    assert alerts[0].deduplication_key is None


def test_ambiguous_timeout_creates_alert_with_reason_type(
    alert_policy, client, settings, session_factory, stub, bot_stub, notifier
):
    make_verified_pending(client, settings, session_factory, stub, order_id="al-amb")
    bot_stub.result = httpx.ReadTimeout("t")
    run_pass(session_factory, notifier, settings)
    assert len(get_alerts(session_factory, "bot_timeout_ambiguous")) == 1
    # No double alert from the manual_review_required event.
    assert get_alerts(session_factory, "manual_review_required") == []


def test_alert_creation_failure_does_not_block_payment(
    alert_policy, client, settings, session_factory, stub, monkeypatch
):
    import app.adminbot.alerts as alerts_module

    def boom(*args, **kwargs):
        raise RuntimeError("alert insert failed")

    monkeypatch.setattr(alerts_module, "create_alert", boom)
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="al-boom"
    )
    # Payment processing succeeded despite alert-creation failure.
    assert payment.gateway_verified_at is not None
    assert payment.status == "bot_notify_pending"


def test_infrastructure_alerts_deduplicate(alert_policy, session_factory):
    with session_factory() as db:
        first = create_alert(
            db,
            alert_type="service_unhealthy",
            severity="error",
            deduplication_key="service_unhealthy:database",
            now=FIXED_NOW,
        )
        second = create_alert(
            db,
            alert_type="service_unhealthy",
            severity="error",
            deduplication_key="service_unhealthy:database",
            now=FIXED_NOW + timedelta(minutes=5),
        )
        third = create_alert(
            db,
            alert_type="service_unhealthy",
            severity="error",
            deduplication_key="service_unhealthy:database",
            now=FIXED_NOW + timedelta(minutes=45),  # outside the 30-min window
        )
        db.commit()
    assert first.status == AlertStatus.PENDING.value
    assert second.status == AlertStatus.SUPPRESSED.value
    assert third.status == AlertStatus.PENDING.value


def test_distinct_payment_alerts_are_not_deduplicated(
    alert_policy, client, settings, session_factory, stub, bot_stub, notifier
):
    for index in range(2):
        make_verified_pending(
            client, settings, session_factory, stub, order_id=f"al-multi-{index}"
        )
    bot_stub.result = httpx.Response(422)
    run_pass(session_factory, notifier, settings)
    alerts = get_alerts(session_factory, "manual_review_required")
    assert len(alerts) == 2
    assert {a.status for a in alerts} == {AlertStatus.PENDING.value}


def test_telegram_429_schedules_retry_with_retry_after(
    alert_policy, admin_settings, session_factory
):
    with session_factory() as db:
        create_alert(db, alert_type="admin_test_alert", now=FIXED_NOW)
        db.commit()
    sender = FakeAlertSender()
    throttled = SendOutcome(
        ok=False, retryable=True, retry_after_seconds=300, error_code="telegram_429"
    )
    sender.results = {TEST_ADMIN_ID: [throttled], TEST_ADMIN_ID_2: [throttled]}
    run_alert_pass(session_factory, sender, admin_settings, now_fn=lambda: FIXED_NOW)

    [alert] = get_alerts(session_factory, "admin_test_alert")
    assert alert.status == AlertStatus.RETRY_SCHEDULED.value
    assert alert.last_error_code == "telegram_429"
    from tests.conftest import as_utc

    assert as_utc(alert.next_retry_at) >= FIXED_NOW + timedelta(seconds=300)
    assert alert.claimed_at is None

    # Due later: retry delivers.
    sender2 = FakeAlertSender()
    run_alert_pass(
        session_factory,
        sender2,
        admin_settings,
        now_fn=lambda: FIXED_NOW + timedelta(seconds=301),
    )
    [alert] = get_alerts(session_factory, "admin_test_alert")
    assert alert.status == AlertStatus.DELIVERED.value


def test_permanent_telegram_error_stops_retries(
    alert_policy, admin_settings, session_factory
):
    with session_factory() as db:
        create_alert(db, alert_type="admin_test_alert", now=FIXED_NOW)
        db.commit()
    sender = FakeAlertSender()
    invalid = SendOutcome(ok=False, retryable=False, error_code="telegram_invalid_token")
    sender.results = {TEST_ADMIN_ID: [invalid], TEST_ADMIN_ID_2: [invalid]}
    run_alert_pass(session_factory, sender, admin_settings, now_fn=lambda: FIXED_NOW)

    [alert] = get_alerts(session_factory, "admin_test_alert")
    assert alert.status == AlertStatus.FAILED.value
    assert alert.last_error_code == "telegram_invalid_token"
    # No further attempts.
    sends_before = len(sender.sent)
    run_alert_pass(
        session_factory, sender, admin_settings, now_fn=lambda: FIXED_NOW + timedelta(hours=1)
    )
    assert len(sender.sent) == sends_before


def test_partial_delivery_counts_as_delivered(
    alert_policy, admin_settings, session_factory
):
    with session_factory() as db:
        create_alert(db, alert_type="admin_test_alert", now=FIXED_NOW)
        db.commit()
    sender = FakeAlertSender()
    blocked = SendOutcome(ok=False, retryable=False, error_code="telegram_forbidden")
    sender.results = {TEST_ADMIN_ID_2: [blocked]}
    run_alert_pass(session_factory, sender, admin_settings, now_fn=lambda: FIXED_NOW)
    [alert] = get_alerts(session_factory, "admin_test_alert")
    assert alert.status == AlertStatus.DELIVERED.value
    assert alert.last_error_code == "partial:telegram_forbidden"


def test_alert_messages_escape_untrusted_text(
    alert_policy, admin_settings, session_factory
):
    evil = "<script>alert('EVIL')</script> & <b>bold</b>"
    with session_factory() as db:
        create_alert(
            db,
            alert_type="service_unhealthy",
            severity="error",
            payload={"check": "api_ready", "detail": evil},
            now=FIXED_NOW,
        )
        db.commit()
    sender = FakeAlertSender()
    run_alert_pass(session_factory, sender, admin_settings, now_fn=lambda: FIXED_NOW)
    assert sender.sent
    for _, text in sender.sent:
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


def test_full_card_numbers_never_sent(
    alert_policy, client, settings, session_factory, stub, bot_stub, notifier, admin_settings
):
    make_verified_pending(client, settings, session_factory, stub, order_id="al-card")
    bot_stub.result = httpx.ReadTimeout("t")
    run_pass(session_factory, notifier, settings)
    sender = FakeAlertSender()
    run_alert_pass(session_factory, sender, admin_settings)
    assert sender.sent
    for _, text in sender.sent:
        assert "6037991234567890" not in text  # full card number from verify stub


def test_tokens_never_logged_during_delivery(
    alert_policy, admin_settings, session_factory
):
    import io
    import logging

    from app.logging_setup import JsonFormatter, SecretRedactor, collect_secret_values

    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(JsonFormatter(SecretRedactor(collect_secret_values(admin_settings))))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with session_factory() as db:
            create_alert(db, alert_type="admin_test_alert", now=FIXED_NOW)
            db.commit()
        sender = FakeAlertSender()
        run_alert_pass(session_factory, sender, admin_settings, now_fn=lambda: FIXED_NOW)
    finally:
        root.removeHandler(handler)
    output = handler.stream.getvalue()
    assert output
    assert admin_settings.admin_bot_token not in output
    assert admin_settings.bot_notify_token not in output


def test_collect_secret_values_includes_admin_token(admin_settings):
    from app.logging_setup import collect_secret_values

    assert admin_settings.admin_bot_token in collect_secret_values(admin_settings)


def test_daily_report_not_duplicated_after_restart(
    alert_policy, admin_settings, session_factory
):
    from app.adminbot.reports import maybe_queue_daily_report

    report_now = datetime(2026, 7, 18, 9, 30, 0, tzinfo=UTC)  # 13:00 Tehran
    with session_factory() as db:
        assert maybe_queue_daily_report(db, admin_settings, now_utc=report_now) is True
    # A "restarted" process attempts again for the same local day.
    with session_factory() as db:
        assert maybe_queue_daily_report(db, admin_settings, now_utc=report_now) is False
    reports = get_alerts(session_factory, "daily_report")
    assert len(reports) == 1
    assert reports[0].payload["report_date"] == "2026-07-18"
    # Not due before the configured time.
    early = datetime(2026, 7, 19, 3, 0, 0, tzinfo=UTC)  # 06:30 Tehran < 09:00
    with session_factory() as db:
        assert maybe_queue_daily_report(db, admin_settings, now_utc=early) is False


def test_payment_success_alerts_disabled_by_default(
    alert_policy, client, settings, session_factory, stub
):
    make_verified_pending(client, settings, session_factory, stub, order_id="al-succ")
    assert get_alerts(session_factory, "gateway_payment_verified") == []


def test_payment_success_alerts_when_enabled(
    app, admin_settings, client, settings, session_factory, stub
):
    from app.adminbot.alerts import configure_alert_creation, reset_alert_creation

    noisy = admin_settings.model_copy(update={"admin_bot_payment_success_alerts": True})
    configure_alert_creation(noisy)
    try:
        make_verified_pending(client, settings, session_factory, stub, order_id="al-succ2")
    finally:
        reset_alert_creation()
    assert len(get_alerts(session_factory, "gateway_payment_verified")) == 1


def test_classify_send_error_maps_ptb_exceptions():
    import telegram.error as terr

    assert classify_send_error(terr.RetryAfter(7)).retry_after_seconds == 8
    assert classify_send_error(terr.RetryAfter(7)).retryable is True
    assert classify_send_error(terr.InvalidToken()).error_code == "telegram_invalid_token"
    assert classify_send_error(terr.InvalidToken()).retryable is False
    assert classify_send_error(terr.Forbidden("blocked")).retryable is False
    assert classify_send_error(terr.BadRequest("chat not found")).retryable is False
    assert classify_send_error(terr.TimedOut()).retryable is True
    assert classify_send_error(terr.NetworkError("boom")).retryable is True
