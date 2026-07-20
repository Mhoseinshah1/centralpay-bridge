"""Payer-facing status page accuracy.

Every callback outcome after successful CentralPay verification —
BOT_ACCEPTED, BOT_PENDING, and UNDER_REVIEW — renders ONE unified page, so
its copy may state only what is true in EVERY one of those states: the
payment succeeded (proven by CentralPay verification), order processing
may take time, and the final order status lives in the customer bot.
Rendering never alters or promotes the stored payment/notification state;
the real state stays machine-readable on ``data-status``.
"""

import httpx
import pytest

from app.api.pages import payment_status_page
from app.models import PaymentStatus
from app.services.verification import CallbackStatus
from tests.conftest import (
    get_payment,
    make_verified_pending,
    run_pass,
    valid_callback_path,
)

# Claims the bridge cannot prove for a page shared by ALL verified
# outcomes: order registration/completion, credit application, bot
# acceptance/confirmation, and promises of near-term application — in
# either language. Payment-level success wording IS allowed (CentralPay
# verification proves it in every state that reaches this page).
FORBIDDEN_PHRASES = [
    "سفارش ثبت شد",
    "سفارش شما ثبت شد",
    "سفارش نهایی شد",
    "درخواست سفارش پذیرفته شد",
    "ربات درخواست ثبت سفارش را پذیرفت",
    "اعتبار شما افزایش یافت",
    "مبلغ به حساب شما اضافه شد",
    "خرید تکمیل شد",
    "سفارش تکمیل شد",
    "به‌زودی",
    "اعمال می‌شود",
    "order has been registered",
    "order registered",
    "order completed",
    "purchase completed",
    "credited",
    "balance",
    "shortly",
    "bot accepted",
    "bot confirmed",
]

VERIFIED_STATUSES = [
    CallbackStatus.BOT_ACCEPTED,
    CallbackStatus.BOT_PENDING,
    CallbackStatus.UNDER_REVIEW,
]


def _page(status: CallbackStatus) -> str:
    return payment_status_page(status, "order-1", bot_username="@my_bot")


def _visible(page: str) -> str:
    """Strip <style>/<script> blocks so the forbidden-claim scan checks what
    the payer READS, not CSS/JS keywords (e.g. the CSS value
    ``text-wrap:balance`` is not an English 'balance' claim)."""
    import re

    return re.sub(r"<(style|script)>.*?</\1>", "", page, flags=re.S)


# --- one unified page for every verified outcome ------------------------------


@pytest.mark.parametrize("status", VERIFIED_STATUSES)
def test_every_verified_outcome_renders_the_unified_page(status):
    page = _page(status)
    # Neutral copy, true in every state that reaches this page.
    assert "پرداخت با موفقیت انجام شد" in page
    assert "پرداخت شما تأیید شد." in page
    assert "پردازش سفارش ممکن است چند لحظه زمان ببرد." in page
    assert "لطفاً برای مشاهده وضعیت سفارش به ربات بازگردید." in page
    assert "بازگشت به ربات" in page
    # Persian-only: the bilingual legacy sections are gone.
    assert 'class="en"' not in page
    assert "Payment verified" not in page
    assert "Your payment was verified" not in page


@pytest.mark.parametrize("status", VERIFIED_STATUSES)
def test_no_unprovable_claim_on_any_verified_outcome(status):
    page = _visible(_page(status))
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in page, (status, phrase)


def test_all_outcomes_render_byte_identical_except_status_attribute():
    """The pages differ ONLY in the machine-readable data-status value."""
    normalized = {
        payment_status_page(s, "order-1").replace(
            f'data-status="{s.value}"', 'data-status="X"'
        )
        for s in VERIFIED_STATUSES
    }
    assert len(normalized) == 1


def test_status_attribute_reflects_the_real_state():
    for status, attr in [
        (CallbackStatus.BOT_ACCEPTED, 'data-status="bot_accepted"'),
        (CallbackStatus.BOT_PENDING, 'data-status="bot_pending"'),
        (CallbackStatus.UNDER_REVIEW, 'data-status="under_review"'),
    ]:
        assert attr in _page(status)


def test_escaping_holds_on_every_outcome():
    for status in VERIFIED_STATUSES:
        page = payment_status_page(status, "<x>&amp", bot_username="@evil<script>")
        assert "&lt;x&gt;&amp;amp" in page  # order id HTML-escaped
        # The ignored username can never become markup; the ONLY <script>
        # is the trusted inline copy-button script.
        assert "evil" not in page
        assert page.count("<script>") == 1


def test_every_page_still_renders():
    for status in CallbackStatus:
        assert payment_status_page(status, "order-x")


# --- behavior unchanged: rendering never promotes state -----------------------


def test_accepted_flow_renders_unified_page_with_accepted_status(
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
    duplicate = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert duplicate.status_code == 200
    assert 'data-status="bot_accepted"' in duplicate.text
    assert "پرداخت با موفقیت انجام شد" in duplicate.text
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in _visible(duplicate.text), phrase


def test_pending_flow_renders_unified_page_without_status_promotion(
    client, settings, session_factory, stub
):
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="page-pend"
    )
    # BOT_PENDING remains BOT_PENDING: rendering promotes nothing.
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    duplicate = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert 'data-status="bot_pending"' in duplicate.text
    assert "پرداخت با موفقیت انجام شد" in duplicate.text
    assert (
        get_payment(session_factory, "page-pend").status
        == PaymentStatus.BOT_NOTIFY_PENDING.value
    )
