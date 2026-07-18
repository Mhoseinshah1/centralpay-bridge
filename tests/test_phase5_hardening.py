"""Phase 5: rate limiting, review operations, health details, reference
integrity, and first-payment guard."""

import json

import pytest

from app.models import PaymentStatus
from app.ops import main as ops_main
from app.ratelimit import SlidingWindowLimiter
from tests.conftest import (
    create_order,
    event_types,
    get_alerts,
    get_events,
    get_payment,
    make_verified_pending,
    valid_callback_path,
    verify_ok_response,
)

# --- rate limiting ----------------------------------------------------------


def test_sliding_window_limiter_bounds():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60.0)
    assert [limiter.allow(now=100.0 + i) for i in range(4)] == [True, True, True, False]
    # Outside the window the budget recovers.
    assert limiter.allow(now=161.0) is True


def test_invalid_api_key_attempts_are_rate_limited(app, client, settings, session_factory):
    limit = settings.rate_limit_invalid_key_per_10min
    codes = []
    for _ in range(limit + 3):
        response = create_order(client, settings, api_key="wrong-key-wrong-key-wrong")
        codes.append(response.status_code)
    assert codes[: limit] == [401] * limit
    assert set(codes[limit:]) == {429}
    # Valid traffic is unaffected by the invalid-key limiter.
    assert create_order(client, settings, order_id="rl-valid").status_code == 200


def test_create_payment_burst_limited(app, client, settings, session_factory):
    app.state.rate_limiters.create = SlidingWindowLimiter(limit=3, window_seconds=60.0)
    codes = [
        create_order(client, settings, order_id=f"rl-burst-{i}").status_code
        for i in range(5)
    ]
    assert codes[:3] == [200, 200, 200]
    assert set(codes[3:]) == {429}


def test_invalid_signature_rate_limited(app, client, settings, session_factory, stub):
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="rl-sig"
    )
    app.state.rate_limiters.invalid_signature = SlidingWindowLimiter(
        limit=2, window_seconds=60.0
    )
    codes = []
    for _ in range(4):
        response = client.get(
            f"/api/centralpay/callback?orderId={payment.gateway_order_id}"
            f"&ct={'a' * 32}&sig={'0' * 64}"
        )
        codes.append(response.status_code)
    assert codes[:2] == [403, 403]
    assert set(codes[2:]) == {429}
    # The legitimate signed link still works.
    stub.verify_result = verify_ok_response(amount=10000, reference_id="REF-rl-sig")
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200


# --- reference integrity ----------------------------------------------------


