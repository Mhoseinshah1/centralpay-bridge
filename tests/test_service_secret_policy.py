"""Per-service secret least privilege in Docker Compose.

The shared env file carries every credential; each application service's
``environment:`` block must mask everything outside that service's role
allowlist (Compose ``environment`` entries override ``env_file`` values).
The matrix test simulates the shared env file with sentinel values, applies
each service's overrides, and fails when any service — including a NEW
service attached to the shared env anchor — would receive a credential
outside its allowlist.

Role allowlist:
- API: payment ingress and CentralPay credentials
- Worker: customer-bot notification credential
- Admin bot: Telegram administration credential
- Migrate: database only
- Caddy: no application credentials
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.adminbot import alerts
from app.config import (
    Settings,
    validate_admin_bot_settings,
    validate_bot_notification_settings,
)
from tests.conftest import (
    TEST_ADMIN_BOT_TOKEN,
    TEST_ADMIN_ID,
    TEST_BOT_TOKEN,
    TEST_CALLBACK_HMAC_SECRET,
    TEST_GETLINK_API_KEY,
    TEST_INBOUND_API_KEY,
    TEST_VERIFY_API_KEY,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"

# Secret-bearing or credential-like fields distributed via the shared env
# file. ADMIN_TELEGRAM_IDS is not an authentication secret but is treated
# as a private operational identifier.
CREDENTIAL_VARS = (
    "DATABASE_URL",
    "INBOUND_API_KEY",
    "CALLBACK_HMAC_SECRET",
    "CENTRALPAY_GETLINK_API_KEY",
    "CENTRALPAY_VERIFY_API_KEY",
    "BOT_NOTIFY_TOKEN",
    "ADMIN_BOT_TOKEN",
    "ADMIN_TELEGRAM_IDS",
)

# Which REAL credentials each env_file service may receive; everything
# else must be masked by an environment: override in docker-compose.yml.
SERVICE_ALLOWLIST: dict[str, set[str]] = {
    # The API validates inbound requests, signs/verifies callbacks and
    # talks to CentralPay. It queues bot notifications and creates
    # admin-alert rows but never sends to the customer bot or Telegram.
    "api": {
        "DATABASE_URL",
        "INBOUND_API_KEY",
        "CALLBACK_HMAC_SECRET",
        "CENTRALPAY_GETLINK_API_KEY",
        "CENTRALPAY_VERIFY_API_KEY",
    },
    # The worker delivers customer-bot notifications only.
    "worker": {"DATABASE_URL", "BOT_NOTIFY_TOKEN"},
    # The admin bot talks to Telegram only.
    "admin-bot": {"DATABASE_URL", "ADMIN_BOT_TOKEN", "ADMIN_TELEGRAM_IDS"},
    # Migrations need the database and nothing else (alembic/env.py reads
    # DATABASE_URL only).
    "migrate": {"DATABASE_URL"},
}

# Masks must be visibly non-production: empty, or a fixed
# not-used-by-<service> placeholder (the -x variants exist only to satisfy
# minimum-length validation). Anything else could pass for a credential.
_PLACEHOLDER = re.compile(r"\A(|not-used-by-[a-z-]+)\Z")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE_FILE.read_text())
    return loaded


def effective_environment(service: dict[str, Any]) -> dict[str, str]:
    """The environment a container sees: sentinel values standing in for
    the shared env file, overridden by the service's environment: block."""
    env = {var: f"SENTINEL-{var}" for var in CREDENTIAL_VARS}
    env.update({key: str(value) for key, value in service.get("environment", {}).items()})
    return env


# --- the policy matrix -------------------------------------------------------


def test_every_env_file_service_has_an_explicit_allowlist(compose):
    """A new service attached to the shared env file must be added to the
    matrix in this test file — otherwise this fails and forces the
    least-privilege decision instead of silently inheriting every secret."""
    for name, svc in compose["services"].items():
        if "env_file" in svc:
            assert name in SERVICE_ALLOWLIST, (
                f"service {name} receives the shared env file but has no "
                f"secret allowlist entry in {Path(__file__).name}"
            )
    assert set(SERVICE_ALLOWLIST) <= set(compose["services"])


def test_service_secret_policy_matrix(compose):
    for name, allowed in SERVICE_ALLOWLIST.items():
        env = effective_environment(compose["services"][name])
        for var in CREDENTIAL_VARS:
            if var in allowed:
                assert env[var] == f"SENTINEL-{var}", (
                    f"{name} must keep the real {var}"
                )
            else:
                assert env[var] != f"SENTINEL-{var}", (
                    f"{name} must not receive the real {var}"
                )
                assert _PLACEHOLDER.fullmatch(env[var]), (
                    f"{name} masks {var} with a non-fixed value: {env[var]!r}"
                )


def test_worker_never_receives_the_admin_bot_token(compose):
    env = effective_environment(compose["services"]["worker"])
    assert env["ADMIN_BOT_TOKEN"] == ""
    assert env["ADMIN_TELEGRAM_IDS"] == ""


def test_api_never_receives_delivery_tokens(compose):
    env = effective_environment(compose["services"]["api"])
    assert env["BOT_NOTIFY_TOKEN"] == ""
    assert env["ADMIN_BOT_TOKEN"] == ""
    assert env["ADMIN_TELEGRAM_IDS"] == ""


def test_migrate_receives_database_credentials_only(compose):
    env = effective_environment(compose["services"]["migrate"])
    assert env["DATABASE_URL"] == "SENTINEL-DATABASE_URL"
    for var in CREDENTIAL_VARS:
        if var != "DATABASE_URL":
            assert env[var] != f"SENTINEL-{var}", var


