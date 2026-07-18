"""End-to-end dynamic fee behavior through the real payment flow.

The contract under test: `payment.amount` stays the ORIGINAL bot invoice,
CentralPay is asked to charge `payable_amount = amount + fee`, verify
validates against `payable_amount`, and the bot notification payload is
unchanged: the exact JSON object and field set (order_id + actions
only — never any amount).
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.adminbot.auth import GENERIC_DENIAL, UpdateContext
from app.models import FeePolicy, Payment, PaymentStatus
from app.ops import main as ops_main
from tests.conftest import (
    DEFAULT_REDIRECT_URL,
    TEST_ADMIN_ID,
    create_order,
    event_types,
    get_events,
    get_payment,
    getlink_ok_response,
    run_pass,
    valid_callback_path,
    verify_ok_response,
)

PAST = datetime(2020, 1, 1, tzinfo=UTC)


def set_fee_policy(session_factory, rate_bps: int, *, effective_at=None) -> int:
    """Insert a fee policy directly (the CLI path is tested separately)."""
    with session_factory() as db:
        policy = FeePolicy(
            rate_bps=rate_bps,
            effective_at=effective_at or PAST,
            created_by="test",
            note="flow test policy",
        )
        db.add(policy)
        db.commit()
        return policy.id


def _payment_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(select(func.count(Payment.id))).scalar_one()


# --- creation: snapshot + getLink amount ------------------------------------


def test_fee_snapshot_and_getlink_receives_payable(client, settings, session_factory, stub):
    policy_id = set_fee_policy(session_factory, 1000)  # 10%

    response = create_order(client, settings, order_id="fee-1", amount=500_000)
    assert response.status_code == 200
    assert response.json() == {"url": DEFAULT_REDIRECT_URL}

    payment = get_payment(session_factory, "fee-1")
    # The original bot invoice is stored untouched...
    assert payment.amount == 500_000
    # ...with the immutable fee snapshot alongside it.
    assert payment.fee_policy_id == policy_id
    assert payment.fee_rate_bps == 1000
    assert payment.fee_amount == 50_000
    assert payment.payable_amount == 550_000

    # CentralPay is asked to charge the payable amount, not the original.
    [request] = stub.getlink_requests
    assert request["amount"] == 550_000

    events = get_events(session_factory, payment.id)
    assert event_types(events) == [
        "payment_created",
        "payment_fee_snapshotted",
        "payment_link_created",
    ]
    snapshot = next(e for e in events if e.event_type == "payment_fee_snapshotted")
    assert snapshot.data == {
        "fee_policy_id": policy_id,
        "fee_rate_bps": 1000,
        "original_amount": 500_000,
        "fee_amount": 50_000,
        "payable_amount": 550_000,
    }


def test_zero_fee_without_policy_preserves_existing_behavior(
    client, settings, session_factory, stub
):
    response = create_order(client, settings, order_id="fee-zero", amount=10_000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "fee-zero")
    assert payment.fee_policy_id is None
    assert payment.fee_rate_bps == 0
    assert payment.fee_amount == 0
    assert payment.payable_amount == 10_000
    assert stub.getlink_requests[0]["amount"] == 10_000


def test_fee_rounding_half_up_in_flow(client, settings, session_factory, stub):
    set_fee_policy(session_factory, 1000)
    # 1005 * 10% = 100.5 -> rounds half UP to 101.
    assert create_order(client, settings, order_id="fee-round", amount=1005).status_code == 200
    payment = get_payment(session_factory, "fee-round")
    assert payment.fee_amount == 101
    assert payment.payable_amount == 1106
    assert stub.getlink_requests[0]["amount"] == 1106


# --- verification against the payable amount --------------------------------


def test_verify_accepts_gateway_report_of_payable(client, settings, session_factory, stub):
    set_fee_policy(session_factory, 1000)
    create_order(client, settings, order_id="fee-ok", amount=500_000)
    payment = get_payment(session_factory, "fee-ok")

    stub.verify_result = verify_ok_response(amount=550_000)
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200

    payment = get_payment(session_factory, "fee-ok")
    assert payment.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    events = get_events(session_factory, payment.id)
    verified = next(e for e in events if e.event_type == "gateway_payment_verified")
    assert verified.data is not None
    assert verified.data["original_amount"] == 500_000
    assert verified.data["fee_amount"] == 50_000
    assert verified.data["payable_amount"] == 550_000


def test_verify_reporting_original_amount_is_a_mismatch(
    client, settings, session_factory, stub
):
    """A gateway that charged only the original (fee missing) must never verify."""
    set_fee_policy(session_factory, 1000)
    create_order(client, settings, order_id="fee-short", amount=500_000)
    payment = get_payment(session_factory, "fee-short")

    stub.verify_result = verify_ok_response(amount=500_000)
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200
    assert 'data-status="under_review"' in response.text

    payment = get_payment(session_factory, "fee-short")
    assert payment.status == PaymentStatus.MANUAL_REVIEW.value
    assert payment.gateway_verified_at is None

    events = get_events(session_factory, payment.id)
    mismatch = next(e for e in events if e.event_type == "verify_payable_amount_mismatch")
    assert mismatch.data is not None
    assert mismatch.data["expected_payable_amount"] == 550_000
    assert mismatch.data["reported_amount"] == 500_000
    assert mismatch.data["original_amount"] == 500_000
    assert mismatch.data["fee_rate_bps"] == 1000
    assert "gateway_payment_verified" not in event_types(events)


# --- snapshot immutability across duplicates, changes, and retries ----------


def test_duplicate_order_preserves_snapshot_after_policy_change(
    client, settings, session_factory, stub
):
    set_fee_policy(session_factory, 1000)
    first = create_order(client, settings, order_id="fee-dup", amount=500_000)
    assert first.status_code == 200

    # The fee changes between the two identical bot requests.
    set_fee_policy(session_factory, 250, effective_at=PAST + timedelta(days=1))

    second = create_order(client, settings, order_id="fee-dup", amount=500_000)
    assert second.status_code == 200
    assert first.json() == second.json()
    assert len(stub.getlink_requests) == 1  # no second gateway call

    payment = get_payment(session_factory, "fee-dup")
    # The snapshot from creation time is untouched by the newer policy.
    assert payment.fee_rate_bps == 1000
    assert payment.fee_amount == 50_000
    assert payment.payable_amount == 550_000


def test_fee_change_affects_only_new_orders(client, settings, session_factory, stub):
    old_policy = set_fee_policy(session_factory, 1000)
    create_order(client, settings, order_id="fee-old", amount=500_000)

    new_policy = set_fee_policy(session_factory, 250, effective_at=PAST + timedelta(days=1))
    create_order(client, settings, order_id="fee-new", amount=500_000)

    old = get_payment(session_factory, "fee-old")
    new = get_payment(session_factory, "fee-new")
    assert (old.fee_policy_id, old.fee_rate_bps, old.payable_amount) == (
        old_policy, 1000, 550_000
    )
    assert (new.fee_policy_id, new.fee_rate_bps, new.payable_amount) == (
        new_policy, 250, 512_500
    )
    assert [r["amount"] for r in stub.getlink_requests] == [550_000, 512_500]


def test_getlink_failed_retry_keeps_original_snapshot(
    client, settings, session_factory, stub
):
    set_fee_policy(session_factory, 1000)
    stub.getlink_result = httpx.ConnectError("connection refused")
    assert create_order(client, settings, order_id="fee-retry", amount=500_000).status_code == 502
    failed = get_payment(session_factory, "fee-retry")
    assert failed.status == PaymentStatus.GETLINK_FAILED.value
    assert failed.payable_amount == 550_000  # snapshot exists even on failure

    # The fee changes before the bot retries the same order.
    set_fee_policy(session_factory, 250, effective_at=PAST + timedelta(days=1))
    stub.getlink_result = getlink_ok_response()
    assert create_order(client, settings, order_id="fee-retry", amount=500_000).status_code == 200

    payment = get_payment(session_factory, "fee-retry")
    # Retry keeps the ORIGINAL snapshot and resends the STORED payable.
    assert payment.fee_rate_bps == 1000
    assert payment.payable_amount == 550_000
    assert stub.getlink_requests[-1]["amount"] == 550_000
    assert payment.gateway_order_id != failed.gateway_order_id  # fresh gateway id


# --- payable maximum ---------------------------------------------------------


def test_payable_above_maximum_rejected_before_any_side_effect(
    client, settings, session_factory, stub
):
    set_fee_policy(session_factory, 1000)
    # 95M original is under the 100M max, but 104.5M payable is not.
    response = create_order(client, settings, order_id="fee-max", amount=95_000_000)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "payable_amount_out_of_range"
    # No payment row, no partial snapshot, no gateway call, no clamping.
    assert _payment_count(session_factory) == 0
    assert stub.getlink_requests == []
    assert get_events(session_factory) == []


def test_original_below_minimum_still_rejected_with_fee_active(
    client, settings, session_factory, stub
):
    set_fee_policy(session_factory, 1000)
    # MIN applies to the ORIGINAL amount: 999 + fee would pass 1000, but
    # the original itself is below the minimum.
    response = create_order(client, settings, order_id="fee-min", amount=999)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "amount_out_of_range"
    assert _payment_count(session_factory) == 0


def test_payable_exactly_at_maximum_accepted(client, settings, session_factory, stub):
    set_fee_policy(session_factory, 1000)
    # 100_000_000 / 1.1 boundary: payable must be exactly <= max.
    amount = 90_909_090  # payable = 90_909_090 + 9_090_909 = 99_999_999
    assert create_order(client, settings, order_id="fee-edge", amount=amount).status_code == 200
    assert get_payment(session_factory, "fee-edge").payable_amount == 99_999_999


# --- bot notification payload: exact JSON object and field set ---------------


def test_bot_notification_payload_contains_no_fee_fields(
    client, settings, session_factory, stub, bot_stub, notifier
):
    set_fee_policy(session_factory, 1000)
    create_order(client, settings, order_id="ntf-money-1", amount=500_000)
    payment = get_payment(session_factory, "ntf-money-1")
    stub.verify_result = verify_ok_response(amount=550_000)
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    run_pass(session_factory, notifier, settings)

    # Byte-for-byte the pre-fee contract: exactly these two keys, nothing
    # about amounts, fees, payable, or references.
    [request] = bot_stub.requests
    assert request == {"order_id": "ntf-money-1", "actions": "custom_payment_verify"}
    assert len(request) == 2
    [headers] = bot_stub.headers
    assert headers["token"] == settings.bot_notify_token
    serialized = json.dumps(request)
    for forbidden in ("amount", "fee", "payable", "reference"):
        assert forbidden not in serialized

    delivered = get_payment(session_factory, "ntf-money-1")
    assert delivered.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


# --- operator CLI (python -m app.ops fee ...) --------------------------------


@pytest.fixture
def ops_env(settings, session_factory, monkeypatch):
    import app.ops as ops_module

    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)
    return settings


def _active_rate(session_factory) -> int | None:
    from app.services.fees import select_effective_policy

    with session_factory() as db:
        policy = select_effective_policy(db)
        return policy.rate_bps if policy is not None else None


def test_ops_fee_set_and_status(ops_env, session_factory, capsys):
    assert ops_main(["fee", "status"]) == 0
    assert "Current fee: 0% (no fee policy configured)" in capsys.readouterr().out

    assert ops_main(["fee", "set", "10", "--note", "launch fee"]) == 0
    out = capsys.readouterr().out
    assert "Fee set: 10%" in out
    assert "Applies to: new payment orders only" in out

    assert ops_main(["fee", "status"]) == 0
    out = capsys.readouterr().out
    assert "Current fee: 10%" in out
    assert "Rate basis points: 1000" in out
    assert _active_rate(session_factory) == 1000
    assert "fee_policy_created" in event_types(get_events(session_factory))


@pytest.mark.parametrize(
    "bad_rate",
    ["-5", "101", "1e2", "10,5", "abc", "10.555", "10; rm -rf /", "$(reboot)"],
)
def test_ops_fee_set_rejects_malformed_rate(ops_env, session_factory, capsys, bad_rate):
    assert ops_main(["fee", "set", bad_rate, "--note", "x"]) == 1
    assert "error:" in capsys.readouterr().err
    assert _active_rate(session_factory) is None  # nothing was created


def test_ops_fee_set_rejects_empty_note(ops_env, session_factory, capsys):
    assert ops_main(["fee", "set", "10", "--note", "   "]) == 1
    assert "note" in capsys.readouterr().err
    assert _active_rate(session_factory) is None


def test_ops_fee_schedule_and_cancel(ops_env, session_factory, capsys):
    assert ops_main(["fee", "set", "10", "--note", "base"]) == 0
    future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    assert ops_main(["fee", "schedule", "2.25", "--at", future, "--note", "seasonal"]) == 0
    capsys.readouterr()

    assert ops_main(["fee", "status"]) == 0
    out = capsys.readouterr().out
    assert "Current fee: 10%" in out  # scheduled policy is NOT active yet
    assert "Next scheduled: 2.25%" in out

    assert ops_main(["fee", "history"]) == 0
    history = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert len(history) == 2
    scheduled_id = next(h["policy_id"] for h in history if h["rate_bps"] == 225)

    assert ops_main(["fee", "cancel", str(scheduled_id), "--note", "wrong rate"]) == 0
    capsys.readouterr()
    assert ops_main(["fee", "status"]) == 0
    out = capsys.readouterr().out
    assert "Current fee: 10%" in out
    assert "Next scheduled" not in out  # cancelled, so nothing upcoming

    # History is permanent: the cancelled row is still listed.
    assert ops_main(["fee", "history"]) == 0
    history = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    cancelled = next(h for h in history if h["policy_id"] == scheduled_id)
    assert cancelled["state"] == "cancelled"
    assert cancelled["cancelled_by"] == "host-cli"


def test_ops_fee_schedule_rejects_past_and_naive_timestamps(
    ops_env, session_factory, capsys
):
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert ops_main(["fee", "schedule", "5", "--at", past, "--note", "x"]) == 1
    assert "future" in capsys.readouterr().err
    naive = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert ops_main(["fee", "schedule", "5", "--at", naive, "--note", "x"]) == 1
    assert "timezone" in capsys.readouterr().err
    assert _active_rate(session_factory) is None


def test_ops_fee_cancel_missing_policy_fails(ops_env, session_factory, capsys):
    assert ops_main(["fee", "cancel", "424242", "--note", "x"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_ops_fee_ensure_initial_never_resets_existing_policy(
    ops_env, session_factory, capsys
):
    # First run: no policy exists, so the installer's value is applied.
    assert ops_main(["fee", "set", "10", "--note", "Initial installation fee",
                     "--actor", "installer", "--ensure-initial"]) == 0
    assert _active_rate(session_factory) == 1000
    capsys.readouterr()

    # Installer re-run with a different answer: the existing policy wins.
    assert ops_main(["fee", "set", "0", "--note", "Initial installation fee",
                     "--actor", "installer", "--ensure-initial"]) == 0
    assert "--ensure-initial makes no change" in capsys.readouterr().out
    assert _active_rate(session_factory) == 1000
    with session_factory() as db:
        assert db.execute(select(func.count(FeePolicy.id))).scalar_one() == 1


# --- admin bot: strictly read-only -------------------------------------------


@pytest.fixture
def admin_handlers(app, session_factory, admin_settings):
    from app.adminbot.commands import CommandHandlers

    return CommandHandlers(
        session_factory,
        admin_settings,
        (TEST_ADMIN_ID,),
        api_probe=lambda: {"live": True, "ready": True},
    )


def _admin_ctx(user_id: int = TEST_ADMIN_ID) -> UpdateContext:
    return UpdateContext(user_id=user_id, chat_id=user_id, chat_type="private")


def test_admin_fee_command_shows_current_and_scheduled(
    admin_handlers, session_factory
):
    text = "\n".join(admin_handlers.handle(_admin_ctx(), "fee", []))
    assert "0%" in text  # no policy yet

    set_fee_policy(session_factory, 1000)
    set_fee_policy(
        session_factory, 225, effective_at=datetime.now(UTC) + timedelta(days=7)
    )
    text = "\n".join(admin_handlers.handle(_admin_ctx(), "fee", []))
    assert "10%" in text
    assert "2.25%" in text
    # The reply directs operators to the server CLI for changes.
    assert "centralpay fee" in text


def test_admin_fee_is_read_only(admin_handlers, session_factory):
    """No Telegram input can mutate fee policies — arguments are ignored."""
    before = _active_rate(session_factory)
    admin_handlers.handle(_admin_ctx(), "fee", ["set", "50", "--note", "hack"])
    assert _active_rate(session_factory) == before
    with session_factory() as db:
        assert db.execute(select(func.count(FeePolicy.id))).scalar_one() == 0
    # And the registry offers no mutating fee commands at all.
    registry = set(admin_handlers._registry())
    assert registry & {"fee_set", "set_fee", "fee_cancel", "fee_schedule"} == set()
    assert "fee" in registry


def test_admin_fee_denied_for_unauthorized_user(admin_handlers, session_factory):
    replies = admin_handlers.handle(_admin_ctx(user_id=999999999), "fee", [])
    assert replies == [GENERIC_DENIAL]


def test_admin_payment_view_separates_original_and_gateway_amounts(
    admin_handlers, client, settings, session_factory, stub
):
    set_fee_policy(session_factory, 1000)
    create_order(client, settings, order_id="fee-admin", amount=500_000)
    text = "\n".join(admin_handlers.handle(_admin_ctx(), "payment", ["fee-admin"]))
    assert "فاکتور اصلی ربات" in text  # original bot invoice, labelled as such
    assert "500,000" in text or "500000" in text
    assert "550,000" in text or "550000" in text  # gateway payable, separate figure


def test_daily_report_separates_fee_totals(client, settings, session_factory, stub):
    from app.adminbot.queries import daily_report_payload

    set_fee_policy(session_factory, 1000)
    for index in range(2):
        order_id = f"fee-report-{index}"
        create_order(client, settings, order_id=order_id, amount=500_000)
        payment = get_payment(session_factory, order_id)
        stub.verify_result = verify_ok_response(
            amount=550_000, reference_id=f"REF-{order_id}"
        )
        assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    with session_factory() as db:
        payload = daily_report_payload(db, report_date="2026-07-18")
    assert payload["total_original_invoices_toman"] == 1_000_000
    assert payload["total_fees_toman"] == 100_000
    assert payload["total_collected_via_gateway_toman"] == 1_100_000


# --- 0.6.0-rc1 release audit: frozen mismatches and end-state integrity ------


def test_payable_mismatch_never_notifies_bot(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """A payable-amount mismatch (fee not actually charged) is frozen in
    manual review and the worker can NEVER deliver it to the bot — the
    claim query requires bot_notify_pending AND a verified fact."""
    set_fee_policy(session_factory, 1000)
    create_order(client, settings, order_id="fee-frozen", amount=500_000)
    payment = get_payment(session_factory, "fee-frozen")
    stub.verify_result = verify_ok_response(amount=500_000)  # original, not payable
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    assert (
        get_payment(session_factory, "fee-frozen").status
        == PaymentStatus.MANUAL_REVIEW.value
    )

    result = run_pass(session_factory, notifier, settings)
    assert result["processed"] == 0
    assert bot_stub.requests == []  # the bot never hears about this payment


def test_delivered_fee_payment_retains_exact_snapshot(
    client, settings, session_factory, stub, bot_stub, notifier
):
    """After the full lifecycle (create -> verify at payable -> deliver),
    every money field still reads exactly as at creation time."""
    policy_id = set_fee_policy(session_factory, 1000)
    create_order(client, settings, order_id="ntf-final-1", amount=500_000)
    payment = get_payment(session_factory, "ntf-final-1")
    stub.verify_result = verify_ok_response(amount=550_000)
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200
    run_pass(session_factory, notifier, settings)

    final = get_payment(session_factory, "ntf-final-1")
    assert final.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert (
        final.amount,
        final.fee_policy_id,
        final.fee_rate_bps,
        final.fee_amount,
        final.payable_amount,
    ) == (500_000, policy_id, 1000, 50_000, 550_000)


# --- zero-based audit: --ensure-initial means ZERO policy rows ---------------


def _policy_count(session_factory) -> int:
    with session_factory() as db:
        return db.execute(select(func.count(FeePolicy.id))).scalar_one()


def test_ops_ensure_initial_creates_only_from_zero_rows(ops_env, session_factory, capsys):
    assert _policy_count(session_factory) == 0
    assert ops_main(["fee", "set", "10", "--note", "Initial installation fee",
                     "--actor", "installer", "--ensure-initial"]) == 0
    assert _policy_count(session_factory) == 1
    assert _active_rate(session_factory) == 1000


def test_ops_ensure_initial_noop_with_only_future_policy(ops_env, session_factory, capsys):
    """A future scheduled policy is an operator decision: the installer must
    NOT inject a surprise immediate policy in front of it."""
    set_fee_policy(
        session_factory, 250, effective_at=datetime.now(UTC) + timedelta(days=7)
    )
    assert ops_main(["fee", "set", "10", "--note", "Initial installation fee",
                     "--actor", "installer", "--ensure-initial"]) == 0
    out = capsys.readouterr().out
    assert "--ensure-initial makes no change" in out
    assert _policy_count(session_factory) == 1  # still only the scheduled one
    assert _active_rate(session_factory) is None  # nothing active yet — preserved


def test_ops_ensure_initial_noop_with_only_cancelled_history(
    ops_env, session_factory, capsys
):
    """Cancelled history is still history: its existence proves an operator
    has managed fees deliberately, so the installer changes nothing."""
    with session_factory() as db:
        db.add(
            FeePolicy(
                rate_bps=250,
                effective_at=datetime.now(UTC) + timedelta(days=7),
                created_by="operator",
                note="scheduled then cancelled",
                cancelled_at=datetime.now(UTC),
                cancelled_by="operator",
                cancellation_note="changed my mind",
            )
        )
        db.commit()
    assert ops_main(["fee", "set", "10", "--note", "Initial installation fee",
                     "--actor", "installer", "--ensure-initial"]) == 0
    assert "--ensure-initial makes no change" in capsys.readouterr().out
    assert _policy_count(session_factory) == 1
    assert _active_rate(session_factory) is None
