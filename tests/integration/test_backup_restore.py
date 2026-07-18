"""Backup/restore validation on real PostgreSQL.

Proves the backup strategy used by scripts/backup.sh end to end: a
pg_dump custom-format archive of a populated database restores to an
identical financial state, and a corrupted archive is rejected by the
same pg_restore --list validation the backup script performs.

These run only when TEST_DATABASE_URL points at a disposable PostgreSQL
database (same gate as test_postgres.py) and pg_dump/pg_restore client
binaries are available.
"""

import os
import shutil
import subprocess
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
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed table names
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
    dump_file = tmp_path / "centralpay-test.dump"
    with open(dump_file, "wb") as out:
        subprocess.run(
            ["pg_dump", *args, "--format=custom"],
            env=env, stdout=out, check=True, timeout=120,
        )
    return dump_file


def test_pg_dump_restore_round_trip(populated_db, pg_engine, tmp_path):
    counts_before, payments_before = _snapshot(pg_engine)
    assert counts_before["payments"] == 2
    assert counts_before["payment_events"] > 0

    dump_file = _dump(tmp_path)
    args, env = _pg_env_and_args()

    # The same validation gate scripts/backup.sh uses before a file counts
    # as a backup at all.
    with open(dump_file, "rb") as dump:
        subprocess.run(
            ["pg_restore", "--list"],
            env=env, stdin=dump, stdout=subprocess.DEVNULL, check=True, timeout=120,
        )

    # Simulate total data loss, then restore.
    with pg_engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    subprocess.run(
        ["pg_restore", *args, "--no-owner", str(dump_file)],
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
    with open(corrupted, "rb") as dump:
        result = subprocess.run(
            ["pg_restore", "--list"],
            env=env, stdin=dump, capture_output=True, timeout=120,
        )
    assert result.returncode != 0  # validation gate refuses the file
