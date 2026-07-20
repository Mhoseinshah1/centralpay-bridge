"""install.sh hardening (audit/adversarial-code-audit).

Deterministic subprocess tests — no root, no Docker, no networking. install.sh
is sourced with its SOURCE_ONLY guard and individual functions are exercised.

1. ``render_template`` substitutes values LITERALLY and without placing any
   value on a command line. The previous ``sed -e "s|{{X}}|${SECRET}|"``
   pipeline exposed every secret in ``ps``/``/proc/<pid>/cmdline`` and silently
   corrupted secrets containing ``&`` (sed's whole-match), ``|`` (delimiter),
   or ``\\`` (escape).
2. A keep-existing rerun reloads ``INBOUND_API_KEY`` so the final summary
   cannot abort on an unbound variable under ``set -u`` (the documented
   safe-to-rerun guarantee).
"""

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = PROJECT_ROOT / "install.sh"

_ENV_BASE = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CENTRALPAY_INSTALL_SOURCE_ONLY": "1"}

# Dummy fixture secrets follow the repo's .gitleaks.toml allowlist convention
# (TEST_* names with `test-`-prefixed values). They are opaque to the code
# under test, which only substitutes them verbatim into a template.
TEST_RENDER_DB_PASSWORD = "test-pg-password-abcdef"
TEST_RENDER_INBOUND_API_KEY = "test-inbound-api-key-abcdef"
TEST_RENDER_HMAC_SECRET = "test-callback-hmac-secret-abcdef"
TEST_RENDER_GETLINK_API_KEY = "test-getlink-api-key-abcdef"
TEST_RENDER_VERIFY_API_KEY = "test-verify-api-key-abcdef"
TEST_RENDER_BOT_TOKEN = "test-bot-notify-token-abcdef"

# Every install.sh variable render_template reads that has no in-function
# default; absent, the function aborts under `set -u`.
_RENDER_REQUIRED = {
    "PAYMENT_DOMAIN": "pay.example.com",
    "TLS_EMAIL": "ops@example.com",
    "POSTGRES_PASSWORD": TEST_RENDER_DB_PASSWORD,
    "INBOUND_API_KEY": TEST_RENDER_INBOUND_API_KEY,
    "CALLBACK_HMAC_SECRET": TEST_RENDER_HMAC_SECRET,
    "CENTRALPAY_GETLINK_API_KEY": TEST_RENDER_GETLINK_API_KEY,
    "CENTRALPAY_VERIFY_API_KEY": TEST_RENDER_VERIFY_API_KEY,
    "MIN_PAYMENT_AMOUNT_TOMAN": "1000",
    "MAX_PAYMENT_AMOUNT_TOMAN": "100000000",
    "PAYMENT_FEE_PERCENT": "2.5",
    "TELEGRAM_BOT_USERNAME": "zedproxy_bot",
    "BOT_PAYMENT_NOTIFY_URL": "https://bot.internal/notify",
    "BOT_NOTIFY_TOKEN": TEST_RENDER_BOT_TOKEN,
    "BOT_NOTIFY_RETRY_MODE": "safe",
}


def _render(template: Path, out: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; render_template "$2" "$3"', "_",
         str(INSTALLER), str(template), str(out)],
        capture_output=True, text=True, timeout=60, env={**_ENV_BASE, **env},
    )


# --- 1. render_template: literal substitution, no secret corruption -----------


@pytest.mark.parametrize(
    "secret",
    [
        "plain-token-123",
        "token&with&ampersands",  # sed replacement `&` = the whole match
        "token|with|pipes",  # sed `s|...|...|` delimiter
        "token\\with\\backslashes",  # sed escape character
        r"nasty&mix|of\everything&&||",
        "",  # empty renders empty, never a stray placeholder
    ],
)
def test_render_template_substitutes_secrets_literally(tmp_path, secret):
    template = tmp_path / "tmpl"
    template.write_text(
        "BOT_NOTIFY_TOKEN={{BOT_NOTIFY_TOKEN}}\n"
        "CENTRALPAY_VERIFY_API_KEY={{CENTRALPAY_VERIFY_API_KEY}}\n"
        "UNTOUCHED={{NOT_A_PLACEHOLDER}}\n"
    )
    out = tmp_path / "out"
    env = {**_RENDER_REQUIRED, "BOT_NOTIFY_TOKEN": secret, "CENTRALPAY_VERIFY_API_KEY": secret}
    result = _render(template, out, env)
    assert result.returncode == 0, result.stderr
    rendered = out.read_text()
    assert f"BOT_NOTIFY_TOKEN={secret}\n" in rendered
    assert f"CENTRALPAY_VERIFY_API_KEY={secret}\n" in rendered
    # Unknown placeholders are left verbatim, never blanked.
    assert "UNTOUCHED={{NOT_A_PLACEHOLDER}}\n" in rendered


