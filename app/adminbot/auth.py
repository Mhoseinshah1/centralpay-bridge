"""Authorization for admin bot updates.

Authorization is by numeric Telegram user ID only — usernames are never
consulted. Private chats only. Unauthorized users get a generic denial that
reveals nothing about configuration, state, or payments.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger("app.adminbot.auth")

GENERIC_DENIAL = "دسترسی مجاز نیست."


@dataclass(frozen=True)
class UpdateContext:
    user_id: int | None
    chat_id: int | None
    chat_type: str | None  # "private", "group", "supergroup", "channel"
    username: str | None = None  # informational only; NEVER used for auth


def is_authorized(admin_ids: tuple[int, ...], ctx: UpdateContext) -> bool:
    if ctx.user_id is None or ctx.chat_type != "private":
        return False
    return ctx.user_id in admin_ids


def log_unauthorized(ctx: UpdateContext, command: str) -> None:
    # Only IDs and the command name — never message content.
    logger.warning(
        "admin_bot_unauthorized_access",
        extra={
            "telegram_user_id": ctx.user_id,
            "chat_id": ctx.chat_id,
            "chat_type": ctx.chat_type,
            "command": command,
        },
    )
