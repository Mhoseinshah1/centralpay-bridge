"""Read-only admin bot command handlers.

Handlers are plain functions decoupled from the Telegram library so they can
be tested directly. Every handler returns a list of pre-split message
strings. All dynamic values are escaped; secrets, redirect URLs, signatures,
full card numbers, and untrusted external error text never appear in output.
"""

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.adminbot import queries
from app.adminbot.auth import GENERIC_DENIAL, UpdateContext, is_authorized, log_unauthorized
from app.adminbot.format import (
    FAIL,
    OK,
    PENDING,
    REVIEW,
    WARN,
    bot_delivery_entry_block,
    esc,
    expired_entry_block,
    fmt_amount,
    fmt_time,
    fmt_time_only_fa,
    payment_block,
    payment_status_fa,
    split_message,
    waiting_entry_block,
)
from app.audit import record_event
from app.config import Settings
from app.services import stuck_payments as stuck_service
from app.services.bulk_resend import (
    PREVIEW_ORDER_LIMIT,
    BulkResendPreview,
    BulkResendResult,
    preview_bulk_resend,
    requeue_failed_deliveries,
)
from app.version import APP_VERSION

logger = logging.getLogger("app.adminbot.commands")

RECENT_DEFAULT = 10
RECENT_MAX = 50

# /waiting and /expired share this default/maximum (kept as separate
# constants from RECENT_DEFAULT/RECENT_MAX even though numerically equal,
# so a future change to one command's limits never silently changes the
# other's).
LIST_DEFAULT = 10
LIST_MAX = 50

# At most this many detailed bot-delivery rows in /stuck, before the
# progressive-length-reduction loop even runs.
STUCK_DETAIL_MAX = 10

_ASCII_DIGITS = re.compile(r"[0-9]+")

WAITING_USAGE_ERROR = "فرمت صحیح:\n/waiting [1-50]"
EXPIRED_USAGE_ERROR = "فرمت صحیح:\n/expired [1-50]"

# Fixed rejection shown for /resend_failed (preview AND confirm) when the
# customer bot's idempotency is not guaranteed. No payment row is modified.
BULK_RESEND_SAFE_MODE_MESSAGE = (
    "ارسال مجدد گروهی غیرفعال است؛ ربات فروش باید دریافت تکراری order_id را "
    "idempotent تضمین کند و BOT_NOTIFY_RETRY_MODE=idempotent باشد."
)

# check_api_health() -> {"live": bool, "ready": bool}
ApiProbe = Callable[[], dict[str, bool]]


def _mark(ok: bool) -> str:
    return OK if ok else FAIL


def _parse_list_count(args: list[str]) -> int | None:
    """/waiting and /expired's shared argument contract: no argument means
    LIST_DEFAULT; exactly one ASCII-decimal integer in [1, LIST_MAX] is
    accepted; anything else (zero, negative, non-numeric, out of range,
    multiple arguments, non-ASCII digits) returns None so the caller shows
    a short usage error instead of silently falling back to a default or
    clamping to the bound."""
    if not args:
        return LIST_DEFAULT
    if len(args) > 1:
        return None
    token = args[0]
    if not _ASCII_DIGITS.fullmatch(token):
        return None
    value = int(token)
    if not (1 <= value <= LIST_MAX):
        return None
    return value


