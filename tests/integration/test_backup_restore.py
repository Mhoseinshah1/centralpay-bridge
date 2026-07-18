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

import httpx
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
    getlink_ok_response,
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

_TABLES = ("admin_alerts", "worker_heartbeats", "payment_events", "payments", "fee_policies")


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


def _build_full_state(settings, pg_engine, bot_stub, notifier):
    """A database carrying every creation/delivery state plus an alert."""
    import datetime as dt

    from sqlalchemy import update

    from app.models import AdminAlert, Payment
    from tests.conftest import run_pass

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0004')"))
    session_factory = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    stub = CentralPayStub()
    application = build_app(settings, session_factory, stub)

    def verify(client, order_id, amount):
        payment = get_payment(session_factory, order_id)
        stub.verify_result = verify_ok_response(amount=amount, reference_id=f"REF-{order_id}")
        assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    with TestClient(application, raise_server_exceptions=False) as client:
        # link_created
        assert create_order(client, settings, order_id="fs-link", amount=1000).status_code == 200
        # getlink_failed
        stub.getlink_result = httpx.ConnectError("refused")
        assert create_order(client, settings, order_id="fs-fail", amount=2000).status_code == 502
        stub.getlink_result = getlink_ok_response()
        # bot_notify_pending
        assert create_order(client, settings, order_id="fs-pend", amount=3000).status_code == 200
        verify(client, "fs-pend", 3000)
        # bot_notify_accepted
        assert create_order(client, settings, order_id="fs-acc", amount=4000).status_code == 200
        verify(client, "fs-acc", 4000)
        # manual_review (bot rejected with 422)
        assert create_order(client, settings, order_id="fs-rev", amount=5000).status_code == 200
        verify(client, "fs-rev", 5000)
        # retry-scheduled (bot 500)
        assert create_order(client, settings, order_id="fs-retry", amount=6000).status_code == 200
        verify(client, "fs-retry", 6000)
    application.state.centralpay.close()

    # Process the four queued payments one at a time, in queue order
    # (fs-pend, fs-acc, fs-rev, fs-retry — next_retry_at ascending), with
    # per-payment bot behavior so every outcome is deterministic.
    bot_stub.result = httpx.Response(200, json={"ok": True})
    run_pass(session_factory, notifier, settings, batch_size=1)  # fs-pend -> accepted
    run_pass(session_factory, notifier, settings, batch_size=1)  # fs-acc -> accepted
    bot_stub.result = httpx.Response(422)
    run_pass(session_factory, notifier, settings, batch_size=1)  # fs-rev -> manual review
    bot_stub.result = httpx.Response(500)
    run_pass(session_factory, notifier, settings, batch_size=1)  # fs-retry -> retry scheduled

    with session_factory() as session:
        # Stale claim on fs-pend... fs-pend was delivered above; instead mark
        # the retry-scheduled payment as stale-claimed to capture claim state.
        session.execute(
            update(Payment)
            .where(Payment.bot_order_id == "fs-retry")
            .values(
                notification_claimed_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                notification_claimed_by="crashed-worker",
            )
        )
        session.add(
            AdminAlert(
                alert_type="backup_test_alert",
                severity="warning",
                payload={"note": "fidelity-check"},
            )
        )
        session.commit()
    return session_factory


_FINANCIAL_COLUMNS = (
    "bot_order_id", "gateway_order_id", "status", "amount",
    "fee_policy_id", "fee_rate_bps", "fee_amount", "payable_amount", "reference_id",
    "card_last4", "callback_token_hash", "bot_notify_reason",
    "bot_notify_attempts", "notification_claimed_by", "review_resolution",
)


def _financial_snapshot(engine):
    with engine.connect() as connection:
        payments = connection.execute(
            text(
                "SELECT "
                + ", ".join(_FINANCIAL_COLUMNS)
                + ", gateway_verified_at IS NOT NULL AS verified,"
                " next_retry_at IS NOT NULL AS retry_scheduled,"
                " manual_review_at IS NOT NULL AS in_review"
                " FROM payments ORDER BY bot_order_id"
            )
        ).all()
        events = connection.execute(
            text(
                "SELECT payment_id, event_type, count(*) FROM payment_events"
                " GROUP BY payment_id, event_type ORDER BY payment_id, event_type"
            )
        ).all()
        alerts = connection.execute(
            text("SELECT alert_type, severity, status FROM admin_alerts ORDER BY id")
        ).all()
    return [tuple(r) for r in payments], [tuple(r) for r in events], [tuple(r) for r in alerts]


