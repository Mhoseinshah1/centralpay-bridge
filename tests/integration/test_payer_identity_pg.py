"""Per-customer payer identity under real PostgreSQL concurrency (incident
2026-07). Proves the isolation invariants hold under genuine row locking and
unique constraints, not just SQLite.

Requires TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
"""

import concurrent.futures
import os
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, CentralPayPayerIdentity
from app.services.payer_identity import resolve_payer_identity
from tests.conftest import (
    TEST_PAYER_ID_SECRET,
    CentralPayStub,
    build_app,
    create_order,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in (
            "admin_alerts",
            "worker_heartbeats",
            "payment_events",
            "payments",
            "centralpay_payer_identities",
            "fee_policies",
            "alembic_version",
        ):
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


def _identity_count(session_factory) -> int:
    with session_factory() as db:
        return db.execute(select(func.count(CentralPayPayerIdentity.id))).scalar_one()


def test_concurrent_same_customer_resolves_to_one_mapping(pg_session_factory):
    """Many concurrent first-purchases by ONE customer create exactly one
    mapping row and one gateway id."""
    barrier = threading.Barrier(6)

    def resolve() -> int:
        session = pg_session_factory()
        try:
            barrier.wait(timeout=30)
            return resolve_payer_identity(
                session, secret=TEST_PAYER_ID_SECRET, customer_id="racer"
            ).gateway_user_id
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ids = [f.result(timeout=30) for f in [pool.submit(resolve) for _ in range(6)]]

    assert len(set(ids)) == 1  # every thread saw the same stable id
    assert _identity_count(pg_session_factory) == 1  # exactly one mapping row


def test_concurrent_different_customers_never_share_an_id(pg_session_factory):
    customers = [f"cust-{i}" for i in range(8)]
    barrier = threading.Barrier(len(customers))

    def resolve(customer: str) -> int:
        session = pg_session_factory()
        try:
            barrier.wait(timeout=30)
            return resolve_payer_identity(
                session, secret=TEST_PAYER_ID_SECRET, customer_id=customer
            ).gateway_user_id
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(customers)) as pool:
        ids = [f.result(timeout=30) for f in [pool.submit(resolve, c) for c in customers]]

    assert len(set(ids)) == len(customers)  # all distinct: no shared identity
    assert _identity_count(pg_session_factory) == len(customers)


def test_mapping_is_stable_across_reconnect(pg_session_factory):
    """Restart/redeploy/backup-restore stability: the stored mapping does not
    change when the process reconnects."""
    with pg_session_factory() as db:
        first = resolve_payer_identity(
            db, secret=TEST_PAYER_ID_SECRET, customer_id="persist"
        ).gateway_user_id
    # A fresh session factory (as a new process would use) sees the same id.
    fresh = sessionmaker(bind=pg_session_factory.kw["bind"], expire_on_commit=False)
    with fresh() as db:
        again = resolve_payer_identity(
            db, secret=TEST_PAYER_ID_SECRET, customer_id="persist"
        ).gateway_user_id
    assert first == again
    assert _identity_count(pg_session_factory) == 1


def test_concurrent_create_orders_two_customers_are_isolated(settings, pg_app, pg_session_factory):
    """End-to-end: two customers creating payments at the same time send two
    DIFFERENT gateway userIds (and never the legacy shared one)."""
    stub = pg_app.state.centralpay_stub
    with (
        TestClient(pg_app, raise_server_exceptions=False) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [
            pool.submit(
                create_order, client, settings, order_id=f"iso-{c}", customer_id=c
            )
            for c in ("alice", "bob")
        ]
        responses = [f.result(timeout=30) for f in futures]

    assert [r.status_code for r in responses] == [200, 200]
    users = {req["userId"] for req in stub.getlink_requests}
    assert len(users) == 2
    assert settings.centralpay_user_id not in users
