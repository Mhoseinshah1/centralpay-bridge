"""Persian-friendly Telegram message formatting.

HTML parse mode with every dynamic value escaped; untrusted text can never
break formatting or inject markup. Timestamps are rendered in the configured
timezone using the Jalali calendar. Long messages are split safely.
"""

import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import jdatetime
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Payment
from app.services.stuck_payments import StuckCategory, StuckEntry, StuckOverview

if TYPE_CHECKING:
    from app.adminbot.alerts import ClaimedAlert

OK = "✅"
WARN = "⚠️"
FAIL = "❌"
PENDING = "⏳"
REVIEW = "🛑"
ALERT_ICON = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def fmt_amount(amount: int | None) -> str:
    if amount is None:
        return "—"
    return f"{amount:,}"


def fmt_fee_rate(rate_bps: int | None) -> str:
    """Basis points as a percent string (1000 -> '10%', 225 -> '2.25%')."""
    if rate_bps is None:
        return "—"
    whole, frac = divmod(rate_bps, 100)
    if frac == 0:
        return f"{whole}%"
    return (f"{whole}.{frac:02d}").rstrip("0") + "%"


def fmt_time(value: datetime | None, tz_name: str) -> str:
    """Jalali date + time in the configured timezone."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(ZoneInfo(tz_name))
    jalali = jdatetime.datetime.fromgregorian(datetime=local)
    return str(jalali.strftime("%Y/%m/%d %H:%M:%S"))


def split_message(text: str, max_length: int) -> list[str]:
    """Split on line boundaries, hard-splitting only oversized single lines."""
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > max_length:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_length])
            line = line[max_length:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_length:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


_STATUS_FA = {
    "created": f"{PENDING} ایجاد شده",
    "link_created": f"{PENDING} لینک ایجاد شده",
    "getlink_failed": f"{FAIL} خطا در ایجاد لینک",
    "gateway_verified": f"{OK} تأیید درگاه",
    "bot_notify_pending": f"{PENDING} در انتظار ارسال به ربات",
    "bot_notify_accepted": f"{OK} پذیرفته‌شده توسط ربات",
    "manual_review": f"{REVIEW} نیازمند بررسی",
}


def payment_status_fa(status: str) -> str:
    return _STATUS_FA.get(status, esc(status))


def payment_block(payment: Payment, tz_name: str) -> str:
    """Safe payment summary. Never includes redirect URLs, signatures, keys,
    full card numbers, or untrusted external error text."""
    lines = [
        f"شماره سفارش:\n<b>{esc(payment.bot_order_id)}</b>",
        f"شماره درگاه:\n{payment.gateway_order_id}",
        # Amounts are labelled unambiguously: the ORIGINAL bot invoice (what
        # the bot credits) vs what the payer paid THROUGH THE GATEWAY
        # (original + fee). The gateway figure is never labelled as the
        # credited or invoice amount.
        f"فاکتور اصلی ربات:\n<b>{fmt_amount(payment.amount)}</b> تومان",
        f"نرخ کارمزد:\n{fmt_fee_rate(payment.fee_rate_bps)}",
        f"مبلغ کارمزد:\n{fmt_amount(payment.fee_amount)} تومان",
        f"پرداختی از درگاه:\n{fmt_amount(payment.payable_amount)} تومان",
        f"وضعیت:\n{payment_status_fa(payment.status)}",
    ]
    gateway_state = (
        f"{OK} تأیید شده"
        if payment.gateway_verified_at is not None
        else f"{PENDING} تأیید نشده"
    )
    lines.append(f"وضعیت درگاه:\n{gateway_state}")
    if payment.bot_notify_reason:
        lines.append(f"دلیل:\n<code>{esc(payment.bot_notify_reason)}</code>")
    if payment.bot_notify_attempts:
        lines.append(f"تعداد تلاش:\n{payment.bot_notify_attempts}")
    if payment.reference_id:
        lines.append(f"کد پیگیری:\n{esc(payment.reference_id)}")
    lines.append(f"زمان ایجاد:\n{fmt_time(payment.created_at, tz_name)}")
    return "\n\n".join(lines)


STUCK_CATEGORY_EMOJI = {
    StuckCategory.NEEDS_ATTENTION: "🔴",
    StuckCategory.WAITING_GATEWAY: "🟡",
    StuckCategory.EXPIRED: "⚫",
}
STUCK_CATEGORY_LABEL_FA = {
    StuckCategory.NEEDS_ATTENTION: "نیازمند توجه",
    StuckCategory.WAITING_GATEWAY: "در انتظار درگاه",
    StuckCategory.EXPIRED: "منقضی‌شده",
}


def stuck_summary_lines(overview: StuckOverview) -> list[str]:
    """Header + category counts. Counts are always exact, independent of
    how many entries are actually rendered below them."""
    return [
        "<b>پرداخت‌های نیازمند توجه</b>",
        "",
        f"{STUCK_CATEGORY_EMOJI[StuckCategory.NEEDS_ATTENTION]} "
        f"{STUCK_CATEGORY_LABEL_FA[StuckCategory.NEEDS_ATTENTION]}: "
        f"{fmt_amount(overview.total_counts['needs_attention'])}",
        f"{STUCK_CATEGORY_EMOJI[StuckCategory.WAITING_GATEWAY]} "
        f"{STUCK_CATEGORY_LABEL_FA[StuckCategory.WAITING_GATEWAY]}: "
        f"{fmt_amount(overview.total_counts['waiting_gateway'])}",
        f"{STUCK_CATEGORY_EMOJI[StuckCategory.EXPIRED]} "
        f"{STUCK_CATEGORY_LABEL_FA[StuckCategory.EXPIRED]}: "
        f"{fmt_amount(overview.total_counts['expired'])}",
    ]


def stuck_entry_block(index: int, entry: StuckEntry, tz_name: str) -> str:
    """One numbered entry. Always shows the exact reason/state — never a
    generic label — matching the invariant the rest of the admin bot
    already keeps for stuck-payment reporting."""
    payment = entry.payment
    is_link_created = payment.status == "link_created"
    attempts = (
        payment.reconciliation_attempts if is_link_created else payment.bot_notify_attempts
    )
    next_retry = payment.reconciliation_next_at if is_link_created else payment.next_retry_at
    lines = [
        f"{index}) {STUCK_CATEGORY_EMOJI[entry.category]} "
        f"<b>{STUCK_CATEGORY_LABEL_FA[entry.category]}</b>",
        f"سفارش: <b>{esc(payment.bot_order_id)}</b>",
        f"مبلغ: {fmt_amount(payment.amount)} تومان",
        f"وضعیت: {payment_status_fa(payment.status)}",
    ]
    if entry.gateway_state is not None:
        lines.append(f"درگاه: <code>{esc(entry.gateway_state)}</code>")
    if attempts:
        lines.append(f"تلاش‌ها: {attempts}")
    if is_link_created and payment.reconciliation_last_at is not None:
        lines.append(f"آخرین بررسی: {fmt_time(payment.reconciliation_last_at, tz_name)}")
    if next_retry is not None:
        lines.append(f"تلاش بعدی: {fmt_time(next_retry, tz_name)}")
    if entry.category == StuckCategory.NEEDS_ATTENTION:
        lines.append(f"دلیل: <code>{esc(entry.reason)}</code>")
    lines.append(f"ایجاد: {fmt_time(payment.created_at, tz_name)}")
    return "\n".join(lines)


_ALERT_TITLES_FA = {
    "gateway_payment_verified": "پرداخت تأیید شد",
    "bot_notify_accepted": "سفارش توسط ربات پذیرفته شد",
    "manual_review_required": "پرداخت نیازمند بررسی",
    "bot_timeout_ambiguous": "تحویل مبهم به ربات — نیازمند بررسی",
    "retry_limit_reached": "پایان تلاش‌های ارسال — نیازمند بررسی",
    "verify_payable_amount_mismatch": "مغایرت مبلغ در تأیید پرداخت",
    "verify_user_id_mismatch": "مغایرت شناسهٔ کاربر در تأیید پرداخت",
    "verify_missing_reference_id": "نبود کد پیگیری در تأیید پرداخت",
    "verify_invalid_reference_id": "کد پیگیری نامعتبر در تأیید پرداخت",
    "centralpay_getlink_failed": "خطا در ایجاد لینک پرداخت",
    "centralpay_verify_failed": "خطا در تأیید پرداخت",
    "callback_signature_failures": "امضاهای نامعتبر مکرر در کال‌بک",
    "notification_recovered_after_restart": "بازیابی اعلان پس از راه‌اندازی مجدد",
    "backup_succeeded": "پشتیبان‌گیری موفق",
    "backup_failed": "خطا در پشتیبان‌گیری",
    "service_unhealthy": "اختلال در سرویس",
    "service_recovered": "سرویس به حالت عادی بازگشت",
    "admin_test_alert": "پیام آزمایشی",
    "daily_report": "گزارش روزانه",
}


def alert_message(db: Session, settings: Settings, claimed: "ClaimedAlert") -> list[str]:
    """Build the Telegram message chunks for a claimed alert."""
    tz = settings.admin_bot_timezone
    icon = ALERT_ICON.get(claimed.severity, "ℹ️")
    title = _ALERT_TITLES_FA.get(claimed.alert_type, claimed.alert_type)
    payload = claimed.payload or {}

    parts = [f"{icon} <b>{esc(title)}</b>"]
    if claimed.alert_type == "daily_report":
        parts.append(daily_report_text(payload, tz))
    else:
        if claimed.payment_id is not None:
            payment = db.get(Payment, claimed.payment_id)
            if payment is not None:
                parts.append(payment_block(payment, tz))
        detail_keys = (
            ("reason", "دلیل"),
            ("action", "اقدام"),
            ("attempt", "تعداد تلاش"),
            ("expected_amount", "مبلغ مورد انتظار"),
            ("reported_amount", "مبلغ اعلام‌شده"),
            ("check", "بررسی"),
            ("detail", "توضیح"),
            ("count", "تعداد"),
            ("size", "حجم"),
            ("file_name", "فایل"),
        )
        for key, label in detail_keys:
            if key in payload and payload[key] is not None:
                parts.append(f"{label}:\n<code>{esc(payload[key])}</code>")
    parts.append(f"زمان:\n{fmt_time(datetime.now(UTC), tz)}")
    text = "\n\n".join(parts)
    return split_message(text, settings.admin_bot_max_message_length)


def daily_report_text(payload: dict[str, Any], tz: str) -> str:
    lines = [
        f"تاریخ گزارش: {esc(payload.get('report_date', '—'))}",
        "",
        f"لینک‌های پرداخت ایجادشده: {fmt_amount(payload.get('links_created'))}",
        f"تأیید شده توسط درگاه: {fmt_amount(payload.get('gateway_verified'))}",
        f"پذیرفته‌شده توسط ربات: {fmt_amount(payload.get('bot_accepted'))}",
        f"مبلغ کل تأییدشده: {fmt_amount(payload.get('total_verified_toman'))} تومان",
        "",
        f"{REVIEW} بررسی دستی: {fmt_amount(payload.get('manual_review'))}",
        f"{PENDING} در صف ارسال: {fmt_amount(payload.get('pending_retry'))}",
        f"{FAIL} خطای ایجاد لینک: {fmt_amount(payload.get('getlink_failures'))}",
        f"{FAIL} خطای تأیید: {fmt_amount(payload.get('verify_failures'))}",
        f"{FAIL} خطای تحویل به ربات: {fmt_amount(payload.get('bot_delivery_failures'))}",
        "",
        f"وضعیت پشتیبان‌گیری: {esc(payload.get('backup_status', 'نامشخص'))}",
        f"سلامت سامانه: {esc(payload.get('health_summary', 'نامشخص'))}",
        "",
        "توجه: «پذیرفته‌شده توسط ربات» یعنی API ربات پاسخ 2xx داده است؛ "
        "قرارداد API ربات واریز قطعی اعتبار را تضمین نمی‌کند.",
    ]
    return "\n".join(lines)
