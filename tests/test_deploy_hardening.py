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

import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "scripts" / "centralpay"

_ENV_BASE = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CENTRALPAY_CLI_SOURCE_ONLY": "1"}

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def cli_call(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {snippet}', "_", str(CLI)],
        capture_output=True, text=True, timeout=60, env={**_ENV_BASE, **env},
    )


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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
    """Fix guard: perform_update (the state-mutating half of cmd_update) must
    forward the captured pre-update commit as the explicit previous,
    otherwise the first update records an empty rollback target again."""
    source = CLI.read_text()
    body = source[source.index("perform_update()") :]
    body = body[: body.index("\n}\n")]
    assert 'record_version_history "$(git -C "$INSTALL_DIR" rev-parse HEAD)" "$previous"' in body


def test_cmd_update_requires_root_before_delegating_to_perform_update():
    """Fix guard: the root/install/lock checks must stay in the thin
    cmd_update entrypoint, with perform_update doing the actual work — this
    is what lets tests call perform_update directly without running as
    root, mirroring the existing cmd_rollback / perform_rollback split."""
    source = CLI.read_text()
    body = source[source.index("cmd_update() {") :]
    body = body[: body.index("\n}\n")]
    assert "require_root update" in body
    assert "perform_update" in body
    # The actual deploy steps live in perform_update, not inline here.
    assert "compose up" not in body


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


def test_update_and_rollback_sync_wrapper_only_on_health_check_success():
    """Fix guard: the installed management command must be synced ONLY after
    `compose up -d --wait` has already confirmed the deploy healthy — never
    unconditionally and never in the failure branch."""
    source = CLI.read_text()
    for start, end in (
        ("perform_update() {", "\nperform_rollback() {"),
        ("perform_rollback() {", "\ncmd_rollback() {"),
    ):
        body = source[source.index(start) : source.index(end)]
        then_i = body.index("if compose up -d --wait; then")
        else_i = body.index("\n    else")
        sync_i = body.index("sync_management_wrapper")
        assert then_i < sync_i < else_i, start


# --- 3. installed management wrapper stays in lockstep with the deployed
#    application commit (sync_management_wrapper) ------------------------------

OLD_WRAPPER = (
    "#!/usr/bin/env bash\n"
    'case "$1" in\n'
    "    oldcmd) echo OLD_COMMAND_OK ;;\n"
    '    *) echo "unknown: $1" >&2; exit 1 ;;\n'
    "esac\n"
)

NEW_WRAPPER = (
    "#!/usr/bin/env bash\n"
    'case "$1" in\n'
    "    oldcmd) echo OLD_COMMAND_OK ;;\n"
    "    newcmd) echo NEW_COMMAND_OK ;;\n"
    '    *) echo "unknown: $1" >&2; exit 1 ;;\n'
    "esac\n"
)


def _commit_deploy(
    repo: Path, *, wrapper_content: str, marker: str, caddy_sync_rc: int = 0
) -> str:
    (repo / "docker-compose.yml").write_text("services: {}\n")
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    backup = scripts_dir / "backup.sh"
    backup.write_text("#!/usr/bin/env bash\nexit 0\n")
    backup.chmod(0o755)
    # These tests exercise wrapper self-replacement / rollback plumbing, not
    # the Caddy config sync itself (see test_caddy_config_upgrade.py for
    # that) -- a stub reporting a fixed exit code (0 "already up to date" by
    # default; 2 "changed" for the activation-gating tests below) keeps them
    # focused on their own concern without a real Docker daemon.
    caddy_sync = scripts_dir / "render-caddy-config.sh"
    caddy_sync.write_text(
        "#!/usr/bin/env bash\n"
        # Mirrors the real script's two invocation modes: `--confirm-active`
        # (called after a restart) always succeeds here; a bare call reports
        # the scripted exit code.
        'if [[ "${1:-}" == "--confirm-active" ]]; then exit 0; fi\n'
        f"exit {caddy_sync_rc}\n"
    )
    caddy_sync.chmod(0o755)
    wrapper = scripts_dir / "centralpay"
    wrapper.write_text(wrapper_content)
    wrapper.chmod(0o755)
    (repo / "marker.txt").write_text(marker)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", marker, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def _write_docker_stub(
    bindir: Path, *, call_log: Path, fail_compose_up: bool, fail_caddy_restart: bool = False
) -> None:
    fail_snippet = (
        'if [[ "$*" == *"up"* && "$*" == *"--wait"* ]]; then exit 1; fi\n'
        if fail_compose_up
        else ""
    )
    if fail_caddy_restart:
        fail_snippet += 'if [[ "$*" == *"restart"* && "$*" == *"caddy"* ]]; then exit 1; fi\n'
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(call_log))}\n" + fail_snippet + "exit 0\n"
    )
    (bindir / "docker").chmod(0o755)


