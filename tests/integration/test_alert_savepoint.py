"""Admin-alert SAVEPOINT isolation on real PostgreSQL.

The alert outbox writes into the caller's financial transaction. These
tests prove the guarantee added by fix/admin-alert-savepoint-isolation:
alert creation runs inside a SAVEPOINT, so a genuine PostgreSQL failure
while creating an alert aborts ONLY the savepoint — the payment state
transition and its PaymentEvent still commit — while a SUCCESSFUL alert
remains fully atomic with the outer transaction.

SQLite is deliberately not used as evidence here: only PostgreSQL's
aborted-transaction semantics can prove the recovery actually works.
"""

import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.adminbot import alerts as alerts_module
from app.adminbot.alerts import configure_alert_creation, reset_alert_creation
from app.audit import record_event
from app.models import AdminAlert, Base, Payment, PaymentEvent, PaymentStatus
from tests.conftest import (
    TEST_ADMIN_BOT_TOKEN,
    TEST_ADMIN_ID,
    CentralPayStub,
    build_app,
    create_order,
    event_types,
    get_events,
    get_payment,
    valid_callback_path,
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
def alert_env(settings, pg_session_factory):
    """Alert-row creation enabled against the PostgreSQL database."""
    admin_settings = settings.model_copy(
        update={
            "admin_bot_enabled": True,
            "admin_bot_token": TEST_ADMIN_BOT_TOKEN,
            "admin_telegram_ids": str(TEST_ADMIN_ID),
        }
    )
    configure_alert_creation(admin_settings)
    yield admin_settings
    reset_alert_creation()


def _new_payment(db, order_id: str, amount: int = 10_000) -> Payment:
    payment = Payment(
        bot_order_id=order_id,
        gateway_order_id=hash(order_id) % 10**11 + 10**11,
        gateway_user_id=1,
        amount=amount,
        payable_amount=amount,
        status=PaymentStatus.LINK_CREATED.value,
    )
    db.add(payment)
    db.flush()
    return payment


def _counts(session_factory, order_id: str):
    with session_factory() as db:
        payments = db.execute(
            select(func.count(Payment.id)).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        alerts = db.execute(select(func.count(AdminAlert.id))).scalar_one()
        events = db.execute(select(PaymentEvent.event_type)).scalars().all()
    return payments, alerts, events


def _fail_alert_inserts(monkeypatch):
    """Force a GENUINE PostgreSQL failure inside the real create_alert path.

    The oversized alert_type exceeds admin_alerts.alert_type VARCHAR(64),
    so the real dedup-select + INSERT + flush sequence runs and PostgreSQL
    itself rejects the statement, marking the (sub)transaction aborted —
    exactly the class of failure that used to poison the outer financial
    transaction. This is not a simulated `raise`.
    """
    real_create_alert = alerts_module.create_alert

    def oversized(db, *, alert_type, **kwargs):
        return real_create_alert(db, alert_type=alert_type + "x" * 100, **kwargs)

    monkeypatch.setattr(alerts_module, "create_alert", oversized)


def test_successful_alert_commits_atomically_with_payment(
    alert_env, pg_session_factory
):
    with pg_session_factory() as db:
        payment = _new_payment(db, "sp-ok")
        # reference_id_collision is a financial-integrity event: always
        # mapped to a critical alert regardless of optional toggles.
        record_event(
            db,
            payment_id=payment.id,
            event_type="reference_id_collision",
            level="error",
            data={"gateway_order_id": payment.gateway_order_id},
        )
        db.commit()

    payments, alerts, events = _counts(pg_session_factory, "sp-ok")
    assert payments == 1
    assert alerts == 1  # exactly one alert — recursion produced no duplicate
    assert events.count("reference_id_collision") == 1
    assert events.count("admin_alert_created") == 1  # exactly one


def test_outer_rollback_discards_successful_alert(alert_env, pg_session_factory):
    with pg_session_factory() as db:
        payment = _new_payment(db, "sp-rb")
        record_event(
            db,
            payment_id=payment.id,
            event_type="reference_id_collision",
            level="error",
            data={},
        )
        # The alert was created (savepoint released into the outer txn)…
        assert db.execute(select(func.count(AdminAlert.id))).scalar_one() == 1
        # …but the OUTER transaction rolls back: everything goes together.
        db.rollback()

    payments, alerts, events = _counts(pg_session_factory, "sp-rb")
    assert payments == 0
    assert alerts == 0
    assert events == []


def test_pg_failure_in_alert_creation_leaves_outer_transaction_usable(
    alert_env, pg_session_factory, monkeypatch, caplog
):
    _fail_alert_inserts(monkeypatch)

    with pg_session_factory() as db:
        payment = _new_payment(db, "sp-fail")
        rollback_calls: list[int] = []
        real_rollback = db.rollback

        def spying_rollback() -> None:
            rollback_calls.append(1)
            real_rollback()

        monkeypatch.setattr(db, "rollback", spying_rollback)

        with caplog.at_level(logging.ERROR, logger="app.adminbot.alerts"):
            record_event(
                db,
                payment_id=payment.id,
                event_type="reference_id_collision",
                level="error",
                data={"reference_id": "REF-sentinel-9f8e7d"},
            )

        # Regression: the implementation never issued a FULL session
        # rollback that would discard the caller's financial changes.
        assert rollback_calls == []

        # The session is still usable on the SAME transaction: a further
        # query succeeds even though PostgreSQL aborted the savepoint.
        assert (
            db.execute(
                select(func.count(Payment.id)).where(Payment.bot_order_id == "sp-fail")
            ).scalar_one()
            == 1
        )
        db.commit()  # and the outer financial transaction commits

    payments, alerts, events = _counts(pg_session_factory, "sp-fail")
    assert payments == 1  # financial row survived the alert failure
    assert events.count("reference_id_collision") == 1  # original event intact
    assert alerts == 0  # no partial AdminAlert row
    assert events.count("admin_alert_created") == 0  # no partial alert event

    # Logging safety: one fixed internal event name with safe metadata only.
    failure_logs = [
        r for r in caplog.records if r.getMessage() == "admin_alert_creation_failed"
    ]
    assert len(failure_logs) == 1
    assert failure_logs[0].error_class  # exception class name, e.g. DataError
    everything_logged = " ".join(
        f"{r.getMessage()} {getattr(r, 'event_type', '')} {getattr(r, 'error_class', '')}"
        for r in caplog.records
    )
    assert "REF-sentinel-9f8e7d" not in everything_logged  # raw payload
    assert "[SQL" not in everything_logged  # statement text
    assert "parameters" not in everything_logged  # bound parameter values


def test_alert_failure_never_changes_verification_outcome(
    settings, alert_env, pg_session_factory, monkeypatch
):
    """Real service path: a payable-amount mismatch generates a critical
    alert. With alert creation failing at the PostgreSQL level, the
    callback still completes, manual review is still recorded, and every
    financial field is untouched. Before the savepoint fix this request
    failed outright: the aborted transaction could not commit."""
    _fail_alert_inserts(monkeypatch)

    stub = CentralPayStub()
    application = build_app(settings, pg_session_factory, stub)
    # build_app disables alert creation for plain test settings; re-enable.
    configure_alert_creation(alert_env)
    # create_app's configure_logging replaces the root handlers (detaching
    # pytest's caplog), so capture with a handler attached directly.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    alerts_logger = logging.getLogger("app.adminbot.alerts")
    alerts_logger.addHandler(handler)
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            created = create_order(client, settings, order_id="sp-verify", amount=10_000)
            assert created.status_code == 200
            payment = get_payment(pg_session_factory, "sp-verify")
            stub.verify_result = verify_ok_response(amount=9_999)  # mismatch
            response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    finally:
        alerts_logger.removeHandler(handler)
        application.state.centralpay.close()

    # The service outcome is exactly the normal mismatch outcome.
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text
    payment = get_payment(pg_session_factory, "sp-verify")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    # Financial fields untouched by the alert failure.
    assert payment.amount == 10_000
    assert payment.payable_amount == 10_000
    assert payment.fee_rate_bps == 0
    assert payment.fee_amount == 0
    assert payment.reference_id is None
    assert payment.gateway_verified_at is None
    assert payment.notification_claimed_at is None
    assert payment.next_retry_at is None

    events = event_types(get_events(pg_session_factory, payment.id))
    assert "verify_payable_amount_mismatch" in events
    assert "manual_review_required" in events
    assert "admin_alert_created" not in events
    with pg_session_factory() as db:
        assert db.execute(select(func.count(AdminAlert.id))).scalar_one() == 0
    assert any(
        r.getMessage() == "admin_alert_creation_failed" for r in records
    )


def test_savepoint_isolation_is_repeatable_within_one_session(
    alert_env, pg_session_factory, monkeypatch
):
    """Two consecutive alert failures in the same session: each rolls back
    only its own savepoint; the session never accumulates aborted state."""
    _fail_alert_inserts(monkeypatch)

    with pg_session_factory() as db:
        first = _new_payment(db, "sp-twice-1")
        record_event(
            db, payment_id=first.id, event_type="reference_id_collision",
            level="error", data={},
        )
        second = _new_payment(db, "sp-twice-2")
        record_event(
            db, payment_id=second.id, event_type="reference_id_collision",
            level="error", data={},
        )
        db.commit()

    with pg_session_factory() as db:
        assert (
            db.execute(
                select(func.count(Payment.id)).where(
                    Payment.bot_order_id.like("sp-twice-%")
                )
            ).scalar_one()
            == 2
        )
        assert db.execute(select(func.count(AdminAlert.id))).scalar_one() == 0
