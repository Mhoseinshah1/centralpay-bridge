"""The /stuck /waiting /expired redesign: three focused admin-bot commands
replacing the old grouped /stuck.

/stuck is now narrowly scoped to bot-delivery problems ONLY — payments that
failed, or are stuck trying, to reach the customer bot's webhook. It is
built from the existing status/reason semantics via
app.adminbot.queries.bot_delivery_snapshot /
_bot_delivery_manual_review_conditions: an open manual-review row counts
only when app.services.notification._move_to_manual_review set
bot_notify_reason (the ONLY code path that does), never when
app.services.verification's financial/identity mismatches moved the row to
manual_review (those never touch bot_notify_reason). Reconciliation-exhausted
and unexpected-status rows are NEVER shown in /stuck's detailed list either
— they remain visible via the "other" summary line, /manual_review, and
/errors, never mislabeled as a bot-delivery error.

/waiting and /expired show ONLY StuckCategory.WAITING_GATEWAY /
StuckCategory.EXPIRED respectively, with their own N (default 10, max 50)
argument and explicit (never silent) validation.

All three are strictly read-only.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.adminbot.auth import UpdateContext
from app.adminbot.commands import CommandHandlers
from app.models import Payment, PaymentEvent, PaymentStatus
from tests.conftest import (
    TEST_ADMIN_ID,
    TEST_ADMIN_ID_2,
    create_order,
    make_verified_pending,
    run_pass,
)

pytestmark = pytest.mark.usefixtures("app")

ADMIN_IDS = (TEST_ADMIN_ID, TEST_ADMIN_ID_2)


@pytest.fixture
def handlers(session_factory, admin_settings):
    return CommandHandlers(
        session_factory, admin_settings, ADMIN_IDS, api_probe=lambda: {"live": True, "ready": True}
    )


def admin_ctx():
    return UpdateContext(user_id=TEST_ADMIN_ID, chat_id=TEST_ADMIN_ID, chat_type="private")


def _get(session_factory, order_id: str) -> Payment:
    with session_factory() as db:
        return db.execute(select(Payment).where(Payment.bot_order_id == order_id)).scalar_one()


def _make_bot_delivery_failure(
    client, settings, session_factory, stub, bot_stub, notifier, order_id
):
    """A gateway-verified payment whose bot notification failed with a
    retryable timeout — sets status=manual_review, bot_notify_reason set."""
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    bot_stub.result = httpx.ReadTimeout("t")
    run_pass(session_factory, notifier, settings)


def _make_financial_manual_review(
    session_factory, order_id: str, *, reason="verify_payable_amount_mismatch"
):
    """A manual-review row caused by a FINANCIAL/verification mismatch —
    bot_notify_reason stays None (never reached notification)."""
    with session_factory() as db:
        payment = db.execute(select(Payment).where(Payment.bot_order_id == order_id)).scalar_one()
        payment.status = PaymentStatus.MANUAL_REVIEW.value
        payment.last_error = reason
        payment.manual_review_at = datetime.now(UTC)
        db.commit()


def _make_reconciliation_exhausted(session_factory, settings, order_id: str) -> None:
    with session_factory() as db:
        payment = db.execute(select(Payment).where(Payment.bot_order_id == order_id)).scalar_one()
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(
            seconds=settings.reconciliation_fast_window_seconds + 60
        )
        payment.reconciliation_attempts = settings.reconciliation_max_attempts
        payment.reconciliation_last_error_code = "gateway_not_paid"
        payment.reconciliation_next_at = None
        db.commit()


def _make_waiting(
    session_factory, order_id: str, *, age_seconds=1260, checks=6, last_check_seconds=12
):
    with session_factory() as db:
        payment = db.execute(select(Payment).where(Payment.bot_order_id == order_id)).scalar_one()
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
        payment.reconciliation_attempts = checks
        payment.reconciliation_last_at = datetime.now(UTC) - timedelta(seconds=last_check_seconds)
        db.commit()


def _make_expired(session_factory, settings, order_id: str, *, extra_seconds=0, checks=1):
    with session_factory() as db:
        payment = db.execute(select(Payment).where(Payment.bot_order_id == order_id)).scalar_one()
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(
            seconds=settings.reconciliation_max_age_seconds + extra_seconds
        )
        payment.reconciliation_attempts = checks
        payment.reconciliation_last_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()


def _backdate_notification_entry_event(session_factory, order_id: str, *, event_type: str, when):
    """The notification-age anchor (app.adminbot.queries._notification_age_anchor)
    reads MAX(created_at) from the payment's own bot_notification_queued /
    manual_review_resend_requested / admin_bulk_resend_requested events, NOT
    a Payment column -- so tests that need to simulate an old notification
    cycle must backdate the event that recorded its start, not
    gateway_verified_at/created_at (those are only fallbacks for a row with
    no matching event at all)."""
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        event = db.execute(
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment.id, PaymentEvent.event_type == event_type)
            .order_by(PaymentEvent.id.desc())
            .limit(1)
        ).scalar_one()
        event.created_at = when
        db.commit()


# --- 1/2/3/4: /stuck shows ONLY bot-delivery problems -------------------------


def test_stuck_shows_only_bot_delivery_problems_in_detail(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    """One of each category exists; only the bot-delivery one appears in the
    detailed list, and the summary counts are all correct simultaneously."""
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "so-delivery"
    )
    assert create_order(client, settings, order_id="so-financial").status_code == 200
    _make_financial_manual_review(session_factory, "so-financial")
    assert create_order(client, settings, order_id="so-exhausted").status_code == 200
    _make_reconciliation_exhausted(session_factory, settings, "so-exhausted")
    assert create_order(client, settings, order_id="so-waiting").status_code == 200

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "so-delivery" in text
    assert "so-financial" not in text
    assert "so-exhausted" not in text
    assert "so-waiting" not in text
    assert "خطای ارسال به ربات: 1" in text


def test_non_delivery_manual_review_not_mislabeled_as_bot_error(
    handlers, client, settings, session_factory
):
    """A financial-mismatch manual-review row (bot_notify_reason=None) must
    never appear in /stuck at all, and the exact bot-delivery count is 0."""
    assert create_order(client, settings, order_id="nd-1").status_code == 200
    _make_financial_manual_review(session_factory, "nd-1", reason="verify_user_id_mismatch")
    assert _get(session_factory, "nd-1").bot_notify_reason is None  # sanity

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 0" in text
    assert "nd-1" not in text
    assert "verify_user_id_mismatch" not in text


def test_reconciliation_exhausted_not_mislabeled_as_bot_error(
    handlers, client, settings, session_factory
):
    assert create_order(client, settings, order_id="ex-1").status_code == 200
    _make_reconciliation_exhausted(session_factory, settings, "ex-1")

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 0" in text
    assert "ex-1" not in text
    # Still visible via the "other" line, never mixed into the bot-delivery list.
    assert "سایر موارد نیازمند بررسی: 1" in text


def test_unexpected_status_not_mislabeled_as_bot_error(handlers, session_factory, settings):
    from app.services.stuck_payments import UNEXPECTED_STATE_GRACE_SECONDS

    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="un-1",
                gateway_order_id=990001,
                gateway_user_id=1,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.GETLINK_FAILED.value,
                created_at=(
                    datetime.now(UTC) - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS + 5)
                ),
            )
        )
        db.commit()

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 0" in text
    assert "un-1" not in text
    assert "سایر موارد نیازمند بررسی: 1" in text


# --- 5/6/7: exact counts -------------------------------------------------------


def test_bot_delivery_summary_count_is_exact(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    for i in range(3):
        _make_bot_delivery_failure(
            client, settings, session_factory, stub, bot_stub, notifier, f"cnt-{i}"
        )
    # A non-delivery manual review must not inflate the count.
    assert create_order(client, settings, order_id="cnt-financial").status_code == 200
    _make_financial_manual_review(session_factory, "cnt-financial")

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 3" in text


def test_waiting_total_is_exact(handlers, client, settings, session_factory):
    for i in range(4):
        assert create_order(client, settings, order_id=f"wt-{i}").status_code == 200
    replies = handlers.handle(admin_ctx(), "waiting", [])
    text = "\n".join(replies)
    assert "تعداد کل: 4" in text


def test_expired_total_is_exact(handlers, client, settings, session_factory):
    for i in range(5):
        assert create_order(client, settings, order_id=f"ex-tot-{i}").status_code == 200
        _make_expired(session_factory, settings, f"ex-tot-{i}", extra_seconds=100 + i)
    replies = handlers.handle(admin_ctx(), "expired", [])
    text = "\n".join(replies)
    assert "تعداد کل: 5" in text


# --- 8/9/10/11: /stuck is always exactly ONE message ---------------------------


def test_stuck_default_shows_at_most_ten_entries(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    for i in range(13):
        _make_bot_delivery_failure(
            client, settings, session_factory, stub, bot_stub, notifier, f"many-{i:02d}"
        )
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 13" in text
    assert text.count("⛔ تحویل به ربات ناموفق") == 10
    assert "نمایش 10 مورد از 13 مورد" in text
    assert "+ 3 مورد دیگر نمایش داده نشد" in text


@pytest.mark.parametrize("scenario_count", [0, 1, 13])
def test_stuck_always_returns_exactly_one_message(
    handlers, client, settings, session_factory, stub, bot_stub, notifier, scenario_count
):
    for i in range(scenario_count):
        _make_bot_delivery_failure(
            client, settings, session_factory, stub, bot_stub, notifier, f"one-{i:02d}"
        )
    replies = handlers.handle(admin_ctx(), "stuck", [])
    assert len(replies) == 1
    assert len(replies[0]) <= handlers._settings.admin_bot_max_message_length


def test_stuck_long_order_ids_cannot_force_multiple_messages(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    """Many long (max-length-safe, 128-char) order ids would overflow
    admin_bot_max_message_length if all ten were rendered at once --
    progressive reduction must keep this to exactly one message, never
    split_message's multi-message fallback."""
    for i in range(10):
        order_id = f"long-order-id-{i:02d}-" + ("x" * 100)
        assert len(order_id) <= 128
        _make_bot_delivery_failure(
            client, settings, session_factory, stub, bot_stub, notifier, order_id
        )
    replies = handlers.handle(admin_ctx(), "stuck", [])
    assert len(replies) == 1
    assert len(replies[0]) <= handlers._settings.admin_bot_max_message_length
    assert "خطای ارسال به ربات: 10" in replies[0]