class CommandHandlers:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        admin_ids: tuple[int, ...],
        api_probe: ApiProbe,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._admin_ids = admin_ids
        self._api_probe = api_probe

    # -- dispatch ----------------------------------------------------------

    def handle(self, ctx: UpdateContext, command: str, args: list[str]) -> list[str]:
        """Authorize, audit, and execute one command."""
        if not is_authorized(self._admin_ids, ctx):
            log_unauthorized(ctx, command)
            with self._session_factory() as db:
                record_event(
                    db,
                    payment_id=None,
                    event_type="admin_bot_unauthorized_access",
                    level="warning",
                    data={
                        "telegram_user_id": ctx.user_id,
                        "chat_id": ctx.chat_id,
                        "command": command,
                    },
                )
                db.commit()
            return [GENERIC_DENIAL]

        started = time.perf_counter()
        log_extra = {
            "telegram_user_id": ctx.user_id,
            "chat_id": ctx.chat_id,
            "command": command,
        }
        logger.info("admin_command_started", extra=log_extra)
        handler = self._registry(ctx).get(command)
        with self._session_factory() as db:
            record_event(
                db,
                payment_id=None,
                event_type="admin_command_received",
                data={"telegram_user_id": ctx.user_id, "command": command},
            )
            db.commit()
        if handler is None:
            return ["دستور ناشناخته است. راهنما: /help"]
        try:
            with self._session_factory() as db:
                messages = handler(db, args)
                record_event(
                    db,
                    payment_id=None,
                    event_type="admin_command_succeeded",
                    data={"telegram_user_id": ctx.user_id, "command": command},
                )
                db.commit()
        except Exception:
            logger.exception("admin_command_failed", extra=log_extra)
            with self._session_factory() as db:
                record_event(
                    db,
                    payment_id=None,
                    event_type="admin_command_failed",
                    level="error",
                    data={"telegram_user_id": ctx.user_id, "command": command},
                )
                db.commit()
            return ["خطای داخلی در اجرای دستور. جزئیات در لاگ سرویس ثبت شد."]
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "admin_command_completed", extra={**log_extra, "duration_ms": duration_ms}
        )
        return messages

    def _registry(
        self, ctx: UpdateContext | None = None
    ) -> dict[str, Callable[[Session, list[str]], list[str]]]:
        registry: dict[str, Callable[[Session, list[str]], list[str]]] = {
            "start": self.cmd_start,
            "help": self.cmd_help,
            "status": self.cmd_status,
            "health": self.cmd_health,
            "recent": self.cmd_recent,
            "stuck": self.cmd_stuck,
            "waiting": self.cmd_waiting,
            "expired": self.cmd_expired,
            "manual_review": self.cmd_manual_review,
            "resolved_reviews": self.cmd_resolved_reviews,
            "errors": self.cmd_errors,
            "payment": self.cmd_payment,
            "retry_queue": self.cmd_retry_queue,
            "backup_status": self.cmd_backup_status,
            "version": self.cmd_version,
            "fee": self.cmd_fee,
        }
        # resend_failed is the only mutating command; it needs the authorized
        # caller's numeric id for the audit trail, threaded explicitly (never
        # via shared state — handlers run in a thread pool). Bound only when a
        # real context is present (handle() always supplies one); a bare
        # _registry() call is introspection-only.
        if ctx is not None:
            registry["resend_failed"] = lambda db, args: self.cmd_resend_failed(db, ctx, args)
        return registry

    def _split(self, text: str) -> list[str]:
        return split_message(text, self._settings.admin_bot_max_message_length)

    # -- commands ----------------------------------------------------------

    def cmd_start(self, db: Session, args: list[str]) -> list[str]:
        text = "\n".join(
            [
                "🤖 <b>CentralPay Bridge — ربات مدیریتی</b>",
                "",
                f"محیط: {esc(self._settings.environment)}",
                f"نسخه: {esc(APP_VERSION)}",
                "",
                "دستورها: /status /health /recent /stuck /waiting /expired",
                "/manual_review /resolved_reviews /errors /payment /retry_queue",
                "/resend_failed /backup_status /version /fee /help",
                "",
                f"{WARN} این ربات فقط برای دیدبانی عملیاتی است. "
                "پاسخ 2xx ربات فروش به معنی واریز قطعی اعتبار مشتری نیست.",
            ]
        )
        return self._split(text)

    def cmd_help(self, db: Session, args: list[str]) -> list[str]:
        text = "\n".join(
            [
                "<b>راهنمای دستورها</b>",
                "",
                "/status — وضعیت کلی سرویس‌ها و صف‌ها",
                "/health — جزئیات سلامت اجزا",
                "/recent [n] — آخرین پرداخت‌ها (حداکثر ۵۰)",
                "/stuck — خطاهای تحویل به ربات فروش",
                "/waiting [n] — پرداخت‌های در انتظار تأیید درگاه (حداکثر ۵۰)",
                "/expired [n] — لینک‌های منقضی‌شده (حداکثر ۵۰)",
                "/manual_review — بررسی‌های دستی باز (تعیین‌تکلیف‌نشده)",
                "/resolved_reviews [n] — بررسی‌های تعیین‌تکلیف‌شده (حداکثر ۵۰)",
                "/errors — خلاصهٔ خطاهای ۲۴ ساعت اخیر",
                "/payment ORDER_ID — جزئیات یک پرداخت",
                "/retry_queue — صف ارسال به ربات فروش",
                "/resend_failed — پیش‌نمایش ارسال مجدد موارد تحویل‌نشده",
                "/resend_failed confirm — بازگرداندن گروهی به صف، فقط در حالت idempotent",
                "/backup_status — وضعیت پشتیبان‌گیری",
                "/version — نسخهٔ برنامه و مهاجرت",
                "/fee — کارمزد فعلی (فقط‌خواندنی؛ تغییر فقط از CLI سرور)",
            ]
        )
        return self._split(text)

    def cmd_fee(self, db: Session, args: list[str]) -> list[str]:
        """Read-only fee view. This bot can never set, schedule, cancel, or
        mutate fee policies — only the host CLI may change them."""
        from app.adminbot.format import fmt_fee_rate
        from app.services.fees import next_scheduled_policy, select_effective_policy

        active = select_effective_policy(db)
        scheduled = next_scheduled_policy(db)
        lines = ["<b>کارمزد خدمات</b>", ""]
        if active is None:
            lines.append("کارمزد فعلی: 0% (هیچ سیاستی ثبت نشده)")
        else:
            lines.append(f"کارمزد فعلی: <b>{fmt_fee_rate(active.rate_bps)}</b>")
            lines.append(f"شناسهٔ سیاست: {active.id}")
            lines.append(
                f"اجرا از: {fmt_time(active.effective_at, self._settings.admin_bot_timezone)}"
            )
        if scheduled is not None:
            lines.append(
                f"سیاست بعدی: {fmt_fee_rate(scheduled.rate_bps)} از "
                f"{fmt_time(scheduled.effective_at, self._settings.admin_bot_timezone)}"
                f" (شناسه {scheduled.id})"
            )
        lines.append("")
        lines.append("تغییر کارمزد فقط روی سفارش‌های جدید اثر دارد؛ سفارش‌های موجود")
        lines.append("با همان کارمزد ثبت‌شدهٔ خود می‌مانند. تغییر فقط از طریق")
        lines.append("<code>centralpay fee</code> روی سرور ممکن است.")
        return self._split("\n".join(lines))

    def cmd_status(self, db: Session, args: list[str]) -> list[str]:
        api = self._probe_api()
        db_ok = queries.database_ok(db)
        heartbeat_age = queries.worker_heartbeat_age_seconds(db)
        worker_ok = heartbeat_age is not None and heartbeat_age < 120
        pending = queries.count_by_status(db, "bot_notify_pending")
        # Open (unresolved) reviews only: rows resolved via the host CLI stay
        # in manual_review as history but no longer need operator attention.
        review = queries.count_open_manual_reviews(db)
        getlink_failures = queries.event_count_since(db, "centralpay_getlink_failed")
        verify_failures = queries.event_count_since(db, "centralpay_verify_failed")
        backup = queries.latest_backup_alert(db, "backup_succeeded")
        tz = self._settings.admin_bot_timezone
        lines = [
            "<b>وضعیت CentralPay Bridge</b>",
            "",
            f"API: {_mark(api['ready'])}",
            f"پایگاه‌داده: {_mark(db_ok)}",
            f"ورکر اعلان: {_mark(worker_ok)}"
            + (
                f" (آخرین فعالیت {int(heartbeat_age)} ثانیه پیش)"
                if heartbeat_age is not None
                else " (بدون ضربان ثبت‌شده)"
            ),
            f"ربات مدیریتی: {OK} فعال",
            "پروکسی معکوس: از داخل کانتینر قابل مشاهده نیست",
            "",
            f"نسخه: {esc(APP_VERSION)} — محیط: {esc(self._settings.environment)}",
            "",
            f"{PENDING} در صف ارسال: {fmt_amount(pending)}",
            f"{REVIEW} بررسی دستی: {fmt_amount(review)}",
            f"{FAIL} خطای ایجاد لینک (۲۴س): {fmt_amount(getlink_failures)}",
            f"{FAIL} خطای تأیید (۲۴س): {fmt_amount(verify_failures)}",
            "",
            "آخرین پشتیبان موفق: "
            + (fmt_time(backup.created_at, tz) if backup else "ثبت نشده"),
            f"زمان کنونی: {fmt_time(datetime.now(UTC), tz)}",
        ]
        return self._split("\n".join(lines))

    def cmd_health(self, db: Session, args: list[str]) -> list[str]:
        api = self._probe_api()
        db_ok = queries.database_ok(db)
        heartbeat = queries.latest_worker_heartbeat(db)
        heartbeat_age = queries.worker_heartbeat_age_seconds(db)
        snapshot = queries.retry_queue_snapshot(db)
        tz = self._settings.admin_bot_timezone
        lines = [
            "<b>سلامت اجزا</b>",
            "",
            f"live: {_mark(api['live'])}",
            f"ready: {_mark(api['ready'])}",
            f"اتصال پایگاه‌داده: {_mark(db_ok)}",
            "ضربان ورکر: "
            + (
                f"{_mark(heartbeat_age is not None and heartbeat_age < 120)} "
                f"({int(heartbeat_age)} ثانیه پیش)"
                if heartbeat_age is not None
                else f"{FAIL} ثبت نشده"
            ),
            "آخرین چرخهٔ ورکر: "
            + (fmt_time(heartbeat.last_cycle_at, tz) if heartbeat else "—"),
            f"صف ارسال: {len(snapshot['due'])} سررسید، "
            f"{len(snapshot['scheduled'])} زمان‌بندی‌شده، "
            f"{len(snapshot['claimed'])} در حال ارسال",
        ]
        return self._split("\n".join(lines))

    def cmd_recent(self, db: Session, args: list[str]) -> list[str]:
        limit = RECENT_DEFAULT
        if args and args[0].isdigit():
            limit = min(int(args[0]), RECENT_MAX)
        payments = queries.recent_payments(db, limit)
        if not payments:
            return ["هنوز پرداختی ثبت نشده است."]
        tz = self._settings.admin_bot_timezone
        blocks = [f"<b>آخرین {len(payments)} پرداخت</b>"]
        for payment in payments:
            reason = f" — <code>{esc(payment.bot_notify_reason)}</code>" if (
                payment.bot_notify_reason
            ) else ""
            reference = f" — پیگیری {esc(payment.reference_id)}" if payment.reference_id else ""
            blocks.append(
                f"• <b>{esc(payment.bot_order_id)}</b> ({payment.gateway_order_id})\n"
                f"  {fmt_amount(payment.amount)} تومان — "
                f"{payment_status_fa(payment.status)}{reason}{reference}\n"
                f"  {fmt_time(payment.created_at, tz)}"
            )
        return self._split("\n".join(blocks))

    def cmd_stuck(self, db: Session, args: list[str]) -> list[str]:
        """ONLY bot-delivery problems: payments that failed, or are stuck
        trying, to reach the customer bot's webhook (open manual-review rows
        whose `bot_notify_reason` is set, plus stale/old bot_notify_pending
        rows). See app.adminbot.queries.bot_delivery_stuck_entries /
        _bot_delivery_manual_review_conditions for the exact predicate.

        Deliberately excludes financial/verification manual-review rows
        (amount/user/reference mismatches — never a delivery problem),
        reconciliation-exhausted rows, and unexpected-status rows: those
        remain visible via /manual_review, /errors, and the "other" summary
        line here, never mixed into the detailed list.

        Intentional UX change from the previous /stuck: no longer accepts a
        numeric argument (it showed three unrelated categories at once, so a
        display limit applied to all of them together; no existing test or
        documented usage relied on `/stuck N`). At most STUCK_DETAIL_MAX
        entries are shown, chosen once a truthful bot-delivery total is
        already known — never split_message'd into multiple messages:
        entries are progressively reduced until the single message fits
        admin_bot_max_message_length.
        """
        now = datetime.now(UTC)
        settings = self._settings
        bot_delivery_total = queries.count_bot_delivery_problems(
            db, pending_age_minutes=30
        )
        waiting_total = stuck_service.count_waiting(db, settings, now=now)
        expired_total = stuck_service.count_expired(db, settings, now=now)
        other_total = stuck_service.count_other_attention(db, settings, now=now)
        entries = queries.bot_delivery_stuck_entries(
            db,
            claim_timeout_seconds=settings.bot_notify_claim_timeout_seconds,
            limit=STUCK_DETAIL_MAX,
        )
        header = self._stuck_header(bot_delivery_total, waiting_total, expired_total, other_total)
        shown_count = len(entries)
        text = self._assemble_stuck_message(header, entries, bot_delivery_total, now)
        while len(text) > settings.admin_bot_max_message_length and shown_count > 0:
            shown_count -= 1
            text = self._assemble_stuck_message(
                header, entries[:shown_count], bot_delivery_total, now
            )
        return [text]

    def _stuck_header(
        self, bot_delivery_total: int, waiting_total: int, expired_total: int, other_total: int
    ) -> list[str]:
        status_line = (
            "🟠 وضعیت کلی: نیازمند توجه" if bot_delivery_total > 0 else f"{OK} وضعیت کلی: سالم"
        )
        lines = [
            "⚠️ گزارش پرداخت‌های نیازمند بررسی",
            "",
            status_line,
            "",
            "📊 خلاصه",
            f"• خطای ارسال به ربات: {fmt_amount(bot_delivery_total)}",
            f"• در انتظار تأیید درگاه: {fmt_amount(waiting_total)}",
            f"• لینک‌های منقضی‌شده: {fmt_amount(expired_total)}",
        ]
        if other_total > 0:
            lines.append(f"• سایر موارد نیازمند بررسی: {fmt_amount(other_total)}")
        return lines

    def _assemble_stuck_message(
        self,
        header: list[str],
        shown_entries: list[queries.StuckEntry],
        total: int,
        now: datetime,
    ) -> str:
        lines = list(header)
        lines.append("")
        if total == 0:
            lines.append(f"{OK} خطایی در تحویل به ربات وجود ندارد.")
        else:
            lines.append("🔥 خطاهای ارسال به ربات")
            for index, entry in enumerate(shown_entries, start=1):
                lines.append("")
                lines.append(bot_delivery_entry_block(index, entry, now))
        lines.append("")
        lines.append("━━━━━━━━━━━━━━")
        if total > 0:
            lines.append(f"نمایش {fmt_amount(len(shown_entries))} مورد از {fmt_amount(total)} مورد")
            remaining = total - len(shown_entries)
            if remaining > 0:
                lines.append(f"+ {fmt_amount(remaining)} مورد دیگر نمایش داده نشد")
            lines.append("")
        lines.append("برای مشاهده:")
        lines.append("🟡 /waiting — در انتظار درگاه")
        lines.append("⚪ /expired — لینک‌های منقضی‌شده")
        lines.append("")
        lines.append(f"🕒 به‌روزرسانی: {fmt_time_only_fa(now, self._settings.admin_bot_timezone)}")
        return "\n".join(lines)

    def cmd_waiting(self, db: Session, args: list[str]) -> list[str]:
        """ONLY StuckCategory.WAITING_GATEWAY payments, longest-waiting
        (oldest link-age anchor) first — the operator's most urgent view.
        Read-only: no verify action, no retry action, no mutation."""
        limit = _parse_list_count(args)
        if limit is None:
            return [WAITING_USAGE_ERROR]
        now = datetime.now(UTC)
        settings = self._settings
        total = stuck_service.count_waiting(db, settings, now=now)
        entries = stuck_service.waiting_entries_by_urgency(db, settings, now=now, limit=limit)
        lines = ["🟡 <b>پرداخت‌های در انتظار تأیید درگاه</b>", "", f"تعداد کل: {fmt_amount(total)}"]
        if not entries:
            lines.append("")
            lines.append(f"{OK} در حال حاضر پرداختی در انتظار تأیید درگاه نیست.")
        else:
            for index, entry in enumerate(entries, start=1):
                lines.append("")
                lines.append(waiting_entry_block(index, entry, now))
            lines.append("")
            lines.append("━━━━━━━━━━━━━━")
            lines.append(f"نمایش {fmt_amount(len(entries))} مورد از {fmt_amount(total)} مورد")
            if total > len(entries):
                suggested = min(total, LIST_MAX)
                lines.append("")
                lines.append(f"/waiting {suggested} — نمایش {suggested} مورد")
        return self._split("\n".join(lines))

    def cmd_expired(self, db: Session, args: list[str]) -> list[str]:
        """ONLY StuckCategory.EXPIRED payments, MOST RECENTLY expired
        (newest link-age anchor still past the cutoff) first — showing the
        oldest legacy rows by default would bury what just expired. Read-only:
        no verify action, no retry action, no mutation."""
        limit = _parse_list_count(args)
        if limit is None:
            return [EXPIRED_USAGE_ERROR]
        now = datetime.now(UTC)
        settings = self._settings
        total = stuck_service.count_expired(db, settings, now=now)
        entries = stuck_service.expired_entries_by_recency(db, settings, now=now, limit=limit)
        lines = ["⚪ <b>لینک‌های منقضی‌شده</b>", "", f"تعداد کل: {fmt_amount(total)}"]
        if not entries:
            lines.append("")
            lines.append(f"{OK} در حال حاضر لینک منقضی‌شده‌ای وجود ندارد.")
        else:
            for index, entry in enumerate(entries, start=1):
                lines.append("")
                lines.append(expired_entry_block(index, entry, now))
            lines.append("")
            lines.append("━━━━━━━━━━━━━━")
            lines.append(f"نمایش {fmt_amount(len(entries))} مورد از {fmt_amount(total)} مورد")
            if total > len(entries):
                suggested = min(total, LIST_MAX)
                lines.append("")
                lines.append(f"/expired {suggested} — نمایش {suggested} مورد")
        return self._split("\n".join(lines))

    def cmd_manual_review(self, db: Session, args: list[str]) -> list[str]:
        payments = queries.manual_review_payments(db)
        if not payments:
            return [f"{OK} هیچ پرداختی در بررسی دستی نیست."]
        blocks = [f"<b>{REVIEW} بررسی دستی ({len(payments)})</b>"]
        now = datetime.now(UTC)
        for payment in payments:
            review_at = payment.manual_review_at
            if review_at is not None and review_at.tzinfo is None:
                review_at = review_at.replace(tzinfo=UTC)
            age_hours = (
                round((now - review_at).total_seconds() / 3600, 1) if review_at else None
            )
            reason = payment.bot_notify_reason or payment.last_error or "—"
            blocks.append(
                "\n".join(
                    [
                        f"• <b>{esc(payment.bot_order_id)}</b> ({payment.gateway_order_id})",
                        f"  مبلغ: {fmt_amount(payment.amount)} تومان — درگاه: "
                        + (OK if payment.gateway_verified_at is not None else FAIL),
                        f"  دلیل: <code>{esc(reason)}</code>",
                        f"  تلاش‌ها: {payment.bot_notify_attempts} — "
                        f"آخرین HTTP: {payment.bot_last_http_status or '—'}",
                        f"  پیگیری: {esc(payment.reference_id or '—')} — "
                        + (f"قدمت: {age_hours} ساعت" if age_hours is not None else "قدمت: —"),
                    ]
                )
            )
        return self._split("\n".join(blocks))

    def cmd_resolved_reviews(self, db: Session, args: list[str]) -> list[str]:
        """Read-only history of manual reviews resolved via the host CLI
        (``centralpay review resolve``). Newest resolution first."""
        limit = RECENT_DEFAULT
        if args and args[0].isdigit():
            limit = min(int(args[0]), RECENT_MAX)
        payments = queries.resolved_review_payments(db, limit)
        if not payments:
            return ["هنوز هیچ بررسی دستی تعیین‌تکلیف‌شده‌ای ثبت نشده است."]
        tz = self._settings.admin_bot_timezone
        blocks = [f"<b>{OK} بررسی‌های تعیین‌تکلیف‌شده ({len(payments)})</b>"]
        for payment in payments:
            reason = payment.bot_notify_reason or "—"
            blocks.append(
                "\n".join(
                    [
                        f"• <b>{esc(payment.bot_order_id)}</b> ({payment.gateway_order_id})",
                        f"  مبلغ: {fmt_amount(payment.amount)} تومان",
                        f"  دلیل ارجاع: <code>{esc(reason)}</code>",
                        f"  نتیجه: <code>{esc(payment.review_resolution or '—')}</code>",
                        f"  زمان تعیین‌تکلیف: {fmt_time(payment.review_resolved_at, tz)}",
                    ]
                )
            )
        return self._split("\n".join(blocks))

    def cmd_errors(self, db: Session, args: list[str]) -> list[str]:
        summary = queries.errors_summary(db)
        if not summary:
            return [f"{OK} در ۲۴ ساعت اخیر خطایی ثبت نشده است."]
        labels = {
            "centralpay_getlink_failed": "خطای ایجاد لینک",
            "centralpay_verify_failed": "خطای تأیید",
            "verify_payable_amount_mismatch": "مغایرت مبلغ",
            "verify_user_id_mismatch": "مغایرت شناسهٔ کاربر",
            "verify_missing_reference_id": "نبود کد پیگیری",
            "verify_invalid_reference_id": "کد پیگیری نامعتبر از درگاه",
            "bot_notification_failed": "خطای تحویل به ربات",
            "bot_timeout_ambiguous": "تایم‌اوت مبهم",
            "notification_recovered_after_restart": "بازیابی ورکر",
            "backup_failed": "خطای پشتیبان‌گیری",
            "callback_signature_failures": "امضای نامعتبر کال‌بک",
        }
        lines = ["<b>خطاهای ۲۴ ساعت اخیر</b>", ""]
        for event_type, count in sorted(summary.items(), key=lambda kv: -kv[1]):
            label = labels.get(event_type, event_type)
            lines.append(f"{FAIL} {label}: {count} — <code>{esc(event_type)}</code>")
        return self._split("\n".join(lines))

    def cmd_payment(self, db: Session, args: list[str]) -> list[str]:
        if not args:
            return ["استفاده: /payment ORDER_ID"]
        identifier = args[0][:128]
        payment = queries.find_payment(db, identifier)
        record_event(
            db,
            payment_id=payment.id if payment else None,
            event_type="admin_command_received",
            data={"command": "payment_lookup"},
        )
        db.commit()
        if payment is None:
            return ["پرداختی با این شناسه پیدا نشد."]
        tz = self._settings.admin_bot_timezone
        lines = [f"<b>جزئیات پرداخت #{payment.id}</b>", "", payment_block(payment, tz)]
        if payment.card_last4:
            lines.append(f"چهار رقم آخر کارت:\n{esc(payment.card_last4)}")
        if payment.bot_last_http_status:
            lines.append(f"آخرین وضعیت HTTP:\n{payment.bot_last_http_status}")
        if payment.next_retry_at:
            lines.append(f"تلاش بعدی:\n{fmt_time(payment.next_retry_at, tz)}")
        if payment.manual_review_at:
            lines.append(f"زمان ارجاع به بررسی:\n{fmt_time(payment.manual_review_at, tz)}")
        if payment.review_resolved_at:
            lines.append(
                f"تعیین‌تکلیف بررسی:\n{fmt_time(payment.review_resolved_at, tz)}"
                f"\nنتیجه: <code>{esc(payment.review_resolution or '—')}</code>"
            )
        lines.append(f"به‌روزرسانی:\n{fmt_time(payment.updated_at, tz)}")
        events = queries.payment_events(db, payment.id)
        if events:
            lines.append("")
            lines.append(f"<b>آخرین {len(events)} رویداد</b>")
            for event in reversed(events):
                lines.append(
                    f"• {fmt_time(event.created_at, tz)} — <code>{esc(event.event_type)}</code>"
                )
        return self._split("\n\n".join(lines))

    def cmd_retry_queue(self, db: Session, args: list[str]) -> list[str]:
        snapshot = queries.retry_queue_snapshot(db)
        tz = self._settings.admin_bot_timezone
        lines = ["<b>صف ارسال به ربات فروش</b>", ""]
        sections = (
            ("سررسید", snapshot["due"], PENDING),
            ("زمان‌بندی‌شده", snapshot["scheduled"], "🕐"),
            ("در حال ارسال", snapshot["claimed"], "📤"),
            ("پایان تلاش‌ها", snapshot["retry_limit"], REVIEW),
        )
        for title, payments, icon in sections:
            lines.append(f"{icon} <b>{title} ({len(payments)})</b>")
            for payment in payments[:10]:
                retry = (
                    fmt_time(payment.next_retry_at, tz) if payment.next_retry_at else "—"
                )
                lines.append(
                    f"• {esc(payment.bot_order_id)} — "
                    f"<code>{esc(payment.bot_notify_reason or 'queued')}</code> — {retry}"
                )
            lines.append("")
        return self._split("\n".join(lines))

    def cmd_backup_status(self, db: Session, args: list[str]) -> list[str]:
        ok = queries.latest_backup_alert(db, "backup_succeeded")
        failed = queries.latest_backup_alert(db, "backup_failed")
        tz = self._settings.admin_bot_timezone
        lines = ["<b>وضعیت پشتیبان‌گیری</b>", ""]
        if ok is not None:
            payload = ok.payload or {}
            lines.append(f"{OK} آخرین موفق: {fmt_time(ok.created_at, tz)}")
            if payload.get("size"):
                lines.append(f"حجم: {esc(payload['size'])}")
            lines.append("اعتبارسنجی: انجام‌شده (pg_restore --list)")
            if payload.get("retention_days"):
                lines.append(f"نگه‌داری: {esc(payload['retention_days'])} روز")
        else:
            lines.append(f"{WARN} هنوز پشتیبان موفقی ثبت نشده است.")
        if failed is not None:
            lines.append(f"{FAIL} آخرین خطا: {fmt_time(failed.created_at, tz)}")
        lines.append("زمان‌بندی: هر شب ساعت ۰۳:۱۵ به وقت سرور")
        return self._split("\n".join(lines))

    def cmd_version(self, db: Session, args: list[str]) -> list[str]:
        lines = [
            f"نسخهٔ برنامه: <code>{esc(APP_VERSION)}</code>",
            f"محیط: {esc(self._settings.environment)}",
            f"نسخهٔ مهاجرت: <code>{esc(queries.migration_revision(db))}</code>",
        ]
        if self._settings.git_commit_sha:
            lines.append(f"کامیت: <code>{esc(self._settings.git_commit_sha[:12])}</code>")
        return self._split("\n".join(lines))

    def cmd_resend_failed(
        self, db: Session, ctx: UpdateContext, args: list[str]
    ) -> list[str]:
        """Preview (default) or execute (``confirm``) a bulk requeue of
        delivery-failed manual-review payments.

        This bot never calls the customer bot: it only changes notification
        state so the existing worker performs the real delivery. Requires
        idempotent retry mode; in safe mode BOTH preview and confirm are
        rejected with a fixed message and no row is modified.
        """
        if self._settings.bot_notify_retry_mode != "idempotent":
            return self._split(BULK_RESEND_SAFE_MODE_MESSAGE)

        confirm = bool(args) and args[0].strip().lower() == "confirm"
        if not confirm:
            preview = preview_bulk_resend(db)
            return self._split(self._render_resend_preview(preview))

        result = requeue_failed_deliveries(
            db, telegram_user_id=ctx.user_id, now=datetime.now(UTC)
        )
        return self._split(self._render_resend_result(result))

    def _render_resend_preview(self, preview: BulkResendPreview) -> str:
        lines = [
            "<b>پیش‌نمایش ارسال مجدد گروهی</b>",
            "",
            f"پرداخت‌های واجد شرایط: {fmt_amount(preview.count)}",
            f"مبلغ اصلی مجموع: {fmt_amount(preview.total_amount)} تومان",
            "",
            f"{WARN} هنوز هیچ ارسال شبکه‌ای انجام نشده است.",
        ]
        if preview.order_ids:
            lines.append("")
            lines.append(f"شناسه‌ها (حداکثر {PREVIEW_ORDER_LIMIT}):")
            lines.extend(f"• <code>{esc(order_id)}</code>" for order_id in preview.order_ids)
        else:
            lines.append("")
            lines.append("هیچ پرداخت واجد شرایطی برای ارسال مجدد وجود ندارد.")
        lines.append("")
        lines.append("برای اجرا این دستور را بفرستید:")
        lines.append("/resend_failed confirm")
        return "\n".join(lines)

    def _render_resend_result(self, result: BulkResendResult) -> str:
        # Requeued for DELIVERY only — never a claim that credit was applied.
        lines = [
            f"{OK} {fmt_amount(result.requeued_count)} پرداخت دوباره وارد صف ارسال شد.",
            "",
            f"مبلغ اصلی مجموع: {fmt_amount(result.total_amount)} تومان",
            "ارسال واقعی توسط Worker انجام می‌شود.",
            "شمارنده تلاش‌ها بازنشانی نشد.",
        ]
        if result.skipped_count > 0:
            lines.append(
                f"{WARN} {fmt_amount(result.skipped_count)} مورد به‌دلیل پردازش "
                "همزمان توسط اجرای دیگری رد شد."
            )
        lines.append("")
        lines.append("برای مشاهده صف:")
        lines.append("/retry_queue")
        return "\n".join(lines)

    # -- helpers -----------------------------------------------------------

    def _probe_api(self) -> dict[str, bool]:
        try:
            return self._api_probe()
        except Exception:
            return {"live": False, "ready": False}


def default_api_probe(settings: Settings) -> ApiProbe:
    def probe() -> dict[str, bool]:
        import httpx

        base = settings.admin_bot_api_url.rstrip("/")
        result = {"live": False, "ready": False}
        with httpx.Client(timeout=5) as client:
            for key, path in (("live", "/health/live"), ("ready", "/health/ready")):
                try:
                    result[key] = client.get(f"{base}{path}").status_code == 200
                except httpx.HTTPError:
                    result[key] = False
        return result

    return probe
