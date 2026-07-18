"""Backup/restore validation on real PostgreSQL.

Proves the backup strategy used by scripts/backup.sh end to end: a
pg_dump custom-format archive of a populated database restores to an
identical financial state, and a corrupted archive is rejected by the
same pg_restore --list validation the backup script performs.

These run only when TEST_DATABASE_URL points at a disposable PostgreSQL
database (same gate as test_postgres.py) and pg_dump/pg_restore client
binaries are available.
"""

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, PaymentStatus
from tests.conftest import (
    CentralPayStub,
    build_app,
    create_order,
    get_payment,
    valid_callback_path,
    verify_ok_response,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
    pytest.mark.skipif(
        shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
        reason="pg_dump/pg_restore client binaries are required",
    ),
]

_TABLES = ("admin_alerts", "worker_heartbeats", "payment_events", "payments")


def _pg_env_and_args():
    """Split TEST_DATABASE_URL into libpq CLI args plus a PGPASSWORD env."""
    parts = urlsplit(TEST_DATABASE_URL.replace("postgresql+psycopg", "postgresql"))
    args = [
        "--host", parts.hostname or "localhost",
        "--port", str(parts.port or 5432),
        "--username", parts.username or "postgres",
        "--dbname", parts.path.lstrip("/"),
    ]
    env = {**os.environ, "PGPASSWORD": parts.password or ""}
    return args, env


@functools.lru_cache(maxsize=1)
def _server_major() -> int:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            version_num = int(
                connection.execute(text("SHOW server_version_num")).scalar_one()
            )
    finally:
        engine.dispose()
    return version_num // 10000


def _tool_major(path: str) -> int:
    out = subprocess.run(
        [path, "--version"], capture_output=True, text=True, check=True, timeout=30
    ).stdout
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", out)
    if match is None:
        raise RuntimeError(f"cannot parse PostgreSQL tool version from {out!r}")
    return int(match.group(1))


def _find_pg_tool(
    name: str, server_major: int, search_root: str | Path = "/usr/lib/postgresql"
) -> str:
    """Resolve a pg_dump/pg_restore binary compatible with the server.

    pg_dump aborts when the server's major version is NEWER than its own
    ("aborting because of server version mismatch") — exactly what happened
    on ubuntu-22.04 CI runners, whose default client is 14 while the test
    server is postgres:16. Production is immune by construction (the backup
    script runs the tools inside the db container, always version-matched);
    this resolver gives the test harness the same guarantee: prefer the
    PATH binary when its major is >= the server's, fall back to the
    Debian/Ubuntu versioned layout, and otherwise fail loudly — never run
    an incompatible tool and never skip the validation.
    """
    default = shutil.which(name)
    default_major = _tool_major(default) if default is not None else None
    if default is not None and default_major is not None and default_major >= server_major:
        return default
    versioned = Path(search_root) / str(server_major) / "bin" / name
    if versioned.is_file():
        return str(versioned)
    found = f"{name} major {default_major}" if default else f"no {name} on PATH"
    raise RuntimeError(
        f"{found} cannot handle PostgreSQL {server_major} (pg tools abort on "
        f"newer-major servers); install postgresql-client-{server_major}"
    )


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in (*_TABLES, "alembic_version"):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    yield engine
    engine.dispose()


@pytest.fixture
def populated_db(settings, pg_engine):
    """A database carrying one verified payment and one still-open payment."""
    Base.metadata.create_all(pg_engine)
    session_factory = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    stub = CentralPayStub()
    application = build_app(settings, session_factory, stub)
    with TestClient(application, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="bk-1", amount=25000).status_code == 200
        payment = get_payment(session_factory, "bk-1")
        stub.verify_result = verify_ok_response(amount=25000, reference_id="REF-bk-1")
        assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
        assert create_order(client, settings, order_id="bk-2", amount=5000).status_code == 200
    application.state.centralpay.close()
    return session_factory


def _snapshot(engine):
    with engine.connect() as connection:
        counts = {
            table: connection.execute(
                text(f"SELECT count(*) FROM {table}")  # fixed table names, not user input
            ).scalar_one()
            for table in _TABLES
        }
        payments = connection.execute(
            text(
                "SELECT bot_order_id, status, amount, reference_id,"
                " gateway_verified_at IS NOT NULL AS verified"
                " FROM payments ORDER BY bot_order_id"
            )
        ).all()
    return counts, [tuple(row) for row in payments]


