"""Production upgrade/recovery path for migration 0009 (raw-id revision).

Production is at revision 0008 (schema: mapping table WITHOUT
``identity_scheme``, payments with ``payer_identity_type``). These tests start
from that EXACT state and prove:

* ``alembic upgrade head`` from 0008 runs exactly 0009, adding
  ``centralpay_payer_identities.identity_scheme`` labeled
  ``historical_hmac_v1`` for every existing row (non-destructive backfill via
  the column default) plus its CHECK;
* existing mappings and payment snapshots are preserved byte-for-byte — no
  data loss, no re-derivation, no re-pointing;
* the upgrade is recovery-safe (re-runnable after ``stamp 0008``; a database
  that already carries the column upgrades cleanly) and the downgrade is
  non-destructive by default;
* the NEW application serves the migrated database: a fresh Telegram payment
  sends the EXACT raw id, a historical v1 payment's callback keeps verifying
  against its stored HMAC snapshot, and a same-user retry of a historical
  order reuses the stored snapshot instead of re-deriving.

Alembic runs in a subprocess (as production does). Requires TEST_DATABASE_URL
(PostgreSQL).
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
from app.services.payer_identity import (
    IDENTITY_TYPE_TELEGRAM_USER,
    historical_identity_key_hash,
    telegram_identity_key,
)
from tests.conftest import (
    TEST_PAYER_ID_SECRET,
    CentralPayStub,
    build_app,
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

# A historical v1 tg-HMAC payment as the deployed 0008-era system wrote it:
# the mapping is keyed by the v1 hash of tg:<id> and carries a DERIVED (not
# raw) gateway id.
_V1_TG_ID = 909555
_V1_HMAC_UID = 1_555_000_777
_V1_GOID = 910000000301


def _alembic(*args: str) -> str:
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


def _seed_production_0008_state(engine) -> str:
    """Raw-SQL rows exactly as the deployed 0008-era system wrote them (no
    identity_scheme column exists yet). Returns the live row's callback token."""
    token = generate_callback_token()
    v1_key_hash = historical_identity_key_hash(
        TEST_PAYER_ID_SECRET, telegram_identity_key(_V1_TG_ID)
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO centralpay_payer_identities"
                " (customer_key_hash, gateway_user_id, derivation_version)"
                " VALUES (:h, :uid, 1)"
            ),
            {"h": v1_key_hash, "uid": _V1_HMAC_UID},
        )
        mapping_id = conn.execute(
            text(
                "SELECT id FROM centralpay_payer_identities"
                " WHERE gateway_user_id = :uid"
            ),
            {"uid": _V1_HMAC_UID},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO payments"
                " (bot_order_id, gateway_order_id, gateway_user_id, payer_identity_id,"
                "  payer_identity_type, payer_derivation_version, amount, payable_amount,"
                "  status, redirect_url, callback_token_hash, callback_token_issued_at)"
                " VALUES (:bo, :go, :gu, :pid, :pt, 1, 10000, 10000,"
                "  'link_created', :ru, :th, :ti)"
            ),
            {
                "bo": "v1-tg-live",
                "go": _V1_GOID,
                "gu": _V1_HMAC_UID,
                "pid": mapping_id,
                "pt": IDENTITY_TYPE_TELEGRAM_USER,
                "ru": "https://gateway.test/pay/v1-tg-live",
                "th": callback_token_hash(token),
                "ti": datetime.now(UTC),
            },
        )
    return token


