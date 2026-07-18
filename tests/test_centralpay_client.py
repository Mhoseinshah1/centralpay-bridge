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
    with pytest.raises(CentralPayRejectedError, match="gateway_response_invalid"):
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
        "gateway_invalid_reference_id",
        "gateway_invalid_amount",
        "gateway_invalid_user_id",
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


# --- audit: redirect URL validation -----------------------------------------


@pytest.mark.parametrize(
    "bad_redirect",
    [
        "",  # empty
        "   ",  # whitespace only
        42,  # wrong type
        None,  # missing/null
        "javascript:alert(1)",  # dangerous scheme
        "ftp://gateway.test/pay",  # non-http scheme
        "http://gateway.test/pay",  # cleartext downgrade (HTTPS only)
        "https://",  # no hostname
        "https:///pay/x",  # empty hostname with path
        "https://user:pass@gateway.test/pay",  # credentials in URL
        "https://user@gateway.test/pay",  # username in URL
        "https://gateway.test/pay\nx",  # interior control character
        "https://gateway.test/pa y",  # interior whitespace
        "https://gateway.test\x00/pay",  # NUL byte
        "https://[bad-ipv6/pay",  # unparseable netloc
        "https://gateway.test:abc/pay",  # malformed port
        "https://gateway.test/" + "a" * 3000,  # over maximum length
    ],
)
def test_get_link_rejects_invalid_redirect_urls(bad_redirect):
    client = make_client(
        respond_with(
            httpx.Response(
                200, json={"status": "success", "data": {"redirectUrl": bad_redirect}}
            )
        )
    )
    with pytest.raises(CentralPayRejectedError, match="gateway_invalid_redirect_url"):
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


def test_get_link_non_object_json_body():
    client = make_client(respond_with(httpx.Response(200, json=["not", "an", "object"])))
    with pytest.raises(CentralPayInvalidResponseError):
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")


# --- audit: gateway-controlled text never reaches logs or exceptions --------

_SENTINEL = "GATEWAY-CONTROLLED-TEXT-8f3a1"


@pytest.mark.parametrize(
    "body",
    [
        {"status": "error", "message": _SENTINEL},
        {"error": _SENTINEL},
        {"success": False, "message": _SENTINEL, "description": _SENTINEL},
        {"status": "failed", "msg": _SENTINEL},
    ],
)
def test_get_link_never_exposes_gateway_text(body, caplog):
    client = make_client(respond_with(httpx.Response(200, json=body)))
    with caplog.at_level("DEBUG"), pytest.raises(CentralPayRejectedError) as excinfo:
        client.get_link(amount=1000, user_id=1, order_id=2, return_url="https://cb.test")
    assert _SENTINEL not in str(excinfo.value)
    assert excinfo.value.message == "getLink rejected: gateway_rejected"
    for record in caplog.records:
        assert _SENTINEL not in record.getMessage()
        assert _SENTINEL not in repr(record.__dict__)


@pytest.mark.parametrize(
    "body",
    [
        {"status": "error", "message": _SENTINEL},
        {"error": _SENTINEL},
        {"success": False, "message": _SENTINEL},
    ],
)
def test_verify_never_exposes_gateway_text(body, caplog):
    client = make_client(respond_with(httpx.Response(200, json=body)))
    with caplog.at_level("DEBUG"):
        result = client.verify(order_id=5)
    assert result.gateway_success is False
    assert result.failure_reason == "gateway_rejected"  # internal code only
    for record in caplog.records:
        assert _SENTINEL not in record.getMessage()
        assert _SENTINEL not in repr(record.__dict__)


def test_verify_unrecognized_response_uses_internal_code():
    client = make_client(
        respond_with(httpx.Response(200, json={"note": _SENTINEL, "data": {}}))
    )
    result = client.verify(order_id=5)
    assert result.gateway_success is False
    assert result.failure_reason == "gateway_response_invalid"


def test_verify_missing_data_uses_internal_code():
    client = make_client(respond_with(httpx.Response(200, json={"status": "success"})))
    result = client.verify(order_id=5)
    assert result.gateway_success is False
    assert result.failure_reason == "gateway_missing_data"


# --- audit: per-field strict parsing codes ----------------------------------


@pytest.mark.parametrize(
    ("data", "expected_errors"),
    [
        ({}, {"gateway_invalid_reference_id", "gateway_invalid_amount", "gateway_invalid_user_id"}),
        (
            {"referenceId": "", "amount": 10000, "userId": 42},
            {"gateway_invalid_reference_id"},
        ),
        (
            {"referenceId": "R1", "amount": 12.5, "userId": 42},
            {"gateway_invalid_amount"},
        ),
        (
            {"referenceId": "R1", "amount": True, "userId": 42},
            {"gateway_invalid_amount"},
        ),
        (
            {"referenceId": "R1", "amount": 10000, "userId": [42]},
            {"gateway_invalid_user_id"},
        ),
        (
            {"referenceId": "R1", "amount": 10000, "userId": "not-a-number"},
            {"gateway_invalid_user_id"},
        ),
    ],
)
def test_verify_field_error_codes(data, expected_errors):
    client = make_client(
        respond_with(httpx.Response(200, json={"status": "success", "data": data}))
    )
    result = client.verify(order_id=5)
    assert result.gateway_success is True
    assert set(result.field_errors) == expected_errors
