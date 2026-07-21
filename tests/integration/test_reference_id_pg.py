"""Reference-ID storage boundary on real PostgreSQL.

The defect under test: before validation, a gateway-successful verify
response with an over-length or NUL-containing referenceId reached the
collision query / model assignment and made PostgreSQL raise a
DataError during flush or commit instead of routing the payment safely
to manual review. Only real PostgreSQL can prove that no statement
error escapes — SQLite is not evidence here.
"""

import logging
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.models import AdminAlert, Base, Payment, PaymentStatus
from tests.conftest import (
    DEFAULT_GATEWAY_USER_ID,
    CentralPayStub,
    build_app,
    create_order,
    event_types,
    get_events,
    get_payment,
    run_pass,
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
]

SENTINEL = "QJ4VN2PLM6"  # unique marker embedded in every invalid value


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in (
            "admin_alerts",
            "worker_heartbeats",
            "payment_events",
            "payments",
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


def _verify_success(reference_id: object, *, amount: int) -> httpx.Response:
    # create_order uses the default customer, so verify must report that
    # customer's derived gateway userId (not the legacy shared id).
    data: dict[str, object] = {"amount": amount, "userId": DEFAULT_GATEWAY_USER_ID}
    if reference_id is not None:
        data["referenceId"] = reference_id
    return httpx.Response(200, json={"status": "success", "data": data})


def _assert_safe_manual_review(
    pg_session_factory, order_id: str, response, caplog, notifier, bot_stub, settings
) -> None:
    assert response.status_code == 200  # never a DataError-driven 500
    assert 'data-status="under_review"' in response.text

    payment = get_payment(pg_session_factory, order_id)
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.reference_id is None
    assert payment.gateway_verified_at is None
    assert payment.card_last4 is None
    # Financial fields untouched.
    assert payment.amount == 10_000
    assert payment.payable_amount == 10_000
    assert payment.fee_rate_bps == 0
    assert payment.fee_amount == 0
    assert payment.next_retry_at is None
    assert payment.notification_claimed_at is None

    events = get_events(pg_session_factory, payment.id)
    types = event_types(events)
    assert "verify_invalid_reference_id" in types
    assert "manual_review_required" in types
    assert "gateway_payment_verified" not in types
    assert "bot_notification_queued" not in types

    # The raw value leaks nowhere.
    assert all(SENTINEL not in repr(event.data) for event in events)
    assert SENTINEL not in (payment.last_error or "")
    assert SENTINEL not in response.text
    assert SENTINEL not in caplog.text
    with pg_session_factory() as db:
        for alert in db.execute(select(AdminAlert)).scalars():
            assert SENTINEL not in repr(alert.payload)

    # The worker delivers nothing for a manual-review payment.
    result = run_pass(pg_session_factory, notifier, settings)
    assert result["processed"] == 0
    assert bot_stub.requests == []


def test_oversized_reference_id_routes_to_manual_review_without_db_error(
    settings, pg_app, pg_session_factory, notifier, bot_stub, caplog
):
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        created = create_order(client, settings, order_id="pg-ref-129", amount=10_000)
        assert created.status_code == 200
        payment = get_payment(pg_session_factory, "pg-ref-129")
        oversized = ("REF-" + SENTINEL + "x" * 129)[:129]  # exactly 129 chars
        assert len(oversized) == 129
        stub.verify_result = _verify_success(oversized, amount=10_000)

        with caplog.at_level(logging.DEBUG):
            response = client.get(valid_callback_path(stub, payment.gateway_order_id))

        _assert_safe_manual_review(
            pg_session_factory, "pg-ref-129", response, caplog, notifier, bot_stub, settings
        )

        # Replay: manual_review is terminal — verify is never called again.
        replay = client.get(valid_callback_path(stub, payment.gateway_order_id))
        assert replay.status_code == 200
        assert 'data-status="under_review"' in replay.text
    assert len(stub.verify_requests) == 1


def test_nul_reference_id_routes_to_manual_review_without_db_error(
    settings, pg_app, pg_session_factory, notifier, bot_stub, caplog
):
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        created = create_order(client, settings, order_id="pg-ref-nul", amount=10_000)
        assert created.status_code == 200
        payment = get_payment(pg_session_factory, "pg-ref-nul")
        stub.verify_result = _verify_success(f"REF-{SENTINEL}\x00TAIL", amount=10_000)

        with caplog.at_level(logging.DEBUG):
            response = client.get(valid_callback_path(stub, payment.gateway_order_id))

        _assert_safe_manual_review(
            pg_session_factory, "pg-ref-nul", response, caplog, notifier, bot_stub, settings
        )
    assert len(stub.verify_requests) == 1


def test_exactly_128_char_reference_id_verifies_and_collides_correctly(
    settings, pg_app, pg_session_factory, notifier, bot_stub
):
    """The boundary value works end to end: stored exactly, collision
    protection intact, exactly one notification queued and delivered."""
    stub = pg_app.state.centralpay_stub
    boundary_ref = "B" * 128
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        created = create_order(client, settings, order_id="pg-ref-128", amount=10_000)
        assert created.status_code == 200
        payment = get_payment(pg_session_factory, "pg-ref-128")
        stub.verify_result = _verify_success(boundary_ref, amount=10_000)
        assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

        payment = get_payment(pg_session_factory, "pg-ref-128")
        assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
        assert payment.reference_id == boundary_ref  # stored exactly
        assert payment.gateway_verified_at is not None
        types = event_types(get_events(pg_session_factory, payment.id))
        assert types.count("bot_notification_queued") == 1

        # Collision protection still works: a second payment reporting the
        # SAME 128-char referenceId goes to manual review, never overwrites.
        second_created = create_order(client, settings, order_id="pg-ref-128b", amount=10_000)
        assert second_created.status_code == 200
        second = get_payment(pg_session_factory, "pg-ref-128b")
        stub.verify_result = _verify_success(boundary_ref, amount=10_000)
        assert client.get(valid_callback_path(stub, second.gateway_order_id)).status_code == 200
        second = get_payment(pg_session_factory, "pg-ref-128b")
        assert second.status == PaymentStatus.MANUAL_REVIEW.value
        assert second.reference_id is None
        assert "reference_id_collision" in event_types(
            get_events(pg_session_factory, second.id)
        )
        # The first payment is untouched by the collision.
        first = get_payment(pg_session_factory, "pg-ref-128")
        assert first.reference_id == boundary_ref

    # Exactly one delivery for the one verified payment.
    result = run_pass(pg_session_factory, notifier, settings)
    assert result["processed"] == 1
    assert len(bot_stub.requests) == 1
    assert bot_stub.requests[0]["order_id"] == "pg-ref-128"


def test_valid_reference_id_regression_on_postgres(
    settings, pg_app, pg_session_factory
):
    """Unchanged normal behavior: an ordinary referenceId verifies as before."""
    stub = pg_app.state.centralpay_stub
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        created = create_order(client, settings, order_id="pg-ref-ok", amount=15_000)
        assert created.status_code == 200
        payment = get_payment(pg_session_factory, "pg-ref-ok")
        stub.verify_result = verify_ok_response(amount=15_000, reference_id="REF-normal-1")
        assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    payment = get_payment(pg_session_factory, "pg-ref-ok")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert payment.reference_id == "REF-normal-1"


def test_reference_id_length_survives_migrated_schema(pg_engine, pg_session_factory):
    """Drift guard at the database itself: the live column is VARCHAR(128),
    equal to the parser bound."""
    from app.models import CENTRALPAY_REFERENCE_ID_MAX_LENGTH

    with pg_engine.connect() as connection:
        length = connection.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns"
                " WHERE table_name = 'payments' AND column_name = 'reference_id'"
            )
        ).scalar_one()
    assert length == CENTRALPAY_REFERENCE_ID_MAX_LENGTH


def test_count_helper_sanity(pg_session_factory):
    """Fixture sanity: the fresh database starts empty."""
    with pg_session_factory() as db:
        assert db.execute(select(func.count(Payment.id))).scalar_one() == 0
