"""CentralPay referenceId storage-contract validation (unit level).

The verify parser must enforce the database storage boundary
(CENTRALPAY_REFERENCE_ID_MAX_LENGTH, no NUL/control characters) BEFORE the
value can reach a query, a model assignment, an audit event, or a log —
and a present-but-invalid value must be distinguished from a missing one.
"""

import logging

import httpx
import pytest
from sqlalchemy import String

from app.centralpay import GATEWAY_INVALID_REFERENCE_ID, _parse_reference_id
from app.models import CENTRALPAY_REFERENCE_ID_MAX_LENGTH, Payment
from tests.conftest import (
    create_order,
    event_types,
    get_events,
    get_payment,
    valid_callback_path,
)
from tests.test_centralpay_client import make_client, respond_with

SENTINEL = "ZX9QW8ERT7"  # unique marker embedded in every invalid value


def _verify_response(reference_id: object) -> httpx.Response:
    data: dict[str, object] = {"amount": 10_000, "userId": 1}
    if reference_id is not None:
        data["referenceId"] = reference_id
    return httpx.Response(200, json={"status": "success", "data": data})


# --- accepted values ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R", "R"),  # one character
        ("x" * 128, "x" * 128),  # exactly the storage limit
        ("REF-12345", "REF-12345"),  # normal format
        ("  REF-77  ", "REF-77"),  # existing strip() normalization
        (123456789, "123456789"),  # non-boolean integer -> decimal string
        (10**120, str(10**120)),  # huge int whose decimal form still fits
    ],
)
def test_parser_accepts_contract_conforming_reference_ids(raw, expected):
    client = make_client(respond_with(_verify_response(raw)))
    result = client.verify(order_id=5)
    assert result.reference_id == expected  # stored exactly, never transformed
    assert result.reference_id_invalid is False
    assert GATEWAY_INVALID_REFERENCE_ID not in result.field_errors


# --- rejected: present but INVALID -------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        SENTINEL + "x" * 119,  # 129 characters
        SENTINEL + "x" * 5000,  # very large value
        f"REF-{SENTINEL}\x00",  # NUL
        f"REF-{SENTINEL}\nB",  # embedded newline
        f"REF-{SENTINEL}\rB",  # embedded carriage return
        f"REF-{SENTINEL}\tB",  # embedded tab
        f"REF-{SENTINEL}\x1b[31m",  # other ASCII control (ESC)
        f"REF-{SENTINEL}\x7f",  # DEL
        True,  # boolean
        [SENTINEL],  # list
        {"v": SENTINEL},  # object
        1234.5,  # float
        10**130,  # integer whose decimal form exceeds the limit
    ],
)
def test_parser_rejects_present_but_invalid_reference_ids(raw, caplog):
    client = make_client(respond_with(_verify_response(raw)))
    with caplog.at_level(logging.DEBUG):
        result = client.verify(order_id=5)
    assert result.reference_id is None  # never usable
    assert result.reference_id_invalid is True  # distinct from missing
    assert GATEWAY_INVALID_REFERENCE_ID in result.field_errors
    # The raw value never leaves app/centralpay.py — not even into logs.
    assert SENTINEL not in caplog.text
    assert "\x00" not in caplog.text


# --- rejected: genuinely MISSING ---------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parser_treats_absent_and_empty_as_missing(raw):
    client = make_client(respond_with(_verify_response(raw)))
    result = client.verify(order_id=5)
    assert result.reference_id is None
    assert result.reference_id_invalid is False  # missing, NOT invalid
    assert GATEWAY_INVALID_REFERENCE_ID in result.field_errors


def test_parse_reference_id_never_truncates():
    """Over-length values are rejected outright — silent truncation would
    store a different identifier than the gateway reported."""
    value, invalid = _parse_reference_id("y" * 129)
    assert value is None and invalid is True
    value, invalid = _parse_reference_id("y" * 128)
    assert value == "y" * 128 and invalid is False


def test_reference_id_limit_matches_database_model():
    """Drift guard: the parser bound and the column length are one contract."""
    assert CENTRALPAY_REFERENCE_ID_MAX_LENGTH == 128
    column_type = Payment.__table__.c.reference_id.type
    assert isinstance(column_type, String)
    assert column_type.length == CENTRALPAY_REFERENCE_ID_MAX_LENGTH


# --- flow (logic level): invalid routes to its own precise event -------------


def test_invalid_reference_id_moves_to_manual_review_with_precise_event(
    client, settings, session_factory, stub, caplog
):
    assert create_order(client, settings, order_id="ref-inv", amount=10_000).status_code == 200
    payment = get_payment(session_factory, "ref-inv")
    stub.verify_result = _verify_response("REF-" + SENTINEL + "x" * 125)

    with caplog.at_level(logging.DEBUG):
        response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text

    payment = get_payment(session_factory, "ref-inv")
    assert payment.status == "manual_review"
    assert payment.reference_id is None
    assert payment.gateway_verified_at is None
    assert payment.card_last4 is None

    events = get_events(session_factory, payment.id)
    types = event_types(events)
    assert "verify_invalid_reference_id" in types  # precise, not "missing"
    assert "verify_missing_reference_id" not in types
    assert "manual_review_required" in types
    assert "gateway_payment_verified" not in types
    assert "bot_notification_queued" not in types
    # The raw value appears nowhere: events, last_error, response, logs.
    assert all(SENTINEL not in repr(event.data) for event in events)
    assert SENTINEL not in (payment.last_error or "")
    assert SENTINEL not in response.text
    assert SENTINEL not in caplog.text


def test_missing_reference_id_event_name_unchanged(
    client, settings, session_factory, stub
):
    """Regression: genuinely missing values keep the existing event name."""
    assert create_order(client, settings, order_id="ref-miss", amount=10_000).status_code == 200
    payment = get_payment(session_factory, "ref-miss")
    stub.verify_result = _verify_response(None)
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    types = event_types(get_events(session_factory, get_payment(session_factory, "ref-miss").id))
    assert "verify_missing_reference_id" in types
    assert "verify_invalid_reference_id" not in types