def test_reference_id_collision_goes_to_manual_review(
    client, settings, session_factory, stub
):
    first = make_verified_pending(
        client, settings, session_factory, stub, order_id="ref-a"
    )
    assert first.reference_id == "REF-ref-a"

    assert create_order(client, settings, order_id="ref-b").status_code == 200
    second = get_payment(session_factory, "ref-b")
    # CentralPay (impossibly) reports the SAME reference id again.
    stub.verify_result = verify_ok_response(amount=10000, reference_id="REF-ref-a")
    response = client.get(valid_callback_path(stub, second.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text

    second = get_payment(session_factory, "ref-b")
    assert second.status == PaymentStatus.MANUAL_REVIEW.value
    assert second.reference_id is None  # never overwritten / never duplicated
    types = event_types(get_events(session_factory, second.id))
    assert "reference_id_collision" in types
    # The first payment is untouched.
    first = get_payment(session_factory, "ref-a")
    assert first.reference_id == "REF-ref-a"
    assert first.status == PaymentStatus.BOT_NOTIFY_PENDING.value


# --- first payment guard ----------------------------------------------------


def test_first_payment_guard_records_alert_once(
    app, admin_settings, client, settings, session_factory, stub
):
    from app.adminbot.alerts import configure_alert_creation, reset_alert_creation

    guard_settings = admin_settings.model_copy(update={"first_payment_guard_enabled": True})
    app.state.settings = app.state.settings.model_copy(
        update={"first_payment_guard_enabled": True}
    )
    configure_alert_creation(guard_settings)
    try:
        make_verified_pending(client, settings, session_factory, stub, order_id="fp-1")
        make_verified_pending(client, settings, session_factory, stub, order_id="fp-2")
    finally:
        reset_alert_creation()

    events = [
        e
        for e in get_events(session_factory)
        if e.event_type == "first_production_payment_verified"
    ]
    assert len(events) == 1  # only the FIRST verified payment triggers it
    alerts = get_alerts(session_factory, "first_production_payment_verified")
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


def test_first_payment_guard_disabled_by_default(
    client, settings, session_factory, stub
):
    make_verified_pending(client, settings, session_factory, stub, order_id="fp-off")
    assert not [
        e
        for e in get_events(session_factory)
        if e.event_type == "first_production_payment_verified"
    ]


# --- manual review operations ----------------------------------------------


@pytest.fixture
def review_env(client, settings, session_factory, stub, bot_stub, notifier, monkeypatch):
    """A manual-review payment plus app.ops wired to the test database."""
    import httpx

    import app.ops as ops_module

    make_verified_pending(client, settings, session_factory, stub, order_id="rv-1")
    bot_stub.result = httpx.Response(422)
    from tests.conftest import run_pass

    run_pass(session_factory, notifier, settings)
    assert get_payment(session_factory, "rv-1").status == PaymentStatus.MANUAL_REVIEW.value

    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)
    return settings


def test_review_show_and_list(review_env, session_factory, capsys):
    assert ops_main(["review", "list"]) == 0
    listed = capsys.readouterr().out
    assert "rv-1" in listed
    assert ops_main(["review", "show", "rv-1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["reason"] == "bot_http_422"
    assert shown["gateway_verified"] is True


def test_review_acknowledge_and_resolve(review_env, session_factory, capsys):
    assert ops_main(["review", "acknowledge", "rv-1", "--note", "checking with operator"]) == 0
    payment = get_payment(session_factory, "rv-1")
    assert payment.review_acknowledged_at is not None

    assert (
        ops_main(
            [
                "review",
                "resolve",
                "rv-1",
                "--resolution",
                "bot_not_credited",
                "--note",
                "operator confirmed no credit; refund issued via shop",
            ]
        )
        == 0
    )
    payment = get_payment(session_factory, "rv-1")
    assert payment.review_resolved_at is not None
    assert payment.review_resolution == "bot_not_credited"
    # Financial facts preserved: status, verification, amounts untouched.
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.gateway_verified_at is not None
    types = event_types(get_events(session_factory, payment.id))
    assert "manual_review_acknowledged" in types
    assert "manual_review_resolved" in types
    # Resolved payments drop out of the default list.
    capsys.readouterr()  # flush the acknowledge/resolve confirmations
    assert ops_main(["review", "list"]) == 0
    assert "rv-1" not in capsys.readouterr().out


def test_review_resolve_requires_valid_resolution(review_env):
    with pytest.raises(SystemExit):
        ops_main(["review", "resolve", "rv-1", "--resolution", "mark_paid", "--note", "x"])


def test_review_resend_refused_in_safe_mode(review_env, session_factory, capsys):
    # Flag gate first: without both flags it never reaches the database.
    assert ops_main(["review", "resend", "rv-1"]) == 1
    assert "--confirm-idempotent-bot" in capsys.readouterr().err
    # With flags, safe mode still refuses.
    assert (
        ops_main(["review", "resend", "rv-1", "--confirm-idempotent-bot", "--yes"]) == 1
    )
    assert "safe mode" in capsys.readouterr().err
    assert get_payment(session_factory, "rv-1").status == PaymentStatus.MANUAL_REVIEW.value


def test_review_resend_requeues_in_idempotent_mode(
    review_env, session_factory, monkeypatch, capsys
):
    import app.ops as ops_module

    idempotent = review_env.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    monkeypatch.setattr(ops_module, "Settings", lambda: idempotent)
    assert (
        ops_main(["review", "resend", "rv-1", "--confirm-idempotent-bot", "--yes"]) == 0
    )
    payment = get_payment(session_factory, "rv-1")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert "manual_review_resend_requested" in event_types(
        get_events(session_factory, payment.id)
    )


# --- health details ---------------------------------------------------------


def test_health_details_machine_readable(client, settings, session_factory, stub):
    make_verified_pending(client, settings, session_factory, stub, order_id="hd-1")
    response = client.get("/health/details")
    assert response.status_code == 200
    details = response.json()
    assert details["version"] == "0.6.0-rc1"
    assert details["database"] == "ok"
    assert details["pending_notifications"] == 1
    assert details["manual_review"] == 0
    # No secrets anywhere in the payload.
    serialized = json.dumps(details)
    for secret in (
        settings.inbound_api_key,
        settings.callback_hmac_secret,
        settings.centralpay_getlink_api_key,
        settings.centralpay_verify_api_key,
    ):
        assert secret not in serialized


def test_health_details_not_publicly_routed():
    with open("deploy/caddy/Caddyfile.template") as f:
        caddy = f.read()
    assert "/health/details" not in caddy  # internal-only endpoint
