"""Admin bot service runtime: long polling plus background loops.

python-telegram-bot handles Telegram long polling; command logic lives in
CommandHandlers (library-independent). Background asyncio tasks run the
alert delivery loop, health monitor, and daily report scheduler. A
heartbeat file is touched every loop for the container liveness check.
"""

import asyncio
import logging
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from app.adminbot.alerts import alert_delivery_pass
from app.adminbot.auth import UpdateContext
from app.adminbot.commands import CommandHandlers, default_api_probe
from app.adminbot.health import HealthMonitor
from app.adminbot.reports import maybe_queue_daily_report
from app.adminbot.telegram import TelegramAlertSender
from app.config import Settings

logger = logging.getLogger("app.adminbot.runner")


def parse_command(text: str) -> tuple[str, list[str]] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    command = parts[0][1:].split("@", 1)[0].lower()
    return command, parts[1:]


def build_update_context(update: Update) -> UpdateContext | None:
    message = update.effective_message
    if message is None:
        return None
    user = update.effective_user
    chat = update.effective_chat
    return UpdateContext(
        user_id=user.id if user else None,
        chat_id=chat.id if chat else None,
        chat_type=chat.type if chat else None,
        username=user.username if user else None,
    )


class AdminBotService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        admin_ids: tuple[int, ...],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.admin_ids = admin_ids
        self.handlers = CommandHandlers(
            session_factory, settings, admin_ids, default_api_probe(settings)
        )
        self.sender = TelegramAlertSender(settings.admin_bot_token)
        self.monitor = HealthMonitor(settings, session_factory, default_api_probe(settings))
        self.stop_event = asyncio.Event()

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        ctx = build_update_context(update)
        if message is None or ctx is None or not message.text:
            return
        parsed = parse_command(message.text)
        if parsed is None:
            return
        command, args = parsed
        replies = await asyncio.to_thread(self.handlers.handle, ctx, command, args)
        for reply in replies:
            try:
                await message.reply_text(
                    reply, parse_mode="HTML", disable_web_page_preview=True
                )
            except Exception:
                logger.exception("admin_reply_failed", extra={"command": command})
                break

    async def background_loop(self) -> None:
        heartbeat = Path(self.settings.admin_bot_heartbeat_file)
        interval = self.settings.admin_bot_alert_poll_interval_seconds
        health_every = max(
            1, int(self.settings.admin_bot_health_check_interval_seconds // interval)
        )
        tick = 0
        while not self.stop_event.is_set():
            try:
                await alert_delivery_pass(
                    self.session_factory, self.sender, self.settings, self.admin_ids
                )
                if tick % health_every == 0:
                    await asyncio.to_thread(self.monitor.run_once)

                    def _report() -> None:
                        with self.session_factory() as db:
                            maybe_queue_daily_report(db, self.settings)

                    await asyncio.to_thread(_report)
                try:
                    heartbeat.touch()
                except OSError:
                    logger.warning("admin_heartbeat_write_failed")
            except Exception:
                logger.exception("admin_background_pass_failed")
            tick += 1
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    def build_application(self) -> Application:  # type: ignore[type-arg]
        application = ApplicationBuilder().token(self.settings.admin_bot_token).build()
        application.add_handler(MessageHandler(filters.TEXT, self.on_message))
        return application

    async def run(self) -> None:
        application = self.build_application()
        background = asyncio.create_task(self.background_loop())
        async with application:
            await application.start()
            assert application.updater is not None
            await application.updater.start_polling(allowed_updates=["message"])
            logger.info(
                "admin_bot_started",
                extra={"administrators": len(self.admin_ids)},
            )
            try:
                await self.stop_event.wait()
            finally:
                await application.updater.stop()
                await application.stop()
        background.cancel()
