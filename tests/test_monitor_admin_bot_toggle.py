"""scripts/centralpay: CENTRALPAY_BACKUP_DIR export and monitor restart-on-toggle.

Deterministic subprocess tests — no root privilege check needed (the test
runner already runs as root), no real Docker (a stub records invocations),
no networking. The CLI is sourced with its CENTRALPAY_CLI_SOURCE_ONLY guard
so individual functions can be called directly, matching the pattern in
test_deploy_hardening.py.

Covers two review findings:

1. CENTRALPAY_BACKUP_DIR is normally set inside centralpay.env (see
   deploy/centralpay.env.template), but docker-compose.yml's own bind-mount
   interpolation (${CENTRALPAY_BACKUP_DIR:-...}) reads the invoking shell's
   environment, not env_file:. Without exporting the configured value into
   this script's own process environment, an operator's customized backup
   path would silently fall back to the default on the host side.
2. The monitor reads ADMIN_BOT_* settings (including the alert-category
   toggles) once at container start. `centralpay admin-bot enable/disable`
   must restart an already-running monitor container so it never keeps
   acting on stale configuration until an operator restarts it by hand.
"""

import shlex
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "scripts" / "centralpay"

_ENV_BASE = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CENTRALPAY_CLI_SOURCE_ONLY": "1"}


def cli_call(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {snippet}', "_", str(CLI)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**_ENV_BASE, **env},
    )


def _write_docker_stub(bindir: Path, *, call_log: Path, monitor_running: bool) -> None:
    ps_reply = "monitor-container-id" if monitor_running else ""
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(call_log))}\n"
        'if [[ "$*" == *"ps -q monitor"* ]]; then\n'
        f"    echo {shlex.quote(ps_reply)}\n"
        "fi\n"
        "exit 0\n"
    )
    (bindir / "docker").chmod(0o755)


def _write_id_stub(bindir: Path) -> None:
    # require_root shells out to `id -u`; these tests exercise the
    # root-gated enable/disable subcommands without actually running as
    # root (the CI runner is an unprivileged user), so this stub reports
    # uid 0 the same way an operator's `sudo centralpay ...` invocation
    # would. Not needed for any other purpose in this script (`id` is
    # used exactly once, in require_root).
    (bindir / "id").write_text("#!/usr/bin/env bash\necho 0\n")
    (bindir / "id").chmod(0o755)


@pytest.fixture
def admin_bot_sandbox(tmp_path):
    install = tmp_path / "install"
    config = tmp_path / "config"
    bindir = tmp_path / "bin"
    install.mkdir()
    config.mkdir()
    bindir.mkdir()
    (install / "docker-compose.yml").write_text("services: {}\n")
    (config / "centralpay.env").write_text(
        "ADMIN_BOT_ENABLED=false\n"
        "ADMIN_BOT_TOKEN=test-token\n"
        "ADMIN_TELEGRAM_IDS=123456\n"
        "MONITOR_ENABLED=true\n"
    )
    return {"install": install, "config": config, "bindir": bindir}


def _env_for(sandbox, *, monitor_running: bool) -> dict[str, str]:
    call_log = sandbox["bindir"] / "docker_calls.log"
    _write_docker_stub(sandbox["bindir"], call_log=call_log, monitor_running=monitor_running)
    _write_id_stub(sandbox["bindir"])
    return {
        "PATH": f"{sandbox['bindir']}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CENTRALPAY_INSTALL_DIR": str(sandbox["install"]),
        "CENTRALPAY_CONFIG_DIR": str(sandbox["config"]),
    }


def test_admin_bot_enable_restarts_a_running_monitor(admin_bot_sandbox):
    env = _env_for(admin_bot_sandbox, monitor_running=True)
    result = cli_call("cmd_admin_bot enable", env)
    assert result.returncode == 0, result.stderr

    calls = (admin_bot_sandbox["bindir"] / "docker_calls.log").read_text()
    assert "restart monitor" in calls

    env_text = (admin_bot_sandbox["config"] / "centralpay.env").read_text()
    assert "ADMIN_BOT_ENABLED=true" in env_text


def test_admin_bot_disable_restarts_a_running_monitor(admin_bot_sandbox):
    (admin_bot_sandbox["config"] / "centralpay.env").write_text(
        "ADMIN_BOT_ENABLED=true\n"
        "ADMIN_BOT_TOKEN=test-token\n"
        "ADMIN_TELEGRAM_IDS=123456\n"
        "MONITOR_ENABLED=true\n"
    )
    env = _env_for(admin_bot_sandbox, monitor_running=True)
    result = cli_call("cmd_admin_bot disable", env)
    assert result.returncode == 0, result.stderr

    calls = (admin_bot_sandbox["bindir"] / "docker_calls.log").read_text()
    assert "restart monitor" in calls

    env_text = (admin_bot_sandbox["config"] / "centralpay.env").read_text()
    assert "ADMIN_BOT_ENABLED=false" in env_text


def test_admin_bot_enable_does_not_touch_monitor_when_not_running(admin_bot_sandbox):
    env = _env_for(admin_bot_sandbox, monitor_running=False)
    result = cli_call("cmd_admin_bot enable", env)
    assert result.returncode == 0, result.stderr

    calls = (admin_bot_sandbox["bindir"] / "docker_calls.log").read_text()
    assert "restart monitor" not in calls


def test_backup_dir_from_env_file_is_exported_for_compose_interpolation(tmp_path):
    """CENTRALPAY_BACKUP_DIR set inside centralpay.env must end up exported
    in this script's own process environment so docker-compose.yml's
    ${CENTRALPAY_BACKUP_DIR:-...} interpolation (which reads the invoking
    shell, never env_file:) mounts the SAME host path."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "centralpay.env").write_text("CENTRALPAY_BACKUP_DIR=/srv/custom-backups\n")

    result = cli_call(
        'echo "BACKUP_DIR=$BACKUP_DIR CENTRALPAY_BACKUP_DIR=$CENTRALPAY_BACKUP_DIR"',
        {"CENTRALPAY_CONFIG_DIR": str(config)},
    )
    assert result.returncode == 0, result.stderr
    assert "BACKUP_DIR=/srv/custom-backups" in result.stdout
    assert "CENTRALPAY_BACKUP_DIR=/srv/custom-backups" in result.stdout


def test_backup_dir_falls_back_to_process_env_without_env_file_entry(tmp_path):
    """Backward compatible with pre-install/testing use: an operator or the
    installer can still override via the process environment before
    centralpay.env sets it explicitly."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "centralpay.env").write_text("ADMIN_BOT_ENABLED=false\n")

    result = cli_call(
        'echo "BACKUP_DIR=$BACKUP_DIR"',
        {
            "CENTRALPAY_CONFIG_DIR": str(config),
            "CENTRALPAY_BACKUP_DIR": "/from/process/env",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "BACKUP_DIR=/from/process/env" in result.stdout


def test_backup_dir_defaults_when_unset_everywhere(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "centralpay.env").write_text("ADMIN_BOT_ENABLED=false\n")

    result = cli_call('echo "BACKUP_DIR=$BACKUP_DIR"', {"CENTRALPAY_CONFIG_DIR": str(config)})
    assert result.returncode == 0, result.stderr
    assert "BACKUP_DIR=/var/backups/centralpay-bridge" in result.stdout