def test_stuck_omitted_count_is_correct(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    for i in range(17):
        _make_bot_delivery_failure(
            client, settings, session_factory, stub, bot_stub, notifier, f"om-{i:02d}"
        )
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "نمایش 10 مورد از 17 مورد" in text
    assert "+ 7 مورد دیگر نمایش داده نشد" in text


def test_stuck_zero_bot_errors_shows_healthy_state(handlers):
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "✅ وضعیت کلی: سالم" in text
    assert "✅ خطایی در تحویل به ربات وجود ندارد." in text
    assert "⛔" not in text


# --- 13/14/15/16: /waiting and /expired default/N -----------------------------


def test_waiting_default_returns_ten_max(handlers, client, settings, session_factory):
    for i in range(15):
        assert create_order(client, settings, order_id=f"wd-{i:02d}").status_code == 200
    replies = handlers.handle(admin_ctx(), "waiting", [])
    text = "\n".join(replies)
    assert text.count("مبلغ: 10,000 تومان") == 10
    assert "نمایش 10 مورد از 15 مورد" in text


def test_waiting_n_respects_n(handlers, client, settings, session_factory):
    for i in range(15):
        assert create_order(client, settings, order_id=f"wn-{i:02d}").status_code == 200
    replies = handlers.handle(admin_ctx(), "waiting", ["4"])
    text = "\n".join(replies)
    assert text.count("مبلغ: 10,000 تومان") == 4
    assert "نمایش 4 مورد از 15 مورد" in text


def test_expired_default_returns_ten_max(handlers, client, settings, session_factory):
    for i in range(15):
        assert create_order(client, settings, order_id=f"ed-{i:02d}").status_code == 200
        _make_expired(session_factory, settings, f"ed-{i:02d}", extra_seconds=100 + i)
    replies = handlers.handle(admin_ctx(), "expired", [])
    text = "\n".join(replies)
    assert text.count("مبلغ: 10,000 تومان") == 10
    assert "نمایش 10 مورد از 15 مورد" in text


def test_expired_n_respects_n(handlers, client, settings, session_factory):
    for i in range(15):
        assert create_order(client, settings, order_id=f"en-{i:02d}").status_code == 200
        _make_expired(session_factory, settings, f"en-{i:02d}", extra_seconds=100 + i)
    replies = handlers.handle(admin_ctx(), "expired", ["6"])
    text = "\n".join(replies)
    assert text.count("مبلغ: 10,000 تومان") == 6
    assert "نمایش 6 مورد از 15 مورد" in text


# --- 17: N validation, 1..50 only ----------------------------------------------


@pytest.mark.parametrize("command", ["waiting", "expired"])
@pytest.mark.parametrize(
    "bad_args",
    [["0"], ["-1"], ["abc"], ["51"], ["1000"], ["1", "2"], ["۱۰"], ["5.5"], ["+5"]],  # noqa: RUF001
    ids=[
        "zero", "negative", "non_number", "over_max", "way_over",
        "multi_arg", "persian_digits", "decimal", "signed",
    ],
)
def test_n_validation_rejects_invalid_arguments(handlers, command, bad_args):
    replies = handlers.handle(admin_ctx(), command, bad_args)
    assert len(replies) == 1
    assert "فرمت صحیح" in replies[0]
    assert f"/{command} [1-50]" in replies[0]


@pytest.mark.parametrize("command", ["waiting", "expired"])
@pytest.mark.parametrize("value", ["1", "50", "10", "25"])
def test_n_validation_accepts_boundary_values(handlers, command, value):
    replies = handlers.handle(admin_ctx(), command, [value])
    assert "فرمت صحیح" not in "\n".join(replies)


# --- 18: ordering is deterministic and tested ----------------------------------


def test_waiting_orders_longest_waiting_first(handlers, client, settings, session_factory):
    """Longest-waiting (oldest link-age anchor) first -- the most urgent
    payments an operator should look at, per the product decision."""
    assert create_order(client, settings, order_id="ord-w-newest").status_code == 200
    assert create_order(client, settings, order_id="ord-w-oldest").status_code == 200
    assert create_order(client, settings, order_id="ord-w-middle").status_code == 200
    _make_waiting(session_factory, "ord-w-newest", age_seconds=200)
    _make_waiting(session_factory, "ord-w-oldest", age_seconds=3000)
    _make_waiting(session_factory, "ord-w-middle", age_seconds=1500)

    [text] = handlers.handle(admin_ctx(), "waiting", [])
    positions = {
        name: text.index(name)
        for name in ("ord-w-oldest", "ord-w-middle", "ord-w-newest")
    }
    assert positions["ord-w-oldest"] < positions["ord-w-middle"] < positions["ord-w-newest"]


def test_expired_orders_most_recently_expired_first(handlers, client, settings, session_factory):
    """MOST RECENTLY expired first -- showing the oldest legacy expired rows
    by default would bury what an operator actually needs to see."""
    assert create_order(client, settings, order_id="ord-e-justexpired").status_code == 200
    assert create_order(client, settings, order_id="ord-e-longago").status_code == 200
    assert create_order(client, settings, order_id="ord-e-middle").status_code == 200
    _make_expired(session_factory, settings, "ord-e-justexpired", extra_seconds=5)
    _make_expired(session_factory, settings, "ord-e-longago", extra_seconds=90000)
    _make_expired(session_factory, settings, "ord-e-middle", extra_seconds=3600)

    [text] = handlers.handle(admin_ctx(), "expired", [])
    positions = {
        name: text.index(name)
        for name in ("ord-e-justexpired", "ord-e-middle", "ord-e-longago")
    }
    assert positions["ord-e-justexpired"] < positions["ord-e-middle"] < positions["ord-e-longago"]


# --- 19: HTML escaping stays safe -----------------------------------------------


def test_html_escaping_in_stuck_waiting_expired(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    order_id = 'evil<script>alert(1)</script>&"quote'[:128]
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    bot_stub.result = httpx.ReadTimeout("t")
    run_pass(session_factory, notifier, settings)
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "<script>" not in text
    assert "&lt;script&gt;" in text

    order_id_2 = 'wait<b>bold</b>'[:128]
    assert create_order(client, settings, order_id=order_id_2).status_code == 200
    replies = handlers.handle(admin_ctx(), "waiting", [])
    text2 = "\n".join(replies)
    assert "<b>bold</b>" not in text2 or "wait&lt;b&gt;bold&lt;/b&gt;" in text2

    order_id_3 = 'exp<i>italic</i>'[:128]
    assert create_order(client, settings, order_id=order_id_3).status_code == 200
    _make_expired(session_factory, settings, order_id_3)
    replies = handlers.handle(admin_ctx(), "expired", [])
    text3 = "\n".join(replies)
    assert "exp&lt;i&gt;italic&lt;/i&gt;" in text3


# --- 20: no secret / raw-body / URL leakage -------------------------------------


def _all_secrets(admin_settings):
    return [
        admin_settings.inbound_api_key,
        admin_settings.callback_hmac_secret,
        admin_settings.centralpay_getlink_api_key,
        admin_settings.centralpay_verify_api_key,
        admin_settings.bot_notify_token,
        admin_settings.admin_bot_token,
    ]


def test_no_secrets_or_urls_exposed_in_stuck_waiting_expired(
    handlers, admin_settings, client, settings, session_factory, stub, bot_stub, notifier
):
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "sec-delivery"
    )
    assert create_order(client, settings, order_id="sec-waiting").status_code == 200
    assert create_order(client, settings, order_id="sec-expired").status_code == 200
    _make_expired(session_factory, settings, "sec-expired")

    all_text = []
    all_text.extend(handlers.handle(admin_ctx(), "stuck", []))
    all_text.extend(handlers.handle(admin_ctx(), "waiting", []))
    all_text.extend(handlers.handle(admin_ctx(), "expired", []))
    combined = "\n".join(all_text)

    for secret in _all_secrets(admin_settings):
        assert secret not in combined
    assert "redirect_url" not in combined
    assert "sig=" not in combined
    assert "callback_token" not in combined
    payment = _get(session_factory, "sec-delivery")
    assert payment.redirect_url is not None
    assert payment.redirect_url not in combined


# --- 21: existing CLI `centralpay stuck` semantics unchanged -------------------


def test_cli_stuck_semantics_unchanged(client, settings, session_factory, stub, monkeypatch):
    import app.cli as cli_module
    from app.cli import main as cli_main

    monkeypatch.setattr(cli_module, "Settings", lambda: settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: session_factory)

    assert create_order(client, settings, order_id="cli-stuck-1").status_code == 200
    # The CLI's own stuck command still exercises the full three-category
    # StuckOverview shared with the (now narrower) admin bot /stuck.
    assert cli_main(["stuck"]) == 0


# --- 22: existing /manual_review, /status, /errors, /retry_queue unaffected ---


def test_no_mutation_across_all_admin_commands_including_new_ones(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "nomut-1"
    )
    assert create_order(client, settings, order_id="nomut-2").status_code == 200

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
                    Payment.reconciliation_attempts,
                    Payment.reconciliation_next_at,
                    Payment.updated_at,
                ).order_by(Payment.id)
            ).all()

    before = snapshot()
    commands: list[tuple[str, list[str]]] = [
        ("manual_review", []),
        ("status", []),
        ("errors", []),
        ("retry_queue", []),
        ("stuck", []),
        ("waiting", []),
        ("expired", []),
    ]
    for command, args in commands:
        handlers.handle(admin_ctx(), command, args)
    assert snapshot() == before


