"""CANON-1 — installer initial-fee recovery on rerun (real PostgreSQL).

The dedicated `app.ops fee ensure-initial` operation is the atomic core of
the fix: under a transaction-level advisory lock it no-ops when any policy
history exists, and when the table is EMPTY it requires an explicit,
validated rate — a missing value never means 0%. These tests prove the
installer can recover the operator's intended rate after a failed first run
and can never silently ship a 0% fee.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import (
    TEST_CALLBACK_HMAC_SECRET,
    TEST_GETLINK_API_KEY,
    TEST_INBOUND_API_KEY,
    TEST_VERIFY_API_KEY,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("postgresql"),
    reason="TEST_DATABASE_URL with a postgresql URL is required",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# A full, valid Settings environment (fixed non-secret test values). Only
# DATABASE_URL matters to the fee operation; the rest satisfy validation.
_OPS_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "DATABASE_URL": TEST_DATABASE_URL,
    "PUBLIC_BASE_URL": "https://pay.example.com",
    # Reference the shared (gitleaks-allowlisted) test fixtures rather than
    # inlining secret-shaped literals in this file.
    "INBOUND_API_KEY": TEST_INBOUND_API_KEY,
    "CALLBACK_HMAC_SECRET": TEST_CALLBACK_HMAC_SECRET,
    "CENTRALPAY_GETLINK_API_KEY": TEST_GETLINK_API_KEY,
    "CENTRALPAY_VERIFY_API_KEY": TEST_VERIFY_API_KEY,
    "CENTRALPAY_USER_ID": "1",
    "LOG_LEVEL": "WARNING",
}


@pytest.fixture
def fresh_db():
    from tests.integration.test_postgres import _drop_all

    engine = create_engine(TEST_DATABASE_URL)
    _drop_all(engine)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}"
    yield engine
    engine.dispose()


def run_ops(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.ops", *args],
        cwd=PROJECT_ROOT,
        env=_OPS_ENV,
        capture_output=True,
        text=True,
        timeout=60,
    )


def policies(engine) -> list[tuple[int, str, str]]:
    with engine.connect() as conn:
        return [
            (row[0], row[1], row[2])
            for row in conn.execute(
                text("SELECT rate_bps, created_by, note FROM fee_policies ORDER BY id")
            )
        ]


# 1 + 2. Failed first run leaves an empty table; the rerun recovers the
# operator's 5% (never 0%).
def test_rerun_recovers_persisted_five_percent_never_zero(fresh_db):
    # The first run's ensure step failed transiently: no policy row exists.
    assert policies(fresh_db) == []
    # The rerun recovers INSTALLER_INITIAL_FEE_PERCENT=5 → exactly one 5% policy.
    result = run_ops("fee", "ensure-initial", "--percent", "5", "--actor", "installer")
    assert result.returncode == 0, result.stderr
    rows = policies(fresh_db)
    assert rows == [(500, "installer", "Initial installation fee")]
    # Explicitly: no 0% policy was ever created.
    assert all(rate != 0 for rate, _, _ in rows)


# 3. Legacy env (no recovery metadata) with an existing 7% policy: the rerun
# passes no rate and preserves the history exactly.
def test_legacy_rerun_with_existing_policy_preserves_history(fresh_db):
    assert run_ops(
        "fee", "ensure-initial", "--percent", "7", "--actor", "installer"
    ).returncode == 0
    before = policies(fresh_db)
    assert before == [(700, "installer", "Initial installation fee")]
    # Legacy rerun: no --percent supplied.
    result = run_ops("fee", "ensure-initial", "--actor", "installer")
    assert result.returncode == 0, result.stderr
    assert "already exists" in result.stdout
    assert policies(fresh_db) == before  # unchanged, exactly


# 4. Legacy env (no metadata) with ZERO policies: the rerun fails, creates no
# policy, and never reports success.
def test_legacy_rerun_with_zero_policies_fails_closed(fresh_db):
    assert policies(fresh_db) == []
    result = run_ops("fee", "ensure-initial", "--actor", "installer")
    assert result.returncode != 0
    assert policies(fresh_db) == []  # nothing created
    assert "NO policy" in result.stderr or "no fee policy" in result.stderr
    # No success wording on the failure path.
    assert "created" not in result.stdout.lower()


# 5. Persisted malformed fee metadata: fails before any mutation.
@pytest.mark.parametrize("bad", ["101", "-5", "1e2", "abc", "10.555"])
def test_malformed_persisted_rate_fails_before_mutation(fresh_db, bad):
    result = run_ops("fee", "ensure-initial", "--percent", bad, "--actor", "installer")
    assert result.returncode != 0, bad
    assert policies(fresh_db) == [], bad


# 6. Two concurrent reruns create at most one initial policy (advisory lock).
def test_concurrent_reruns_create_at_most_one_policy(fresh_db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_ops, "fee", "ensure-initial", "--percent", "5", "--actor", "installer")
            for _ in range(2)
        ]
        results = [f.result() for f in futures]
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    rows = policies(fresh_db)
    assert len(rows) == 1, rows
    assert rows[0][0] == 500


# 7. A subsequent explicit fee change is never touched by installer reruns.
def test_installer_rerun_never_touches_a_later_explicit_change(fresh_db):
    assert run_ops(
        "fee", "ensure-initial", "--percent", "5", "--actor", "installer"
    ).returncode == 0
    # Operator explicitly changes the fee to 8% (new policy, history appended).
    assert run_ops(
        "fee", "set", "8", "--note", "operator change", "--actor", "host-cli"
    ).returncode == 0
    before = policies(fresh_db)
    assert len(before) == 2
    # A later installer rerun (with the stale recorded 5%) must no-op.
    result = run_ops("fee", "ensure-initial", "--percent", "5", "--actor", "installer")
    assert result.returncode == 0
    assert "already exists" in result.stdout
    assert policies(fresh_db) == before  # the 8% change is untouched
    # The effective rate is still the operator's 8%, not the installer's 5%.
    status = run_ops("fee", "status")
    assert "8%" in status.stdout
