"""The /stuck /waiting /expired redesign: three focused admin-bot commands
replacing the old grouped /stuck.

/stuck is now narrowly scoped to bot-delivery problems ONLY — payments that
failed, or are stuck trying, to reach the customer bot's webhook. It is
built from the existing status/reason semantics via
app.adminbot.queries.bot_delivery_stuck_entries /
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
from app.models import Payment, PaymentStatus
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
        assert queries.count_bot_delivery_problems(db) == 0
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
        assert queries.count_bot_delivery_problems(db) == 1
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
        bot_delivery_total = queries.count_bot_delivery_problems(db)
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