def test_full_state_round_trip_and_sequence_safety(
    settings, pg_engine, bot_stub, notifier, tmp_path
):
    """Audit task 041/049: every payment state, its audit history, alert
    outbox rows, and sequence positions survive a dump/wipe/restore
    byte-for-byte — and new inserts after restore cannot collide."""
    session_factory = _build_full_state(settings, pg_engine, bot_stub, notifier)

    # Sanity: the fixture really covers the states we claim it does.
    with pg_engine.connect() as connection:
        statuses = dict(
            connection.execute(text("SELECT bot_order_id, status FROM payments")).all()
        )
    assert statuses["fs-link"] == "link_created"
    assert statuses["fs-fail"] == "getlink_failed"
    assert statuses["fs-pend"] == "bot_notify_accepted"  # first delivery pass
    assert statuses["fs-acc"] == "bot_notify_accepted"
    assert statuses["fs-rev"] == "manual_review"
    assert statuses["fs-retry"] == "bot_notify_pending"

    before = _financial_snapshot(pg_engine)
    dump_file = _dump(tmp_path)
    args, env = _pg_env_and_args()
    pg_restore = _find_pg_tool("pg_restore", _server_major())

    with pg_engine.begin() as connection:
        for table in (*_TABLES, "alembic_version"):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    subprocess.run(
        [pg_restore, *args, "--no-owner", "--exit-on-error", str(dump_file)],
        env=env, check=True, timeout=120,
    )

    assert _financial_snapshot(pg_engine) == before  # full financial fidelity

    # Sequences were preserved by the custom-format dump: inserting a new
    # payment must not collide with restored primary keys.
    from app.models import Payment

    with session_factory() as session:
        session.add(
            Payment(
                bot_order_id="fs-after-restore",
                gateway_order_id=999_000_111_222,
                gateway_user_id=1,
                amount=7000,
                payable_amount=7000,
                status="created",
            )
        )
        session.commit()
    with pg_engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM payments")).scalar_one()
    assert count == 7


def test_db_check_detects_and_repairs_sequence_drift(
    settings, pg_engine, bot_stub, notifier, monkeypatch, capsys
):
    """Audit task 049: a sequence forced behind its table maximum (manual
    import, drifted restore) is detected by db-check, repaired with
    --repair-sequences, and inserts succeed afterwards."""
    import app.ops as ops_module
    from app.models import Payment
    from app.ops import main as ops_main

    session_factory = _build_full_state(settings, pg_engine, bot_stub, notifier)
    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)

    # Healthy database: db-check passes.
    assert ops_main(["db-check"]) == 0
    capsys.readouterr()

    # Force the payments sequence behind the table maximum.
    with pg_engine.begin() as connection:
        connection.execute(
            text("SELECT setval(pg_get_serial_sequence('payments', 'id'), 1)")
        )
    assert ops_main(["db-check"]) == 1
    out = capsys.readouterr().out
    assert "sequence_behind:payments" in out

    # Repair, then inserts succeed without PK collisions.
    assert ops_main(["db-check", "--repair-sequences"]) == 0
    with session_factory() as session:
        session.add(
            Payment(
                bot_order_id="seq-after-repair",
                gateway_order_id=999_000_111_333,
                gateway_user_id=1,
                amount=1000,
                payable_amount=1000,
                status="created",
            )
        )
        session.commit()


