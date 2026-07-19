"""User-facing payment status pages for the CentralPay callback.

Static bilingual (Persian/English) templates. The only interpolated value is
the bot order id, HTML-escaped. Stack traces, secrets, bot responses, and raw
gateway errors never reach these pages.
"""

import html

from app.services.verification import CallbackStatus

# Wording contract (fix/payer-status-page-accuracy): the bridge KNOWS only
# (1) CentralPay verification and, for BOT_ACCEPTED, (2) that the bot API
# accepted the order-processing request (HTTP 2xx = acceptance only —
# see app/services/notification.py). The final business result inside the
# customer bot is never known here, so these pages must never claim the
# order was registered/completed or that credit was applied, and must
# never promise near-term application. The payer is always directed to
# the bot for the final order status.
_PAGE_TEXTS: dict[CallbackStatus, dict[str, str]] = {
    CallbackStatus.BOT_ACCEPTED: {
        "title_fa": "پرداخت تأیید شد",
        "body_fa": "پرداخت شما تأیید شد و ربات درخواست ثبت سفارش را پذیرفت. "
        "برای مشاهدهٔ وضعیت نهایی سفارش به ربات بازگردید.",
        "title_en": "Payment verified",
        "body_en": "Your payment was verified, and the bot accepted the "
        "order-processing request. Return to the bot to check the final "
        "order status.",
    },
    CallbackStatus.BOT_PENDING: {
        "title_fa": "پرداخت تأیید شد",
        "body_fa": "پرداخت شما تأیید شد، اما پذیرش درخواست ثبت سفارش توسط ربات "
        "هنوز تأیید نشده است. لطفاً وضعیت سفارش را در ربات بررسی کنید.",
        "title_en": "Payment verified",
        "body_en": "Your payment was verified, but the bot has not yet confirmed "
        "acceptance of the order-processing request. Please check the order "
        "status in the bot.",
    },
    CallbackStatus.UNDER_REVIEW: {
        "title_fa": "پرداخت در حال بررسی است",
        "body_fa": "پرداخت شما دریافت شد و توسط پشتیبانی بررسی می‌شود. "
        "سفارش شما به‌صورت دستی پیگیری خواهد شد.",
        "title_en": "Payment under review",
        "body_en": "Your payment was received and requires administrator review. "
        "Your order will be handled manually.",
    },
}


def payment_status_page(
    status: CallbackStatus, bot_order_id: str, *, bot_username: str = ""
) -> str:
    texts = _PAGE_TEXTS[status]
    order = html.escape(bot_order_id)
    bot_link = ""
    if bot_username:
        username = html.escape(bot_username.lstrip("@"))
        bot_link = (
            f'<p><a href="https://t.me/{username}">'
            f"بازگشت به ربات / Return to bot @{username}</a></p>"
        )
    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{texts["title_fa"]}</title>
<style>
body {{ font-family: system-ui, Tahoma, sans-serif; background: #f5f6f8; margin: 0;
       display: flex; min-height: 100vh; align-items: center; justify-content: center; }}
main {{ background: #fff; border-radius: 12px; padding: 2rem 2.5rem; max-width: 28rem;
        box-shadow: 0 2px 12px rgba(0,0,0,.08); text-align: center; }}
h1 {{ font-size: 1.25rem; margin: 0 0 .75rem; }}
p {{ color: #444; line-height: 1.9; margin: .5rem 0; }}
.order {{ color: #666; font-size: .85rem; margin-top: 1.25rem; direction: ltr; }}
.en {{ color: #777; font-size: .85rem; direction: ltr; text-align: left;
       border-top: 1px solid #eee; margin-top: 1.25rem; padding-top: 1rem; }}
</style>
</head>
<body>
<main data-status="{status.value}">
<h1>{texts["title_fa"]}</h1>
<p>{texts["body_fa"]}</p>
{bot_link}
<div class="en"><strong>{texts["title_en"]}</strong><br>{texts["body_en"]}</div>
<div class="order">Order: {order}</div>
</main>
</body>
</html>"""