def test_render_template_preserves_trailing_newline(tmp_path):
    template = tmp_path / "tmpl"
    template.write_text("DOMAIN={{PAYMENT_DOMAIN}}\n")
    out = tmp_path / "out"
    result = _render(template, out, _RENDER_REQUIRED)
    assert result.returncode == 0, result.stderr
    assert out.read_text() == "DOMAIN=pay.example.com\n"


def test_render_template_does_not_pass_secrets_to_external_commands():
    """Regression guard: the old implementation ran
    ``sed -e "s|{{SECRET}}|${SECRET}|"``, exposing every secret in argv
    (visible in ``ps``/``/proc/<pid>/cmdline``). The rewrite must not
    reintroduce a sed substitution of the placeholder values."""
    source = INSTALLER.read_text()
    start = source.index("render_template()")
    body = source[start : source.index("\n}\n", start)]
    # The vulnerable form was `sed -e "s|{{PLACEHOLDER}}|${VALUE}|"`.
    assert 'sed -e "s|{{' not in body, "render_template must not sed-substitute secret values"
    assert "content=${content//" in body, "render_template must use literal bash substitution"


# --- 2. keep-existing rerun reloads INBOUND_API_KEY ---------------------------


def _summary_env(**overrides: str) -> dict[str, str]:
    env = {
        **_ENV_BASE,
        "PAYMENT_DOMAIN": "pay.example.com",
        "BOT_PAYMENT_NOTIFY_URL": "https://bot.internal/notify",
        "TLS_STATE": "active",
        "DNS_READY": "true",
        "API_HEALTH": "ready",
        "DB_HEALTH": "ready",
        "WORKER_STATE": "running",
    }
    env.update(overrides)
    return env


def _print_summary(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; print_summary', "_", str(INSTALLER)],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_print_summary_aborts_when_inbound_api_key_unset(tmp_path):
    """Characterizes the hazard: print_summary reads ${INBOUND_API_KEY};
    unset under `set -u` it aborts. On a keep-existing rerun the key must
    therefore be reloaded before the summary runs."""
    env_file = tmp_path / "centralpay.env"
    env_file.write_text("ADMIN_BOT_ENABLED=false\n")
    env = _summary_env(ENV_FILE=str(env_file), CREDENTIALS_FILE=str(tmp_path / "cred"))
    # INBOUND_API_KEY intentionally omitted.
    result = _print_summary(env)
    assert result.returncode != 0
    assert "INBOUND_API_KEY" in result.stderr


def test_print_summary_succeeds_with_inbound_api_key(tmp_path):
    env_file = tmp_path / "centralpay.env"
    env_file.write_text("ADMIN_BOT_ENABLED=false\n")
    env = _summary_env(
        ENV_FILE=str(env_file),
        CREDENTIALS_FILE=str(tmp_path / "cred"),
        INBOUND_API_KEY=TEST_RENDER_INBOUND_API_KEY,
    )
    result = _print_summary(env)
    assert result.returncode == 0, result.stderr
    assert TEST_RENDER_INBOUND_API_KEY in result.stdout


def test_keep_existing_rerun_loads_inbound_api_key_from_env_file():
    """Fix guard: main()'s keep-existing branch must reload INBOUND_API_KEY
    from the env file (it is otherwise only set by the skipped
    load_or_generate_secrets)."""
    source = INSTALLER.read_text()
    keep_block = source[source.index('if [[ "$KEEP_EXISTING" == "true" ]]'):]
    keep_block = keep_block[: keep_block.index("else")]
    assert "INBOUND_API_KEY=$(grep" in keep_block
