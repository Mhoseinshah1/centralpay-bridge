"""scripts/centralpay: caddy_activation_status_line() -- the read-only Caddy
config drift check surfaced by `centralpay status` and `centralpay diagnose`.

Context: PR review identified a P1 finding that the very FIRST `centralpay
update` past this PR's commit, run from an already-installed PRE-commit
wrapper, cannot execute this PR's new Caddy-sync logic at all -- the
running process's `perform_update` function body was already resolved from
the OLD file at process start, and `git checkout`-ing new source mid-run
does not retroactively change it (see SECURITY.md's "First-upgrade Caddy
bootstrap gap" entry for the full proof). That specific transition is
provably not fixable by any code shipped in this commit.

What CAN be fixed: once that first (flawed) update completes,
`sync_management_wrapper` has already replaced the installed `centralpay`
command with the current one -- so the VERY NEXT invocation of ANY
subcommand runs the current code. caddy_activation_status_line() turns the
silent "update reported success but Caddy still serves the old config"
state into a loud, immediate warning on that next `status`/`diagnose` call,
instead of it persisting silently until an operator happens to rerun
`update` or the installer.

These tests prove the check is read-only (never mutates the Caddyfile,
never validates via Docker, never writes the activation marker) and that
both `cmd_status` and `cmd_diagnose` are wired to call it.
"""

import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "scripts" / "centralpay"
RENDER_SCRIPT = PROJECT_ROOT / "scripts" / "render-caddy-config.sh"
TEMPLATE = PROJECT_ROOT / "deploy" / "caddy" / "Caddyfile.template"

OLD_CADDYFILE = """{
\temail old@example.com
\tadmin off
}

old.example.com {
\tencode gzip
}
"""


def _install_dir(tmp_path) -> Path:
    install = tmp_path / "install"
    (install / "deploy" / "caddy").mkdir(parents=True)
    (install / "scripts").mkdir(parents=True)
    shutil.copy(TEMPLATE, install / "deploy" / "caddy" / "Caddyfile.template")
    shutil.copy(RENDER_SCRIPT, install / "scripts" / "render-caddy-config.sh")
    return install


def _call_status_line(tmp_path, install: Path, config: Path) -> subprocess.CompletedProcess[str]:
    # No docker on PATH at all -- proves the status line never shells out
    # to it (a real drift-detecting docker validate would fail loudly here
    # if it were ever reached).
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "CENTRALPAY_CLI_SOURCE_ONLY": "1",
        "CENTRALPAY_INSTALL_DIR": str(install),
        "CENTRALPAY_CONFIG_DIR": str(config),
    }
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; caddy_activation_status_line', "_", str(CLI)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_warns_loudly_when_installed_config_is_out_of_sync(tmp_path):
    install = _install_dir(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "Caddyfile").write_text(OLD_CADDYFILE)

    result = _call_status_line(tmp_path, install, config)
    assert result.returncode == 0, result.stderr  # the status line itself never fails the caller
    assert "OUT OF SYNC" in result.stderr
    assert "sudo centralpay update" in result.stderr
    assert "install.sh" in result.stderr
    # Read-only: the Caddyfile and its surrounding directory are untouched.
    assert (config / "Caddyfile").read_text() == OLD_CADDYFILE
    assert not list(config.glob("Caddyfile.bak.*"))
    assert not (config / ".caddy-active-sha256").exists()


def test_quiet_confirmation_when_in_sync_and_confirmed(tmp_path):
    install = _install_dir(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "Caddyfile").write_text(OLD_CADDYFILE)

    # Real render (fake docker so validation passes) + confirm, exactly as
    # a real install/update would, so content and marker genuinely match.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker_stub = bindir / "docker"
    docker_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker_stub.chmod(0o755)
    render_env = {
        "PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CENTRALPAY_INSTALL_DIR": str(install),
        "CENTRALPAY_CONFIG_DIR": str(config),
    }
    render = subprocess.run(
        ["bash", str(install / "scripts" / "render-caddy-config.sh")],
        capture_output=True, text=True, timeout=30, env=render_env,
    )
    assert render.returncode == 2, render.stderr
    confirm = subprocess.run(
        ["bash", str(install / "scripts" / "render-caddy-config.sh"), "--confirm-active"],
        capture_output=True, text=True, timeout=30, env=render_env,
    )
    assert confirm.returncode == 0, confirm.stderr

    result = _call_status_line(tmp_path, install, config)
    assert result.returncode == 0, result.stderr
    assert "in sync, activation confirmed" in result.stdout
    assert "OUT OF SYNC" not in result.stderr


def test_no_crash_and_no_warning_when_render_script_is_absent(tmp_path):
    """An install predating render-caddy-config.sh entirely (should not be
    reachable in practice post-#78, but the check must degrade gracefully,
    never crash `status`/`diagnose` themselves)."""
    install = tmp_path / "install"
    install.mkdir()  # no scripts/render-caddy-config.sh at all
    config = tmp_path / "config"
    config.mkdir()

    result = _call_status_line(tmp_path, install, config)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_cmd_status_and_cmd_diagnose_are_wired_to_the_drift_check():
    body = CLI.read_text()
    status_start = body.index("cmd_status()")
    status_end = body.index("\n}\n", status_start)
    diagnose_start = body.index("cmd_diagnose()")
    diagnose_end = body.index("\n}\n", diagnose_start)
    assert "caddy_activation_status_line" in body[status_start:status_end]
    assert "caddy_activation_status_line" in body[diagnose_start:diagnose_end]


def test_status_line_never_invokes_the_mutating_default_or_confirm_active_mode():
    """caddy_activation_status_line must invoke render-caddy-config.sh with
    --check ONLY -- never bare (mutating) and never --confirm-active."""
    body = CLI.read_text()
    fn_start = body.index("caddy_activation_status_line()")
    fn_end = body.index("\n}\n", fn_start)
    fn_body = body[fn_start:fn_end]
    assert '"$script" --check' in fn_body
    assert "--confirm-active" not in fn_body