def test_manual_review_status_errors_retry_queue_still_work(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "reg-1"
    )
    assert "reg-1" in "\n".join(handlers.handle(admin_ctx(), "manual_review", []))
    assert "بررسی دستی" in "\n".join(handlers.handle(admin_ctx(), "status", []))
    assert "bot_timeout_ambiguous" in "\n".join(handlers.handle(admin_ctx(), "errors", []))
    retry_text = "\n".join(handlers.handle(admin_ctx(), "retry_queue", []))
    assert "صف ارسال" in retry_text


# ============================================================================
# PR #57 review fixes
# ============================================================================

# --- fix 1: count_other_attention must include non-delivery manual review ----


def test_other_attention_counts_a_lone_financial_manual_review(
    handlers, client, settings, session_factory
):
    assert create_order(client, settings, order_id="oa-fin").status_code == 200
    _make_financial_manual_review(
        session_factory, "oa-fin", reason="verify_payable_amount_mismatch"
    )

    from app.adminbot import queries
    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert queries.bot_delivery_snapshot(db, now=now).total == 0
        assert stuck_service.count_other_attention(db, settings, now=now) == 1

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "سایر موارد نیازمند بررسی: 1" in text
    assert "oa-fin" not in text


@pytest.mark.parametrize(
    "reason",
    [
        "verify_payable_amount_mismatch",
        "verify_user_id_mismatch",
        "verify_missing_reference_id",
        "verify_invalid_reference_id",
        "reference_id_collision",
    ],
)
def test_other_attention_counts_every_financial_reason(
    handlers, client, settings, session_factory, reason
):
    assert create_order(client, settings, order_id="oa-reason").status_code == 200
    _make_financial_manual_review(session_factory, "oa-reason", reason=reason)

    from app.adminbot import queries
    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert queries.count_non_delivery_manual_reviews(db) == 1
        assert stuck_service.count_other_attention(db, settings, now=now) == 1


def test_other_attention_does_not_count_bot_delivery_manual_review(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    """A bot-delivery manual review must count in bot_delivery_total ONLY --
    never leak into other_attention (no double counting)."""
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "oa-delivery"
    )

    from app.adminbot import queries
    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert queries.bot_delivery_snapshot(db, now=now).total == 1
        assert stuck_service.count_other_attention(db, settings, now=now) == 0


def test_other_attention_counts_reconciliation_exhausted_alone(
    handlers, client, settings, session_factory
):
    assert create_order(client, settings, order_id="oa-exh").status_code == 200
    _make_reconciliation_exhausted(session_factory, settings, "oa-exh")

    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert stuck_service.count_other_attention(db, settings, now=now) == 1


def test_other_attention_counts_unexpected_status_alone(handlers, session_factory, settings):
    from app.services.stuck_payments import UNEXPECTED_STATE_GRACE_SECONDS

    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="oa-unexpected",
                gateway_order_id=990101,
                gateway_user_id=1,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.GETLINK_FAILED.value,
                created_at=(
                    datetime.now(UTC) - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS + 5)
                ),
            )
        )
        db.commit()

    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert stuck_service.count_other_attention(db, settings, now=now) == 1


def test_other_attention_exact_arithmetic_with_all_four_categories_and_no_double_counting(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    """The preferred invariant: bot_delivery_total + other_total equals the
    total across all four attention-bearing categories, with each row
    counted in EXACTLY one of the two buckets."""
    from app.services.stuck_payments import UNEXPECTED_STATE_GRACE_SECONDS

    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "arith-delivery"
    )
    assert create_order(client, settings, order_id="arith-financial").status_code == 200
    _make_financial_manual_review(session_factory, "arith-financial")
    assert create_order(client, settings, order_id="arith-exhausted").status_code == 200
    _make_reconciliation_exhausted(session_factory, settings, "arith-exhausted")
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="arith-unexpected",
                gateway_order_id=990102,
                gateway_user_id=1,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.CREATED.value,
                created_at=(
                    datetime.now(UTC) - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS + 5)
                ),
            )
        )
        db.commit()
    # Non-attention noise that must not be counted by either bucket.
    assert create_order(client, settings, order_id="arith-waiting").status_code == 200

    from app.adminbot import queries
    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        bot_delivery_total = queries.bot_delivery_snapshot(db, now=now).total
        other_total = stuck_service.count_other_attention(db, settings, now=now)

    assert bot_delivery_total == 1
    assert other_total == 3
    assert bot_delivery_total + other_total == 4

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 1" in text
    assert "سایر موارد نیازمند بررسی: 3" in text
    assert "arith-delivery" in text
    for hidden in ("arith-financial", "arith-exhausted", "arith-unexpected", "arith-waiting"):
        assert hidden not in text


# --- audit: resolved manual review rows must never count as attention --------


def test_resolved_financial_manual_review_excluded_from_other_attention(
    handlers, client, settings, session_factory
):
    assert create_order(client, settings, order_id="resolved-fin").status_code == 200
    _make_financial_manual_review(session_factory, "resolved-fin")
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "resolved-fin")
        ).scalar_one()
        payment.review_resolved_at = datetime.now(UTC)
        payment.review_resolution = "resolved-manually"
        db.commit()

    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert stuck_service.count_other_attention(db, settings, now=now) == 0

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "سایر موارد نیازمند بررسی" not in text  # omitted entirely when 0
    assert "resolved-fin" not in text


