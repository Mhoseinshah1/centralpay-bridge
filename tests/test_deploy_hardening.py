"""scripts/centralpay update/rollback hardening (audit/adversarial-code-audit).

Deterministic subprocess tests — no root, no Docker, no networking. The CLI is
sourced with its SOURCE_ONLY guard and individual functions are exercised.

1. ``record_version_history`` records an explicit previous commit, so the
   FIRST ``centralpay update`` (no version_history file yet) still leaves a
   working rollback target instead of ``previous=`` (empty), which used to
   make the first ``centralpay rollback`` fail.
2. A deploy lock serializes update/rollback so two concurrent deploys cannot
   interleave their git checkout + version-history write + compose build/up.
"""

import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "scripts" / "centralpay"

_ENV_BASE = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CENTRALPAY_CLI_SOURCE_ONLY": "1"}


def cli_call(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {snippet}', "_", str(CLI)],
        capture_output=True, text=True, timeout=60, env={**_ENV_BASE, **env},
    )


# --- 1. first-update rollback target ------------------------------------------


def _read_history(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_record_version_history_uses_explicit_previous(tmp_path):
    """The first update has no version_history file yet; passing the
    pre-update commit explicitly must still record a rollback target."""
    history = tmp_path / "version_history"
    result = cli_call(
        'record_version_history "newsha1234" "prevsha5678"',
        {"CENTRALPAY_CONFIG_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    data = _read_history(history)
    assert data["current"] == "newsha1234"
    assert data["previous"] == "prevsha5678"  # NOT empty — rollback works


def test_record_version_history_falls_back_to_file_current(tmp_path):
    """Backward compatibility: with no explicit previous, derive it from the
    existing current= line (the later-update / rollback behavior)."""
    history = tmp_path / "version_history"
    history.write_text("current=oldsha0000\nprevious=oldersha\nupdated_at=x\n")
    result = cli_call(
        'record_version_history "newsha1111"', {"CENTRALPAY_CONFIG_DIR": str(tmp_path)}
    )
    assert result.returncode == 0, result.stderr
    data = _read_history(history)
    assert data["current"] == "newsha1111"
    assert data["previous"] == "oldsha0000"


def test_first_update_records_no_empty_previous(tmp_path):
    """The exact bug: a first update (no pre-existing file) must never write
    `previous=` empty, which made the first `centralpay rollback` fail."""
    history = tmp_path / "version_history"
    cli_call(
        'record_version_history "deployed_commit" "pre_update_commit"',
        {"CENTRALPAY_CONFIG_DIR": str(tmp_path)},
    )
    text = history.read_text()
    assert "previous=pre_update_commit" in text
    assert "previous=\n" not in text


def test_cmd_update_passes_pre_update_commit_to_history():
    """Fix guard: cmd_update must forward the captured pre-update commit as the
    explicit previous, otherwise the first update records an empty rollback
    target again."""
    source = CLI.read_text()
    body = source[source.index("cmd_update()") :]
    body = body[: body.index("\n}\n")]
    assert 'record_version_history "$(git -C "$INSTALL_DIR" rev-parse HEAD)" "$previous"' in body


# --- 2. deploy lock serializes update/rollback --------------------------------


def test_acquire_deploy_lock_succeeds_when_free(tmp_path):
    result = cli_call(
        "acquire_deploy_lock && echo ACQUIRED", {"CENTRALPAY_CONFIG_DIR": str(tmp_path)}
    )
    assert result.returncode == 0, result.stderr
    assert "ACQUIRED" in result.stdout


def test_acquire_deploy_lock_rejects_concurrent_deploy(tmp_path):
    lock_file = tmp_path / ".deploy.lock"
    ready = tmp_path / "ready"
    # Holder: take the SAME lock file (a different fd; flock contends on the
    # file's open description, not the fd number) and hold it.
    holder = subprocess.Popen(
        ["bash", "-c",
         f'exec 9>"{lock_file}"; flock -n 9 || exit 3; : > "{ready}"; sleep 10'],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    try:
        for _ in range(200):  # wait up to ~4s for the holder to take the lock
            if ready.exists():
                break
            time.sleep(0.02)
        assert ready.exists(), "holder never acquired the lock"
        result = cli_call(
            "acquire_deploy_lock && echo ACQUIRED", {"CENTRALPAY_CONFIG_DIR": str(tmp_path)}
        )
        assert result.returncode != 0
        assert "ACQUIRED" not in result.stdout
        assert "already running" in result.stderr.lower()
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_update_and_rollback_acquire_the_deploy_lock():
    """Fix guard: both mutating deploy commands must take the deploy lock."""
    source = CLI.read_text()
    for fn in ("cmd_update()", "cmd_rollback()"):
        body = source[source.index(fn) :]
        body = body[: body.index("\n}\n")]
        assert "acquire_deploy_lock" in body, f"{fn} must acquire the deploy lock"
