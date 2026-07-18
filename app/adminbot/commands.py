"""Read-only admin bot command handlers.

Handlers are plain functions decoupled from the Telegram library so they can
be tested directly. Every handler returns a list of pre-split message
strings. All dynamic values are escaped; secrets, redirect URLs, signatures,
full card numbers, and untrusted external error text never appear in output.
"""

import logging
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
    esc,
    fmt_amount,
    fmt_time,
    payment_block,
    payment_status_fa,
    split_message,
)
from app.audit import record_event
from app.config import Settings
from app.version import APP_VERSION

logger = logging.getLogger("app.adminbot.commands")

RECENT_DEFAULT = 10
RECENT_MAX = 50

# check_api_health() -> {"live": bool, "ready": bool}
ApiProbe = Callable[[], dict[str, bool]]


def _mark(ok: bool) -> str:
    return OK if ok else FAIL


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
        handler = self._registry().get(command)
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

    def _registry(self) -> dict[str, Callable[[Session, list[str]], list[str]]]:
        return {
            "start": self.cmd_start,
            "help": self.cmd_help,
            "status": self.cmd_status,
            "health": self.cmd_health,
            "recent": self.cmd_recent,
            "stuck": self.cmd_stuck,
            "manual_review": self.cmd_manual_review,
            "errors": self.cmd_errors,
            "payment": self.cmd_payment,
            "retry_queue": self.cmd_retry_queue,
            "backup_status": self.cmd_backup_status,
            "version": self.cmd_version,
            "fee": self.cmd_fee,
        }

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
                "دستورها: /status /health /recent /stuck /manual_review",
                "/errors /payment /retry_queue /backup_status /version /fee /help",
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
                "/stuck — پرداخت‌های نیازمند توجه با دلیل دقیق",
                "/manual_review — پرداخت‌های در بررسی دستی",
                "/errors — خلاصهٔ خطاهای ۲۴ ساعت اخیر",
                "/payment ORDER_ID — جزئیات یک پرداخت",
                "/retry_queue — صف ارسال به ربات فروش",
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
        review = queries.count_by_status(db, "manual_review")
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
        entries = queries.stuck_payments(
            db,
            claim_timeout_seconds=self._settings.bot_notify_claim_timeout_seconds,
        )
        if not entries:
            return [f"{OK} هیچ پرداختی نیازمند توجه نیست."]
        tz = self._settings.admin_bot_timezone
        blocks = [f"<b>پرداخت‌های نیازمند توجه ({len(entries)})</b>"]
        for entry in entries:
            payment = entry.payment
            blocks.append(
                f"• <b>{esc(payment.bot_order_id)}</b> — "
                f"{fmt_amount(payment.amount)} تومان\n"
                f"  دسته: <code>{esc(entry.category)}</code>\n"
                f"  تلاش‌ها: {payment.bot_notify_attempts} — "
                f"ایجاد: {fmt_time(payment.created_at, tz)}"
            )
        return self._split("\n".join(blocks))

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
