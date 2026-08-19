"""Regression coverage for the CI-only pg_dump/pg_restore -> service
container wrapper (.github/scripts/pg-client-wrapper.sh).

These tests never touch a real Docker daemon or PostgreSQL server: `docker`
itself is replaced with a fake script placed first on PATH that records how
it was invoked and simulates the tool's stdout/stdin/exit-code behavior. That
proves the wrapper's argument forwarding, PGPASSWORD propagation, and
binary-safe stdin/stdout passthrough deterministically, in CI and locally
alike -- no docker daemon or postgres:16 service container required.

The wrapper's real target -- the postgres:16 service container's own
pg_dump/pg_restore -- is exercised end to end whenever CI runs
tests/integration/test_backup_restore.py, which is what these tests can't
cover: this file proves the shell plumbing, that file proves the actual
backup/restore semantics.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).parent.parent / ".github" / "scripts" / "pg-client-wrapper.sh"

_FAKE_DOCKER = '''#!/usr/bin/env python3
import os
import sys

with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(repr(sys.argv[1:]) + "\\n")

# Expected shape: exec --interactive --env PGPASSWORD=<v> <container> <tool> [args...]
args = sys.argv[1:]
assert args[:2] == ["exec", "--interactive"], args
assert args[2] == "--env", args
assert args[3].startswith("PGPASSWORD="), args
container = args[4]
tool = args[5]
rest = args[6:]

with open(os.environ["FAKE_DOCKER_CONTAINER_SEEN"], "w") as f:
    f.write(container)

exit_code = int(os.environ.get("FAKE_DOCKER_EXIT_CODE", "0"))

if "--version" in rest:
    sys.stdout.write(f"{tool} (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)\\n")
    sys.exit(exit_code)

if tool == "pg_dump":
    sys.stdout.buffer.write(b"FAKE-DUMP-BYTES-\\x00\\x01\\xff-END")
    sys.exit(exit_code)

if tool == "pg_restore":
    data = sys.stdin.buffer.read()
    with open(os.environ["FAKE_DOCKER_STDIN_CAPTURE"], "wb") as f:
        f.write(data)
    sys.exit(exit_code)

sys.exit(99)
'''


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def wrapper_bin(tmp_path):
    """A PATH directory with pg_dump/pg_restore (the real wrapper, installed
    exactly as the CI setup step installs it) and a fake docker standing in
    for the real one."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    pg_dump = bindir / "pg_dump"
    pg_dump.write_bytes(WRAPPER.read_bytes())
    _make_executable(pg_dump)
    (bindir / "pg_restore").symlink_to("pg_dump")

    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER)
    _make_executable(docker)

    return bindir


def _env(bindir, tmp_path, **extra):
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "POSTGRES_SERVICE_CONTAINER": "fake-container-id",
        "FAKE_DOCKER_LOG": str(tmp_path / "docker-invocations.log"),
        "FAKE_DOCKER_CONTAINER_SEEN": str(tmp_path / "container-seen.txt"),
        "FAKE_DOCKER_STDIN_CAPTURE": str(tmp_path / "stdin-capture.bin"),
    }
    env.update(extra)
    return env


def test_pg_dump_version_reports_the_container_tools_version(wrapper_bin, tmp_path):
    result = subprocess.run(
        [str(wrapper_bin / "pg_dump"), "--version"],
        env=_env(wrapper_bin, tmp_path),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "16.4" in result.stdout


def test_pg_restore_version_reports_the_container_tools_version(wrapper_bin, tmp_path):
    result = subprocess.run(
        [str(wrapper_bin / "pg_restore"), "--version"],
        env=_env(wrapper_bin, tmp_path),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "16.4" in result.stdout


def test_pg_dump_forwards_args_container_id_and_streams_stdout_untouched(wrapper_bin, tmp_path):
    result = subprocess.run(
        [
            str(wrapper_bin / "pg_dump"),
            "--host", "localhost", "--port", "5432",
            "--username", "centralpay", "--dbname", "centralpay_test",
            "--format=custom",
        ],
        env=_env(wrapper_bin, tmp_path, PGPASSWORD="s3cret"),
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == b"FAKE-DUMP-BYTES-\x00\x01\xff-END"  # binary-safe passthrough
    assert (tmp_path / "container-seen.txt").read_text() == "fake-container-id"
    invocation = (tmp_path / "docker-invocations.log").read_text()
    assert "PGPASSWORD=s3cret" in invocation
    assert "'pg_dump'" in invocation
    assert "'--dbname', 'centralpay_test'" in invocation


def test_pg_restore_forwards_stdin_untouched_binary_safe(wrapper_bin, tmp_path):
    payload = b"PGDMP" + bytes(range(256)) * 4
    result = subprocess.run(
        [str(wrapper_bin / "pg_restore"), "--list"],
        input=payload,
        env=_env(wrapper_bin, tmp_path),
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert (tmp_path / "stdin-capture.bin").read_bytes() == payload


def test_wrapper_exit_code_propagates(wrapper_bin, tmp_path):
    result = subprocess.run(
        [str(wrapper_bin / "pg_restore"), "--list"],
        input=b"not-a-real-archive",
        env=_env(wrapper_bin, tmp_path, FAKE_DOCKER_EXIT_CODE="1"),
        capture_output=True, timeout=10,
    )
    assert result.returncode == 1


def test_wrapper_fails_loudly_without_service_container_set(wrapper_bin, tmp_path):
    env = _env(wrapper_bin, tmp_path)
    del env["POSTGRES_SERVICE_CONTAINER"]
    result = subprocess.run(
        [str(wrapper_bin / "pg_dump"), "--version"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "POSTGRES_SERVICE_CONTAINER" in result.stderr


def test_wrapper_forwards_empty_pgpassword_when_unset(wrapper_bin, tmp_path):
    env = _env(wrapper_bin, tmp_path)
    env.pop("PGPASSWORD", None)
    result = subprocess.run(
        [str(wrapper_bin / "pg_dump"), "--version"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    invocation = (tmp_path / "docker-invocations.log").read_text()
    assert "PGPASSWORD=" in invocation
