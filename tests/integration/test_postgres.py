"""PostgreSQL integration tests: migration, full flow, and row-lock concurrency.

These run only when TEST_DATABASE_URL points at a disposable PostgreSQL
database:

    export TEST_DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/centralpay_test'
    pytest -m postgres
"""

import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, PaymentStatus
from tests.conftest import (
    CentralPayStub,
    build_app,
    callback_path,
    create_order,
    event_types,
    get_events,
    get_payment,
    verify_ok_response,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]


def _drop_all(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS payment_events CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS payments CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    _drop_all(engine)
    yield engine
    engine.dispose()


def test_alembic_upgrade_on_empty_database(pg_engine):
    """The quality gate: the migration must bring up an empty PostgreSQL database."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}"

    inspector = inspect(pg_engine)
    tables = set(inspector.get_table_names())
    assert {"payments", "payment_events", "alembic_version"} <= tables

    payment_indexes = {index["name"] for index in inspector.get_indexes("payments")}
    assert "ix_payments_bot_order_id" in payment_indexes
    assert "ix_payments_gateway_order_id" in payment_indexes

    # JSONB on PostgreSQL, and a second upgrade run is a no-op.
    columns = {c["name"]: c for c in inspector.get_columns("payment_events")}
    assert str(columns["data"]["type"]).upper() == "JSONB"
    rerun = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert rerun.returncode == 0


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


def test_full_payment_flow_on_postgres(settings, pg_app, pg_session_factory):
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-flow", amount=15000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-flow")
        stub.verify_result = verify_ok_response(amount=15000)
        response = client.get(callback_path(settings, payment.gateway_order_id))
        assert response.status_code == 200
        assert response.json()["status"] == "verified"

    payment = get_payment(pg_session_factory, "pg-flow")
    assert payment.status == PaymentStatus.GATEWAY_VERIFIED.value
    assert event_types(get_events(pg_session_factory, payment.id)) == [
        "payment_created",
        "payment_link_created",
        "callback_received",
        "gateway_payment_verified",
    ]


def test_concurrent_callbacks_verify_exactly_once(settings, pg_app, pg_session_factory):
    """Row locking must serialize concurrent callbacks: verify runs once."""
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        assert create_order(client, settings, order_id="pg-race", amount=20000).status_code == 200
        payment = get_payment(pg_session_factory, "pg-race")
        stub.verify_result = verify_ok_response(amount=20000)
        stub.verify_delay_seconds = 0.5

        path = callback_path(settings, payment.gateway_order_id)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.get, path) for _ in range(2)]
            responses = [future.result(timeout=30) for future in futures]

    statuses = sorted(response.json()["status"] for response in responses)
    assert statuses == ["already_verified", "verified"]
    # The gateway verify endpoint was hit exactly once.
    assert len(stub.verify_requests) == 1

    payment = get_payment(pg_session_factory, "pg-race")
    assert payment.status == PaymentStatus.GATEWAY_VERIFIED.value
    events = event_types(get_events(pg_session_factory, payment.id))
    assert events.count("gateway_payment_verified") == 1


def test_concurrent_creates_return_one_link(settings, pg_app, pg_session_factory):
    """Concurrent duplicate creates must serialize to a single payment link."""
    stub = pg_app.state.centralpay_stub
    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [
            pool.submit(create_order, client, settings, order_id="pg-create-race")
            for _ in range(2)
        ]
        responses = [future.result(timeout=30) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    urls = {response.json()["url"] for response in responses}
    assert len(urls) == 1
    assert len(stub.getlink_requests) == 1
