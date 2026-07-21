"""Production upgrade/recovery path for migration 0008 (incident 2026-07).

Production already executed the ORIGINAL 0007 (mapping table +
payer_identity_id/payer_derivation_version, NO payer_identity_type) and its
``alembic_version`` is ``0007``; the application may currently be rolled back
to the pre-0007 code (commit b897e69) while the schema stays forward. These
tests start from that EXACT deployed state and prove:

* 0007 in this tree is the deployed original (it must never be edited as the
  mechanism for adding ``payer_identity_type`` — Alembic will not re-run it);
* ``alembic upgrade head`` from revision 0007 runs exactly 0008, which adds the
  nullable typed-scope column + CHECK without touching existing rows;
* 0007-era rows (payer_identity_id set, scope never stored) and pre-0007 legacy
  rows keep ``payer_identity_type IS NULL`` — no backfill guessing;
* the upgrade is recovery-safe (re-runnable after a rollback leaves the DB at
  0008 while the pointer says 0007) with no schema downgrade and no data loss;
* the NEW application starts against the migrated database, returns existing
  links unchanged, adopts identities only pre-link, and existing callbacks
  keep verifying against their stored snapshots.

Alembic runs in a subprocess (as production does) so its logging config cannot
leak into the test process. Requires TEST_DATABASE_URL (PostgreSQL).
"""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.security import callback_signature, callback_token_hash, generate_callback_token
from app.services.payer_identity import IDENTITY_TYPE_TELEGRAM_USER
from tests.conftest import (
    TEST_USER_ID,
    CentralPayStub,
    build_app,
    expected_gateway_user_id,
    verify_ok_response,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

_TABLES = (
    "admin_alerts",
    "worker_heartbeats",
    "payment_events",
    "payments",
    "centralpay_payer_identities",
    "fee_policies",
    "alembic_version",
)

# Deployed-production stand-ins (test values only, gitleaks-safe).
_LEGACY_GOID = 910000000001  # pre-0007 row: shared payer id, live link
_ERA7_LINKED_GOID = 910000000002  # 0007-era row: mapped identity, live link
_ERA7_PRELINK_GOID = 910000000003  # 0007-era row: mapped identity, no link yet
_ERA7_LINKED_UID = 1234567890
_ERA7_PRELINK_UID = 1234567891


def _alembic(*args: str) -> str:
    """Run alembic in a subprocess exactly as production does."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout + result.stderr


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    yield engine
    engine.dispose()


def _column_names(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _check_names(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_check_constraints(table)}


def _alembic_version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _seed_deployed_production_state(engine) -> dict[str, str]:
    """Raw-SQL rows exactly as the deployed 0007-era system could have written
    them (the current ORM model cannot be used: it already knows the 0008
    column). Returns the plaintext callback tokens keyed by scenario."""
    tokens = {"legacy": generate_callback_token(), "era7_linked": generate_callback_token()}
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO centralpay_payer_identities"
                " (customer_key_hash, gateway_user_id, derivation_version)"
                " VALUES (:h, :uid, 1)"
            ),
            [
                {"h": "a" * 64, "uid": _ERA7_LINKED_UID},
                {"h": "b" * 64, "uid": _ERA7_PRELINK_UID},
            ],
        )
        ids = dict(
            conn.execute(
                text(
                    "SELECT gateway_user_id, id FROM centralpay_payer_identities"
                    " WHERE gateway_user_id IN (:a, :b)"
                ),
                {"a": _ERA7_LINKED_UID, "b": _ERA7_PRELINK_UID},
            ).all()
        )
        insert = text(
            "INSERT INTO payments"
            " (bot_order_id, gateway_order_id, gateway_user_id, payer_identity_id,"
            "  payer_derivation_version, amount, payable_amount, status, redirect_url,"
            "  callback_token_hash, callback_token_issued_at)"
            " VALUES (:bo, :go, :gu, :pid, :dv, 10000, 10000, :st, :ru, :th, :ti)"
        )
        conn.execute(
            insert,
            [
                {  # pre-0007 legacy: shared payer id, no mapping, live link
                    "bo": "legacy-live",
                    "go": _LEGACY_GOID,
                    "gu": TEST_USER_ID,
                    "pid": None,
                    "dv": None,
                    "st": "link_created",
                    "ru": "https://gateway.test/pay/legacy-live",
                    "th": callback_token_hash(tokens["legacy"]),
                    "ti": now,
                },
                {  # 0007-era: customer-scoped identity, live link
                    "bo": "era7-linked",
                    "go": _ERA7_LINKED_GOID,
                    "gu": _ERA7_LINKED_UID,
                    "pid": ids[_ERA7_LINKED_UID],
                    "dv": 1,
                    "st": "link_created",
                    "ru": "https://gateway.test/pay/era7-linked",
                    "th": callback_token_hash(tokens["era7_linked"]),
                    "ti": now,
                },
                {  # 0007-era: customer-scoped identity, still awaiting a link
                    "bo": "era7-prelink",
                    "go": _ERA7_PRELINK_GOID,
                    "gu": _ERA7_PRELINK_UID,
                    "pid": ids[_ERA7_PRELINK_UID],
                    "dv": 1,
                    "st": "created",
                    "ru": None,
                    "th": None,
                    "ti": now,
                },
            ],
        )
    return tokens


def test_deployed_0007_then_upgrade_head_runs_0008_and_app_survives(
    settings, pg_engine
):
    """Items 5+6 of the blocker: from the EXACT deployed original-0007 state
    (alembic_version=0007, app possibly rolled back to b897e69 — which leaves
    the schema forward exactly like this), `alembic upgrade head` applies 0008
    with no schema downgrade and no data loss, and the new app serves existing
    payments and callbacks."""
    # --- reproduce the deployed production schema ---------------------------
    _alembic("upgrade", "0007")
    assert _alembic_version(pg_engine) == "0007"
    # The deployed 0007 must NOT create the 0008 column: proves 0007 in this
    # tree is the original and was not edited as the delivery mechanism.
    assert "payer_identity_type" not in _column_names(pg_engine, "payments")
    assert "payer_identity_id" in _column_names(pg_engine, "payments")

    tokens = _seed_deployed_production_state(pg_engine)

    # --- the production upgrade step (pinned to the 0008 slice) -------------
    _alembic("upgrade", "0008")
    assert _alembic_version(pg_engine) == "0008"
    assert "payer_identity_type" in _column_names(pg_engine, "payments")
    assert "ck_payments_payer_identity_type_valid" in _check_names(pg_engine, "payments")

    with pg_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT bot_order_id, payer_identity_id IS NOT NULL AS mapped,"
                " payer_identity_type, redirect_url, gateway_user_id"
                " FROM payments ORDER BY gateway_order_id"
            )
        ).all()
    assert [r.bot_order_id for r in rows] == ["legacy-live", "era7-linked", "era7-prelink"]
    # No data loss and NO backfill guessing: every historical row keeps NULL.
    assert [r.payer_identity_type for r in rows] == [None, None, None]
    assert rows[0].mapped is False and rows[1].mapped is True and rows[2].mapped is True
    assert rows[0].redirect_url == "https://gateway.test/pay/legacy-live"
    assert rows[1].redirect_url == "https://gateway.test/pay/era7-linked"

    # The CHECK admits only the two known scopes (or NULL).
    with pg_engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                text(
                    "UPDATE payments SET payer_identity_type = 'bogus'"
                    " WHERE bot_order_id = 'era7-prelink'"
                )
            )
        conn.rollback()
        conn.execute(
            text(
                "UPDATE payments SET payer_identity_type = 'telegram_user'"
                " WHERE bot_order_id = 'era7-prelink'"
            )
        )
        conn.rollback()  # keep the seeded state for the app-level checks

    # --- recovery-safety: pointer at 0007 with 0008 schema already present --
    # (an app rollback moves code, never schema; re-upgrading must be a no-op
    # instead of an "already exists" failure, with zero data loss)
    _alembic("stamp", "0007")
    _alembic("upgrade", "0008")
    assert _alembic_version(pg_engine) == "0008"
    with pg_engine.connect() as conn:
        kept = conn.execute(text("SELECT count(*) FROM payments")).scalar_one()
    assert kept == 3

    # --- bring the schema to the full head for the application section ------
    # (0009 adds centralpay_payer_identities.identity_scheme, which the
    # current ORM model selects)
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0009"

    # --- the NEW application against the migrated database ------------------
    session_factory = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    stub = CentralPayStub()
    app = build_app(settings, session_factory, stub)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Existing legacy callback still verifies against the row's own
        # shared-id snapshot (never the mapping table or config).
        stub.verify_result = verify_ok_response(
            amount=10000, user_id=TEST_USER_ID, reference_id="REF-legacy-live"
        )
        sig = callback_signature(
            settings.callback_hmac_secret, _LEGACY_GOID, tokens["legacy"]
        )
        response = client.get(
            f"/api/centralpay/callback?orderId={_LEGACY_GOID}&ct={tokens['legacy']}&sig={sig}"
        )
        assert response.status_code == 200

        # A retry of the 0007-era LINKED order returns its existing link
        # unchanged: no new gateway call, scope stays NULL (immutable history).
        calls_before = len(stub.getlink_requests)
        retry = client.post(
            "/api/custom-payment",
            json={
                "api_key": settings.inbound_api_key,
                "amount": 10000,
                "order_id": "era7-linked",
                "user_id": 424242,
            },
        )
        assert retry.status_code == 200
        assert retry.json() == {"url": "https://gateway.test/pay/era7-linked"}
        assert len(stub.getlink_requests) == calls_before

        # A retry of the 0007-era PRE-link order adopts the requester's
        # isolated identity (typed) — never the historical or shared id.
        adopt = client.post(
            "/api/custom-payment",
            json={
                "api_key": settings.inbound_api_key,
                "amount": 10000,
                "order_id": "era7-prelink",
                "user_id": 525252,
            },
        )
        assert adopt.status_code == 200
        assert stub.getlink_requests[-1]["userId"] == expected_gateway_user_id(
            telegram_user_id=525252
        )
        assert stub.getlink_requests[-1]["userId"] not in (
            TEST_USER_ID,
            _ERA7_PRELINK_UID,
        )
    app.state.centralpay.close()

    with pg_engine.connect() as conn:
        after = dict(
            conn.execute(
                text("SELECT bot_order_id, payer_identity_type FROM payments")
            ).all()
        )
        legacy_status = conn.execute(
            text("SELECT status FROM payments WHERE bot_order_id = 'legacy-live'")
        ).scalar_one()
    assert after["era7-linked"] is None  # immutable historical scope
    assert after["era7-prelink"] == IDENTITY_TYPE_TELEGRAM_USER  # adopted + typed
    assert legacy_status == "bot_notify_pending"  # callback verified + queued


def test_upgrade_head_is_idempotent_on_partially_applied_0008(pg_engine):
    """Recovery-safety edge: a database that already carries the 0008 column
    (e.g. built from this branch before the pointer moved) upgrades cleanly —
    0008's guards make it a no-op instead of an 'already exists' error."""
    _alembic("upgrade", "0007")
    with pg_engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE payments ADD COLUMN payer_identity_type VARCHAR(16)")
        )
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0009"
    assert "ck_payments_payer_identity_type_valid" in _check_names(pg_engine, "payments")


def test_0008_downgrade_is_non_destructive_by_default(pg_engine):
    """`alembic downgrade 0007` moves only the pointer: the typed-scope column
    and CHECK survive, so no rollback ever requires a destructive schema change
    (dropping is an explicit CENTRALPAY_DROP_PAYER_IDENTITY=1 opt-in)."""
    _alembic("upgrade", "head")
    _alembic("downgrade", "0007")
    assert _alembic_version(pg_engine) == "0007"
    assert "payer_identity_type" in _column_names(pg_engine, "payments")
    assert "ck_payments_payer_identity_type_valid" in _check_names(pg_engine, "payments")
    # And forward again from that state.
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0009"