def test_admin_bot_keeps_telegram_but_not_delivery_credentials(compose):
    env = effective_environment(compose["services"]["admin-bot"])
    assert env["ADMIN_BOT_TOKEN"] == "SENTINEL-ADMIN_BOT_TOKEN"
    assert env["ADMIN_TELEGRAM_IDS"] == "SENTINEL-ADMIN_TELEGRAM_IDS"
    assert env["BOT_NOTIFY_TOKEN"] == ""
    assert env["CENTRALPAY_GETLINK_API_KEY"] == "not-used-by-admin-bot"
    assert env["CENTRALPAY_VERIFY_API_KEY"] == "not-used-by-admin-bot"


def test_caddy_and_db_receive_no_application_credentials(compose):
    caddy = compose["services"]["caddy"]
    assert "env_file" not in caddy
    assert "environment" not in caddy
    assert "secrets" not in caddy
    db = compose["services"]["db"]
    assert "env_file" not in db
    # PostgreSQL sees only its own settings and password file — never an
    # application API or Telegram credential.
    assert set(db["environment"]) == {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD_FILE",
    }
    assert db["secrets"] == ["db_password"]


# --- role startup under masking ----------------------------------------------

# What a production env file provides, simulated with the repository's
# fixed test fixtures (never real credentials).
_ENV_FILE_SIMULATION = {
    "PUBLIC_BASE_URL": "https://pay.example.com",
    "DATABASE_URL": "postgresql+psycopg://centralpay:pw@db:5432/centralpay",
    "INBOUND_API_KEY": TEST_INBOUND_API_KEY,
    "CALLBACK_HMAC_SECRET": TEST_CALLBACK_HMAC_SECRET,
    "CENTRALPAY_GETLINK_API_KEY": TEST_GETLINK_API_KEY,
    "CENTRALPAY_VERIFY_API_KEY": TEST_VERIFY_API_KEY,
    "CENTRALPAY_USER_ID": "42",
    "BOT_PAYMENT_NOTIFY_URL": "https://bot.example.com/api/payment",
    "BOT_NOTIFY_TOKEN": TEST_BOT_TOKEN,
    "ADMIN_BOT_ENABLED": "true",
    "ADMIN_BOT_TOKEN": TEST_ADMIN_BOT_TOKEN,
    "ADMIN_TELEGRAM_IDS": str(TEST_ADMIN_ID),
}


def settings_for(compose: dict[str, Any], service: str) -> Settings:
    """Construct Settings exactly as the service's container would: the
    simulated env file overridden by the service's Compose environment."""
    env = dict(_ENV_FILE_SIMULATION)
    overrides = compose["services"][service].get("environment", {})
    env.update({key: str(value) for key, value in overrides.items()})
    kwargs: dict[str, Any] = {key.lower(): value for key, value in env.items()}
    return Settings(**kwargs)


def test_api_startup_with_admin_alerts_enabled_under_masking(compose):
    """The API decides alert-row creation from the ADMIN_BOT_* flags alone —
    it never needs the Telegram token — so masking must not disable alerts,
    and Settings must construct with the delivery tokens blanked."""
    settings = settings_for(compose, "api")
    assert settings.admin_bot_enabled is True
    assert settings.admin_bot_token == ""
    assert settings.admin_telegram_ids == ""
    assert settings.bot_notify_token == ""
    assert settings.inbound_api_key == TEST_INBOUND_API_KEY
    assert settings.centralpay_getlink_api_key == TEST_GETLINK_API_KEY
    saved = alerts._policy
    try:
        alerts.configure_alert_creation(settings)
        assert alerts._policy is not None, "alert-row creation must stay enabled"
    finally:
        alerts._policy = saved


def test_worker_startup_under_masking(compose):
    settings = settings_for(compose, "worker")
    validate_bot_notification_settings(settings)  # must not raise
    assert settings.bot_notify_token == TEST_BOT_TOKEN
    assert settings.bot_payment_notify_url == "https://bot.example.com/api/payment"
    assert settings.inbound_api_key == "not-used-by-worker-x"
    assert settings.callback_hmac_secret == "not-used-by-worker-x"
    assert settings.centralpay_getlink_api_key == "not-used-by-worker"
    assert settings.admin_bot_token == ""
    assert settings.admin_telegram_ids == ""


def test_admin_bot_startup_under_masking(compose):
    settings = settings_for(compose, "admin-bot")
    assert validate_admin_bot_settings(settings) == (TEST_ADMIN_ID,)
    assert settings.admin_bot_token == TEST_ADMIN_BOT_TOKEN
    assert settings.bot_notify_token == ""
    assert settings.inbound_api_key == "not-used-by-admin-bot-x"
    assert settings.centralpay_verify_api_key == "not-used-by-admin-bot"


def test_migrate_settings_construction_under_masking(compose):
    """Alembic reads DATABASE_URL only, but Settings must still construct
    under the migrate masks (defense in depth for anything importing
    app.config inside that container)."""
    settings = settings_for(compose, "migrate")
    assert settings.database_url == _ENV_FILE_SIMULATION["DATABASE_URL"]
    assert settings.inbound_api_key == "not-used-by-migrate-x"
    assert settings.callback_hmac_secret == "not-used-by-migrate-x"
    assert settings.centralpay_getlink_api_key == "not-used-by-migrate"
    assert settings.bot_notify_token == ""
    assert settings.admin_bot_token == ""
    assert settings.admin_telegram_ids == ""
