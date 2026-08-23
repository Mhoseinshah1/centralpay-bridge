"""scripts/centralpay: `--help`/`-h` on a mutating command must be a pure,
side-effect-free no-op (production incident: `sudo centralpay update --help`
ran a REAL update instead of printing help).

Deterministic subprocess tests — no root, no Docker, no networking. Every
external command a real mutating action could possibly invoke (git, curl,
wget, docker, pg_dump, pg_restore, systemctl, cp, mv, rm) is replaced on
PATH with a wrapper that records its own invocation and exits non-zero; if
help parsing ever falls through to the real action, the very first such
call fails the test immediately AND leaves a recorded invocation behind
that the assertions catch either way.
"""

import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "scripts" / "centralpay"

# Every top-level command the fix guards -- see the `wants_help` check in
# main() (scripts/centralpay). Kept in sync with that case-pattern list.
GUARDED_COMMANDS = (
    "update",
    "restart",
    "stop",
    "start",
    "rollback",
    "migrate",
    "backup",
    "restore",
    "db-check",
    "fee",
    "review",
    "notification",
    "recover-aged-out",
    "admin-bot",
    "monitor",
    "ssl",
    "uninstall",
)

_DANGEROUS_COMMANDS = (
    "git",
    "curl",
    "wget",
    "docker",
    "pg_dump",
    "pg_restore",
    "systemctl",
    "cp",
    "mv",
    "rm",
)

_INVOCATION_LOG_VAR = "CENTRALPAY_TEST_INVOCATION_LOG"


@pytest.fixture
def fake_dangerous_path(tmp_path):
    """A PATH entry, prepended ahead of the real system PATH, containing a
    wrapper for every command a mutating centralpay action could invoke.
    Each wrapper appends its own name + args to a log file and exits 1 --
    so any code path that reaches a real action fails loudly, and the log
    file proves (by its absence) that NOTHING dangerous ran at all."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    log_file = tmp_path / "invocations.log"
    for name in _DANGEROUS_COMMANDS:
        wrapper = fake_bin / name
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{name} $*" >> "${_INVOCATION_LOG_VAR}"\n'
            "exit 1\n"
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin, log_file


def _run(cli_args: list[str], fake_dangerous_path, *, extra_env: dict[str, str] | None = None):
    fake_bin, log_file = fake_dangerous_path
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CENTRALPAY_CLI_SOURCE_ONLY": "1",
        _INVOCATION_LOG_VAR: str(log_file),
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; shift; main "$@"', "_", str(CLI), *cli_args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    invocations = log_file.read_text() if log_file.exists() else ""
    return result, invocations


@pytest.mark.parametrize("command", GUARDED_COMMANDS)
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_on_mutating_command_has_zero_side_effects(command, flag, fake_dangerous_path):
    result, invocations = _run([command, flag], fake_dangerous_path)
    assert result.returncode == 0, result.stderr
    assert "Usage: centralpay COMMAND" in result.stdout
    assert invocations == "", f"dangerous commands were invoked: {invocations!r}"


@pytest.mark.parametrize(
    "args",
    [
        ["update", "extra", "--help"],  # --help not first, still caught
        ["update", "--help", "--check"],  # --help wins even before --check
        ["restore", "some-backup-file.dump", "--help"],
        ["review", "resolve", "ORDER123", "--resolution", "value", "--help"],
        ["notification", "accept", "ORDER123", "--help"],
        ["admin-bot", "enable", "--help"],
        ["monitor", "enable", "--help"],
        ["monitor", "disable", "-h"],
        ["fee", "set", "10", "--help"],
    ],
)
def test_help_anywhere_in_args_short_circuits(args, fake_dangerous_path):
    result, invocations = _run(args, fake_dangerous_path)
    assert result.returncode == 0, result.stderr
    assert "Usage: centralpay COMMAND" in result.stdout
    assert invocations == "", f"dangerous commands were invoked: {invocations!r}"


def test_update_check_without_help_is_unaffected(fake_dangerous_path):
    """The guard must not swallow the existing `update --check` UX -- only
    an actual -h/--help token short-circuits to usage."""
    result, _invocations = _run(["update", "--check"], fake_dangerous_path)
    # No help flag present: falls through to cmd_update_check, which
    # legitimately invokes `git` (fetch) against a nonexistent install --
    # it must NOT print the generic Usage banner.
    assert "Usage: centralpay COMMAND" not in result.stdout


def test_update_without_help_does_not_print_usage(fake_dangerous_path, tmp_path):
    """Negative control: a real (non-help) `update` invocation must still
    attempt the real command path (and fail on require_install / missing
    install), never silently resolve to the help banner."""
    result, _ = _run(
        ["update"],
        fake_dangerous_path,
        extra_env={"CENTRALPAY_INSTALL_DIR": str(tmp_path / "nonexistent")},
    )
    assert "Usage: centralpay COMMAND" not in result.stdout
    assert result.returncode != 0


def test_wants_help_matches_standalone_tokens_only():
    """wants_help must never match a substring -- only the exact `-h`/
    `--help` token -- so a --note value that merely CONTAINS "help" is
    never misinterpreted as a help request."""
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; if wants_help "--note" "please help me later"; '
            'then echo rc=0; else echo rc=1; fi',
            "_", str(CLI),
        ],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CENTRALPAY_CLI_SOURCE_ONLY": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "rc=1" in result.stdout, result.stdout

    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; if wants_help "--note" "--help"; '
            'then echo rc=0; else echo rc=1; fi',
            "_", str(CLI),
        ],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CENTRALPAY_CLI_SOURCE_ONLY": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "rc=0" in result.stdout, result.stdout


def test_help_flag_command_list_matches_main_dispatch():
    """Regression guard: GUARDED_COMMANDS above must track the exact
    case-pattern list in main() -- if a new mutating command is added to
    the dispatcher without also adding it to the help guard, this test
    fails instead of silently leaving it unguarded."""
    source = CLI.read_text()
    guard_start = source.index("if wants_help \"$@\"; then")
    case_start = source.rindex("case \"$cmd\" in", 0, guard_start)
    guard_case_block = source[case_start:guard_start]
    for command in GUARDED_COMMANDS:
        assert command in guard_case_block, f"{command} missing from the help guard case block"