def _dump(tmp_path):
    args, env = _pg_env_and_args()
    pg_dump = _find_pg_tool("pg_dump", _server_major())
    dump_file = tmp_path / "centralpay-test.dump"
    with open(dump_file, "wb") as out:
        subprocess.run(
            [pg_dump, *args, "--format=custom"],
            env=env, stdout=out, check=True, timeout=120,
        )
    return dump_file


def test_pg_dump_restore_round_trip(populated_db, pg_engine, tmp_path):
    counts_before, payments_before = _snapshot(pg_engine)
    assert counts_before["payments"] == 2
    assert counts_before["payment_events"] > 0

    dump_file = _dump(tmp_path)
    args, env = _pg_env_and_args()
    pg_restore = _find_pg_tool("pg_restore", _server_major())

    # The same validation gate scripts/backup.sh uses before a file counts
    # as a backup at all.
    with open(dump_file, "rb") as dump:
        subprocess.run(
            [pg_restore, "--list"],
            env=env, stdin=dump, stdout=subprocess.DEVNULL, check=True, timeout=120,
        )

    # Simulate total data loss, then restore.
    with pg_engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    subprocess.run(
        [pg_restore, *args, "--no-owner", str(dump_file)],
        env=env, check=True, timeout=120,
    )

    counts_after, payments_after = _snapshot(pg_engine)
    assert counts_after == counts_before
    assert payments_after == payments_before
    # The financial facts survived byte-for-byte.
    restored = {row[0]: row for row in payments_after}
    assert restored["bk-1"][1] == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert restored["bk-1"][2] == 25000
    assert restored["bk-1"][3] == "REF-bk-1"
    assert restored["bk-1"][4] is True
    assert restored["bk-2"][1] == PaymentStatus.LINK_CREATED.value
    assert restored["bk-2"][4] is False


def test_corrupted_backup_is_rejected(populated_db, tmp_path):
    dump_file = _dump(tmp_path)
    corrupted = tmp_path / "corrupted.dump"
    data = dump_file.read_bytes()
    # Truncate and flip header bytes: an interrupted/damaged transfer.
    corrupted.write_bytes(b"\x00\xff" + data[10 : len(data) // 2])

    _, env = _pg_env_and_args()
    pg_restore = _find_pg_tool("pg_restore", _server_major())
    with open(corrupted, "rb") as dump:
        result = subprocess.run(
            [pg_restore, "--list"],
            env=env, stdin=dump, capture_output=True, timeout=120,
        )
    assert result.returncode != 0  # validation gate refuses the file


# --- regression: ubuntu-22.04 client/server version mismatch ----------------


def _write_fake_tool(path: Path, version_line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{version_line}'\n")
    path.chmod(0o755)


def test_pg_tool_resolver_prefers_version_matched_binary(tmp_path, monkeypatch):
    """Regression for the ubuntu-22.04 CI failure: the runner's default
    pg_dump was major 14 while the server was PostgreSQL 16, and pg_dump
    aborted with 'server version mismatch'. The resolver must pick a
    version-matched binary over the older PATH default."""
    _write_fake_tool(
        tmp_path / "bin" / "pg_dump",
        "pg_dump (PostgreSQL) 14.23 (Ubuntu 14.23-1.pgdg22.04+1)",
    )
    versioned = tmp_path / "lib" / "16" / "bin" / "pg_dump"
    _write_fake_tool(versioned, "pg_dump (PostgreSQL) 16.14")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert _find_pg_tool("pg_dump", 16, search_root=tmp_path / "lib") == str(versioned)


def test_pg_tool_resolver_accepts_newer_client(tmp_path, monkeypatch):
    """A client NEWER than the server is supported by PostgreSQL and must be
    used as-is (pg_dump 16 dumping a 16-or-older server)."""
    default = tmp_path / "bin" / "pg_dump"
    _write_fake_tool(default, "pg_dump (PostgreSQL) 16.14")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert _find_pg_tool("pg_dump", 16, search_root=tmp_path / "lib") == str(default)


def test_pg_tool_resolver_fails_loudly_without_compatible_binary(tmp_path, monkeypatch):
    """With only an older client available, the resolver must raise a clear
    error — never invoke the incompatible tool, never silently skip the
    backup validation."""
    _write_fake_tool(
        tmp_path / "bin" / "pg_dump",
        "pg_dump (PostgreSQL) 14.23 (Ubuntu 14.23-1.pgdg22.04+1)",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    with pytest.raises(RuntimeError, match="postgresql-client-16"):
        _find_pg_tool("pg_dump", 16, search_root=tmp_path / "lib")
