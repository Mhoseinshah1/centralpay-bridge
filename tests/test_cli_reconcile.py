"""app.cli / app.services.reconcile_inspect: `centralpay reconcile ORDER_ID`.

The safety contract under test: `reconcile` (default mode) NEVER makes a
network call and NEVER writes to the database; `--verify` (gated behind
settings.centralpay_diagnostic_verify_enabled, off by default -- see
app.config) makes EXACTLY ONE diagnostic CentralPayClient.verify() call and
still never writes to the database, never queues a notification, never
records a PaymentEvent, and never calls verify_and_settle / process_callback
/ run_reconciliation_pass. It is a diagnostic prediction only -- see
app/services/reconcile_inspect.py. The `settings` fixture (tests/conftest.py)
enables the diagnostic-verify gate so this file's --verify tests exercise
the real flow; the gate itself is tested directly further below.
"""

import inspect
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.centralpay import CentralPayClient
from app.cli import build_parser
from app.cli import main as cli_main
from app.models import Payment, PaymentStatus
from app.services.reconcile_inspect import (
    VerifyRefusal,
    build_local_snapshot,
    determine_verify_refusal,
)
from tests.conftest import (
    DEFAULT_GATEWAY_USER_ID,
    TEST_ADMIN_BOT_TOKEN,
    TEST_BOT_TOKEN,
    TEST_CALLBACK_HMAC_SECRET,
    TEST_DB_PASSWORD,
    TEST_GETLINK_API_KEY,
    TEST_INBOUND_API_KEY,
    TEST_PAYER_ID_SECRET,
    TEST_VERIFY_API_KEY,
    create_order,
    get_events,
    get_payment,
    make_verified_pending,
    verify_ok_response,
)

_TRACKED_FIELDS = [
    "status",
    "gateway_verified_at",
    "reference_id",
    "card_last4",
    "last_error",
    "amount",
    "payable_amount",
    "fee_amount",
    "fee_rate_bps",
    "bot_notify_reason",
    "bot_notify_attempts",
    "bot_last_http_status",
    "bot_last_error_code",
    "bot_notify_started_at",
    "bot_notify_accepted_at",
    "next_retry_at",
    "manual_review_at",
    "notification_claimed_at",
    "notification_claimed_by",
    "reconciliation_attempts",
    "reconciliation_next_at",
    "reconciliation_last_at",
    "reconciliation_last_error_code",
    "reconciliation_claimed_at",
    "reconciliation_claimed_by",
    "updated_at",
]

_ALL_SECRETS = [
    TEST_INBOUND_API_KEY,
    TEST_CALLBACK_HMAC_SECRET,
    TEST_GETLINK_API_KEY,
    TEST_VERIFY_API_KEY,
    TEST_DB_PASSWORD,
    TEST_BOT_TOKEN,
    TEST_ADMIN_BOT_TOKEN,
    TEST_PAYER_ID_SECRET,
]


def _snapshot(payment: Payment) -> dict[str, object]:
    return {field: getattr(payment, field) for field in _TRACKED_FIELDS}


def _assert_payment_unchanged(
    session_factory, bot_order_id: str, before: dict[str, object]
) -> None:
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == bot_order_id)
        ).scalar_one()
        after = _snapshot(payment)
    assert after == before


def _age_payment(session_factory, bot_order_id: str, *, seconds: int) -> None:
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == bot_order_id)
        ).scalar_one()
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(seconds=seconds)
        db.commit()


def _patch_centralpay_client(monkeypatch, stub) -> None:
    import app.cli as cli_module

    def factory(*, base_url, getlink_api_key, verify_api_key, timeout_seconds):
        return CentralPayClient(
            base_url=base_url,
            getlink_api_key=getlink_api_key,
            verify_api_key=verify_api_key,
            timeout_seconds=timeout_seconds,
            transport=httpx.MockTransport(stub.handler),
        )

    monkeypatch.setattr(cli_module, "CentralPayClient", factory)


def _make_manual_review_payment(
    session_factory,
    *,
    bot_order_id: str,
    gateway_order_id: int,
    amount: int = 8000,
    age_seconds: int = 60,
) -> Payment:
    with session_factory() as db:
        payment = Payment(
            bot_order_id=bot_order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=DEFAULT_GATEWAY_USER_ID,
            amount=amount,
            payable_amount=amount,
            status=PaymentStatus.MANUAL_REVIEW.value,
            last_error="verify_payable_amount_mismatch",
            manual_review_at=datetime.now(UTC),
            callback_token_issued_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment


@pytest.fixture
def cli_env(settings, session_factory, monkeypatch):
    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "Settings", lambda: settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: session_factory)
    return settings


# --- argparse wiring ---------------------------------------------------------


def test_reconcile_parser_defaults():
    args = build_parser().parse_args(["reconcile", "abc"])
    assert args.command == "reconcile"
    assert args.order_id == "abc"
    assert args.verify is False
    assert args.confirm_aged_out is False
    assert args.as_json is False


def test_reconcile_parser_verify_and_confirm_and_json_flags():
    args = build_parser().parse_args(
        ["reconcile", "abc", "--verify", "--confirm-aged-out", "--json"]
    )
    assert args.verify is True
    assert args.confirm_aged_out is True
    assert args.as_json is True


