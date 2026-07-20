"""Production integration of the approved ZedProxy payment-success page.

BOT_ACCEPTED renders the approved Persian "Color Pop Receipt" design with
the REAL order id (escaped); BOT_PENDING / UNDER_REVIEW keep the legacy
bilingual template, and every payment/callback behavior is unchanged.
CentralPay and the customer bot are faked at the httpx transport layer —
no real external service is contacted.
"""

from pathlib import Path

import httpx

from app.api.pages import payment_status_page
from app.models import PaymentStatus
from app.services.verification import CallbackStatus
from tests.conftest import (
    get_payment,
    make_verified_pending,
    run_pass,
    valid_callback_path,
)

PREVIEW_EXAMPLE_ID = "1efea273b3"
NEW_HEADING = "پرداخت سفارش شما تأیید شد"


def _accepted_response(client, settings, session_factory, stub, bot_stub, notifier, order_id):
    """Full real flow to a BOT_ACCEPTED render: create, verify via signed
    callback, deliver the bot notification, then re-render via a duplicate
    callback from persisted state."""
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id=order_id
    )
    bot_stub.result = httpx.Response(200, json={"ok": True})
    run_pass(session_factory, notifier, settings)
    return client.get(valid_callback_path(stub, payment.gateway_order_id))


# --- the accepted page is the new design with the real order id ---------------


def test_verified_payment_renders_new_persian_page(
    client, settings, session_factory, stub, bot_stub, notifier
):
    response = _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, "zx-real-1"
    )
    assert response.status_code == 200  # success HTTP status unchanged
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert NEW_HEADING in page
    assert "زدپروکسی" in page
    assert "از خرید شما از" in page and "سپاسگزاریم" in page
    assert "پرداخت با موفقیت انجام شد؛ برای مشاهده وضعیت سفارش به ربات بازگردید." in page
    assert "شماره سفارش" in page
    assert 'data-status="bot_accepted"' in page


def test_real_order_id_rendered_not_the_preview_example(
    client, settings, session_factory, stub, bot_stub, notifier
):
    response = _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, "real-order-7f"
    )
    assert ">real-order-7f</span>" in response.text
    assert PREVIEW_EXAMPLE_ID not in response.text


def test_preview_example_id_not_hardcoded_in_production_source():
    source = (Path(__file__).resolve().parent.parent / "app").rglob("*.py")
    for path in source:
        assert PREVIEW_EXAMPLE_ID not in path.read_text(), path


# --- injection safety ---------------------------------------------------------


def test_hostile_order_id_cannot_inject_html_unit():
    page = payment_status_page(
        CallbackStatus.BOT_ACCEPTED, '<script>alert(1)</script><img src=x>'
    )
    assert "<script>alert" not in page
    assert "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x&gt;" in page
    # The only live <script> is the trusted inline copy-button script.
    assert page.count("<script>") == 1


def test_hostile_order_id_escaped_end_to_end(
    client, settings, session_factory, stub, bot_stub, notifier
):
    hostile = '<b onmouseover="x">o</b>'
    response = _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, hostile
    )
    assert response.status_code == 200
    assert "<b onmouseover" not in response.text
    assert "&lt;b onmouseover=&quot;x&quot;&gt;o&lt;/b&gt;" in response.text


# --- no secret ever reaches the page ------------------------------------------


def test_secrets_absent_from_rendered_page(
    client, settings, session_factory, stub, bot_stub, notifier
):
    response = _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, "sec-scan"
    )
    page = response.text
    for secret in (
        settings.inbound_api_key,
        settings.callback_hmac_secret,
        settings.centralpay_getlink_api_key,
        settings.centralpay_verify_api_key,
        settings.bot_notify_token,
    ):
        assert secret not in page
    # The one-time callback token from the URL never appears in the body.
    ct = valid_callback_path(stub, None).split("ct=")[1].split("&")[0]
    assert ct not in page


# --- fully self-contained: no external asset or request -----------------------


def test_no_external_assets_or_requests(
    client, settings, session_factory, stub, bot_stub, notifier
):
    page = _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, "asset-scan"
    ).text
    assert "<link" not in page
    assert "<img" not in page
    assert "<script src" not in page
    assert "@import" not in page
    assert "url(http" not in page and "url('http" not in page and 'url("http' not in page
    assert "integrity=" not in page
    # No remote font source of any kind.
    for marker in ("googleapis", "gstatic", "cdn.", "jsdelivr", "unpkg"):
        assert marker not in page, marker
    # The ONLY absolute URL in the page is the FIXED return-to-bot anchor.
    for chunk in page.split("https://")[1:]:
        assert chunk.startswith("t.me/zedproxy_bot"), chunk[:40]


# --- fixed return-to-bot button ----------------------------------------------


def test_status_strip_removed_from_success_page():
    page = payment_status_page(CallbackStatus.BOT_ACCEPTED, "o-1")
    assert "پرداخت تأیید شد" not in page  # h1 wording differs; exact item absent
    assert "درخواست سفارش پذیرفته شد" not in page
    assert "آماده بازگشت به ربات" not in page
    assert 'class="status"' not in page


def test_exactly_one_fixed_return_to_bot_action():
    page = payment_status_page(CallbackStatus.BOT_ACCEPTED, "o-1")
    assert page.count('href="https://t.me/zedproxy_bot"') == 1
    assert page.count("بازگشت به ربات") == 1
    assert page.count("https://t.me/") == 1
    # A real same-context anchor: no target/JS navigation, no tracking params.
    anchor = page.split('<a class="botlink"', 1)[1].split("</a>", 1)[0]
    assert "target=" not in anchor
    assert "onclick" not in anchor
    assert "?" not in anchor.split('href="', 1)[1].split('"', 1)[0]


