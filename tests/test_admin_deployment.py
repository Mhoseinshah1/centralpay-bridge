"""Phase 4 deployment artifacts: compose service, installer, management CLI."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ENV_TEMPLATE = PROJECT_ROOT / "deploy" / "centralpay.env.template"
INSTALLER = PROJECT_ROOT / "install.sh"
MANAGEMENT = PROJECT_ROOT / "scripts" / "centralpay"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

ADMIN_ENV_VARS = [
    "ADMIN_BOT_ENABLED",
    "ADMIN_BOT_TOKEN",
    "ADMIN_TELEGRAM_IDS",
    "ADMIN_BOT_ALERTS_ENABLED",
    "ADMIN_BOT_PAYMENT_SUCCESS_ALERTS",
    "ADMIN_BOT_ERROR_ALERTS",
    "ADMIN_BOT_MANUAL_REVIEW_ALERTS",
    "ADMIN_BOT_BACKUP_ALERTS",
    "ADMIN_BOT_HEALTH_ALERTS",
    "ADMIN_BOT_DAILY_REPORT_ENABLED",
    "ADMIN_BOT_DAILY_REPORT_TIME",
    "ADMIN_BOT_TIMEZONE",
    "ADMIN_BOT_MAX_MESSAGE_LENGTH",
    "ADMIN_BOT_ALERT_DEDUP_MINUTES",
]


@pytest.fixture(scope="module")
def compose() -> dict[str, object]:
    loaded: dict[str, object] = yaml.safe_load(COMPOSE_FILE.read_text())
    return loaded


@pytest.fixture(scope="module")
def admin_service(compose) -> dict[str, object]:
    from typing import cast

    services = cast(dict[str, dict[str, object]], compose["services"])
    return services["admin-bot"]


def test_admin_bot_is_profile_gated_disabled_by_default(admin_service):
    # Without the profile, `docker compose up` never starts the admin bot.
    assert admin_service["profiles"] == ["admin-bot"]


def test_admin_bot_has_no_published_ports(admin_service):
    assert "ports" not in admin_service


def test_admin_bot_does_not_mount_docker_socket(admin_service):
    for volume in admin_service.get("volumes", []):
        assert "docker.sock" not in str(volume)
    assert "privileged" not in admin_service


def test_admin_bot_is_hardened(admin_service):
    assert admin_service["read_only"] is True
    assert admin_service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in admin_service["security_opt"]
    assert admin_service["restart"] == "unless-stopped"
    assert "healthcheck" in admin_service
    assert admin_service["depends_on"]["db"]["condition"] == "service_healthy"
    assert (
        admin_service["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert admin_service["logging"]["options"]["max-size"] == "20m"


def test_env_template_contains_admin_variables_without_real_token():
    text = ENV_TEMPLATE.read_text()
    for var in ADMIN_ENV_VARS:
        assert re.search(rf"^{var}=", text, re.MULTILINE), f"missing {var}"
    token_line = next(
        line for line in text.splitlines() if line.startswith("ADMIN_BOT_TOKEN=")
    )
    value = token_line.split("=", 1)[1]
    assert value in ("", "{{ADMIN_BOT_TOKEN}}")  # placeholder only, never a real token
    example = ENV_EXAMPLE.read_text()
    assert re.search(r"^ADMIN_BOT_TOKEN=$", example, re.MULTILINE)
    # No real-looking Telegram token anywhere in tracked templates/examples.
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", text + example)


def test_installer_asks_admin_bot_question():
    text = INSTALLER.read_text()
    assert "Enable administrator Telegram bot? [y/N]" in text
    # Token gathered silently; IDs validated.
    assert 'ask_secret ADMIN_BOT_TOKEN' in text
    assert "validate_admin_ids" in text
    # Never echo the token.
    for line in text.splitlines():
        if re.search(r"(echo|printf).*\$\{?ADMIN_BOT_TOKEN", line):
            assert ">" in line, f"token printed: {line.strip()}"


def test_installer_admin_id_validation():
    def call(function_call: str):
        return subprocess.run(
            ["bash", "-c", f"source {INSTALLER} && {function_call}"],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "CENTRALPAY_INSTALL_SOURCE_ONLY": "1",
            },
        )

    assert call("validate_admin_ids 123456789").returncode == 0
    assert call("validate_admin_ids 123456789,987654321").returncode == 0
    for bad in ("@username", "123,abc", "123,", "-5", ""):
        assert call(f"validate_admin_ids '{bad}'").returncode != 0, bad
    assert call("validate_report_time 09:00").returncode == 0
    assert call("validate_report_time 23:59").returncode == 0
    assert call("validate_report_time 24:00").returncode != 0


def test_management_cli_admin_bot_commands():
    result = subprocess.run(
        ["bash", str(MANAGEMENT), "help"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    for sub in ("admin-bot status", "admin-bot logs", "admin-bot restart",
                "admin-bot enable", "admin-bot disable", "admin-bot test-alert"):
        assert sub in result.stdout
    text = MANAGEMENT.read_text()
    # enable validates the token's presence but never prints its value.
    assert 'env_value ADMIN_BOT_TOKEN' in text
    assert not re.search(r"echo.*\$\(env_value ADMIN_BOT_TOKEN\)", text)


def test_summary_shows_count_not_token():
    text = INSTALLER.read_text()
    # The terminal summary shows a count of IDs; the full list lives only in
    # the protected credentials file.
    assert "administrator ID(s); full list in" in text
