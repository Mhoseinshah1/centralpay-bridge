"""Unit tests for the CentralPay HTTP client: parsing and error mapping."""

import httpx
import pytest

from app.centralpay import CentralPayClient
from app.exceptions import (
    CentralPayConnectionError,
    CentralPayInvalidResponseError,
    CentralPayRejectedError,
)


def make_client(handler) -> CentralPayClient:
    return CentralPayClient(
        base_url="https://centralpay.test.local/basic",
        getlink_api_key="unit-getlink-key",
        verify_api_key="unit-verify-key",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


def respond_with(response: httpx.Response | Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    return handler


def test_get_link_success():
    client = make_client(
        respond_with(
            httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"redirectUrl": "https://gateway.test/pay/x"},
                },
            )
        )
    )
    url = client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")
    assert url == "https://gateway.test/pay/x"


def test_get_link_without_explicit_success_marker_rejected():
    """Success is never inferred from the presence of data alone."""
    client = make_client(
        respond_with(
            httpx.Response(200, json={"data": {"redirectUrl": "https://gateway.test/pay/x"}})
        )
    )
    with pytest.raises(CentralPayRejectedError, match="getlink_response_unrecognized"):
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


def test_get_link_invalid_redirect_url_rejected():
    for bad_redirect in ("", "javascript:alert(1)", 42, None):
        client = make_client(
            respond_with(
                httpx.Response(
                    200, json={"status": "success", "data": {"redirectUrl": bad_redirect}}
                )
            )
        )
        with pytest.raises(CentralPayRejectedError):
            client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


def test_verify_success_with_mistyped_fields_reports_field_errors():
    """Gateway says success but fields are malformed: parse strictly, flag
    explicit reason codes, and let the service route to manual review."""
    client = make_client(
        respond_with(
            httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"referenceId": "", "amount": 12.5, "userId": None},
                },
            )
        )
    )
    result = client.verify(order_id=5)
    assert result.gateway_success is True
    assert result.reference_id is None
    assert result.amount is None
    assert result.user_id is None
    assert set(result.field_errors) == {
        "verify_empty_reference_id",
        "verify_invalid_amount",
        "verify_invalid_user_id",
    }


def test_get_link_http_error_status():
    client = make_client(respond_with(httpx.Response(500, text="oops")))
    with pytest.raises(CentralPayRejectedError):
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


def test_get_link_non_json_body():
    client = make_client(respond_with(httpx.Response(200, text="<html>not json</html>")))
    with pytest.raises(CentralPayInvalidResponseError):
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


def test_get_link_missing_redirect_url():
    client = make_client(respond_with(httpx.Response(200, json={"status": "success", "data": {}})))
    with pytest.raises(CentralPayRejectedError):
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


def test_get_link_timeout():
    client = make_client(respond_with(httpx.ReadTimeout("timed out")))
    with pytest.raises(CentralPayConnectionError):
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


def test_verify_success_extracts_fields():
    client = make_client(
        respond_with(
            httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "referenceId": 998877,
                        "amount": "10000",
                        "userId": "42",
                        "cardNumber": "6037991234567890",
                    },
                },
            )
        )
    )
    result = client.verify(order_id=5)
    assert result.gateway_success is True
    assert result.reference_id == "998877"
    # Numeric strings from the gateway are coerced to integers.
    assert result.amount == 10000
    assert result.user_id == 42
    assert result.card_number == "6037991234567890"


@pytest.mark.parametrize(
    "body",
    [
        {"success": False, "data": {"referenceId": "R"}},
        {"status": "error", "message": "not paid"},
        {"status": 0, "data": {}},
        {"status": "failed"},
        {"error": "order not found"},
        {"status": "success"},  # no data object
    ],
)
def test_verify_not_successful(body):
    client = make_client(respond_with(httpx.Response(200, json=body)))
    result = client.verify(order_id=5)
    assert result.gateway_success is False
    assert result.failure_reason


def test_verify_connection_error():
    client = make_client(respond_with(httpx.ConnectError("refused")))
    with pytest.raises(CentralPayConnectionError):
        client.verify(order_id=5)


def test_verify_non_object_json():
    client = make_client(respond_with(httpx.Response(200, json=["not", "object"])))
    with pytest.raises(CentralPayInvalidResponseError):
        client.verify(order_id=5)