def test_other_attention_excludes_waiting_payments(client, settings, session_factory):
    assert create_order(client, settings, order_id="oa-waiting").status_code == 200
    _make_waiting(session_factory, "oa-waiting")

    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert stuck_service.count_other_attention(db, settings, now=now) == 0


def test_other_attention_excludes_expired_payments(client, settings, session_factory):
    assert create_order(client, settings, order_id="oa-expired").status_code == 200
    _make_expired(session_factory, settings, "oa-expired")

    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        assert stuck_service.count_other_attention(db, settings, now=now) == 0


# ============================================================================
# PR #57 fourth review: count_other_attention's union in ONE SQL statement
# ============================================================================

# --- fix: reconciliation-exhausted / unexpected-status / non-delivery ------
# --- manual-review must be counted by ONE `WHERE ... OR ...` SELECT, not ---
# --- three separate COUNTs summed in Python -- a payment transitioning ----
# --- between categories mid-sequence could otherwise be counted twice -----


def test_old_three_statement_design_could_double_count_a_transitioning_row(
    client, settings, session_factory
):
    """Regression proof for the exact race described in review: reproduce
    the PRE-FIX shape `count_other_attention` used to have -- a separate
    COUNT for reconciliation-exhausted, then a separate COUNT for
    unexpected-status, then a separate COUNT for non-delivery manual
    review, summed in Python -- with a worker-style write (a separate
    session, committed) moving a reconciliation-exhausted payment into an
    open financial manual review in the gap between the first and third
    statements. No sleeping, no real threads: program order alone
    reproduces the race. The first COUNT sees the payment while it is
    still exhausted; the worker moves it to manual_review; the third COUNT
    sees the SAME payment again under its new state -- one payment,
    counted twice."""
    from sqlalchemy import func

    from app.adminbot.queries import non_delivery_manual_review_conditions
    from app.services.reconciliation import reconciliation_exhausted_conditions
    from app.services.stuck_payments import _UNEXPECTED_STATUSES, UNEXPECTED_STATE_GRACE_SECONDS

    order_id = "race-other-attention"
    assert create_order(client, settings, order_id=order_id).status_code == 200
    _make_reconciliation_exhausted(session_factory, settings, order_id)
    now = datetime.now(UTC)
    exhausted_conditions = reconciliation_exhausted_conditions(settings, now=now)
    unexpected_cutoff = now - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS)

    with session_factory() as db:
        # Statement 1 of the OLD design: reconciliation-exhausted COUNT.
        exhausted_total = db.execute(
            select(func.count(Payment.id)).where(*exhausted_conditions)
        ).scalar_one()
        db.commit()  # end the read cleanly before a concurrent writer commits

        # A worker moves the payment out of the exhausted shape and into an
        # open financial manual review in the gap between the OLD design's
        # statements -- its own session, its own commit.
        with session_factory() as worker_db:
            worker_payment = worker_db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            worker_payment.status = PaymentStatus.MANUAL_REVIEW.value
            worker_payment.last_error = "verify_payable_amount_mismatch"
            worker_payment.manual_review_at = datetime.now(UTC)
            worker_db.commit()

        # Statement 2 of the OLD design: unexpected-status COUNT -- 0 here,
        # included only to reproduce the exact three-statement shape.
        unexpected_total = db.execute(
            select(func.count(Payment.id)).where(
                Payment.status.in_(_UNEXPECTED_STATUSES), Payment.created_at <= unexpected_cutoff
            )
        ).scalar_one()

        # Statement 3 of the OLD design: non-delivery-manual-review COUNT --
        # now ALSO counts the same payment, since the worker's commit landed.
        non_delivery_total = db.execute(
            select(func.count(Payment.id)).where(*non_delivery_manual_review_conditions())
        ).scalar_one()

    old_design_total = exhausted_total + unexpected_total + non_delivery_total
    assert exhausted_total == 1
    assert non_delivery_total == 1
    assert old_design_total == 2  # one payment, double-counted


def test_count_other_attention_issues_exactly_one_sql_statement(
    client, settings, session_factory, engine
):
    """Structural proof the fused union query is a single round trip."""
    from sqlalchemy import event

    from app.services import stuck_payments as stuck_service

    assert create_order(client, settings, order_id="single-stmt-oa").status_code == 200
    _make_financial_manual_review(session_factory, "single-stmt-oa")

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if "payments" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        with session_factory() as db:
            now = datetime.now(UTC)
            total = stuck_service.count_other_attention(db, settings, now=now)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(statements) == 1  # one round trip -- no gap for a race
    assert total == 1


def test_count_other_attention_single_statement_cannot_double_count_a_transitioning_row(
    client, settings, session_factory
):
    """Same setup as the OLD-design reproduction above, but exercised
    through the new, single-statement `count_other_attention`: the same
    payment moved from reconciliation-exhausted to an open financial
    manual review BEFORE the (one) call. Because the fused query evaluates
    all three conditions against one consistent read of the row's CURRENT
    state in a single statement, there is no gap between sub-queries for a
    transition to land in -- the payment is counted exactly once, never
    twice."""
    order_id = "no-race-other-attention"
    assert create_order(client, settings, order_id=order_id).status_code == 200
    _make_reconciliation_exhausted(session_factory, settings, order_id)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.status = PaymentStatus.MANUAL_REVIEW.value
        payment.last_error = "verify_payable_amount_mismatch"
        payment.manual_review_at = datetime.now(UTC)
        db.commit()

    from app.services import stuck_payments as stuck_service

    with session_factory() as db:
        now = datetime.now(UTC)
        total = stuck_service.count_other_attention(db, settings, now=now)

    assert total == 1  # never 2 -- the payment is in exactly one state, counted once


# --- fix 2: overall health state considers bot-delivery OR other attention ---


def test_health_nothing_exists_is_healthy(handlers):
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "✅ وضعیت کلی: سالم" in text


def test_health_only_waiting_exists_is_healthy(handlers, client, settings, session_factory):
    assert create_order(client, settings, order_id="health-waiting").status_code == 200
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "✅ وضعیت کلی: سالم" in text


def test_health_only_expired_exists_is_healthy(handlers, client, settings, session_factory):
    assert create_order(client, settings, order_id="health-expired").status_code == 200
    _make_expired(session_factory, settings, "health-expired")
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "✅ وضعیت کلی: سالم" in text


def test_health_only_bot_delivery_failure_is_attention(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "health-delivery"
    )
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "🟠 وضعیت کلی: نیازمند توجه" in text


def test_health_only_financial_manual_review_is_attention(
    handlers, client, settings, session_factory
):
    assert create_order(client, settings, order_id="health-fin").status_code == 200
    _make_financial_manual_review(session_factory, "health-fin")
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "🟠 وضعیت کلی: نیازمند توجه" in text


def test_health_only_reconciliation_exhausted_is_attention(
    handlers, client, settings, session_factory
):
    assert create_order(client, settings, order_id="health-exh").status_code == 200
    _make_reconciliation_exhausted(session_factory, settings, "health-exh")
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "🟠 وضعیت کلی: نیازمند توجه" in text


def test_health_only_unexpected_status_is_attention(handlers, session_factory):
    from app.services.stuck_payments import UNEXPECTED_STATE_GRACE_SECONDS

    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="health-unexpected",
                gateway_order_id=990103,
                gateway_user_id=1,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.GATEWAY_VERIFIED.value,
                created_at=(
                    datetime.now(UTC) - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS + 5)
                ),
            )
        )
        db.commit()
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "🟠 وضعیت کلی: نیازمند توجه" in text


def test_health_mixed_conditions_is_attention(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "health-mixed-delivery"
    )
    assert create_order(client, settings, order_id="health-mixed-fin").status_code == 200
    _make_financial_manual_review(session_factory, "health-mixed-fin")
    assert create_order(client, settings, order_id="health-mixed-wait").status_code == 200
    assert create_order(client, settings, order_id="health-mixed-exp").status_code == 200
    _make_expired(session_factory, settings, "health-mixed-exp")
    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "🟠 وضعیت کلی: نیازمند توجه" in text


# --- fix 3: /stuck rejects any argument, no silent ignore ---------------------


@pytest.mark.parametrize("bad_args", [["10"], ["abc"], ["1", "2"], ["50"], ["0"]])
def test_stuck_rejects_any_argument(
    handlers, client, settings, session_factory, stub, bot_stub, notifier, bad_args
):
    # A bot-delivery problem exists so a silently-ignored argument would
    # otherwise still render a normal (wrong) report instead of the error.
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "argtest-1"
    )

    def snapshot():
        with session_factory() as db:
            return db.execute(
                select(Payment.id, Payment.status, Payment.updated_at).order_by(Payment.id)
            ).all()

    before = snapshot()
    replies = handlers.handle(admin_ctx(), "stuck", bad_args)
    assert replies == ["فرمت صحیح:\n/stuck"]
    assert snapshot() == before