@pytest.fixture
def deploy_sandbox(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    install = tmp_path / "install"
    config = tmp_path / "config"
    backups = tmp_path / "backups"
    bindir = tmp_path / "bin"
    for d in (config, backups, bindir):
        d.mkdir()
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    commit_a = _commit_deploy(work, wrapper_content=OLD_WRAPPER, marker="A")
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
    git("clone", "-q", str(origin), str(install), cwd=tmp_path)

    management_bin = tmp_path / "installed" / "centralpay"
    management_bin.parent.mkdir()
    # An already-installed wrapper matching the currently-deployed commit A
    # (the pre-existing-install state the fix operates on).
    management_bin.write_text(OLD_WRAPPER)
    management_bin.chmod(0o755)

    # These tests deploy a plain "main" branch commit on purpose (they
    # exercise wrapper self-replacement / rollback plumbing, not release-tag
    # checksum verification), so they need the explicit local-development
    # opt-in that production installs must never set.
    (config / "centralpay.env").write_text("CENTRALPAY_UPDATE_ALLOW_DEV_REF=true\n")

    return {
        "origin": origin,
        "work": work,
        "install": install,
        "config": config,
        "backups": backups,
        "bindir": bindir,
        "management_bin": management_bin,
        "commit_a": commit_a,
    }


def _env_for(
    sandbox, *, fail_compose_up: bool = False, fail_caddy_restart: bool = False
) -> dict[str, str]:
    call_log = sandbox["bindir"] / "docker_calls.log"
    _write_docker_stub(
        sandbox["bindir"],
        call_log=call_log,
        fail_compose_up=fail_compose_up,
        fail_caddy_restart=fail_caddy_restart,
    )
    return {
        "PATH": f"{sandbox['bindir']}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CENTRALPAY_INSTALL_DIR": str(sandbox["install"]),
        "CENTRALPAY_CONFIG_DIR": str(sandbox["config"]),
        "CENTRALPAY_BACKUP_DIR": str(sandbox["backups"]),
        "CENTRALPAY_MANAGEMENT_BIN": str(sandbox["management_bin"]),
        **_GIT_ENV,
    }


def test_update_syncs_installed_wrapper_with_newly_deployed_commit(deploy_sandbox):
    """The reported bug: after `centralpay update`, a newly added host
    subcommand must actually be runnable through the installed wrapper."""
    commit_b = _commit_deploy(deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B")
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    result = cli_call("perform_update", _env_for(deploy_sandbox))
    assert result.returncode == 0, result.stderr

    management_bin = deploy_sandbox["management_bin"]
    assert management_bin.read_text() == NEW_WRAPPER
    assert oct(management_bin.stat().st_mode)[-3:] == "755"  # executable mode preserved

    invoke = subprocess.run(
        [str(management_bin), "newcmd"], capture_output=True, text=True, timeout=10
    )
    assert invoke.returncode == 0
    assert "NEW_COMMAND_OK" in invoke.stdout

    history = dict(
        line.split("=", 1)
        for line in (deploy_sandbox["config"] / "version_history").read_text().splitlines()
        if "=" in line
    )
    assert history["current"] == commit_b
    assert history["previous"] == deploy_sandbox["commit_a"]


def test_update_leaves_wrapper_unchanged_when_deploy_fails(deploy_sandbox):
    """Application files may already have moved to the new commit when
    health checks fail, but the installed wrapper must never point at a
    deploy that did not succeed."""
    _commit_deploy(deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B")
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    result = cli_call("perform_update", _env_for(deploy_sandbox, fail_compose_up=True))
    assert result.returncode != 0

    assert git("rev-parse", "HEAD", cwd=deploy_sandbox["install"]) != deploy_sandbox["commit_a"]
    assert deploy_sandbox["management_bin"].read_text() == OLD_WRAPPER


def test_rollback_restores_wrapper_matching_rolled_back_version(deploy_sandbox):
    _commit_deploy(deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B")
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    update_result = cli_call("perform_update", _env_for(deploy_sandbox))
    assert update_result.returncode == 0, update_result.stderr
    assert deploy_sandbox["management_bin"].read_text() == NEW_WRAPPER  # sanity: update landed

    rollback_result = cli_call(
        f'perform_rollback "{deploy_sandbox["commit_a"]}"', _env_for(deploy_sandbox)
    )
    assert rollback_result.returncode == 0, rollback_result.stderr

    management_bin = deploy_sandbox["management_bin"]
    assert management_bin.read_text() == OLD_WRAPPER
    assert git("rev-parse", "HEAD", cwd=deploy_sandbox["install"]) == deploy_sandbox["commit_a"]

    invoke = subprocess.run(
        [str(management_bin), "newcmd"], capture_output=True, text=True, timeout=10
    )
    assert invoke.returncode != 0
    assert "NEW_COMMAND_OK" not in invoke.stdout


def test_rollback_leaves_wrapper_unchanged_when_health_check_fails(deploy_sandbox):
    _commit_deploy(deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B")
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])
    update_result = cli_call("perform_update", _env_for(deploy_sandbox))
    assert update_result.returncode == 0, update_result.stderr

    rollback_result = cli_call(
        f'perform_rollback "{deploy_sandbox["commit_a"]}"',
        _env_for(deploy_sandbox, fail_compose_up=True),
    )
    assert rollback_result.returncode != 0

    # The rollback's own health check failed: the wrapper must stay exactly
    # as it was, not advertise a rollback that never actually succeeded.
    assert deploy_sandbox["management_bin"].read_text() == NEW_WRAPPER


def test_update_self_replaces_the_running_wrapper_without_breaking_it(deploy_sandbox):
    """The realistic production case: `centralpay update` IS the installed
    wrapper, already executing, when sync_management_wrapper replaces that
    very file out from under itself. The in-flight bash process must keep
    running to completion from the OLD bytes (never truncated/corrupted),
    and only the NEXT invocation observes the new content."""
    _commit_deploy(deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B")
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    management_bin = deploy_sandbox["management_bin"]
    # A self-invoking wrapper that sources the real update machinery and
    # keeps executing (a marker line) after perform_update — which calls
    # sync_management_wrapper — replaces this very file mid-run. Calls
    # perform_update (not cmd_update) so this test doesn't depend on
    # running as root, same as the other direct-invocation tests here.
    management_bin.write_text(
        "#!/usr/bin/env bash\n"
        f"source {shlex.quote(str(CLI))}\n"
        "perform_update\n"
        "echo SELF_UPDATE_SURVIVED\n"
    )
    management_bin.chmod(0o755)

    env = {**_env_for(deploy_sandbox), "CENTRALPAY_CLI_SOURCE_ONLY": "1"}
    result = subprocess.run(
        [str(management_bin)], capture_output=True, text=True, timeout=30, env=env
    )
    assert result.returncode == 0, result.stderr
    assert "SELF_UPDATE_SURVIVED" in result.stdout
    assert management_bin.read_text() == NEW_WRAPPER


# --- 4. `status`/`diagnose` report UNRESOLVED manual reviews, not a raw
#    historical status count (production incident: `centralpay status` and
#    `centralpay diagnose` showed 53 while `centralpay review list` and the
#    monitor both agreed on 0 -- a resolved payment keeps status=
#    'manual_review' permanently as history; see review_resolved_at) -------


def test_cmd_status_manual_review_query_excludes_resolved_rows():
    body = CLI.read_text()
    body = body[body.index("cmd_status()") : body.index("cmd_status()") + 4000]
    line = next(ln for ln in body.splitlines() if "manual review" in ln.lower())
    assert "status='manual_review'" in line
    assert "review_resolved_at IS NULL" in line
    assert "unresolved" in line.lower()


def test_cmd_diagnose_manual_review_query_excludes_resolved_rows():
    source = CLI.read_text()
    body = source[source.index("cmd_diagnose()") :]
    body = body[: body.index("\n}\n")]
    line = next(ln for ln in body.splitlines() if "manual review" in ln.lower())
    assert "status='manual_review'" in line
    assert "review_resolved_at IS NULL" in line
    assert "unresolved" in line.lower()


def test_cmd_status_and_diagnose_manual_review_query_matches_count_open_manual_reviews():
    """The shell SQL must express the exact same "open" predicate as
    app.adminbot.queries._open_manual_review_conditions (status=
    'manual_review' AND review_resolved_at IS NULL) -- the two must never
    quietly disagree about what counts as unresolved."""
    source = CLI.read_text()
    queries_source = (PROJECT_ROOT / "app" / "adminbot" / "queries.py").read_text()
    assert 'Payment.status == "manual_review"' in queries_source
    assert "Payment.review_resolved_at.is_(None)" in queries_source
    for fn in ("cmd_status()", "cmd_diagnose()"):
        body = source[source.index(fn) :]
        body = body[: body.index("\n}\n")]
        assert "status='manual_review' AND review_resolved_at IS NULL" in body, fn


def _init_install_with_source(tmp_path: Path, *, source_content: str, source_mode: int) -> Path:
    install_dir = tmp_path / "install"
    scripts_dir = install_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    src = scripts_dir / "centralpay"
    src.write_text(source_content)
    src.chmod(source_mode)
    git("init", "-q", cwd=install_dir)
    git("add", "-A", cwd=install_dir)
    git("commit", "-q", "-m", "x", cwd=install_dir)
    return install_dir


def test_sync_management_wrapper_atomically_replaces_destination(tmp_path):
    """Requirements 2-4: the destination is replaced via a same-filesystem
    rename (a genuinely new inode, never edited in place), mode 0755 is
    guaranteed regardless of the source file's mode, and no temp file is
    left behind."""
    install_dir = _init_install_with_source(
        tmp_path, source_content=NEW_WRAPPER, source_mode=0o644  # deliberately non-executable
    )
    dest = tmp_path / "installed_centralpay"
    dest.write_text(OLD_WRAPPER)
    dest.chmod(0o755)
    before_inode = dest.stat().st_ino

    result = cli_call(
        "sync_management_wrapper",
        {
            "CENTRALPAY_INSTALL_DIR": str(install_dir),
            "CENTRALPAY_MANAGEMENT_BIN": str(dest),
            **_GIT_ENV,
        },
    )
    assert result.returncode == 0, result.stderr

    assert dest.stat().st_ino != before_inode  # replaced via rename, not edited in place
    assert dest.read_text() == NEW_WRAPPER
    assert oct(dest.stat().st_mode)[-3:] == "755"
    assert list(tmp_path.glob(".centralpay.tmp.*")) == []  # no leftover staging file


def test_sync_management_wrapper_fails_closed_when_source_script_missing(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    dest = tmp_path / "installed_centralpay"
    dest.write_text("OLD CONTENT")
    dest.chmod(0o755)
    before_inode = dest.stat().st_ino
    result = cli_call(
        "sync_management_wrapper",
        {"CENTRALPAY_INSTALL_DIR": str(install_dir), "CENTRALPAY_MANAGEMENT_BIN": str(dest)},
    )
    assert result.returncode != 0
    assert dest.read_text() == "OLD CONTENT"  # left untouched on failure
    assert dest.stat().st_ino == before_inode
    assert list(tmp_path.glob(".centralpay.tmp.*")) == []


def test_sync_management_wrapper_leaves_destination_unchanged_when_staging_fails(tmp_path):
    """Requirement 1: a failure BEFORE the rename (staging the new content
    into the temp file) must leave the destination completely untouched —
    same inode, same bytes, same mode."""
    install_dir = _init_install_with_source(
        tmp_path, source_content=NEW_WRAPPER, source_mode=0o755
    )
    dest = tmp_path / "installed_centralpay"
    dest.write_text(OLD_WRAPPER)
    dest.chmod(0o755)
    before_inode = dest.stat().st_ino

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "install").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bindir / "install").chmod(0o755)

    result = cli_call(
        "sync_management_wrapper",
        {
            "CENTRALPAY_INSTALL_DIR": str(install_dir),
            "CENTRALPAY_MANAGEMENT_BIN": str(dest),
            "PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin",
            **_GIT_ENV,
        },
    )
    assert result.returncode != 0

    assert dest.stat().st_ino == before_inode
    assert dest.read_text() == OLD_WRAPPER
    assert oct(dest.stat().st_mode)[-3:] == "755"
    assert list(tmp_path.glob(".centralpay.tmp.*")) == []  # staging file cleaned up


def test_sync_management_wrapper_leaves_destination_unchanged_when_rename_fails(tmp_path):
    """Requirement 1/2/4: inject a failure at the final rename step (staging
    already succeeded) and prove the OLD destination inode + contents +
    mode survive untouched — the atomic swap either fully happens or not at
    all, never a partial/corrupted destination."""
    install_dir = _init_install_with_source(
        tmp_path, source_content=NEW_WRAPPER, source_mode=0o755
    )
    dest = tmp_path / "installed_centralpay"
    dest.write_text(OLD_WRAPPER)
    dest.chmod(0o755)
    before_inode = dest.stat().st_ino

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "mv").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bindir / "mv").chmod(0o755)

    result = cli_call(
        "sync_management_wrapper",
        {
            "CENTRALPAY_INSTALL_DIR": str(install_dir),
            "CENTRALPAY_MANAGEMENT_BIN": str(dest),
            "PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin",
            **_GIT_ENV,
        },
    )
    assert result.returncode != 0

    assert dest.stat().st_ino == before_inode
    assert dest.read_text() == OLD_WRAPPER
    assert oct(dest.stat().st_mode)[-3:] == "755"
    assert list(tmp_path.glob(".centralpay.tmp.*")) == []  # staging file cleaned up


def test_update_and_rollback_acquire_the_deploy_lock():
    """Fix guard: both mutating deploy commands must take the deploy lock."""
    source = CLI.read_text()
    for fn in ("cmd_update()", "cmd_rollback()"):
        body = source[source.index(fn) :]
        body = body[: body.index("\n}\n")]
        assert "acquire_deploy_lock" in body, f"{fn} must acquire the deploy lock"


# --- 5. Caddy config sync activation gating (Issue 2) ------------------------
#
# render-caddy-config.sh's own behavioral tests live in
# test_caddy_config_upgrade.py; these prove perform_update's INTEGRATION
# with it end-to-end (real git checkout, real perform_update execution),
# using a scripted stub for render-caddy-config.sh itself so no real Docker
# daemon is required here either.


def test_update_restarts_caddy_when_config_reports_changed(deploy_sandbox):
    _commit_deploy(
        deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B", caddy_sync_rc=2
    )
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    result = cli_call("perform_update", _env_for(deploy_sandbox))
    assert result.returncode == 0, result.stderr

    call_log = deploy_sandbox["bindir"] / "docker_calls.log"
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    # Token-based match, not substring: pytest's own tmp_path embeds this
    # test's NAME (which itself contains "restart") into every path
    # argument logged here, so a naive substring check on the whole line
    # would false-positive/false-negative on that coincidence.
    assert any("restart" in c.split() and "caddy" in c.split() for c in calls), calls


def test_update_does_not_restart_caddy_when_config_unchanged(deploy_sandbox):
    _commit_deploy(
        deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B", caddy_sync_rc=0
    )
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    result = cli_call("perform_update", _env_for(deploy_sandbox))
    assert result.returncode == 0, result.stderr

    call_log = deploy_sandbox["bindir"] / "docker_calls.log"
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    assert not any("restart" in c.split() and "caddy" in c.split() for c in calls), calls


def test_update_aborts_before_build_when_caddy_sync_fails(deploy_sandbox):
    """A fatal Caddy-config sync (exit 1, e.g. failed validation) must abort
    the whole update BEFORE compose build/migrate/restart -- never a
    half-applied update, and the installed wrapper must stay unchanged."""
    _commit_deploy(
        deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B", caddy_sync_rc=1
    )
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    result = cli_call("perform_update", _env_for(deploy_sandbox))
    assert result.returncode != 0
    assert "Managed Caddy configuration could not be safely synced" in result.stderr

    call_log = deploy_sandbox["bindir"] / "docker_calls.log"
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    assert not any("build" in c for c in calls), calls
    assert not any(("up" in c and "--wait" in c) for c in calls), calls
    # The installed wrapper is still the OLD one -- never synced on a
    # failed update.
    assert deploy_sandbox["management_bin"].read_text() == OLD_WRAPPER


def test_update_fails_when_caddy_restart_itself_fails(deploy_sandbox):
    """Activation failure handling: if restarting caddy to pick up the new
    config fails, the update must not report success (never silently
    leave a security-relevant config change unapplied while claiming the
    update completed)."""
    _commit_deploy(
        deploy_sandbox["work"], wrapper_content=NEW_WRAPPER, marker="B", caddy_sync_rc=2
    )
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=deploy_sandbox["work"])

    result = cli_call(
        "perform_update", _env_for(deploy_sandbox, fail_caddy_restart=True)
    )
    assert result.returncode != 0
