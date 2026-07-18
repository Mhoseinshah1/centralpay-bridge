"""Admin bot authorization: numeric IDs only, private chats only."""

import pytest

from app.adminbot.auth import GENERIC_DENIAL, UpdateContext, is_authorized
from app.adminbot.commands import CommandHandlers
from app.config import ConfigurationError, parse_admin_telegram_ids, validate_admin_bot_settings
from tests.conftest import TEST_ADMIN_ID, TEST_ADMIN_ID_2, event_types, get_events

ADMIN_IDS = (TEST_ADMIN_ID, TEST_ADMIN_ID_2)


def _handlers(session_factory, admin_settings) -> CommandHandlers:
    return CommandHandlers(
        session_factory,
        admin_settings,
        ADMIN_IDS,
        api_probe=lambda: {"live": True, "ready": True},
    )


def _ctx(user_id, chat_type="private", username=None):
    return UpdateContext(
        user_id=user_id, chat_id=user_id, chat_type=chat_type, username=username
    )


def test_configured_administrator_is_allowed(session_factory, admin_settings):
    handlers = _handlers(session_factory, admin_settings)
    replies = handlers.handle(_ctx(TEST_ADMIN_ID), "help", [])
    assert replies
    assert GENERIC_DENIAL not in replies[0]
    assert "/status" in replies[0]


def test_unauthorized_user_gets_generic_denial_only(session_factory, admin_settings):
    handlers = _handlers(session_factory, admin_settings)
    replies = handlers.handle(_ctx(999999999), "status", [])
    assert replies == [GENERIC_DENIAL]
    # The denial reveals nothing: no state, versions, counts, or IDs.
    assert str(TEST_ADMIN_ID) not in replies[0]

    events = get_events(session_factory)
    assert "admin_bot_unauthorized_access" in event_types(events)
    unauthorized = [e for e in events if e.event_type == "admin_bot_unauthorized_access"]
    data = unauthorized[0].data
    assert data is not None
    assert data["telegram_user_id"] == 999999999
    assert data["command"] == "status"


def test_username_alone_cannot_grant_access(session_factory, admin_settings):
    handlers = _handlers(session_factory, admin_settings)
    # An attacker with a matching username but a different numeric ID.
    replies = handlers.handle(
        _ctx(555000555, username="legit_admin_username"), "status", []
    )
    assert replies == [GENERIC_DENIAL]


def test_group_chats_are_denied_even_for_admins(session_factory, admin_settings):
    handlers = _handlers(session_factory, admin_settings)
    for chat_type in ("group", "supergroup", "channel", None):
        replies = handlers.handle(_ctx(TEST_ADMIN_ID, chat_type=chat_type), "status", [])
        assert replies == [GENERIC_DENIAL], chat_type
    assert is_authorized(ADMIN_IDS, _ctx(TEST_ADMIN_ID, chat_type="private"))


def test_missing_user_id_is_denied(session_factory, admin_settings):
    handlers = _handlers(session_factory, admin_settings)
    assert handlers.handle(_ctx(None), "status", []) == [GENERIC_DENIAL]


def test_invalid_admin_ids_configuration_fails_safely(settings):
    for bad in ("abc", "123,@user", "123,-5", "0", "123;456"):
        with pytest.raises(ConfigurationError):
            parse_admin_telegram_ids(bad)
    # ...but constructing Settings with a bad value never raises, so the
    # API and worker can always start.
    broken = settings.model_copy(update={"admin_telegram_ids": "not-numeric"})
    assert broken.admin_telegram_ids == "not-numeric"

    enabled = settings.model_copy(
        update={
            "admin_bot_enabled": True,
            "admin_bot_token": "1:t",
            "admin_telegram_ids": "not-numeric",
        }
    )
    with pytest.raises(ConfigurationError):
        validate_admin_bot_settings(enabled)


def test_parse_admin_ids_supports_multiple_and_dedup():
    assert parse_admin_telegram_ids("123456789,987654321") == (123456789, 987654321)
    assert parse_admin_telegram_ids(" 123 , 123 ,456 ") == (123, 456)
    assert parse_admin_telegram_ids("") == ()


def test_admin_bot_validation_requires_token_and_ids(admin_settings):
    ids = validate_admin_bot_settings(admin_settings)
    assert ids == ADMIN_IDS
    no_token = admin_settings.model_copy(update={"admin_bot_token": ""})
    with pytest.raises(ConfigurationError, match="ADMIN_BOT_TOKEN"):
        validate_admin_bot_settings(no_token)
    no_ids = admin_settings.model_copy(update={"admin_telegram_ids": ""})
    with pytest.raises(ConfigurationError, match="ADMIN_TELEGRAM_IDS"):
        validate_admin_bot_settings(no_ids)
