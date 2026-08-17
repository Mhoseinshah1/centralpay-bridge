"""PostgreSQL integration coverage for `centralpay reconcile` (scenario 14).

Only real PostgreSQL proves the read-only lookup and reference_id-collision
SELECT queries behave correctly under the database's own types/constraints
(BigInteger order ids, unique reference_id, CHECK constraints) without ever
provoking a write or a DataError -- SQLite is not evidence here (see
tests/integration/test_reference_id_pg.py's module docstring for the same
rationale). Also proves `reconcile` never mutates a row on real PostgreSQL,
matching the SQLite-backed proof in tests/test_cli_reconcile.py.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.centralpay import CentralPayClient
from app.cli import main as cli_main
from app.models import Base, Payment, PaymentStatus
from tests.conftest import CentralPayStub, build_app, create_order, get_payment, verify_ok_response

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

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


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    Base.metadata.create_all(pg_engine)
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def pg_app(settings, pg_session_factory):
    stub = CentralPayStub()
    application = build_app(settings, pg_session_factory, stub)
    application.state.centralpay_stub = stub
    yield application
    application.state.centralpay.close()


@pytest.fixture
def cli_env(settings, pg_session_factory, monkeypatch):
    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "Settings", lambda: settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: pg_session_factory)
    return settings


def _patch_centralpay_client(monkeypatch, stub) -> None:
    import app.cli as cli_module

    def factory(*, base_url, getlink_api_key, verify_api_key, timeout_seconds):
        return CentralPayClient(
            base_url=base_url,
            getlink_api_key=getlink_api_key,
            verify_api_key=verify_api_key,
            timeout_seconds=timeout_seconds,
            transport=httpx.MockTransport(stub.handler),
        )

    monkeypatch.setattr(cli_module, "CentralPayClient", factory)


def test_reconcile_lookup_by_bot_order_id_and_numeric_gateway_order_id_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, capsys
):
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(client, settings, order_id="pg-reconcile-lookup", amount=5000)
        assert response.status_code == 200
    payment = get_payment(pg_session_factory, "pg-reconcile-lookup")

    assert cli_main(["reconcile", "pg-reconcile-lookup"]) == 0
    out_by_bot_order = capsys.readouterr().out
    assert "pg-reconcile-lookup" in out_by_bot_order
    assert "link_created" in out_by_bot_order

    assert cli_main(["reconcile", str(payment.gateway_order_id)]) == 0
    out_by_gateway_order = capsys.readouterr().out
    assert "pg-reconcile-lookup" in out_by_gateway_order


def test_reconcile_verify_reference_id_collision_read_only_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with pg_session_factory() as db:
        holder = Payment(
            bot_order_id="pg-collision-holder",
            gateway_order_id=850002,
            gateway_user_id=1,
            amount=5000,
            payable_amount=5000,
            status=PaymentStatus.BOT_NOTIFY_PENDING.value,
            gateway_verified_at=datetime.now(UTC),
            reference_id="REF-PG-COLLIDE",
        )
        db.add(holder)
        db.commit()

    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(
            client, settings, order_id="pg-collision-candidate", amount=6000
        )
        assert response.status_code == 200
    candidate = get_payment(pg_session_factory, "pg-collision-candidate")

    stub.verify_result = verify_ok_response(
        amount=6000, user_id=candidate.gateway_user_id, reference_id="REF-PG-COLLIDE"
    )

    assert cli_main(["reconcile", "pg-collision-candidate", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "reference_id_collision" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    refetched_candidate = get_payment(pg_session_factory, "pg-collision-candidate")
    refetched_holder = get_payment(pg_session_factory, "pg-collision-holder")
    assert refetched_candidate.status == PaymentStatus.LINK_CREATED.value
    assert refetched_candidate.reference_id is None
    assert refetched_candidate.gateway_verified_at is None
    assert refetched_holder.reference_id == "REF-PG-COLLIDE"  # untouched by the collision check
    assert refetched_holder.status == PaymentStatus.BOT_NOTIFY_PENDING.value


def test_reconcile_default_and_verify_zero_mutation_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = create_order(client, settings, order_id="pg-zero-write", amount=4000)
        assert response.status_code == 200
    payment = get_payment(pg_session_factory, "pg-zero-write")
    before_updated_at = payment.updated_at

    assert cli_main(["reconcile", "pg-zero-write"]) == 0
    capsys.readouterr()

    stub.verify_result = verify_ok_response(
        amount=4000, user_id=payment.gateway_user_id, reference_id="REF-pg-zero-write"
    )
    assert cli_main(["reconcile", "pg-zero-write", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_VERIFY" in out

    refetched = get_payment(pg_session_factory, "pg-zero-write")
    assert refetched.updated_at == before_updated_at
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None
    assert refetched.reference_id is None
    assert len(stub.verify_requests) == 1


def test_reconcile_aged_out_refused_without_confirm_on_postgres(
    cli_env, settings, pg_app, pg_session_factory, monkeypatch, capsys
):
    stub = pg_app.state.centralpay_stub
    _patch_centralpay_client(monkeypatch, stub)

    with pg_session_factory() as db:
        db.add(
            Payment(
                bot_order_id="pg-aged-out",
                gateway_order_id=850005,
                gateway_user_id=1,
                amount=3000,
                payable_amount=3000,
                status=PaymentStatus.LINK_CREATED.value,
                callback_token_issued_at=datetime.now(UTC)
                - timedelta(seconds=settings.reconciliation_max_age_seconds + 120),
            )
        )
        db.commit()

    assert cli_main(["reconcile", "pg-aged-out", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "aged out" in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert len(stub.verify_requests) == 0
