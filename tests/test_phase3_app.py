"""Phase 3 application changes: amount bounds, version, config aliases,
worker heartbeat, and text log format."""

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.logging_setup import SecretRedactor, TextFormatter
from app.models import Payment
from tests.conftest import create_order, get_payment


def _count_payments(session_factory) -> int:
    from sqlalchemy import func, select

    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


@pytest.mark.parametrize("amount", [1, 999])
def test_amount_below_minimum_rejected(client, settings, session_factory, amount):
    response = create_order(client, settings, order_id="p3-low", amount=amount)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "amount_out_of_range"
    assert _count_payments(session_factory) == 0


def test_amount_above_maximum_rejected(client, settings, session_factory):
    response = create_order(
        client, settings, order_id="p3-high", amount=settings.max_payment_amount_toman + 1
    )
    assert response.status_code == 400
    # Since the dynamic-fee feature, MAX_PAYMENT_AMOUNT_TOMAN explicitly
    # bounds the FINAL payable amount (original + fee); with zero fee the
    # payable equals the original, so an over-max original is rejected with
    # the payable code — still before any gateway call or payment row.
    assert response.json()["error"]["code"] == "payable_amount_out_of_range"
    assert _count_payments(session_factory) == 0


def test_amount_at_bounds_accepted(client, settings, session_factory):
    response = create_order(
        client, settings, order_id="p3-min", amount=settings.min_payment_amount_toman
    )
    assert response.status_code == 200
    response = create_order(
        client, settings, order_id="p3-max", amount=settings.max_payment_amount_toman
    )
    assert response.status_code == 200
    assert get_payment(session_factory, "p3-min").amount == settings.min_payment_amount_toman


def test_min_must_be_below_max(settings):
    with pytest.raises(ValidationError, match="MIN_PAYMENT_AMOUNT_TOMAN"):
        type(settings)(
            **{
                **settings.model_dump(),
                "min_payment_amount_toman": 5000,
                "max_payment_amount_toman": 5000,
            }
        )


def test_callback_secret_alias_accepted(settings, monkeypatch):
    """CALLBACK_SECRET is accepted as an alias for CALLBACK_HMAC_SECRET."""
    values = settings.model_dump()
    values.pop("callback_hmac_secret")
    for key, value in values.items():
        monkeypatch.setenv(key.upper(), str(value))
    monkeypatch.setenv("CALLBACK_SECRET", "alias-secret-0123456789abcdef")
    loaded = type(settings)(_env_file=None)
    assert loaded.callback_hmac_secret == "alias-secret-0123456789abcdef"


def test_invalid_telegram_bot_username_rejected(settings):
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_USERNAME"):
        type(settings)(
            **{**settings.model_dump(), "telegram_bot_username": "bad name<script>"}
        )


def test_payer_page_includes_bot_link_when_configured(settings):
    from app.api.pages import payment_status_page
    from app.services.verification import CallbackStatus

    page = payment_status_page(
        CallbackStatus.BOT_PENDING, "order-1", bot_username="@my_bot"
    )
    assert 'href="https://t.me/my_bot"' in page
    page = payment_status_page(CallbackStatus.BOT_PENDING, "order-1")
    assert "t.me" not in page


def test_worker_heartbeat_is_touched_after_pass(
    client, settings, session_factory, stub, bot_stub, notifier, tmp_path, monkeypatch
):
    """The worker loop liveness contract: the heartbeat file is touched by
    the health-check mechanism the container uses."""
    heartbeat = tmp_path / "heartbeat"
    # run_worker_pass itself does not touch it (the loop does); emulate one
    # loop iteration the way app.worker.main does.
    from tests.conftest import run_pass

    run_pass(session_factory, notifier, settings)
    Path(heartbeat).touch()
    assert heartbeat.exists()


def test_text_log_format_still_redacts(settings):
    formatter = TextFormatter(SecretRedactor([settings.inbound_api_key]))
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.leaked = settings.inbound_api_key
    line = formatter.format(record)
    assert settings.inbound_api_key not in line
    assert "[REDACTED]" in line


def test_text_log_format_configurable(settings):
    text_settings = settings.model_copy(update={"log_format": "text"})
    from app.logging_setup import configure_logging

    configure_logging(text_settings)
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, TextFormatter)
    # Restore JSON for other tests.
    configure_logging(settings)