def test_stuck_plain_command_still_works(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, "plain-1"
    )
    replies = handlers.handle(admin_ctx(), "stuck", [])
    assert "فرمت صحیح" not in "\n".join(replies)
    assert "plain-1" in "\n".join(replies)


# ============================================================================
# PR #57 third review: count + detail list from ONE SQL statement
# ============================================================================

# --- fix: bot-delivery / waiting / expired total+entries must come from ---
# --- ONE SQL statement (window COUNT(*) OVER()), not two separate reads ---


def test_old_two_statement_design_could_disagree_under_concurrent_mutation(
    client, settings, session_factory, stub
):
    """Regression proof for the exact race described in review: reproduce
    the PRE-FIX shape -- a separate COUNT, then a separate SELECT, against
    the same bot-delivery-pending predicate `bot_delivery_snapshot` now
    combines into one statement -- with a notification-worker-style write
    (a separate session, committed) landing deterministically in the gap
    between the two statements. No sleeping, no real threads: the program
    order alone reproduces the race. The count sees the stale pending row;
    the worker delivers it; the list SELECT no longer sees it -- exactly
    the disagreement `bot_delivery_snapshot` (below) cannot produce."""
    from sqlalchemy import func

    order_id = "race-old-design"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    pinned_now = datetime.now(UTC)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = pinned_now - timedelta(minutes=45)
        db.commit()

    pending_cutoff = pinned_now - timedelta(minutes=30)
    conditions = (Payment.status == "bot_notify_pending", Payment.created_at <= pending_cutoff)

    with session_factory() as db:
        # Statement 1 of the OLD design: the summary COUNT.
        count = db.execute(select(func.count(Payment.id)).where(*conditions)).scalar_one()
        db.commit()  # end the read cleanly before a concurrent writer commits

        # The notification worker successfully delivers the payment in the
        # gap between the OLD design's two statements -- its own session,
        # its own commit, exactly like the real worker process.
        with session_factory() as worker_db:
            worker_payment = worker_db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            worker_payment.status = PaymentStatus.BOT_NOTIFY_ACCEPTED.value
            worker_db.commit()

        # Statement 2 of the OLD design: the detail SELECT.
        entries = list(db.execute(select(Payment).where(*conditions)).scalars())

    assert count == 1
    assert len(entries) == 0  # the disagreement: count said 1, detail rows said 0


def test_bot_delivery_snapshot_reads_total_and_entries_from_one_statement(
    client, settings, session_factory, stub, engine
):
    """Structural proof `bot_delivery_snapshot` cannot exhibit the race
    above: it issues exactly ONE SQL statement -- a window `COUNT(*)
    OVER()` alongside the LIMIT'd rows -- so there is no gap for a
    concurrent writer to land in between a count and a list."""
    from sqlalchemy import event

    from app.adminbot import queries

    order_id = "single-stmt-bd"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    pinned_now = datetime.now(UTC)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = pinned_now - timedelta(minutes=45)
        payment.gateway_verified_at = pinned_now - timedelta(minutes=45)
        db.commit()
    _backdate_notification_entry_event(
        session_factory,
        order_id,
        event_type="bot_notification_queued",
        when=pinned_now - timedelta(minutes=45),
    )

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if "payments" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        with session_factory() as db:
            snapshot = queries.bot_delivery_snapshot(db, now=pinned_now, limit=30)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(statements) == 1  # one round trip -- no gap for a race
    assert snapshot.total == 1
    assert len(snapshot.entries) == 1


def test_waiting_snapshot_reads_total_and_entries_from_one_statement(
    client, settings, session_factory, engine
):
    """Same structural proof as bot_delivery_snapshot, for waiting_snapshot."""
    from sqlalchemy import event

    from app.services import stuck_payments as stuck_service

    assert create_order(client, settings, order_id="single-stmt-w").status_code == 200

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if "payments" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        with session_factory() as db:
            now = datetime.now(UTC)
            snapshot = stuck_service.waiting_snapshot(db, settings, now=now, limit=30)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(statements) == 1
    assert snapshot.total == 1
    assert len(snapshot.entries) == 1


def test_expired_snapshot_reads_total_and_entries_from_one_statement(
    client, settings, session_factory, engine
):
    """Same structural proof as bot_delivery_snapshot, for expired_snapshot."""
    from sqlalchemy import event

    from app.services import stuck_payments as stuck_service

    assert create_order(client, settings, order_id="single-stmt-e").status_code == 200
    _make_expired(session_factory, settings, "single-stmt-e")

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if "payments" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        with session_factory() as db:
            now = datetime.now(UTC)
            snapshot = stuck_service.expired_snapshot(db, settings, now=now, limit=30)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(statements) == 1
    assert snapshot.total == 1
    assert len(snapshot.entries) == 1


# --- structural invariants: total >= len(entries); total == len(entries) --
# --- when total <= limit; LIMIT never reduces the window total ------------


def test_bot_delivery_snapshot_total_equals_entries_when_under_limit(
    client, settings, session_factory, stub, bot_stub, notifier
):
    from app.adminbot import queries

    for i in range(3):
        _make_bot_delivery_failure(
            client, settings, session_factory, stub, bot_stub, notifier, f"bdsnap-small-{i}"
        )
    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, limit=30)
    assert snapshot.total == 3
    assert len(snapshot.entries) == 3
    assert snapshot.total >= len(snapshot.entries)


def test_bot_delivery_snapshot_total_not_reduced_by_limit(
    handlers, client, settings, session_factory, stub, bot_stub, notifier
):
    """17 bot-delivery-problem rows, limit=STUCK_DETAIL_MAX (10): the
    window total must report the full 17, never the truncated 10 -- LIMIT
    must not affect the COUNT."""
    from app.adminbot import queries
    from app.adminbot.commands import STUCK_DETAIL_MAX

    for i in range(17):
        _make_bot_delivery_failure(
            client, settings, session_factory, stub, bot_stub, notifier, f"bdsnap-big-{i:02d}"
        )
    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, limit=STUCK_DETAIL_MAX)
    assert snapshot.total == 17
    assert len(snapshot.entries) == 10
    assert snapshot.total >= len(snapshot.entries)

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 17" in text
    assert text.count("⛔ تحویل به ربات ناموفق") == 10
    assert "نمایش 10 مورد از 17 مورد" in text


def test_waiting_snapshot_total_and_entries_invariants(client, settings, session_factory):
    """total >= len(entries) always; total == len(entries) exactly when
    total <= limit (never truncated, never inflated)."""
    from app.services import stuck_payments as stuck_service

    for i in range(3):
        assert create_order(client, settings, order_id=f"wsnap-small-{i}").status_code == 200
    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = stuck_service.waiting_snapshot(db, settings, now=now, limit=30)
    assert snapshot.total == 3
    assert len(snapshot.entries) == 3
    assert snapshot.total >= len(snapshot.entries)


def test_waiting_snapshot_total_not_reduced_by_limit(client, settings, session_factory):
    """15 waiting rows, limit=10: the window total must report the full
    15, never the truncated 10."""
    from app.services import stuck_payments as stuck_service

    for i in range(15):
        assert create_order(client, settings, order_id=f"wsnap-big-{i:02d}").status_code == 200
    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = stuck_service.waiting_snapshot(db, settings, now=now, limit=10)
    assert snapshot.total == 15
    assert len(snapshot.entries) == 10
    assert snapshot.total >= len(snapshot.entries)


def test_expired_snapshot_total_and_entries_invariants(client, settings, session_factory):
    from app.services import stuck_payments as stuck_service

    for i in range(3):
        assert create_order(client, settings, order_id=f"esnap-small-{i}").status_code == 200
        _make_expired(session_factory, settings, f"esnap-small-{i}", extra_seconds=100 + i)
    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = stuck_service.expired_snapshot(db, settings, now=now, limit=30)
    assert snapshot.total == 3
    assert len(snapshot.entries) == 3
    assert snapshot.total >= len(snapshot.entries)


