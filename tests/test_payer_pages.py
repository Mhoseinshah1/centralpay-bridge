"""Payer-facing status page accuracy.

The bridge knows only (1) CentralPay verification and, for BOT_ACCEPTED,
(2) that the bot API accepted the order-processing request — never the
final business result inside the customer bot. The pages must state
exactly those facts and nothing stronger.
"""

import httpx
import pytest

from app.api.pages import _PAGE_TEXTS, payment_status_page
from app.models import PaymentStatus
from app.services.verification import CallbackStatus
from tests.conftest import (
    get_payment,
    make_verified_pending,
    run_pass,
    valid_callback_path,
)

# Claims the bridge cannot prove: final order/credit facts and promises
# of near-term application, in either language. Scoped to the payer-facing
# page templates only.
FORBIDDEN_PHRASES = [
    "سفارش شما ثبت شد",
    "سفارش نهایی شد",
    "اعتبار شما افزایش یافت",
    "مبلغ به حساب شما اضافه شد",
    "خرید تکمیل شد",
    "به‌زودی",
    "اعمال می‌شود",
    "order has been registered",
    "order registered",
    "order completed",
    "purchase completed",
    "credited",
    "balance",
    "shortly",
    "با موفقیت انجام شد",
    "Payment completed",
]


def _page(status: CallbackStatus) -> str:
    return payment_status_page(status, "order-1", bot_username="@my_bot")


# --- BOT_ACCEPTED: verified + request accepted + check the bot ---------------


def test_bot_accepted_states_exactly_the_known_facts():
    page = _page(CallbackStatus.BOT_ACCEPTED)
    # Fact 1: the payment was verified.
    assert "پرداخت شما تأیید شد" in page
    assert "Your payment was verified" in page
    # Fact 2: the bot ACCEPTED the order-processing request (not more).
    assert "ربات درخواست ثبت سفارش را پذیرفت" in page
    assert "the bot accepted the order-processing request" in page
    # Instruction: the FINAL status lives in the bot.
    assert "وضعیت نهایی سفارش" in page
    assert "final order status" in page


def test_bot_accepted_makes_no_final_credit_or_order_claim():
    page = _page(CallbackStatus.BOT_ACCEPTED)
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in page, phrase


# --- BOT_PENDING: verified + acceptance NOT yet confirmed --------------------


def test_bot_pending_states_verification_but_unconfirmed_acceptance():
    page = _page(CallbackStatus.BOT_PENDING)
    assert "پرداخت شما تأیید شد" in page
    assert "Your payment was verified" in page
    # Acceptance is explicitly NOT yet confirmed.
    assert "هنوز تأیید نشده است" in page
    assert "has not yet confirmed acceptance" in page
    # Instruction: check the bot.
    assert "وضعیت سفارش را در ربات بررسی کنید" in page
    assert "check the order status in the bot" in page


def test_bot_pending_promises_nothing_about_eventual_application():
    page = _page(CallbackStatus.BOT_PENDING)
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in page, phrase


# --- template-wide scan and semantics ----------------------------------------


def test_no_payer_template_contains_forbidden_claims():
    """Regression scan over the page templates themselves (not docs)."""
    for texts in _PAGE_TEXTS.values():
        blob = " ".join(texts.values())
        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in blob, (phrase, texts)


def test_persian_and_english_express_equivalent_semantics():
    accepted = _PAGE_TEXTS[CallbackStatus.BOT_ACCEPTED]
    pending = _PAGE_TEXTS[CallbackStatus.BOT_PENDING]
    # Both languages assert verification...
    assert "تأیید شد" in accepted["body_fa"] and "verified" in accepted["body_en"]
    assert "تأیید شد" in pending["body_fa"] and "verified" in pending["body_en"]
    # ...acceptance (accepted) vs unconfirmed acceptance (pending)...
    assert "پذیرفت" in accepted["body_fa"] and "accepted" in accepted["body_en"]
    assert "هنوز" in pending["body_fa"] and "not yet" in pending["body_en"]
    # ...and both direct the payer to the bot for the outcome.
    assert "ربات" in accepted["body_fa"] and "bot" in accepted["body_en"]
    assert "ربات" in pending["body_fa"] and "bot" in pending["body_en"]
    # No implementation details (HTTP codes) reach the payer.
    for texts in _PAGE_TEXTS.values():
        blob = " ".join(texts.values())
        assert "2xx" not in blob and "HTTP" not in blob


def test_under_review_wording_unchanged():
    """UNDER_REVIEW made no unproven claim (received + reviewed + manual
    follow-up are all true) — its wording is pinned as-is."""
    texts = _PAGE_TEXTS[CallbackStatus.UNDER_REVIEW]
    assert texts["title_fa"] == "پرداخت در حال بررسی است"
    assert "بررسی می‌شود" in texts["body_fa"]
    assert "administrator review" in texts["body_en"]


def test_escaping_and_status_attributes_unchanged():
    page = payment_status_page(
        CallbackStatus.BOT_ACCEPTED, "<x>&amp", bot_username="@evil<script>"
    )
    assert "&lt;x&gt;&amp;amp" in page  # order id HTML-escaped
    assert "<script>" not in page.replace("&lt;script&gt;", "")  # username escaped
    for status, attr in [
        (CallbackStatus.BOT_ACCEPTED, 'data-status="bot_accepted"'),
        (CallbackStatus.BOT_PENDING, 'data-status="bot_pending"'),
        (CallbackStatus.UNDER_REVIEW, 'data-status="under_review"'),
    ]:
        assert attr in _page(status)


# --- behavior unchanged: only the words moved --------------------------------


def test_accepted_state_and_duplicate_callback_render_accepted_page(
    client, settings, session_factory, stub, bot_stub, notifier
):
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="page-acc"
    )
    bot_stub.result = httpx.Response(200, json={"ok": True})
    run_pass(session_factory, notifier, settings)
    # 2xx still maps to BOT_NOTIFY_ACCEPTED — classification untouched.
    assert (
        get_payment(session_factory, "page-acc").status
        == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    )

    # A duplicate callback renders the ACCEPTED page from persisted state,
    # with the accurate wording and no forbidden claim.
    duplicate = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert duplicate.status_code == 200
    assert 'data-status="bot_accepted"' in duplicate.text
    assert "ربات درخواست ثبت سفارش را پذیرفت" in duplicate.text
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in duplicate.text, phrase


def test_pending_callback_renders_pending_page(client, settings, session_factory, stub):
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="page-pend"
    )
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    duplicate = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert 'data-status="bot_pending"' in duplicate.text
    assert "هنوز تأیید نشده است" in duplicate.text


@pytest.mark.parametrize("status", list(CallbackStatus))
def test_every_page_still_renders(status):
    assert payment_status_page(status, "order-x")