def test_reconcile_payment_not_found(cli_env, capsys):
    assert cli_main(["reconcile", "does-not-exist"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "payment_not_found", "order_id": "does-not-exist"}


def test_reconcile_confirm_aged_out_without_verify_rejected(cli_env, capsys):
    """Scenario 11: --confirm-aged-out without --verify is rejected -- and
    rejected BEFORE any payment lookup, so it works even for a bogus
    order id (usage error, not a lookup problem)."""
    assert cli_main(["reconcile", "whatever", "--confirm-aged-out"]) == 1
    out = capsys.readouterr()
    assert "--confirm-aged-out requires --verify" in out.err
    assert out.out == ""


# --- scenario 1: default mode is local-only ---------------------------------


def test_reconcile_default_zero_network_zero_writes_zero_events(
    cli_env, client, settings, session_factory, stub, capsys
):
    assert create_order(client, settings, order_id="rc-default-1", amount=7000).status_code == 200
    payment = get_payment(session_factory, "rc-default-1")
    verify_requests_before = len(stub.verify_requests)
    events_before = len(get_events(session_factory, payment.id))
    before = _snapshot(payment)

    assert cli_main(["reconcile", "rc-default-1"]) == 0
    out = capsys.readouterr().out
    assert "🔍 Reconcile: rc-default-1" in out
    assert "link_created" in out
    assert "gateway_verified:        no" in out

    assert len(stub.verify_requests) == verify_requests_before  # zero network calls
    assert len(get_events(session_factory, payment.id)) == events_before  # zero new events
    _assert_payment_unchanged(session_factory, "rc-default-1", before)


def test_reconcile_default_json_shape(cli_env, client, settings, session_factory, stub, capsys):
    response = create_order(client, settings, order_id="rc-default-json", amount=7000)
    assert response.status_code == 200
    assert cli_main(["reconcile", "rc-default-json", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"] is None
    local = payload["local"]
    assert local["bot_order_id"] == "rc-default-json"
    assert local["status"] == "link_created"
    assert local["gateway_verified"] is False
    assert local["payable_amount"] == 7000
    assert local["verify_aged_out"] is False
    assert "reconciliation" in local
    assert set(local["reconciliation"]) == {
        "age_bucket",
        "attempts",
        "last_at",
        "next_at",
        "last_error_code",
        "enabled",
        "schedule_due",
        "auto_reconciliation_due",
        "aged_out",
        "attempts_exhausted",
    }


# --- PR #59 follow-up: RECONCILIATION_ENABLED must gate auto_reconciliation_due


def test_reconcile_auto_due_true_when_enabled_and_schedule_due(
    cli_env, client, settings, session_factory, capsys
):
    assert create_order(client, settings, order_id="rc-enabled-due", amount=5000).status_code == 200
    _age_payment(
        session_factory, "rc-enabled-due", seconds=settings.reconciliation_min_age_seconds + 5
    )

    assert cli_main(["reconcile", "rc-enabled-due", "--json"]) == 0
    recon = json.loads(capsys.readouterr().out)["local"]["reconciliation"]
    assert recon["enabled"] is True
    assert recon["schedule_due"] is True
    assert recon["auto_reconciliation_due"] is True

    assert cli_main(["reconcile", "rc-enabled-due"]) == 0
    out = capsys.readouterr().out
    assert "reconciliation enabled:  yes" in out
    assert "auto-reconciliation due: yes" in out


def test_reconcile_auto_due_false_when_disabled_even_if_otherwise_due(
    cli_env, client, settings, session_factory, monkeypatch, capsys
):
    """The task's core requirement: RECONCILIATION_ENABLED=false must never
    let auto_reconciliation_due read true, even for a payment whose age/
    attempts schedule alone says it is due."""
    import app.cli as cli_module

    response = create_order(client, settings, order_id="rc-disabled-due", amount=5000)
    assert response.status_code == 200
    _age_payment(
        session_factory, "rc-disabled-due", seconds=settings.reconciliation_min_age_seconds + 5
    )

    disabled = settings.model_copy(update={"reconciliation_enabled": False})
    monkeypatch.setattr(cli_module, "Settings", lambda: disabled)

    assert cli_main(["reconcile", "rc-disabled-due", "--json"]) == 0
    recon = json.loads(capsys.readouterr().out)["local"]["reconciliation"]
    assert recon["enabled"] is False
    assert recon["schedule_due"] is True  # would be due on schedule...
    assert recon["auto_reconciliation_due"] is False  # ...but the worker is disabled

    assert cli_main(["reconcile", "rc-disabled-due"]) == 0
    out = capsys.readouterr().out
    assert "reconciliation enabled:  no" in out
    assert "auto-reconciliation due: no" in out


# --- scenario 12: lookup by bot_order_id and numeric gateway_order_id ------


def test_reconcile_lookup_supports_bot_order_id_and_numeric_gateway_order_id(
    cli_env, client, settings, session_factory, stub, capsys
):
    assert create_order(client, settings, order_id="rc-lookup-1", amount=5000).status_code == 200
    payment = get_payment(session_factory, "rc-lookup-1")

    assert cli_main(["reconcile", "rc-lookup-1"]) == 0
    by_bot_order = capsys.readouterr().out
    assert "rc-lookup-1" in by_bot_order

    assert cli_main(["reconcile", str(payment.gateway_order_id)]) == 0
    by_gateway_order = capsys.readouterr().out
    assert "rc-lookup-1" in by_gateway_order
    assert str(payment.gateway_order_id) in by_gateway_order


# --- Codex follow-up: refuse ambiguous numeric ORDER_IDs --------------------


def _make_minimal_payment(session_factory, *, bot_order_id: str, gateway_order_id: int) -> None:
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id=bot_order_id,
                gateway_order_id=gateway_order_id,
                gateway_user_id=1,
                amount=4000,
                payable_amount=4000,
                status=PaymentStatus.LINK_CREATED.value,
            )
        )
        db.commit()


def test_reconcile_refuses_ambiguous_order_id(cli_env, session_factory, capsys):
    """A numeric ORDER_ID that is simultaneously one payment's bot_order_id
    AND a DIFFERENT payment's gateway_order_id must never be silently
    resolved to either one -- that could point --verify at the wrong
    payment's gateway_order_id entirely."""
    _make_minimal_payment(
        session_factory, bot_order_id="778899001122", gateway_order_id=100201
    )
    _make_minimal_payment(
        session_factory, bot_order_id="rc-ambiguous-other", gateway_order_id=778899001122
    )

    assert cli_main(["reconcile", "778899001122"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "ambiguous_order_id", "order_id": "778899001122"}


def test_reconcile_verify_also_refuses_ambiguous_order_id_with_zero_gateway_calls(
    cli_env, session_factory, stub, monkeypatch, capsys
):
    """The ambiguity refusal is checked before anything else -- including
    the diagnostic-verify-enabled gate and any gateway call."""
    _patch_centralpay_client(monkeypatch, stub)
    _make_minimal_payment(
        session_factory, bot_order_id="778899003344", gateway_order_id=100202
    )
    _make_minimal_payment(
        session_factory, bot_order_id="rc-ambiguous-verify-other", gateway_order_id=778899003344
    )

    assert cli_main(["reconcile", "778899003344", "--verify"]) == 1
    out = capsys.readouterr().out
    assert "ambiguous_order_id" in out
    assert len(stub.verify_requests) == 0


def test_reconcile_lookup_by_own_gateway_order_id_is_not_ambiguous(
    cli_env, client, settings, session_factory, capsys
):
    """The common case must keep working: looking a payment up by its OWN
    numeric gateway_order_id (which trivially also equals itself, not a
    DIFFERENT payment) is not ambiguous."""
    response = create_order(client, settings, order_id="rc-not-ambiguous", amount=5000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-not-ambiguous")

    assert cli_main(["reconcile", str(payment.gateway_order_id)]) == 0
    out = capsys.readouterr().out
    assert "rc-not-ambiguous" in out


def test_payment_command_refuses_ambiguous_order_id(cli_env, session_factory, capsys):
    """The `payment` command shares `_find_payment` with `reconcile` and
    must refuse the same way."""
    _make_minimal_payment(
        session_factory, bot_order_id="778899005566", gateway_order_id=100203
    )
    _make_minimal_payment(
        session_factory, bot_order_id="payment-ambiguous-other", gateway_order_id=778899005566
    )

    assert cli_main(["payment", "778899005566"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "ambiguous_order_id", "order_id": "778899005566"}


# --- Codex P2 follow-up: bound numeric ORDER_ID before the gateway lookup ---
#
# bot_order_id is an arbitrary string (AGENTS.md: "Bot order_id may be a
# string") and can itself be a longer all-digit value than
# Payment.gateway_order_id (a PostgreSQL BIGINT, signed 64-bit) can hold.
# Binding such a value to that column raises psycopg.errors.
# NumericValueOutOfRange against real PostgreSQL -- SQLite has no fixed
# integer width and would not reproduce the crash, so this test proves the
# gateway lookup is skipped (not merely "happens not to crash here"); see
# test_find_payment_huge_numeric_bot_order_id_does_not_crash_pg in
# tests/integration/test_reconcile_inspect_pg.py for the real-PostgreSQL,
# would-have-crashed-under-the-old-code proof.


def test_find_payment_skips_gateway_lookup_for_out_of_bigint_range_order_id(
    cli_env, session_factory, monkeypatch, capsys
):
    """A numeric ORDER_ID too large for a signed 64-bit BIGINT must never
    even be bound into a gateway_order_id query -- only the bot_order_id
    lookup runs. The bot_order_id lookup alone must still succeed."""
    huge_bot_order_id = "9" * 30  # far beyond 2**63 - 1 (19 digits)
    _make_minimal_payment(
        session_factory, bot_order_id=huge_bot_order_id, gateway_order_id=100210
    )

    gateway_lookup_count = 0
    real_execute = Session.execute

    def recording_execute(self, statement, *args, **kwargs):
        nonlocal gateway_lookup_count
        # Every Payment SELECT lists the gateway_order_id COLUMN regardless
        # of filter, so only a WHERE clause filtering ON it counts as "a
        # gateway lookup" here.
        compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
        if "WHERE payments.gateway_order_id" in compiled:
            gateway_lookup_count += 1
        return real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", recording_execute)

    assert cli_main(["payment", huge_bot_order_id]) == 0
    out = capsys.readouterr().out
    assert huge_bot_order_id in out
    assert gateway_lookup_count == 0  # the oversized-value guard short-circuited first


# --- scenario 2: --verify, gateway not successful ---------------------------


def test_reconcile_verify_gateway_not_successful(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    assert create_order(client, settings, order_id="rc-notpaid-1", amount=6000).status_code == 200
    payment = get_payment(session_factory, "rc-notpaid-1")
    before = _snapshot(payment)
    events_before = len(get_events(session_factory, payment.id))

    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid yet"})

    assert cli_main(["reconcile", "rc-notpaid-1", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "NOT_SUCCESSFUL" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    assert len(stub.verify_requests) == 1
    assert len(get_events(session_factory, payment.id)) == events_before
    _assert_payment_unchanged(session_factory, "rc-notpaid-1", before)


# --- scenario 3: --verify, gateway success, all fields matching ------------


def test_reconcile_verify_success_reports_would_verify_without_mutating(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-wouldverify-1", amount=9000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-wouldverify-1")
    before = _snapshot(payment)
    events_before = len(get_events(session_factory, payment.id))

    stub.verify_result = verify_ok_response(
        amount=9000, user_id=payment.gateway_user_id, reference_id="REF-would-verify-1"
    )

    assert cli_main(["reconcile", "rc-wouldverify-1", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_VERIFY" in out
    assert "REF-would-verify-1" in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert "NOT settled" in out

    assert len(stub.verify_requests) == 1
    assert len(get_events(session_factory, payment.id)) == events_before
    _assert_payment_unchanged(session_factory, "rc-wouldverify-1", before)

    refetched = get_payment(session_factory, "rc-wouldverify-1")
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None
    assert refetched.reference_id is None


# --- scenario 4: amount mismatch --------------------------------------------


def test_reconcile_verify_amount_mismatch_diagnostic_only(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-amount-mismatch", amount=10000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-amount-mismatch")
    before = _snapshot(payment)

    stub.verify_result = verify_ok_response(
        amount=999, user_id=payment.gateway_user_id, reference_id="REF-amount-mismatch"
    )

    assert cli_main(["reconcile", "rc-amount-mismatch", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "payable_amount_mismatch" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    _assert_payment_unchanged(session_factory, "rc-amount-mismatch", before)
    refetched = get_payment(session_factory, "rc-amount-mismatch")
    assert refetched.status == PaymentStatus.LINK_CREATED.value  # never actually moved


# --- scenario 5: user-id mismatch, read-only, no raw id leakage ------------


def test_reconcile_verify_user_id_mismatch_read_only_no_raw_id_leak(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-userid-mismatch", amount=4000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-userid-mismatch")
    before = _snapshot(payment)
    mismatched_user_id = payment.gateway_user_id + 987654321

    stub.verify_result = verify_ok_response(
        amount=4000, user_id=mismatched_user_id, reference_id="REF-userid-mismatch"
    )

    assert cli_main(["reconcile", "rc-userid-mismatch", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "user_id_mismatch" in out
    assert "user_id matches:         no" in out
    # Neither the expected nor the reported raw gateway user id ever appears.
    assert str(payment.gateway_user_id) not in out
    assert str(mismatched_user_id) not in out

    _assert_payment_unchanged(session_factory, "rc-userid-mismatch", before)


# --- scenario 6: missing/invalid reference id, field errors ----------------


def test_reconcile_verify_missing_reference_id_diagnostic_only(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    assert create_order(client, settings, order_id="rc-missing-ref", amount=3000).status_code == 200
    payment = get_payment(session_factory, "rc-missing-ref")
    before = _snapshot(payment)

    stub.verify_result = httpx.Response(
        200,
        json={"status": "success", "data": {"amount": 3000, "userId": payment.gateway_user_id}},
    )

    assert cli_main(["reconcile", "rc-missing-ref", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "missing_reference_id" in out

    _assert_payment_unchanged(session_factory, "rc-missing-ref", before)


def test_reconcile_verify_invalid_reference_id_diagnostic_only(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    assert create_order(client, settings, order_id="rc-invalid-ref", amount=3000).status_code == 200
    payment = get_payment(session_factory, "rc-invalid-ref")
    before = _snapshot(payment)

    oversized = "X" * 200  # over CENTRALPAY_REFERENCE_ID_MAX_LENGTH (128)
    stub.verify_result = httpx.Response(
        200,
        json={
            "status": "success",
            "data": {"amount": 3000, "userId": payment.gateway_user_id, "referenceId": oversized},
        },
    )

    assert cli_main(["reconcile", "rc-invalid-ref", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "invalid_reference_id" in out
    assert oversized not in out  # the raw invalid value never leaves app.centralpay

    _assert_payment_unchanged(session_factory, "rc-invalid-ref", before)


def test_reconcile_verify_field_errors_surfaced_diagnostic_only(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-field-errors", amount=3000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-field-errors")
    before = _snapshot(payment)

    stub.verify_result = httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "amount": "not-a-number",
                "userId": payment.gateway_user_id,
                "referenceId": "REF-field-errors",
            },
        },
    )

    assert cli_main(["reconcile", "rc-field-errors", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "gateway_invalid_amount" in out  # fixed internal reason code, never raw text

    _assert_payment_unchanged(session_factory, "rc-field-errors", before)


# --- scenario 7: reference-id collision, detected read-only ----------------


def test_reconcile_verify_reference_id_collision_detected_read_only(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    with session_factory() as db:
        holder = Payment(
            bot_order_id="rc-collision-holder",
            gateway_order_id=910001,
            gateway_user_id=1,
            amount=5000,
            payable_amount=5000,
            status=PaymentStatus.BOT_NOTIFY_PENDING.value,
            gateway_verified_at=datetime.now(UTC),
            reference_id="REF-COLLIDE-XYZ",
        )
        db.add(holder)
        db.commit()
        db.refresh(holder)
    holder_before = _snapshot(holder)

    response = create_order(client, settings, order_id="rc-collision-candidate", amount=5000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-collision-candidate")
    candidate_before = _snapshot(payment)

    stub.verify_result = verify_ok_response(
        amount=5000, user_id=payment.gateway_user_id, reference_id="REF-COLLIDE-XYZ"
    )

    assert cli_main(["reconcile", "rc-collision-candidate", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_REQUIRE_MANUAL_REVIEW" in out
    assert "reference_id_collision" in out

    _assert_payment_unchanged(session_factory, "rc-collision-candidate", candidate_before)
    with session_factory() as db:
        refetched_holder = db.execute(
            select(Payment).where(Payment.bot_order_id == "rc-collision-holder")
        ).scalar_one()
        assert _snapshot(refetched_holder) == holder_before


# --- scenario 8: already gateway-verified -----------------------------------


def test_reconcile_verify_already_verified_refused_zero_network(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    make_verified_pending(client, settings, session_factory, stub, order_id="rc-already-verified")
    verify_requests_before = len(stub.verify_requests)
    payment = get_payment(session_factory, "rc-already-verified")
    before = _snapshot(payment)

    assert cli_main(["reconcile", "rc-already-verified", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "already denotes successful gateway verification" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    assert len(stub.verify_requests) == verify_requests_before  # zero NEW network calls
    _assert_payment_unchanged(session_factory, "rc-already-verified", before)

    # Default (non---verify) inspection still works normally.
    assert cli_main(["reconcile", "rc-already-verified"]) == 0
    default_out = capsys.readouterr().out
    assert "gateway_verified:        yes" in default_out


# --- scenario 9: manual_review ----------------------------------------------


def test_reconcile_verify_manual_review_refused_zero_network(
    cli_env, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    _make_manual_review_payment(
        session_factory, bot_order_id="rc-manual-review-1", gateway_order_id=920001
    )
    before_requests = len(stub.verify_requests)

    assert cli_main(["reconcile", "rc-manual-review-1", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "administrator already owns" in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert len(stub.verify_requests) == before_requests

    # Default inspection still works.
    assert cli_main(["reconcile", "rc-manual-review-1"]) == 0
    default_out = capsys.readouterr().out
    assert "manual_review" in default_out


def test_determine_verify_refusal_manual_review_takes_precedence_over_already_verified(
    session_factory, settings
):
    """Review-requested regression: status=manual_review with
    gateway_verified_at != NULL must return manual_review_owned, not
    already_gateway_verified -- a gateway-verified payment can legitimately
    sit in manual_review (e.g. a delivery-failure review that never touched
    the financial/verification facts), and the operationally important
    fact is that an administrator already owns the review."""
    with session_factory() as db:
        payment = Payment(
            bot_order_id="rc-precedence-unit",
            gateway_order_id=930002,
            gateway_user_id=DEFAULT_GATEWAY_USER_ID,
            amount=8000,
            payable_amount=8000,
            status=PaymentStatus.MANUAL_REVIEW.value,
            gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
            reference_id="REF-precedence-unit",
            manual_review_at=datetime.now(UTC),
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)

        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        payment, local = snapshot
        refusal = determine_verify_refusal(payment, local, confirm_aged_out=False)

    assert refusal == VerifyRefusal.MANUAL_REVIEW_OWNED


def test_reconcile_verify_manual_review_with_gateway_verified_refused_zero_network(
    cli_env, session_factory, stub, monkeypatch, capsys
):
    """End-to-end counterpart of the unit-level precedence test above: the
    CLI must report manual_review_owned (not already-verified) and make
    zero HTTP requests either way."""
    _patch_centralpay_client(monkeypatch, stub)
    with session_factory() as db:
        payment = Payment(
            bot_order_id="rc-manual-review-verified",
            gateway_order_id=930003,
            gateway_user_id=DEFAULT_GATEWAY_USER_ID,
            amount=8000,
            payable_amount=8000,
            status=PaymentStatus.MANUAL_REVIEW.value,
            gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
            reference_id="REF-manual-review-verified",
            manual_review_at=datetime.now(UTC),
            bot_notify_reason="retry_limit_reached",
        )
        db.add(payment)
        db.commit()
    before_requests = len(stub.verify_requests)

    assert cli_main(["reconcile", "rc-manual-review-verified", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "administrator already owns" in out
    assert "already denotes successful gateway verification" not in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert len(stub.verify_requests) == before_requests


# --- Codex P1 follow-up: refuse VERIFIED_STATUSES, not only gateway_verified_at
#
# The callback route's settlement handler (app.services.verification)
# suppresses re-verification when `payment.status in VERIFIED_STATUSES OR
# payment.gateway_verified_at is not None`. Only a database CHECK constraint
# (ck_payments_delivery_requires_verification) ties gateway_verified_at to
# the two bot-notify statuses -- gateway_verified carries no such constraint,
# so a gateway_verified row with gateway_verified_at still NULL is a
# database-valid state, and checking gateway_verified_at alone would have
# missed it. determine_verify_refusal must check `status in VERIFIED_STATUSES`
# itself, not rely on that constraint holding for every member of the set.


def _make_status_payment(
    session_factory,
    *,
    bot_order_id: str,
    gateway_order_id: int,
    status: str,
    gateway_verified_at: datetime | None = None,
    amount: int = 8000,
) -> Payment:
    with session_factory() as db:
        payment = Payment(
            bot_order_id=bot_order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=DEFAULT_GATEWAY_USER_ID,
            amount=amount,
            payable_amount=amount,
            status=status,
            gateway_verified_at=gateway_verified_at,
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment


def test_determine_verify_refusal_gateway_verified_status_with_null_timestamp(
    session_factory, settings
):
    """status=gateway_verified with gateway_verified_at still NULL (a
    database-valid row -- no CHECK constraint requires the timestamp for
    this status) must refuse as already_gateway_verified. Checking
    gateway_verified_at alone would miss this exact case."""
    payment = _make_status_payment(
        session_factory,
        bot_order_id="rc-p1-gv-null-ts",
        gateway_order_id=940001,
        status=PaymentStatus.GATEWAY_VERIFIED.value,
        gateway_verified_at=None,
    )

    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded_payment, local = snapshot
        refusal = determine_verify_refusal(loaded_payment, local, confirm_aged_out=False)

    assert refusal == VerifyRefusal.ALREADY_VERIFIED


def test_reconcile_verify_gateway_verified_status_null_timestamp_zero_network(
    cli_env, session_factory, stub, monkeypatch, capsys
):
    """End-to-end counterpart: the CLI must refuse and make zero HTTP
    requests for a gateway_verified payment whose gateway_verified_at is
    still NULL -- this is the exact regression the gateway_verified_at-only
    check missed."""
    _patch_centralpay_client(monkeypatch, stub)
    _make_status_payment(
        session_factory,
        bot_order_id="rc-p1-e2e-gateway-verified",
        gateway_order_id=940006,
        status=PaymentStatus.GATEWAY_VERIFIED.value,
        gateway_verified_at=None,
    )
    before_requests = len(stub.verify_requests)

    assert cli_main(["reconcile", "rc-p1-e2e-gateway-verified", "--verify", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["refused"] == "already_gateway_verified"
    assert payload["verify"]["performed"] is False
    assert len(stub.verify_requests) == before_requests
    # Codex follow-up: the local report must not contradict the refusal --
    # "gateway_verified: false" alongside "refused: already_gateway_verified"
    # would be self-contradictory. gateway_verified_at itself still reports
    # the real (NULL) timestamp so the anomaly remains visible.
    assert payload["local"]["gateway_verified"] is True
    assert payload["local"]["gateway_verified_at"] is None


@pytest.mark.parametrize(
    "status",
    [PaymentStatus.BOT_NOTIFY_PENDING.value, PaymentStatus.BOT_NOTIFY_ACCEPTED.value],
)
def test_reconcile_verify_bot_notify_statuses_refuse_before_gateway_request(
    cli_env, session_factory, stub, monkeypatch, capsys, status
):
    """status=bot_notify_pending / bot_notify_accepted must refuse via
    already_gateway_verified and make zero HTTP requests -- both are members
    of app.services.verification.VERIFIED_STATUSES, reused (not
    re-derived) by determine_verify_refusal."""
    _patch_centralpay_client(monkeypatch, stub)
    order_id = f"rc-p1-{status}"
    _make_status_payment(
        session_factory,
        bot_order_id=order_id,
        gateway_order_id=940002 if status == PaymentStatus.BOT_NOTIFY_PENDING.value else 940003,
        status=status,
        gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    before_requests = len(stub.verify_requests)

    assert cli_main(["reconcile", order_id, "--verify", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["refused"] == "already_gateway_verified"
    assert payload["verify"]["performed"] is False
    assert len(stub.verify_requests) == before_requests


def test_reconcile_verify_manual_review_precedence_holds_over_verified_status(
    session_factory, settings
):
    """manual_review must still take precedence over already_gateway_verified
    even when gateway_verified_at is also set -- restates the existing
    precedence guarantee for a status/timestamp combination this P1 fix
    touches, so the two checks are proven not to have regressed each other."""
    payment = _make_status_payment(
        session_factory,
        bot_order_id="rc-p1-manual-review-wins",
        gateway_order_id=940004,
        status=PaymentStatus.MANUAL_REVIEW.value,
        gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded_payment, local = snapshot
        refusal = determine_verify_refusal(loaded_payment, local, confirm_aged_out=False)

    assert refusal == VerifyRefusal.MANUAL_REVIEW_OWNED


def test_reconcile_verify_disabled_gate_refuses_first_for_verified_status_payment(
    cli_env, session_factory, monkeypatch, capsys
):
    """The CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED gate is checked before any
    payment-state refusal -- disabled must refuse with
    diagnostic_verify_not_enabled (never already_gateway_verified) and make
    zero HTTP requests, even for a payment already in a VERIFIED_STATUSES
    status, and without ever taking the verify row lock."""
    import app.cli as cli_module
    from app.services.reconcile_inspect import build_local_snapshot as real_build_local_snapshot

    settings = cli_env
    disabled = settings.model_copy(update={"centralpay_diagnostic_verify_enabled": False})
    monkeypatch.setattr(cli_module, "Settings", lambda: disabled)

    _make_status_payment(
        session_factory,
        bot_order_id="rc-p1-gate-first",
        gateway_order_id=940005,
        status=PaymentStatus.BOT_NOTIFY_PENDING.value,
        gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    for_update_flags: list[bool] = []

    def recording_build_local_snapshot(*args, **kwargs):
        for_update_flags.append(kwargs.get("for_update", False))
        return real_build_local_snapshot(*args, **kwargs)

    monkeypatch.setattr(cli_module, "build_local_snapshot", recording_build_local_snapshot)

    assert cli_main(["reconcile", "rc-p1-gate-first", "--verify", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["refused"] == "diagnostic_verify_not_enabled"
    assert for_update_flags == [False]  # never locked -- the gate short-circuits first


# --- scenario 10: aged-out safety -------------------------------------------


def test_reconcile_verify_aged_out_refused_without_confirm(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    assert create_order(client, settings, order_id="rc-aged-out-1", amount=5000).status_code == 200
    _age_payment(
        session_factory, "rc-aged-out-1", seconds=settings.reconciliation_max_age_seconds + 60
    )
    payment = get_payment(session_factory, "rc-aged-out-1")
    before = _snapshot(payment)

    assert cli_main(["reconcile", "rc-aged-out-1", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "aged out" in out
    assert "--confirm-aged-out" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    assert len(stub.verify_requests) == 0
    _assert_payment_unchanged(session_factory, "rc-aged-out-1", before)


def test_reconcile_verify_aged_out_with_confirm_makes_one_read_only_call(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    assert create_order(client, settings, order_id="rc-aged-out-2", amount=5000).status_code == 200
    _age_payment(
        session_factory, "rc-aged-out-2", seconds=settings.reconciliation_max_age_seconds + 60
    )
    payment = get_payment(session_factory, "rc-aged-out-2")
    before = _snapshot(payment)

    stub.verify_result = verify_ok_response(
        amount=5000, user_id=payment.gateway_user_id, reference_id="REF-aged-out-2"
    )

    assert (
        cli_main(["reconcile", "rc-aged-out-2", "--verify", "--confirm-aged-out"]) == 0
    )
    out = capsys.readouterr().out
    assert "WOULD_VERIFY" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    assert len(stub.verify_requests) == 1
    _assert_payment_unchanged(session_factory, "rc-aged-out-2", before)


def test_reconcile_reports_reconciliation_aged_out_separately_from_verify_gate(
    cli_env, session_factory, settings, capsys
):
    """Codex follow-up: reconciliation.aged_out must reflect only the
    reconciliation WORKER's own tier (age_bucket == 'aged_out', link_created
    rows only) -- a manual_review payment the worker never touches must
    report reconciliation.aged_out=False even though it is old and
    unverified, while the broader --verify safety-gate flag (verify_aged_out,
    reported at the top level, not nested under reconciliation) is True for
    it. Reporting the broad flag AS reconciliation.aged_out would
    contradict age_bucket=None for this exact payment."""
    _make_manual_review_payment(
        session_factory,
        bot_order_id="rc-aged-manual-review",
        gateway_order_id=940007,
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )

    assert cli_main(["reconcile", "rc-aged-manual-review", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    local = payload["local"]
    assert local["reconciliation"]["age_bucket"] is None  # not a link_created row
    assert local["reconciliation"]["aged_out"] is False  # worker never touches this payment
    assert local["verify_aged_out"] is True  # but --verify's broader gate would still refuse it


# --- final safety follow-up: --verify gated behind an off-by-default flag --
#
# settings.centralpay_diagnostic_verify_enabled defaults False in production
# (app.config) -- real CentralPay verify.php verify-after-verify/idempotency
# behavior has never been confirmed (STAGING_VALIDATION.md, blocker B2). The
# shared `settings` fixture sets it True so every OTHER --verify test in this
# file exercises the real flow; these tests build their own disabled copy.


def test_reconcile_verify_disabled_by_default_refuses_before_any_http_request(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    import app.cli as cli_module

    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-diag-disabled", amount=5000)
    assert response.status_code == 200

    disabled = settings.model_copy(update={"centralpay_diagnostic_verify_enabled": False})
    monkeypatch.setattr(cli_module, "Settings", lambda: disabled)

    requests_before = len(stub.verify_requests)
    assert cli_main(["reconcile", "rc-diag-disabled", "--verify", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"] == {
        "requested": True,
        "performed": False,
        "refused": "diagnostic_verify_not_enabled",
        "note": "NO LOCAL CHANGES WERE MADE.",
    }
    assert len(stub.verify_requests) == requests_before  # zero HTTP calls

    assert cli_main(["reconcile", "rc-diag-disabled", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "diagnostic gateway verification is disabled" in out
    assert "CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED=false" in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert len(stub.verify_requests) == requests_before


def test_reconcile_verify_disabled_confirm_aged_out_cannot_bypass_gate(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    import app.cli as cli_module

    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-diag-disabled-aged", amount=5000)
    assert response.status_code == 200
    _age_payment(
        session_factory,
        "rc-diag-disabled-aged",
        seconds=settings.reconciliation_max_age_seconds + 120,
    )

    disabled = settings.model_copy(update={"centralpay_diagnostic_verify_enabled": False})
    monkeypatch.setattr(cli_module, "Settings", lambda: disabled)

    assert (
        cli_main(
            ["reconcile", "rc-diag-disabled-aged", "--verify", "--confirm-aged-out", "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["refused"] == "diagnostic_verify_not_enabled"
    assert len(stub.verify_requests) == 0


def test_reconcile_default_inspection_unaffected_by_disabled_verify_gate(
    cli_env, client, settings, session_factory, monkeypatch, capsys
):
    """`centralpay reconcile ORDER_ID` (no --verify) must keep working
    identically whether or not diagnostic verify is enabled."""
    import app.cli as cli_module

    response = create_order(client, settings, order_id="rc-diag-disabled-default", amount=5000)
    assert response.status_code == 200

    disabled = settings.model_copy(update={"centralpay_diagnostic_verify_enabled": False})
    monkeypatch.setattr(cli_module, "Settings", lambda: disabled)

    assert cli_main(["reconcile", "rc-diag-disabled-default"]) == 0
    out = capsys.readouterr().out
    assert "rc-diag-disabled-default" in out
    assert "link_created" in out


def test_reconcile_verify_enabled_setting_preserves_full_verify_flow(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """Sanity check that the shared `settings` fixture actually exercises
    the real --verify flow when the gate is enabled -- i.e. the gate is not
    accidentally always-refusing."""
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-diag-enabled", amount=5000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-diag-enabled")
    stub.verify_result = verify_ok_response(
        amount=5000, user_id=payment.gateway_user_id, reference_id="REF-diag-enabled"
    )

    assert settings.centralpay_diagnostic_verify_enabled is True
    assert cli_main(["reconcile", "rc-diag-enabled", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_VERIFY" in out
    assert len(stub.verify_requests) == 1


# --- PR #59 follow-up: --verify row-lock discipline (real-Postgres races --
# live in tests/integration/test_reconcile_inspect_pg.py; these are the fast,
# deterministic SQLite-backed proofs of the same contract) --------------------


def test_reconcile_default_never_locks_verify_always_locks(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """Default inspection must never request a row lock; --verify always
    must -- the exact `for_update` flag app.cli._cmd_reconcile passes to
    build_local_snapshot for each mode."""
    import app.cli as cli_module
    from app.services.reconcile_inspect import build_local_snapshot as real_build_local_snapshot

    assert create_order(client, settings, order_id="rc-lock-flag", amount=5000).status_code == 200

    calls: list[bool] = []

    def spy(db, settings_arg, payment_id, *, now, for_update=False):
        calls.append(for_update)
        return real_build_local_snapshot(
            db, settings_arg, payment_id, now=now, for_update=for_update
        )

    monkeypatch.setattr(cli_module, "build_local_snapshot", spy)

    assert cli_main(["reconcile", "rc-lock-flag"]) == 0
    capsys.readouterr()

    _patch_centralpay_client(monkeypatch, stub)
    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid yet"})
    assert cli_main(["reconcile", "rc-lock-flag", "--verify"]) == 0
    capsys.readouterr()

    assert calls == [False, True]


def test_reconcile_verify_captures_now_after_lock_acquired_not_before(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """PR #59 final safety follow-up: `now` must be captured AFTER the row
    lock is acquired, not before -- otherwise a payment that crosses
    RECONCILIATION_MAX_AGE_SECONDS while --verify waits behind a concurrent
    transaction's lock could pass the aged-out gate using a stale, pre-wait
    timestamp. This proves the CODE ORDER: the lock-acquiring probe SELECT
    executes, THEN datetime.now() is captured, THEN the locked
    classification SELECT (reusing that same now) executes -- never
    datetime.now() before the first lock-acquiring execute. SQLite's single
    shared connection cannot simulate a REAL blocking wait, so this is a
    structural proof, not a timing one -- see
    test_reconcile_verify_recomputes_aged_out_gate_using_time_after_lock_wait_pg
    in tests/integration/test_reconcile_inspect_pg.py for the real
    concurrent-transaction, blocking-wait version."""
    import app.cli as cli_module

    response = create_order(client, settings, order_id="rc-post-lock-time", amount=5000)
    assert response.status_code == 200

    events: list[str] = []
    real_execute = Session.execute

    def recording_execute(self, statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            events.append("lock_execute")
        return real_execute(self, statement, *args, **kwargs)

    class _RecordingClock:
        @staticmethod
        def now(tz=None):
            events.append("now")
            return datetime.now(tz)

    monkeypatch.setattr(Session, "execute", recording_execute)
    monkeypatch.setattr(cli_module, "datetime", _RecordingClock)

    _patch_centralpay_client(monkeypatch, stub)
    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid yet"})

    assert cli_main(["reconcile", "rc-post-lock-time", "--verify"]) == 0
    capsys.readouterr()

    lock_indexes = [i for i, e in enumerate(events) if e == "lock_execute"]
    now_indexes = [i for i, e in enumerate(events) if e == "now"]
    # The minimal lock-probe SELECT, then now(), then the locked
    # classification SELECT reusing that same now -- in that exact order.
    assert len(lock_indexes) == 2
    assert len(now_indexes) == 1
    assert lock_indexes[0] < now_indexes[0] < lock_indexes[1]


def test_reconcile_verify_rereads_under_lock_after_race_before_lock_acquired(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """Deterministic simulation of required race scenario A: a settlement
    that lands AFTER the CLI's initial non-locking lookup but BEFORE it
    reloads the row under the --verify lock must be visible to that reload
    -- --verify refuses using the FRESH state and makes zero gateway calls
    of its own. (The real concurrent-transaction version of this proof,
    using an actual held FOR UPDATE lock on PostgreSQL, is
    test_reconcile_verify_reloads_under_lock_after_concurrent_settlement in
    tests/integration/test_reconcile_inspect_pg.py.)"""
    _patch_centralpay_client(monkeypatch, stub)
    assert create_order(client, settings, order_id="rc-race-reload", amount=5000).status_code == 200

    import app.cli as cli_module

    original_find_payment = cli_module._find_payment
    raced = {"done": False}

    def racing_find_payment(db, order_id):
        found = original_find_payment(db, order_id)
        if found is not None and not raced["done"]:
            raced["done"] = True
            # A concurrent settlement landing in the window between this
            # non-locking lookup and the CLI's FOR UPDATE reload.
            with session_factory() as settle_db:
                row = settle_db.execute(
                    select(Payment).where(Payment.id == found.id)
                ).scalar_one()
                row.status = PaymentStatus.BOT_NOTIFY_PENDING.value
                row.gateway_verified_at = datetime.now(UTC)
                row.reference_id = "REF-raced-settlement"
                settle_db.commit()
        return found

    monkeypatch.setattr(cli_module, "_find_payment", racing_find_payment)

    assert cli_main(["reconcile", "rc-race-reload", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "already denotes successful gateway verification" in out
    assert "NO LOCAL CHANGES WERE MADE." in out
    assert len(stub.verify_requests) == 0  # zero calls from the CLI itself

    refetched = get_payment(session_factory, "rc-race-reload")
    assert refetched.reference_id == "REF-raced-settlement"  # the race's write stands


# --- verify: transport/protocol failure -------------------------------------


def test_reconcile_verify_transport_error_zero_mutation(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-transport-error", amount=5000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rc-transport-error")
    before = _snapshot(payment)

    stub.verify_result = httpx.ConnectError("boom")

    assert cli_main(["reconcile", "rc-transport-error", "--verify"]) == 1
    out = capsys.readouterr().out
    assert "gateway call failed" in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    _assert_payment_unchanged(session_factory, "rc-transport-error", before)


# --- Codex follow-up: a failed verify attempt is not necessarily "unperformed"


def test_reconcile_verify_connection_error_reports_delivery_uncertain_not_false(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """A connection-level failure (ConnectError, timeout, ...) cannot be
    told apart from "sent, then the response never arrived" -- httpx gives
    us no way to know whether bytes reached the gateway. Reporting
    `performed: false` here would tell a machine consumer no request was
    made, when it may well have been -- report it as uncertain instead."""
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-delivery-uncertain", amount=5000)
    assert response.status_code == 200

    stub.verify_result = httpx.ConnectError("boom")

    assert cli_main(["reconcile", "rc-delivery-uncertain", "--verify", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["requested"] is True
    assert payload["verify"]["performed"] is None
    assert payload["verify"]["delivery_uncertain"] is True
    assert payload["verify"]["refused"] is None
    assert payload["verify"]["transport_error_code"] == "centralpay_connection_error"

    assert cli_main(["reconcile", "rc-delivery-uncertain", "--verify"]) == 1
    out = capsys.readouterr().out
    assert "delivery uncertain" in out


def test_reconcile_verify_non_200_status_reports_performed_true(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """A non-200 HTTP status PROVES the request was transmitted and
    answered -- this must never be reported as `performed: false`."""
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-rejected-status", amount=5000)
    assert response.status_code == 200

    stub.verify_result = httpx.Response(500, text="internal error")

    assert cli_main(["reconcile", "rc-rejected-status", "--verify", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["requested"] is True
    assert payload["verify"]["performed"] is True
    assert payload["verify"]["delivery_uncertain"] is False
    assert payload["verify"]["transport_error_code"] == "centralpay_rejected"

    assert cli_main(["reconcile", "rc-rejected-status", "--verify"]) == 1
    out = capsys.readouterr().out
    assert "request reached gateway: yes" in out


def test_reconcile_verify_malformed_json_reports_performed_true(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """A 200 response with an unparseable body also PROVES the request was
    transmitted and answered."""
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-invalid-body", amount=5000)
    assert response.status_code == 200

    stub.verify_result = httpx.Response(200, text="not json at all")

    assert cli_main(["reconcile", "rc-invalid-body", "--verify", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["performed"] is True
    assert payload["verify"]["delivery_uncertain"] is False
    assert payload["verify"]["transport_error_code"] == "centralpay_invalid_response"


# --- scenario 13: no secret / raw body / card / raw gateway user id leak ----


def test_reconcile_output_never_leaks_secrets_or_card_or_raw_user_id(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    assert create_order(client, settings, order_id="rc-leak-check", amount=12000).status_code == 200
    payment = get_payment(session_factory, "rc-leak-check")

    raw_card_number = "6037991234567890"
    stub.verify_result = verify_ok_response(
        amount=12000,
        user_id=payment.gateway_user_id,
        reference_id="REF-leak-check",
        card_number=raw_card_number,
    )

    all_output = []
    assert cli_main(["reconcile", "rc-leak-check"]) == 0
    all_output.append(capsys.readouterr().out)
    assert cli_main(["reconcile", "rc-leak-check", "--json"]) == 0
    all_output.append(capsys.readouterr().out)
    assert cli_main(["reconcile", "rc-leak-check", "--verify"]) == 0
    all_output.append(capsys.readouterr().out)
    assert cli_main(["reconcile", "rc-leak-check", "--verify", "--json"]) == 0
    all_output.append(capsys.readouterr().out)

    combined = "\n".join(all_output)
    for secret in _ALL_SECRETS:
        assert secret not in combined
    assert raw_card_number not in combined
    assert raw_card_number[-4:] not in combined
    assert str(payment.gateway_user_id) not in combined
    assert payment.callback_token_hash is None or payment.callback_token_hash not in combined
    assert settings.centralpay_base_url not in combined  # no raw gateway URLs either


def test_reconcile_output_never_leaks_gateway_free_text(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """Even if a compromised/misbehaving gateway sends arbitrary free text,
    app.centralpay already classifies it into a fixed reason-code vocabulary
    before reconcile ever sees it -- this is a defensive regression test for
    that boundary, exercised through the reconcile command."""
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rc-leak-freetext", amount=5000)
    assert response.status_code == 200

    sentinel = "TOTALLY-RAW-GATEWAY-TEXT-SENTINEL-9f81c2"
    stub.verify_result = httpx.Response(
        200, json={"status": "error", "message": sentinel, "reason": sentinel}
    )

    assert cli_main(["reconcile", "rc-leak-freetext", "--verify"]) == 0
    out = capsys.readouterr().out
    assert sentinel not in out


# --- hard regression: reconcile never touches the mutating settlement path -


def test_reconcile_code_never_references_mutating_settlement_functions():
    """Static guard: neither app.cli's reconcile wiring nor
    app.services.reconcile_inspect may even mention verify_and_settle,
    process_callback, or run_reconciliation_pass -- not as an import, not as
    a call. Catches any future `from ... import X` as well as a bare call."""
    import app.cli as cli_module
    import app.services.reconcile_inspect as reconcile_inspect_module

    forbidden = ("verify_and_settle", "process_callback", "run_reconciliation_pass")
    cli_source = inspect.getsource(cli_module)
    inspect_source = inspect.getsource(reconcile_inspect_module)
    for name in forbidden:
        assert name not in cli_source, f"app.cli must never reference {name}"
        assert name not in inspect_source, f"reconcile_inspect must never reference {name}"


def test_reconcile_dynamic_never_invokes_mutating_settlement_functions(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    """Dynamic guard: even if verify_and_settle / process_callback /
    run_reconciliation_pass are made to explode, `reconcile --verify` must
    complete normally -- proving the reconcile code path never calls them."""
    import app.services.reconciliation as reconciliation_module
    import app.services.verification as verification_module

    def _boom(*args, **kwargs):
        raise AssertionError("mutating settlement path must never be called by `reconcile`")

    monkeypatch.setattr(verification_module, "verify_and_settle", _boom)
    monkeypatch.setattr(verification_module, "process_callback", _boom)
    monkeypatch.setattr(reconciliation_module, "run_reconciliation_pass", _boom)
    _patch_centralpay_client(monkeypatch, stub)

    assert create_order(client, settings, order_id="rc-guard-1", amount=5000).status_code == 200
    payment = get_payment(session_factory, "rc-guard-1")
    stub.verify_result = verify_ok_response(
        amount=5000, user_id=payment.gateway_user_id, reference_id="REF-guard-1"
    )

    assert cli_main(["reconcile", "rc-guard-1"]) == 0
    capsys.readouterr()
    assert cli_main(["reconcile", "rc-guard-1", "--verify"]) == 0
    out = capsys.readouterr().out
    assert "WOULD_VERIFY" in out