def test_expired_snapshot_total_not_reduced_by_limit(client, settings, session_factory):
    """15 expired rows, limit=10: the window total must report the full
    15, never the truncated 10."""
    from app.services import stuck_payments as stuck_service

    for i in range(15):
        assert create_order(client, settings, order_id=f"esnap-big-{i:02d}").status_code == 200
        _make_expired(session_factory, settings, f"esnap-big-{i:02d}", extra_seconds=100 + i)
    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = stuck_service.expired_snapshot(db, settings, now=now, limit=10)
    assert snapshot.total == 15
    assert len(snapshot.entries) == 10
    assert snapshot.total >= len(snapshot.entries)


# --- 30-minute pending-cutoff boundary, at the snapshot level -------------


def test_bot_delivery_snapshot_count_and_entries_agree_at_pending_cutoff_boundary(
    client, settings, session_factory, stub
):
    """bot_delivery_snapshot's total and entries are read from the SAME
    result set here -- proving they agree exactly at, just before, and
    just after the 30-minute bot_notify_pending staleness cutoff. The
    cutoff predicate is `gateway_verified_at <= pending_cutoff` (the
    notification-phase-entry anchor, not order-creation time), so a row
    verified exactly 30 minutes before `now` IS included."""
    from app.adminbot import queries

    pinned_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    ages = {
        "cutoff-exact": timedelta(minutes=30),
        "cutoff-younger": timedelta(minutes=29, seconds=59),
        "cutoff-older": timedelta(minutes=30, seconds=1),
    }
    for order_id in ages:
        make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    with session_factory() as db:
        for order_id, age in ages.items():
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            payment.created_at = pinned_now - age
            payment.gateway_verified_at = pinned_now - age
        db.commit()
    for order_id, age in ages.items():
        _backdate_notification_entry_event(
            session_factory, order_id, event_type="bot_notification_queued", when=pinned_now - age
        )

    with session_factory() as db:
        snapshot = queries.bot_delivery_snapshot(db, now=pinned_now, limit=30)

    assert snapshot.total == 2
    assert len(snapshot.entries) == snapshot.total  # same statement -> always agree
    included = {entry.payment.bot_order_id for entry in snapshot.entries}
    assert included == {"cutoff-exact", "cutoff-older"}
    assert "cutoff-younger" not in included


def test_stuck_count_and_detail_share_one_snapshot_at_cutoff_boundary(
    handlers, client, settings, session_factory, stub, monkeypatch
):
    """End-to-end: /stuck must build its summary count, detail list, and
    health line from ONE captured `now`, with the count and detail rows
    additionally coming from `bot_delivery_snapshot`'s single statement.
    Proven with a monkeypatched clock and pinned created_at values
    straddling the 30-minute staleness boundary -- no sleeping."""
    pinned_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return pinned_now

    ages = {
        "snap-exact": timedelta(minutes=30),
        "snap-younger": timedelta(minutes=29, seconds=59),
        "snap-older": timedelta(minutes=30, seconds=1),
    }
    for order_id in ages:
        make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    with session_factory() as db:
        for order_id, age in ages.items():
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            payment.created_at = pinned_now - age
            payment.gateway_verified_at = pinned_now - age
        db.commit()
    for order_id, age in ages.items():
        _backdate_notification_entry_event(
            session_factory, order_id, event_type="bot_notification_queued", when=pinned_now - age
        )

    monkeypatch.setattr("app.adminbot.commands.datetime", _FixedDateTime)

    [text] = handlers.handle(admin_ctx(), "stuck", [])
    assert "خطای ارسال به ربات: 2" in text
    assert text.count("⛔ تحویل به ربات ناموفق") == 2
    assert "snap-exact" in text
    assert "snap-older" in text
    assert "snap-younger" not in text
    # The summary count and the rendered detail count must never disagree --
    # in particular never claim zero bot-delivery errors while still
    # listing entries below it, or vice versa.
    assert "نمایش 1 مورد از 0 مورد" not in text
    assert "🟠 وضعیت کلی: نیازمند توجه" in text


def test_bot_delivery_snapshot_tie_broken_by_id_when_pending_created_at_ties(
    client, settings, session_factory, stub
):
    """SQL-visible ordering contract for /stuck's own snapshot query: stale
    bot_notify_pending rows sharing the exact same notification-age anchor
    (gateway_verified_at) sort by ascending Payment.id -- the same
    tie-break convention as /waiting and /expired -- rather than being left
    database-dependent."""
    from app.adminbot import queries

    pinned_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    tied_verified_at = pinned_now - timedelta(minutes=45)
    order_ids = ["tie-bd-a", "tie-bd-b", "tie-bd-c"]
    for order_id in order_ids:
        make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    with session_factory() as db:
        for order_id in order_ids:
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            payment.created_at = tied_verified_at
            payment.gateway_verified_at = tied_verified_at
        db.commit()
    for order_id in order_ids:
        _backdate_notification_entry_event(
            session_factory,
            order_id,
            event_type="bot_notification_queued",
            when=tied_verified_at,
        )

    with session_factory() as db:
        snapshot = queries.bot_delivery_snapshot(db, now=pinned_now, limit=50)

    tied_order = [
        entry.payment.bot_order_id
        for entry in snapshot.entries
        if entry.payment.bot_order_id in order_ids
    ]
    assert tied_order == order_ids  # ascending Payment.id == creation order


# --- fix 2 (previous round): /waiting and /expired equal-anchor ties ------
# --- are deterministic -- re-verified against the new snapshot functions --


def test_waiting_equal_anchor_ties_broken_by_ascending_id(client, settings, session_factory):
    """SQL-visible ordering contract: WAITING_GATEWAY rows sharing the exact
    same link-age anchor timestamp sort by ascending Payment.id, not by
    whatever order the database happens to return ties in."""
    from app.services import stuck_payments as stuck_service

    tied_anchor = datetime.now(UTC) - timedelta(seconds=1500)
    order_ids = ["tie-w-a", "tie-w-b", "tie-w-c"]
    for order_id in order_ids:
        assert create_order(client, settings, order_id=order_id).status_code == 200
    with session_factory() as db:
        for order_id in order_ids:
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            payment.callback_token_issued_at = tied_anchor
        db.commit()

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = stuck_service.waiting_snapshot(db, settings, now=now, limit=50)

    tied_order = [
        entry.payment.bot_order_id
        for entry in snapshot.entries
        if entry.payment.bot_order_id in order_ids
    ]
    assert tied_order == order_ids  # ascending Payment.id == creation order


def test_expired_equal_anchor_ties_broken_by_descending_id(client, settings, session_factory):
    """SQL-visible ordering contract: EXPIRED rows sharing the exact same
    link-age anchor timestamp sort by descending Payment.id, not by
    whatever order the database happens to return ties in."""
    from app.services import stuck_payments as stuck_service

    tied_anchor = datetime.now(UTC) - timedelta(
        seconds=settings.reconciliation_max_age_seconds + 500
    )
    order_ids = ["tie-e-a", "tie-e-b", "tie-e-c"]
    for order_id in order_ids:
        assert create_order(client, settings, order_id=order_id).status_code == 200
    with session_factory() as db:
        for order_id in order_ids:
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            payment.callback_token_issued_at = tied_anchor
        db.commit()

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = stuck_service.expired_snapshot(db, settings, now=now, limit=50)

    tied_order = [
        entry.payment.bot_order_id
        for entry in snapshot.entries
        if entry.payment.bot_order_id in order_ids
    ]
    assert tied_order == list(reversed(order_ids))  # descending Payment.id


def test_waiting_command_orders_equal_anchor_ties_deterministically(
    handlers, client, settings, session_factory
):
    """Same tie-break, exercised end-to-end through /waiting."""
    tied_anchor = datetime.now(UTC) - timedelta(seconds=1500)
    order_ids = ["tie-cmd-w-a", "tie-cmd-w-b", "tie-cmd-w-c"]
    for order_id in order_ids:
        assert create_order(client, settings, order_id=order_id).status_code == 200
    with session_factory() as db:
        for order_id in order_ids:
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            payment.callback_token_issued_at = tied_anchor
        db.commit()

    [text] = handlers.handle(admin_ctx(), "waiting", [])
    positions = {order_id: text.index(order_id) for order_id in order_ids}
    assert positions["tie-cmd-w-a"] < positions["tie-cmd-w-b"] < positions["tie-cmd-w-c"]


