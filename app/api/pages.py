"""User-facing payment status pages for the CentralPay callback.

Static bilingual (Persian/English) templates. The only interpolated value is
the bot order id, HTML-escaped. Stack traces, secrets, bot responses, and raw
gateway errors never reach these pages.
"""

import html

from app.services.verification import CallbackStatus

_PAGE_TEXTS: dict[CallbackStatus, dict[str, str]] = {
    CallbackStatus.BOT_ACCEPTED: {
        "title_fa": "پرداخت با موفقیت انجام شد",
        "body_fa": "پرداخت شما تأیید شد و سفارش شما ثبت شد. می‌توانید به ربات بازگردید.",
        "title_en": "Payment completed",
        "body_en": "Your payment was verified and your order has been registered. "
        "You can return to the bot.",
    },
    CallbackStatus.BOT_PENDING: {
        "title_fa": "پرداخت تأیید شد",
        "body_fa": "پرداخت شما تأیید شد. ثبت نهایی سفارش در حال انجام است و "
        "به‌زودی در ربات اعمال می‌شود.",
        "title_en": "Payment verified",
        "body_en": "Your payment has been verified. Final processing is in progress "
        "and your order will be applied in the bot shortly.",
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


def payment_status_page(status: CallbackStatus, bot_order_id: str) -> str:
    texts = _PAGE_TEXTS[status]
    order = html.escape(bot_order_id)
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
<div class="en"><strong>{texts["title_en"]}</strong><br>{texts["body_en"]}</div>
<div class="order">Order: {order}</div>
</main>
</body>
</html>"""
