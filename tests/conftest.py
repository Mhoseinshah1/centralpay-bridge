"""Shared fixtures.

Unit tests run against in-memory SQLite; CentralPay is faked at the HTTP
transport layer (httpx.MockTransport) so the real client code — request
building, response parsing, error mapping — is always exercised.
"""

import json
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.centralpay import CentralPayClient
from app.config import Settings
from app.main import create_app
from app.models import Base, Payment, PaymentEvent
from app.security import callback_signature

TEST_INBOUND_API_KEY = "test-inbound-api-key-cf1fd2f7e2a94"
TEST_CALLBACK_HMAC_SECRET = "test-callback-hmac-secret-8d11a52b"
TEST_GETLINK_API_KEY = "test-getlink-api-key-55e0b2b7"
TEST_VERIFY_API_KEY = "test-verify-api-key-9c23aa41"
TEST_DB_PASSWORD = "test-db-password-77aa88bb"
TEST_USER_ID = 4242

DEFAULT_REDIRECT_URL = "https://gateway.test/pay/tok123"


def getlink_ok_response(redirect_url: str = DEFAULT_REDIRECT_URL) -> httpx.Response:
    return httpx.Response(
        200, json={"status": "success", "data": {"redirectUrl": redirect_url}}
    )


def verify_ok_response(
    *,
    amount: int,
    user_id: int = TEST_USER_ID,
    reference_id: str | None = "REF-12345",
    card_number: str | None = "6037991234567890",
) -> httpx.Response:
    data: dict[str, object] = {"amount": amount, "userId": user_id}
    if reference_id is not None:
        data["referenceId"] = reference_id
    if card_number is not None:
        data["cardNumber"] = card_number
    return httpx.Response(200, json={"status": "success", "data": data})


class CentralPayStub:
    """Programmable fake CentralPay backend behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.getlink_requests: list[dict[str, object]] = []
        self.verify_requests: list[dict[str, object]] = []
        self.getlink_result: httpx.Response | Exception = getlink_ok_response()
        self.verify_result: httpx.Response | Exception = httpx.Response(
            200, json={"status": "error", "message": "verify result not configured"}
        )
        self.verify_delay_seconds = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        path = request.url.path
        if path.endswith("getLink.php"):
            with self.lock:
                self.getlink_requests.append(payload)
            result = self.getlink_result
        elif path.endswith("verify.php"):
            with self.lock:
                self.verify_requests.append(payload)
            if self.verify_delay_seconds:
                time.sleep(self.verify_delay_seconds)
            result = self.verify_result
        else:
            return httpx.Response(404)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_level="INFO",
        database_url=f"postgresql+psycopg://centralpay:{TEST_DB_PASSWORD}@db.test:5432/centralpay",
        public_base_url="https://pay.test.local",
        inbound_api_key=TEST_INBOUND_API_KEY,
        callback_hmac_secret=TEST_CALLBACK_HMAC_SECRET,
        centralpay_base_url="https://centralpay.test.local/basic",
        centralpay_getlink_api_key=TEST_GETLINK_API_KEY,
        centralpay_verify_api_key=TEST_VERIFY_API_KEY,
        centralpay_user_id=TEST_USER_ID,
        centralpay_timeout_seconds=5.0,
    )


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def stub() -> CentralPayStub:
    return CentralPayStub()


def build_app(
    settings: Settings, session_factory: sessionmaker[Session], stub: CentralPayStub
) -> FastAPI:
    application = create_app(settings)
    application.state.centralpay.close()
    application.state.session_factory = session_factory
    application.state.centralpay = CentralPayClient(
        base_url=settings.centralpay_base_url,
        getlink_api_key=settings.centralpay_getlink_api_key,
        verify_api_key=settings.centralpay_verify_api_key,
        timeout_seconds=settings.centralpay_timeout_seconds,
        transport=httpx.MockTransport(stub.handler),
    )
    return application


@pytest.fixture
def app(settings, session_factory, stub) -> Iterator[FastAPI]:
    application = build_app(settings, session_factory, stub)
    yield application
    application.state.centralpay.close()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# --- helpers used across test modules ---


def create_order(
    client: TestClient,
    settings: Settings,
    *,
    order_id: str = "order-abc-1",
    amount: int = 10000,
    api_key: str | None = None,
) -> httpx.Response:
    return client.post(
        "/api/custom-payment",
        json={
            "api_key": api_key if api_key is not None else settings.inbound_api_key,
            "amount": amount,
            "order_id": order_id,
        },
    )


def callback_path(settings: Settings, gateway_order_id: int, sig: str | None = None) -> str:
    signature = (
        sig
        if sig is not None
        else callback_signature(settings.callback_hmac_secret, gateway_order_id)
    )
    return f"/api/centralpay/callback?orderId={gateway_order_id}&sig={signature}"


def get_payment(session_factory: sessionmaker[Session], bot_order_id: str) -> Payment:
    with session_factory() as session:
        return session.execute(
            select(Payment).where(Payment.bot_order_id == bot_order_id)
        ).scalar_one()


def get_events(
    session_factory: sessionmaker[Session], payment_id: int | None = None
) -> list[PaymentEvent]:
    with session_factory() as session:
        query = select(PaymentEvent).order_by(PaymentEvent.id)
        if payment_id is not None:
            query = query.where(PaymentEvent.payment_id == payment_id)
        return list(session.execute(query).scalars())


def event_types(events: list[PaymentEvent]) -> list[str]:
    return [event.event_type for event in events]