def test_expired_command_orders_equal_anchor_ties_deterministically(
    handlers, client, settings, session_factory
):
    """Same tie-break, exercised end-to-end through /expired."""
    tied_anchor = datetime.now(UTC) - timedelta(
        seconds=settings.reconciliation_max_age_seconds + 500
    )
    order_ids = ["tie-cmd-e-a", "tie-cmd-e-b", "tie-cmd-e-c"]
    for order_id in order_ids:
        assert create_order(client, settings, order_id=order_id).status_code == 200
    with session_factory() as db:
        for order_id in order_ids:
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            payment.callback_token_issued_at = tied_anchor
        db.commit()

    [text] = handlers.handle(admin_ctx(), "expired", [])
    positions = {order_id: text.index(order_id) for order_id in order_ids}
    assert positions["tie-cmd-e-c"] < positions["tie-cmd-e-b"] < positions["tie-cmd-e-a"]


# ============================================================================
# Hotfix: bot_notify_pending staleness must anchor on the START OF THE
# CURRENT NOTIFICATION CYCLE, never bare order-creation time (created_at)
# and never bare gateway_verified_at either.
#
# gateway_verified_at is a ONE-TIME financial fact, set once at the original
# gateway verification and never touched again. queue_notification always
# sets it in the same transaction as the FIRST entry into bot_notify_pending,
# so it's a fine anchor for that initial cycle -- but app.ops "review resend"
# and app.services.bulk_resend RE-ENTER bot_notify_pending at a brand-new
# time while gateway_verified_at stays exactly what it was (a payment
# verified 2 days ago, resent seconds ago, must look freshly-queued, not
# 2-days stale).
#
# The correct anchor is MAX(created_at) among the payment's own permanent
# payment_events rows of type bot_notification_queued /
# manual_review_resend_requested / admin_bulk_resend_requested -- each entry
# path records exactly one of these, atomically, in the same transaction as
# the status change. This is deliberately NOT next_retry_at: that field is
# overwritten to a FUTURE value on every retry-backoff reschedule
# (app.services.notification.record_attempt_result), so it would make an
# actively-retrying row look perpetually "fresh" and hide it even once
# genuinely overdue. See app.adminbot.queries._notification_age_anchor.
#
# Both queries.bot_delivery_snapshot and queries.stuck_payments share
# _stale_bot_notify_pending_conditions, so fixing it there fixes both /stuck
# (admin bot) and `centralpay stuck` (CLI) identically.
# ============================================================================


def test_old_order_fresh_notification_not_flagged_as_stale_bot_delivery(
    client, settings, session_factory, stub
):
    """The exact bug report: created_at is 45 minutes old, but the
    notification cycle (bot_notification_queued, fired by the real
    verification flow just now) is fresh -- the row must NOT appear as a
    stale bot-delivery problem in EITHER shared caller. INITIAL PAYMENT
    case #1."""
    from app.adminbot import queries

    order_id = "anchor-old-order-fresh-notify"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = datetime.now(UTC) - timedelta(minutes=45)
        db.commit()

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)
        stuck_entries = queries.stuck_payments(db, pending_age_minutes=30)

    assert snapshot.total == 0
    assert order_id not in {entry.payment.bot_order_id for entry in snapshot.entries}
    assert order_id not in {entry.payment.bot_order_id for entry in stuck_entries}


def test_old_order_old_notification_flagged_as_stale_bot_delivery(
    client, settings, session_factory, stub
):
    """The notification cycle itself (bot_notification_queued) is old:
    still correctly flagged as stale in both shared callers -- the fix
    narrows the false positive, it does not stop detecting genuinely stuck
    rows. INITIAL OLD NOTIFICATION case #2."""
    from app.adminbot import queries

    order_id = "anchor-old-order-old-notify"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    old = datetime.now(UTC) - timedelta(hours=2)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = old
        payment.gateway_verified_at = old
        db.commit()
    _backdate_notification_entry_event(
        session_factory, order_id, event_type="bot_notification_queued", when=old
    )

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)
        stuck_entries = queries.stuck_payments(db, pending_age_minutes=30)

    assert snapshot.total == 1
    assert order_id in {entry.payment.bot_order_id for entry in snapshot.entries}
    assert order_id in {entry.payment.bot_order_id for entry in stuck_entries}


def test_fresh_order_fresh_notification_not_flagged_as_stale_bot_delivery(
    client, settings, session_factory, stub
):
    """Baseline: a payment verified moments ago is never stale. FRESH ORDER
    FRESH NOTIFICATION case #3."""
    from app.adminbot import queries

    order_id = "anchor-fresh-order-fresh-notify"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 0
    assert order_id not in {entry.payment.bot_order_id for entry in snapshot.entries}


def test_stale_notification_claim_label_unaffected_by_age_anchor_change(
    client, settings, session_factory, stub
):
    """stale_notification_claim (case #9) depends only on
    notification_claimed_at vs. the claim-timeout cutoff -- entirely
    separate machinery from the pending-age anchor. A row whose
    notification cycle is old enough to be in the stale bucket, whose claim
    is older than claim_timeout_seconds, must still be labeled
    stale_notification_claim, never the generic bot_notify_pending_old."""
    from app.adminbot import queries

    order_id = "claim-stale-anchor-fix"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    now = datetime.now(UTC)
    old = now - timedelta(minutes=45)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = old  # irrelevant to inclusion after the fix
        payment.gateway_verified_at = old
        payment.notification_claimed_at = now - timedelta(seconds=200)
        payment.notification_claimed_by = "worker-1"
        db.commit()
    _backdate_notification_entry_event(
        session_factory, order_id, event_type="bot_notification_queued", when=old
    )

    with session_factory() as db:
        snapshot = queries.bot_delivery_snapshot(
            db, now=now, pending_age_minutes=30, claim_timeout_seconds=120.0
        )

    [entry] = [e for e in snapshot.entries if e.payment.bot_order_id == order_id]
    assert entry.category == "stale_notification_claim"


def test_manual_review_delivery_failure_included_regardless_of_notification_age_anchor(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """Bot-delivery manual-review rows are matched by manual_review_at via
    _bot_delivery_manual_review_conditions, never by the pending-age
    anchor -- the fix must not change whether they appear, however old
    gateway_verified_at happens to be."""
    from app.adminbot import queries

    order_id = "mr-delivery-anchor-fix"
    _make_bot_delivery_failure(
        client, settings, session_factory, stub, bot_stub, notifier, order_id
    )
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.gateway_verified_at = datetime.now(UTC) - timedelta(hours=3)
        db.commit()

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert order_id in {entry.payment.bot_order_id for entry in snapshot.entries}


def test_non_delivery_manual_review_excluded_regardless_of_notification_age_anchor(
    client, settings, session_factory, stub
):
    """Financial/verification manual-review rows never reached notification
    (bot_notify_reason stays None, gateway_verified_at stays NULL here) --
    still correctly excluded from bot-delivery details after the fix."""
    from app.adminbot import queries

    order_id = "fin-mr-anchor-fix"
    assert create_order(client, settings, order_id=order_id).status_code == 200
    _make_financial_manual_review(session_factory, order_id)

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 0
    assert order_id not in {entry.payment.bot_order_id for entry in snapshot.entries}


# --- CLI resend (app.ops "review resend") re-enters the notification cycle -


def _move_to_manual_review_for_resend(session_factory, order_id: str, *, verified_at):
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.gateway_verified_at = verified_at
        payment.status = PaymentStatus.MANUAL_REVIEW.value
        payment.bot_notify_reason = "retry_limit_reached"
        payment.manual_review_at = verified_at
        payment.next_retry_at = None
        payment.notification_claimed_at = None
        payment.notification_claimed_by = None
        db.commit()
    # MAX(created_at) picks the LATEST matching event -- keep the original
    # bot_notification_queued event consistent with the backdated
    # gateway_verified_at (real timestamps only ever move forward), so a
    # later-backdated resend event is unambiguously the most recent one.
    _backdate_notification_entry_event(
        session_factory, order_id, event_type="bot_notification_queued", when=verified_at
    )


def test_cli_review_resend_not_flagged_stale_immediately_after_resend(
    client, settings, session_factory, stub, monkeypatch, capsys
):
    """CLI RESEND case #3: gateway_verified_at is 2 days old, the payment is
    in manual_review, `centralpay review resend` runs NOW -- immediately
    after, the row must NOT be stale, exercised through the actual `app.ops`
    resend command (not by hand-editing model fields)."""
    import app.ops as ops_module
    from app.adminbot import queries
    from app.ops import main as ops_main

    order_id = "cli-resend-fresh"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    old = datetime.now(UTC) - timedelta(days=2)
    _move_to_manual_review_for_resend(session_factory, order_id, verified_at=old)

    idempotent = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    monkeypatch.setattr(ops_module, "Settings", lambda: idempotent)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)

    assert ops_main(["review", "resend", order_id, "--confirm-idempotent-bot", "--yes"]) == 0
    capsys.readouterr()

    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value  # sanity
    assert payment.gateway_verified_at.replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(
        days=1
    )  # sanity: still the ORIGINAL, untouched, 2-day-old fact

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 0
    assert order_id not in {entry.payment.bot_order_id for entry in snapshot.entries}