def test_dynamic_username_cannot_alter_success_destination():
    """The configurable bot username must not affect the success page; it only
    feeds the legacy pending/review pages."""
    page = payment_status_page(
        CallbackStatus.BOT_ACCEPTED, "o-1", bot_username="@attacker_bot"
    )
    assert 'href="https://t.me/zedproxy_bot"' in page
    assert "attacker_bot" not in page
    # Legacy pages keep their long-standing dynamic link behavior.
    pending = payment_status_page(
        CallbackStatus.BOT_PENDING, "o-1", bot_username="@my_bot"
    )
    assert "https://t.me/my_bot" in pending


# --- bundled Vazirmatn webfont ------------------------------------------------


def test_vazirmatn_loaded_via_local_font_face():
    page = payment_status_page(CallbackStatus.BOT_ACCEPTED, "o-1")
    assert "@font-face" in page
    assert 'font-family:"Vazirmatn"' in page
    assert "font-display:swap" in page
    assert 'url("/static/fonts/vazirmatn-v33/vazirmatn-variable.woff2")' in page
    assert "data:" not in page  # no base64/data-URI font embedding
    assert '"Vazirmatn", Tahoma, "Segoe UI", Arial, sans-serif' in page


def test_font_asset_served_with_correct_type(client):
    response = client.get("/static/fonts/vazirmatn-v33/vazirmatn-variable.woff2")
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"
    assert response.content[:4] == b"wOF2"  # a real WOFF2 payload
    # The OFL license ships alongside the font.
    assert client.get("/static/fonts/vazirmatn-v33/OFL.txt").status_code == 200


def test_static_mount_serves_no_directory_listing(client):
    for path in ("/static/", "/static/fonts/", "/static/fonts/vazirmatn-v33/"):
        assert client.get(path).status_code == 404, path


# --- accessibility / RTL / typography markers ---------------------------------


def test_accessibility_and_rtl_markers(
    client, settings, session_factory, stub, bot_stub, notifier
):
    page = _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, "a11y-scan"
    ).text
    assert 'lang="fa"' in page and 'dir="rtl"' in page
    assert page.count("<h1>") == 1
    assert "<main" in page
    # LTR isolation + monospace stack on the order id.
    assert "direction:ltr" in page and "unicode-bidi:isolate" in page
    assert "ui-monospace" in page
    # Decorative graphics are hidden from assistive technology.
    assert '<div class="hero" aria-hidden="true">' in page
    assert '<div class="bg" aria-hidden="true">' in page
    # Accessible Persian copy-button label and reduced-motion support.
    assert 'aria-label="کپی شمارهٔ سفارش"' in page
    assert "prefers-reduced-motion" in page
    # Bundled-Vazirmatn-first stack; order id keeps the monospace stack.
    assert '"Vazirmatn", Tahoma, "Segoe UI", Arial, sans-serif' in page


# --- unchanged behavior: statuses, sibling pages, notification ----------------


def test_verification_classification_unchanged(
    client, settings, session_factory, stub, bot_stub, notifier
):
    _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, "cls-check"
    )
    assert (
        get_payment(session_factory, "cls-check").status
        == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    )


def test_pending_page_unchanged(client, settings, session_factory, stub):
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="pend-same"
    )
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    page = response.text
    assert 'data-status="bot_pending"' in page
    # Exact legacy wording and the bilingual section are intact.
    assert "هنوز تأیید نشده است" in page
    assert "has not yet confirmed acceptance" in page
    assert 'class="en"' in page
    # The new design did not leak into the pending page.
    assert NEW_HEADING not in page
    assert "زدپروکسی" not in page


def test_failed_callback_behavior_unchanged(client, settings, session_factory, stub):
    # An invalid signature is still the same sanitized 403 error — no page.
    response = client.get(
        "/api/centralpay/callback?orderId=12345&ct=" + "a" * 32 + "&sig=" + "b" * 64
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_callback_signature"
    assert NEW_HEADING not in response.text


def test_outbound_bot_notification_unchanged(
    client, settings, session_factory, stub, bot_stub, notifier
):
    _accepted_response(
        client, settings, session_factory, stub, bot_stub, notifier, "ntf-same"
    )
    [request] = bot_stub.requests
    assert request == {"order_id": "ntf-same", "actions": "custom_payment_verify"}


def test_viewport_fit_css_markers():
    """The one-viewport sizing contract (fix/success-page-fit-viewport):
    height-aware media queries exist, the aggressive desktop hero upscale is
    gone, the hero scales via the --hs variable with layout-height
    compensation, and the layout column never crops content to fake a fit.
    (Fit itself was measured in a real browser: scrollHeight == viewport
    height at 360x800, 390x844, 430x932, 1366x768, and 1440x900.)"""
    page = payment_status_page(CallbackStatus.BOT_ACCEPTED, "o-1")
    assert "@media (max-height:850px)" in page
    assert "@media (max-height:780px)" in page
    assert "scale(1.2)" not in page
    assert "--hs" in page
    wrap_rule = page.split(".wrap{", 1)[1].split("}", 1)[0]
    assert "overflow" not in wrap_rule


def test_long_order_id_kept_on_one_scrollable_line():
    long_id = "x" * 128
    page = payment_status_page(CallbackStatus.BOT_ACCEPTED, long_id)
    assert long_id in page
    # The pill keeps the id unwrapped and scrolls internally on overflow.
    assert "white-space:nowrap" in page
    assert "overflow-x:auto" in page