def test_production_0008_then_upgrade_head_runs_0009_and_app_survives(
    settings, pg_engine
):
    """From the exact production-0008 state, `alembic upgrade head` applies
    0009 with no data loss; the new app sends raw ids for new payments while
    historical rows keep their stored HMAC snapshots and callbacks."""
    # --- reproduce the production schema ------------------------------------
    _alembic("upgrade", "0008")
    assert _alembic_version(pg_engine) == "0008"
    assert "identity_scheme" not in _column_names(pg_engine, "centralpay_payer_identities")

    token = _seed_production_0008_state(pg_engine)

    # --- the production upgrade step (pinned to the 0009 slice) -------------
    _alembic("upgrade", "0009")
    assert _alembic_version(pg_engine) == "0009"
    assert "identity_scheme" in _column_names(pg_engine, "centralpay_payer_identities")
    assert "ck_payer_identities_identity_scheme_valid" in _check_names(
        pg_engine, "centralpay_payer_identities"
    )

    with pg_engine.connect() as conn:
        scheme, uid = conn.execute(
            text(
                "SELECT identity_scheme, gateway_user_id"
                " FROM centralpay_payer_identities"
            )
        ).one()
        redirect = conn.execute(
            text("SELECT redirect_url FROM payments WHERE bot_order_id = 'v1-tg-live'")
        ).scalar_one()
    # Non-destructive backfill: the existing row is labeled historical and its
    # DERIVED id is preserved byte-for-byte (never replaced with the raw id).
    assert scheme == "historical_hmac_v1"
    assert uid == _V1_HMAC_UID
    assert redirect == "https://gateway.test/pay/v1-tg-live"

    # The CHECK admits only the three known schemes.
    with pg_engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                text("UPDATE centralpay_payer_identities SET identity_scheme = 'bogus'")
            )
        conn.rollback()

    # --- recovery-safety ----------------------------------------------------
    _alembic("stamp", "0008")
    _alembic("upgrade", "0009")  # re-upgrade over existing schema: clean no-op
    assert _alembic_version(pg_engine) == "0009"

    # Bring the schema to the full head for the application section (0010
    # adds the reconciliation columns the current ORM model selects).
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0010"

    # --- the NEW application against the migrated database ------------------
    session_factory = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    stub = CentralPayStub()
    app = build_app(settings, session_factory, stub)
    with TestClient(app, raise_server_exceptions=False) as client:
        # A fresh Telegram payment sends the EXACT raw id.
        fresh = client.post(
            "/api/custom-payment",
            json={
                "api_key": settings.inbound_api_key,
                "amount": 50000,
                "order_id": "order-123",
                "user_id": 123456789,
            },
        )
        assert fresh.status_code == 200
        assert stub.getlink_requests[-1]["userId"] == 123456789

        # A same-user retry of the historical order returns its live link
        # unchanged — the stored HMAC snapshot is never re-derived/re-pointed.
        calls_before = len(stub.getlink_requests)
        retry = client.post(
            "/api/custom-payment",
            json={
                "api_key": settings.inbound_api_key,
                "amount": 10000,
                "order_id": "v1-tg-live",
                "user_id": _V1_TG_ID,
            },
        )
        assert retry.status_code == 200
        assert retry.json() == {"url": "https://gateway.test/pay/v1-tg-live"}
        assert len(stub.getlink_requests) == calls_before

        # The historical payment's callback still verifies against its stored
        # HMAC snapshot (never the raw Telegram id).
        stub.verify_result = verify_ok_response(
            amount=10000, user_id=_V1_HMAC_UID, reference_id="REF-v1-tg-live"
        )
        sig = callback_signature(settings.callback_hmac_secret, _V1_GOID, token)
        callback = client.get(
            f"/api/centralpay/callback?orderId={_V1_GOID}&ct={token}&sig={sig}"
        )
        assert callback.status_code == 200
    app.state.centralpay.close()

    with pg_engine.connect() as conn:
        status, uid_after = conn.execute(
            text(
                "SELECT status, gateway_user_id FROM payments"
                " WHERE bot_order_id = 'v1-tg-live'"
            )
        ).one()
        fresh_scheme = conn.execute(
            text(
                "SELECT identity_scheme FROM centralpay_payer_identities"
                " WHERE gateway_user_id = 123456789"
            )
        ).scalar_one()
    assert status == "bot_notify_pending"  # callback verified + queued
    assert uid_after == _V1_HMAC_UID  # snapshot untouched
    assert fresh_scheme == "telegram_raw_v1"


def test_upgrade_head_is_idempotent_on_partially_applied_0009(pg_engine):
    """A database that already carries the 0009 column (e.g. built from this
    branch before the pointer moved) upgrades cleanly — 0009's guards make it
    a no-op instead of an 'already exists' error."""
    _alembic("upgrade", "0008")
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE centralpay_payer_identities ADD COLUMN identity_scheme"
                " VARCHAR(32) NOT NULL DEFAULT 'historical_hmac_v1'"
            )
        )
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0010"
    assert "ck_payer_identities_identity_scheme_valid" in _check_names(
        pg_engine, "centralpay_payer_identities"
    )


def test_0009_downgrade_is_non_destructive_by_default(pg_engine):
    """`alembic downgrade 0008` moves only the pointer: the scheme column and
    CHECK survive (dropping is an explicit CENTRALPAY_DROP_PAYER_IDENTITY=1
    opt-in), and the schema upgrades forward again cleanly."""
    _alembic("upgrade", "head")
    _alembic("downgrade", "0008")  # 0010 + 0009 downgrades are pointer-only
    assert _alembic_version(pg_engine) == "0008"
    assert "identity_scheme" in _column_names(pg_engine, "centralpay_payer_identities")
    assert "ck_payer_identities_identity_scheme_valid" in _check_names(
        pg_engine, "centralpay_payer_identities"
    )
    _alembic("upgrade", "head")
    assert _alembic_version(pg_engine) == "0010"