def test_cli_review_resend_becomes_stale_once_its_own_cycle_ages(
    client, settings, session_factory, stub, monkeypatch, capsys
):
    """CLI RESEND AGES case #4: the SAME resent row, once ITS OWN cycle
    (from manual_review_resend_requested) exceeds the pending-age
    threshold, must become stale -- the anchor tracks re-entry time, it
    doesn't exempt resent rows forever."""
    import app.ops as ops_module
    from app.adminbot import queries
    from app.ops import main as ops_main

    order_id = "cli-resend-ages"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    old = datetime.now(UTC) - timedelta(days=2)
    _move_to_manual_review_for_resend(session_factory, order_id, verified_at=old)

    idempotent = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    monkeypatch.setattr(ops_module, "Settings", lambda: idempotent)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)
    assert ops_main(["review", "resend", order_id, "--confirm-idempotent-bot", "--yes"]) == 0
    capsys.readouterr()

    _backdate_notification_entry_event(
        session_factory,
        order_id,
        event_type="manual_review_resend_requested",
        when=datetime.now(UTC) - timedelta(minutes=45),
    )

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 1
    assert order_id in {entry.payment.bot_order_id for entry in snapshot.entries}


# --- admin bulk resend (app.services.bulk_resend) re-enters the cycle too --


def test_bulk_resend_not_flagged_stale_immediately_after_resend(
    client, settings, session_factory, stub
):
    """BULK RESEND case #5: same requirement as CLI resend, exercised
    through the actual /resend_failed admin-bot command (not by
    hand-editing model fields)."""
    from app.adminbot import queries

    order_id = "bulk-resend-fresh"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    old = datetime.now(UTC) - timedelta(days=2)
    _move_to_manual_review_for_resend(session_factory, order_id, verified_at=old)

    idem_settings = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    idem_handlers = CommandHandlers(
        session_factory, idem_settings, ADMIN_IDS, api_probe=lambda: {"live": True, "ready": True}
    )
    replies = idem_handlers.handle(admin_ctx(), "resend_failed", ["confirm"])
    assert any("1" in r for r in replies)  # sanity: exactly one row requeued

    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value  # sanity

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 0
    assert order_id not in {entry.payment.bot_order_id for entry in snapshot.entries}


def test_bulk_resend_becomes_stale_once_its_own_cycle_ages(
    client, settings, session_factory, stub
):
    """BULK RESEND AGES case #6: once ITS OWN cycle (from
    admin_bulk_resend_requested) exceeds the pending-age threshold, the row
    must become stale."""
    from app.adminbot import queries

    order_id = "bulk-resend-ages"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    old = datetime.now(UTC) - timedelta(days=2)
    _move_to_manual_review_for_resend(session_factory, order_id, verified_at=old)

    idem_settings = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    idem_handlers = CommandHandlers(
        session_factory, idem_settings, ADMIN_IDS, api_probe=lambda: {"live": True, "ready": True}
    )
    idem_handlers.handle(admin_ctx(), "resend_failed", ["confirm"])

    _backdate_notification_entry_event(
        session_factory,
        order_id,
        event_type="admin_bulk_resend_requested",
        when=datetime.now(UTC) - timedelta(minutes=45),
    )

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 1
    assert order_id in {entry.payment.bot_order_id for entry in snapshot.entries}


# --- retry backoff must not be fooled by an old gateway_verified_at, and --
# --- must not be hidden forever by an accumulating cycle age either ------


def test_retryable_failure_in_active_backoff_not_flagged_stale(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """RETRY BACKOFF case #7: a retryable failure schedules next_retry_at
    into the FUTURE (app.services.notification.record_attempt_result) --
    the age anchor must be unaffected by that scheduling, and must judge
    the row by its notification cycle's actual start (fresh here), not by
    an old gateway_verified_at that has nothing to do with when this cycle
    began."""
    from app.adminbot import queries

    order_id = "retry-backoff-fresh-cycle"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    bot_stub.result = httpx.Response(500)
    run_pass(session_factory, notifier, settings)

    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value  # still pending
        assert payment.next_retry_at is not None
        assert payment.next_retry_at.replace(tzinfo=UTC) > datetime.now(UTC)  # future backoff
        # An intentionally old, IRRELEVANT gateway_verified_at -- must not
        # fool the anchor now that it's cycle-event-driven.
        payment.gateway_verified_at = datetime.now(UTC) - timedelta(days=2)
        db.commit()

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 0
    assert order_id not in {entry.payment.bot_order_id for entry in snapshot.entries}


def test_retryable_failure_becomes_stale_once_the_cycle_is_genuinely_overdue(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """OVERDUE RETRY case #8: the same kind of actively-backing-off row,
    once its notification CYCLE (from bot_notification_queued) has
    genuinely run past the pending-age threshold, must still surface --
    an in-progress backoff schedule alone must never hide a payment that's
    been undelivered too long."""
    from app.adminbot import queries

    order_id = "retry-backoff-overdue"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    bot_stub.result = httpx.Response(500)
    run_pass(session_factory, notifier, settings)

    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
        assert payment.next_retry_at is not None
        # A retry is still scheduled a little into the future -- this alone
        # must not suppress detection once the CYCLE itself is overdue.
        assert payment.next_retry_at.replace(tzinfo=UTC) > datetime.now(UTC)

    _backdate_notification_entry_event(
        session_factory,
        order_id,
        event_type="bot_notification_queued",
        when=datetime.now(UTC) - timedelta(minutes=45),
    )

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 1
    assert order_id in {entry.payment.bot_order_id for entry in snapshot.entries}


# --- legacy / anomalous data: never crash, always conservative -----------


def test_no_matching_entry_event_falls_back_to_gateway_verified_at(
    client, settings, session_factory, stub
):
    """A bot_notify_pending row with NO matching entry event (structurally
    shouldn't happen -- every entry path records one atomically -- but
    simulates data from before event logging existed, or a lost event)
    falls back to gateway_verified_at, the next-best available fact."""
    from sqlalchemy import delete

    from app.adminbot import queries

    order_id = "no-event-has-verified-at"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    old = datetime.now(UTC) - timedelta(minutes=45)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.gateway_verified_at = old
        db.execute(
            delete(PaymentEvent).where(
                PaymentEvent.payment_id == payment.id,
                PaymentEvent.event_type == "bot_notification_queued",
            )
        )
        db.commit()

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 1
    assert order_id in {entry.payment.bot_order_id for entry in snapshot.entries}


def test_legacy_null_gateway_verified_at_and_no_event_falls_back_to_created_at(
    client, settings, session_factory, stub
):
    """LEGACY NULL case #10: no matching entry event AND
    gateway_verified_at IS NULL -- both structurally impossible together
    (ck_payments_delivery_requires_verification, migration 0005, enforced
    identically by SQLite here) but deliberately constructed by bypassing
    the constraint for one UPDATE, proving the final COALESCE fallback to
    created_at: never crashes, conservatively stays visible for review
    rather than silently looking fresh."""
    from sqlalchemy import delete, text

    from app.adminbot import queries

    order_id = "legacy-null-no-event"
    make_verified_pending(client, settings, session_factory, stub, order_id=order_id)
    old = datetime.now(UTC) - timedelta(minutes=45)
    with session_factory() as db:
        db.execute(text("PRAGMA ignore_check_constraints = 1"))
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = old
        payment.gateway_verified_at = None
        db.execute(
            delete(PaymentEvent).where(
                PaymentEvent.payment_id == payment.id,
                PaymentEvent.event_type == "bot_notification_queued",
            )
        )
        db.commit()
        db.execute(text("PRAGMA ignore_check_constraints = 0"))
        db.commit()

    with session_factory() as db:
        now = datetime.now(UTC)
        snapshot = queries.bot_delivery_snapshot(db, now=now, pending_age_minutes=30)

    assert snapshot.total == 1
    assert order_id in {entry.payment.bot_order_id for entry in snapshot.entries}