def test_zero_byte_and_plain_sql_archives_rejected(populated_db, tmp_path):
    """Audit task 042: the pg_restore --list validation gate refuses empty
    files and plain SQL passed off as custom-format archives."""
    _, env = _pg_env_and_args()
    pg_restore = _find_pg_tool("pg_restore", _server_major())

    empty = tmp_path / "empty.dump"
    empty.write_bytes(b"")
    sql = tmp_path / "sql.dump"
    sql.write_text("SELECT 1;\n-- not a custom-format archive\n")

    for candidate in (empty, sql):
        with open(candidate, "rb") as fh:
            result = subprocess.run(
                [pg_restore, "--list"],
                env=env, stdin=fh, capture_output=True, timeout=60,
            )
        assert result.returncode != 0, candidate


# --- dynamic fee policies survive backup and restore -------------------------


def test_fee_policies_survive_restore_and_stay_decoupled(
    settings, pg_engine, tmp_path
):
    """Active, scheduled, AND cancelled fee policies survive a
    dump/wipe/restore with full history; payment fee snapshots restore
    byte-for-byte; and a policy change made AFTER the restore still leaves
    restored payments untouched and gets a non-colliding policy id."""
    from datetime import UTC, datetime, timedelta

    from app.models import FeePolicy

    Base.metadata.create_all(pg_engine)
    session_factory = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)

    with session_factory() as db:
        db.add(
            FeePolicy(
                rate_bps=1000,
                effective_at=datetime(2020, 1, 1, tzinfo=UTC),
                created_by="test",
                note="active policy",
            )
        )
        db.add(
            FeePolicy(
                rate_bps=250,
                effective_at=datetime.now(UTC) + timedelta(days=30),
                created_by="test",
                note="scheduled policy",
            )
        )
        db.add(
            FeePolicy(
                rate_bps=9000,
                effective_at=datetime(2019, 1, 1, tzinfo=UTC),
                created_by="test",
                note="cancelled policy",
                cancelled_at=datetime(2019, 6, 1, tzinfo=UTC),
                cancelled_by="test",
                cancellation_note="was a mistake",
            )
        )
        db.commit()

    stub = CentralPayStub()
    application = build_app(settings, session_factory, stub)
    with TestClient(application, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="bk-fee", amount=500_000).status_code == 200
    application.state.centralpay.close()

    payment_before = get_payment(session_factory, "bk-fee")
    assert payment_before.fee_rate_bps == 1000
    assert payment_before.payable_amount == 550_000

    with pg_engine.connect() as connection:
        policies_before = [
            tuple(r)
            for r in connection.execute(
                text(
                    "SELECT id, rate_bps, effective_at, created_by, note,"
                    " cancelled_at, cancelled_by, cancellation_note"
                    " FROM fee_policies ORDER BY id"
                )
            ).all()
        ]
    assert len(policies_before) == 3

    dump_file = _dump(tmp_path)
    args, env = _pg_env_and_args()
    pg_restore = _find_pg_tool("pg_restore", _server_major())

    with pg_engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    subprocess.run(
        [pg_restore, *args, "--no-owner", "--exit-on-error", str(dump_file)],
        env=env, check=True, timeout=120,
    )

    with pg_engine.connect() as connection:
        policies_after = [
            tuple(r)
            for r in connection.execute(
                text(
                    "SELECT id, rate_bps, effective_at, created_by, note,"
                    " cancelled_at, cancelled_by, cancellation_note"
                    " FROM fee_policies ORDER BY id"
                )
            ).all()
        ]
    assert policies_after == policies_before  # full history, ids included

    payment_after = get_payment(session_factory, "bk-fee")
    assert payment_after.fee_policy_id == payment_before.fee_policy_id
    assert payment_after.fee_rate_bps == 1000
    assert payment_after.fee_amount == 50_000
    assert payment_after.payable_amount == 550_000

    # A fee change AFTER the restore: the sequence was preserved, so the
    # new policy id cannot collide, and restored payments keep their
    # snapshot untouched.
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    with session_factory() as db:
        new_policy = FeePolicy(
            rate_bps=500,
            effective_at=_datetime.now(_UTC),
            created_by="test",
            note="post-restore change",
        )
        db.add(new_policy)
        db.commit()
        new_policy_id = new_policy.id
    assert new_policy_id not in {row[0] for row in policies_before}

    unchanged = get_payment(session_factory, "bk-fee")
    assert unchanged.fee_rate_bps == 1000
    assert unchanged.payable_amount == 550_000
