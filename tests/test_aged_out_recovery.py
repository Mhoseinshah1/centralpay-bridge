"""app.services.aged_out_recovery / `centralpay recover-aged-out ORDER_ID`.

Safety contract under test: default (preview) mode NEVER makes a network
call, NEVER writes to the database, and NEVER takes a row lock. `--confirm`
is the ONLY mutating path: it locks the row, reloads and re-checks
eligibility under that lock, and -- only if still eligible -- calls the
canonical `app.services.verification.verify_and_settle()` EXACTLY ONCE. This
module never duplicates any financial check and never re-implements ORDER_ID
resolution (both are reused, not copied, from `app.cli`).
"""

import argparse
import inspect
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.centralpay import CentralPayClient
from app.cli import build_parser
from app.cli import main as cli_main
from app.models import Payment, PaymentStatus
from app.services.aged_out_recovery import RecoveryRefusal, determine_recovery_refusal
from app.services.reconcile_inspect import build_local_snapshot
from app.services.verification import VERIFIED_STATUSES
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
    "next_retry_at",
    "manual_review_at",
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


def _age_payment(session_factory, bot_order_id: str, *, seconds: float) -> None:
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


def _make_status_payment(
    session_factory,
    *,
    bot_order_id: str,
    gateway_order_id: int,
    status: str,
    gateway_verified_at: datetime | None = None,
    amount: int = 8000,
    age_seconds: float = 60,
    reconciliation_attempts: int = 0,
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
            callback_token_issued_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
            reconciliation_attempts=reconciliation_attempts,
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment


def _make_manual_review_payment(
    session_factory,
    *,
    bot_order_id: str,
    gateway_order_id: int,
    amount: int = 8000,
    age_seconds: float = 60,
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


def _make_aged_out_payment(
    session_factory, settings, *, bot_order_id: str, gateway_order_id: int
) -> Payment:
    return _make_status_payment(
        session_factory,
        bot_order_id=bot_order_id,
        gateway_order_id=gateway_order_id,
        status=PaymentStatus.LINK_CREATED.value,
        gateway_verified_at=None,
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )


@pytest.fixture
def cli_env(settings, session_factory, monkeypatch):
    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "Settings", lambda: settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: session_factory)
    return settings


# --- argparse wiring ---------------------------------------------------------


def test_recover_aged_out_parser_defaults():
    args = build_parser().parse_args(["recover-aged-out", "abc"])
    assert args.command == "recover-aged-out"
    assert args.order_id == "abc"
    assert args.confirm is False
    assert args.as_json is False


def test_recover_aged_out_parser_confirm_and_json_flags():
    args = build_parser().parse_args(["recover-aged-out", "abc", "--confirm", "--json"])
    assert args.confirm is True
    assert args.as_json is True


# --- ORDER_ID resolution: not found / ambiguous -----------------------------


def test_recover_aged_out_payment_not_found(cli_env, capsys):
    assert cli_main(["recover-aged-out", "does-not-exist"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "payment_not_found", "order_id": "does-not-exist"}


def test_recover_aged_out_confirm_payment_not_found(cli_env, capsys):
    assert cli_main(["recover-aged-out", "does-not-exist", "--confirm"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "payment_not_found", "order_id": "does-not-exist"}


def test_recover_aged_out_refuses_ambiguous_order_id(cli_env, session_factory, capsys):
    _make_minimal_payment(session_factory, bot_order_id="778899011122", gateway_order_id=100301)
    _make_minimal_payment(
        session_factory, bot_order_id="rec-ambiguous-other", gateway_order_id=778899011122
    )

    assert cli_main(["recover-aged-out", "778899011122"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "ambiguous_order_id", "order_id": "778899011122"}


def test_recover_aged_out_confirm_refuses_ambiguous_order_id_zero_http(
    cli_env, session_factory, stub, monkeypatch, capsys
):
    """The ambiguity refusal happens before ANY lock or gateway call --
    including for --confirm."""
    _patch_centralpay_client(monkeypatch, stub)
    _make_minimal_payment(session_factory, bot_order_id="778899022233", gateway_order_id=100302)
    _make_minimal_payment(
        session_factory, bot_order_id="rec-ambiguous-confirm-other", gateway_order_id=778899022233
    )

    assert cli_main(["recover-aged-out", "778899022233", "--confirm"]) == 1
    out = capsys.readouterr().out
    assert "ambiguous_order_id" in out
    assert len(stub.verify_requests) == 0


# --- eligibility matrix (determine_recovery_refusal) ------------------------


def test_determine_recovery_refusal_eligible_when_link_created_and_aged_out(
    session_factory, settings
):
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-eligible-unit", gateway_order_id=200001
    )
    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded, local = snapshot
        assert determine_recovery_refusal(loaded, local) is None


def test_determine_recovery_refusal_manual_review_takes_precedence(session_factory, settings):
    """manual_review wins even when gateway_verified_at is ALSO set -- an
    administrator already owns the review either way."""
    payment = _make_status_payment(
        session_factory,
        bot_order_id="rec-manual-precedence-unit",
        gateway_order_id=200002,
        status=PaymentStatus.MANUAL_REVIEW.value,
        gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )
    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded, local = snapshot
        assert determine_recovery_refusal(loaded, local) == RecoveryRefusal.MANUAL_REVIEW_OWNED


@pytest.mark.parametrize("status", sorted(VERIFIED_STATUSES))
def test_determine_recovery_refusal_already_verified_statuses(session_factory, settings, status):
    payment = _make_status_payment(
        session_factory,
        bot_order_id=f"rec-verified-{status}",
        gateway_order_id=200003 if status == PaymentStatus.GATEWAY_VERIFIED.value else 200004,
        status=status,
        gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )
    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded, local = snapshot
        assert determine_recovery_refusal(loaded, local) == RecoveryRefusal.ALREADY_VERIFIED


def test_determine_recovery_refusal_gateway_verified_status_null_timestamp(
    session_factory, settings
):
    """status=gateway_verified with gateway_verified_at still NULL is a
    database-valid row (no CHECK constraint requires the timestamp for this
    status); must still refuse as already-verified."""
    payment = _make_status_payment(
        session_factory,
        bot_order_id="rec-gv-null-ts-unit",
        gateway_order_id=200005,
        status=PaymentStatus.GATEWAY_VERIFIED.value,
        gateway_verified_at=None,
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )
    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded, local = snapshot
        assert determine_recovery_refusal(loaded, local) == RecoveryRefusal.ALREADY_VERIFIED


@pytest.mark.parametrize(
    "status", [PaymentStatus.CREATED.value, PaymentStatus.GETLINK_FAILED.value]
)
def test_determine_recovery_refusal_not_link_created(session_factory, settings, status):
    payment = _make_status_payment(
        session_factory,
        bot_order_id=f"rec-not-link-created-{status}",
        gateway_order_id=200006 if status == PaymentStatus.CREATED.value else 200007,
        status=status,
        gateway_verified_at=None,
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )
    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded, local = snapshot
        assert determine_recovery_refusal(loaded, local) == RecoveryRefusal.NOT_LINK_CREATED


def test_determine_recovery_refusal_link_created_not_aged_out(session_factory, settings):
    payment = _make_status_payment(
        session_factory,
        bot_order_id="rec-not-aged-out-unit",
        gateway_order_id=200008,
        status=PaymentStatus.LINK_CREATED.value,
        gateway_verified_at=None,
        age_seconds=60,
    )
    with session_factory() as db:
        snapshot = build_local_snapshot(db, settings, payment.id, now=datetime.now(UTC))
        assert snapshot is not None
        loaded, local = snapshot
        assert determine_recovery_refusal(loaded, local) == RecoveryRefusal.NOT_AGED_OUT


# --- preview (default): zero HTTP / writes / lock / events ------------------


def test_recover_aged_out_preview_eligible_zero_network_zero_writes_zero_events(
    cli_env, session_factory, settings, stub, capsys
):
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-preview-eligible", gateway_order_id=210001
    )
    events_before = len(get_events(session_factory, payment.id))
    before = _snapshot(payment)

    assert cli_main(["recover-aged-out", "rec-preview-eligible"]) == 0
    out = capsys.readouterr().out
    assert "🛟 Aged-out recovery: rec-preview-eligible" in out
    assert "aged out:                yes" in out
    assert "eligible:                yes" in out
    assert "PREVIEW ONLY. Pass --confirm to attempt recovery." in out
    assert "NO LOCAL CHANGES WERE MADE." in out

    assert len(stub.verify_requests) == 0
    assert len(get_events(session_factory, payment.id)) == events_before
    _assert_payment_unchanged(session_factory, "rec-preview-eligible", before)


def test_recover_aged_out_preview_json_shape(cli_env, session_factory, settings, capsys):
    _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-preview-json", gateway_order_id=210002
    )

    assert cli_main(["recover-aged-out", "rec-preview-json", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preview"] is True
    assert payload["confirm_requested"] is False
    assert payload["bot_order_id"] == "rec-preview-json"
    assert payload["status"] == "link_created"
    assert payload["gateway_verified"] is False
    assert payload["aged_out"] is True
    assert payload["eligible"] is True
    assert payload["refusal_reason"] is None
    assert payload["reconciliation_max_age_seconds"] == settings.reconciliation_max_age_seconds
    assert payload["reconciliation_max_attempts"] == settings.reconciliation_max_attempts
    assert "re-evaluate current eligibility" in payload["confirm_would"]
    assert payload["note"] == "NO LOCAL CHANGES WERE MADE."


def test_recover_aged_out_confirm_would_text_identical_regardless_of_current_eligibility(
    cli_env, client, settings, session_factory, capsys
):
    """Item G: eligibility is re-evaluated fresh under the row lock at
    --confirm time, so a preview refusal is NOT a guarantee about what a
    LATER --confirm will find (the payment may age out, become verified,
    enter manual_review, or otherwise change state in between). The
    "--confirm would" text must therefore say the SAME thing regardless of
    the payment's current eligibility -- never "refuse for the same reason
    as above" -- and must explicitly acknowledge re-evaluation."""
    _make_aged_out_payment(
        session_factory,
        settings,
        bot_order_id="rec-confirm-would-eligible",
        gateway_order_id=210010,
    )
    response = create_order(
        client, settings, order_id="rec-confirm-would-ineligible", amount=5000
    )
    assert response.status_code == 200

    assert cli_main(["recover-aged-out", "rec-confirm-would-eligible", "--json"]) == 0
    eligible_payload = json.loads(capsys.readouterr().out)
    assert eligible_payload["eligible"] is True

    assert cli_main(["recover-aged-out", "rec-confirm-would-ineligible", "--json"]) == 0
    ineligible_payload = json.loads(capsys.readouterr().out)
    assert ineligible_payload["eligible"] is False

    assert eligible_payload["confirm_would"] == ineligible_payload["confirm_would"]
    assert "re-evaluate current eligibility" in eligible_payload["confirm_would"]
    assert "same reason" not in eligible_payload["confirm_would"]

    assert cli_main(["recover-aged-out", "rec-confirm-would-ineligible"]) == 0
    out = capsys.readouterr().out
    assert "same reason as above" not in out
    assert "re-evaluate current eligibility" in out


def test_recover_aged_out_preview_not_aged_out_shows_refusal(
    cli_env, client, settings, session_factory, stub, capsys
):
    response = create_order(client, settings, order_id="rec-preview-fresh", amount=5000)
    assert response.status_code == 200

    assert cli_main(["recover-aged-out", "rec-preview-fresh", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["eligible"] is False
    assert payload["refusal_reason"] == "not_aged_out"
    assert len(stub.verify_requests) == 0

    assert cli_main(["recover-aged-out", "rec-preview-fresh"]) == 0
    out = capsys.readouterr().out
    assert "eligible:                no" in out
    assert "refusal reason:          not_aged_out" in out
    assert "has not aged out" in out


def test_recover_aged_out_not_aged_out_message_never_claims_reconciliation_will_handle_it(
    cli_env, client, settings, session_factory, capsys
):
    """Item F: RECONCILIATION_ENABLED can be false, attempts may already be
    at the cap, or scheduling may otherwise make the payment non-runnable
    -- so the not-aged-out refusal must never assert that automatic
    reconciliation "is still (or will be) handling it". It must only make
    the narrow, always-true claim that this command applies past the
    max-age boundary."""
    response = create_order(client, settings, order_id="rec-not-aged-out-msg", amount=5000)
    assert response.status_code == 200

    assert cli_main(["recover-aged-out", "rec-not-aged-out-msg"]) == 0
    out = capsys.readouterr().out
    assert "still (or will be) handling it" not in out
    assert "Automatic reconciliation is" not in out
    assert "This recovery command only applies after the max-age boundary." in out
    assert "inspect the current reconciliation state" in out


def test_recover_aged_out_preview_manual_review_shows_refusal(cli_env, session_factory, capsys):
    _make_manual_review_payment(
        session_factory, bot_order_id="rec-preview-manual-review", gateway_order_id=210003
    )

    assert cli_main(["recover-aged-out", "rec-preview-manual-review", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refusal_reason"] == "manual_review_owned"


def test_recover_aged_out_preview_already_verified_shows_refusal(
    cli_env, session_factory, settings, capsys
):
    _make_status_payment(
        session_factory,
        bot_order_id="rec-preview-already-verified",
        gateway_order_id=210004,
        status=PaymentStatus.BOT_NOTIFY_PENDING.value,
        gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )

    assert cli_main(["recover-aged-out", "rec-preview-already-verified", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refusal_reason"] == "already_gateway_verified"


def test_recover_aged_out_preview_never_locks(
    cli_env, session_factory, settings, monkeypatch, capsys
):
    _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-preview-lock-flag", gateway_order_id=210005
    )

    for_update_flags: list[bool] = []
    real_build_local_snapshot = build_local_snapshot

    def spy(db, settings_arg, payment_id, *, now, for_update=False):
        for_update_flags.append(for_update)
        return real_build_local_snapshot(
            db, settings_arg, payment_id, now=now, for_update=for_update
        )

    import app.services.aged_out_recovery as recovery_module

    monkeypatch.setattr(recovery_module, "build_local_snapshot", spy)

    assert cli_main(["recover-aged-out", "rec-preview-lock-flag"]) == 0
    capsys.readouterr()
    assert for_update_flags == [False]


# --- --confirm: eligible, gateway success -----------------------------------


def test_recover_aged_out_confirm_eligible_verifies_and_settles(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-confirm-verified", gateway_order_id=220001
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount,
        user_id=payment.gateway_user_id,
        reference_id="REF-rec-confirm-verified",
    )

    assert cli_main(["recover-aged-out", "rec-confirm-verified", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: verified ---" in out
    assert "canonical settlement" in out

    assert len(stub.verify_requests) == 1
    refetched = get_payment(session_factory, "rec-confirm-verified")
    assert refetched.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert refetched.gateway_verified_at is not None
    assert refetched.reference_id == "REF-rec-confirm-verified"

    events = get_events(session_factory, payment.id)
    event_types = [e.event_type for e in events]
    assert "aged_out_recovery_requested" in event_types
    assert "aged_out_recovery_verified" in event_types
    assert "gateway_payment_verified" in event_types  # verify_and_settle's own event
    assert "bot_notification_queued" in event_types  # normal notification queueing


def test_recover_aged_out_confirm_success_output_labels_snapshot_as_pre_attempt(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    """Item A: a successful --confirm's human output must clearly label
    the returned snapshot as PRE-ATTEMPT state -- captured under the lock
    BEFORE verify_and_settle ran -- and must never present
    status=link_created / gateway_verified=no as though they were the
    payment's current, post-settlement facts sitting next to
    outcome=verified. The post-settlement facts (BOT_NOTIFY_PENDING,
    gateway_verified_at set) are asserted directly against the database
    instead."""
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory,
        settings,
        bot_order_id="rec-confirm-pre-attempt-label",
        gateway_order_id=220010,
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount,
        user_id=payment.gateway_user_id,
        reference_id="REF-rec-confirm-pre-attempt-label",
    )

    assert cli_main(["recover-aged-out", "rec-confirm-pre-attempt-label", "--confirm"]) == 0
    out = capsys.readouterr().out

    # Explicitly labeled as pre-attempt, never as "current".
    assert "pre-attempt status:" in out
    assert "pre-attempt gateway_verified:" in out
    assert "current status:" not in out
    assert "  gateway_verified:        " not in out  # the unqualified preview-only label
    assert "eligible at attempt:     yes" in out

    # The pre-attempt facts reported are the ones that made recovery
    # eligible in the first place -- link_created / not yet verified --
    # which is correct as a PRE-ATTEMPT fact, not a claim about current
    # state.
    assert "pre-attempt status:      link_created" in out
    assert "pre-attempt gateway_verified: no" in out
    assert "--- --confirm: verified ---" in out

    # The database's actual current state is the opposite of the
    # pre-attempt snapshot -- proving the snapshot was never re-read or
    # conflated with post-settlement state.
    refetched = get_payment(session_factory, "rec-confirm-pre-attempt-label")
    assert refetched.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert refetched.gateway_verified_at is not None


def test_recover_aged_out_confirm_json_shape_verified(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-confirm-json-verified", gateway_order_id=220002
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount, user_id=payment.gateway_user_id, reference_id="REF-json-verified"
    )

    assert cli_main(["recover-aged-out", "rec-confirm-json-verified", "--confirm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preview"] is False
    assert payload["confirm_requested"] is True
    assert payload["outcome"] == "verified"
    assert payload["gateway_request_performed"] is True
    assert payload["delivery_uncertain"] is False
    assert payload["transport_error_code"] is None
    assert "status" not in payload  # never a flat, ambiguous "current" field
    assert "gateway_verified" not in payload
    # Pre-attempt snapshot (captured under the lock BEFORE verify_and_settle
    # ran) is explicitly nested and labeled -- never presented as the
    # payment's current/post-settlement state.
    pre_attempt = payload["pre_attempt"]
    assert pre_attempt["eligible"] is True
    assert pre_attempt["refusal_reason"] is None
    assert pre_attempt["status"] == "link_created"
    assert pre_attempt["gateway_verified"] is False
    assert pre_attempt["aged_out"] is True


def test_recover_aged_out_confirm_locks_row(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    """--confirm must always request the row lock (for_update=True), even
    when the gateway call itself is fast/deterministic -- distinguishing it
    from preview's for_update=False (see
    test_recover_aged_out_preview_never_locks)."""
    _patch_centralpay_client(monkeypatch, stub)
    real_build_local_snapshot = build_local_snapshot
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-confirm-lock-flag", gateway_order_id=220003
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount, user_id=payment.gateway_user_id, reference_id="REF-lock-flag"
    )

    for_update_flags: list[bool] = []

    def spy(db, settings_arg, payment_id, *, now, for_update=False):
        for_update_flags.append(for_update)
        return real_build_local_snapshot(
            db, settings_arg, payment_id, now=now, for_update=for_update
        )

    import app.services.aged_out_recovery as recovery_module

    monkeypatch.setattr(recovery_module, "build_local_snapshot", spy)

    assert cli_main(["recover-aged-out", "rec-confirm-lock-flag", "--confirm", "--json"]) == 0
    capsys.readouterr()
    assert for_update_flags == [True]


def test_recover_aged_out_confirm_configures_structured_logging_and_admin_alerts(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    """Codex follow-up: --confirm is the ONLY mutating command in app.cli,
    so -- like app.ops's own mutating commands (e.g. `review resend`) --
    it must configure structured/redacted logging and enable admin-alert
    creation before attempting settlement; otherwise a manual_review or
    verified outcome from a deliberate operator recovery would silently
    never alert administrators, and its audit events would never reach
    the structured log stream. Logging must be routed to STDERR (never
    the default stdout) so it can never interleave with and corrupt
    --json's single-object-on-stdout contract."""
    import sys as sys_module

    import app.cli as cli_module

    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory,
        settings,
        bot_order_id="rec-confirm-logging-alerts",
        gateway_order_id=220021,
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount, user_id=payment.gateway_user_id, reference_id="REF-logging-alerts"
    )

    logging_calls: list[dict[str, object]] = []
    alert_calls: list[object] = []

    def spy_configure_logging(settings_arg, *, stream=None):
        logging_calls.append({"settings": settings_arg, "stream": stream})

    def spy_configure_alert_creation(settings_arg):
        alert_calls.append(settings_arg)

    monkeypatch.setattr(cli_module, "configure_logging", spy_configure_logging)
    monkeypatch.setattr(cli_module, "configure_alert_creation", spy_configure_alert_creation)

    assert (
        cli_main(["recover-aged-out", "rec-confirm-logging-alerts", "--confirm", "--json"]) == 0
    )
    capsys.readouterr()

    assert len(logging_calls) == 1
    assert logging_calls[0]["settings"] is settings
    assert logging_calls[0]["stream"] is sys_module.stderr
    assert alert_calls == [settings]


def test_recover_aged_out_preview_never_configures_logging_or_alerts(
    cli_env, session_factory, settings, monkeypatch, capsys
):
    """The read-only preview path must never touch shared logging/alert
    configuration -- only --confirm, the one mutating path, needs it."""
    import app.cli as cli_module

    _make_aged_out_payment(
        session_factory,
        settings,
        bot_order_id="rec-preview-no-logging-alerts",
        gateway_order_id=220022,
    )

    calls: list[str] = []
    monkeypatch.setattr(cli_module, "configure_logging", lambda *a, **k: calls.append("logging"))
    monkeypatch.setattr(
        cli_module, "configure_alert_creation", lambda *a, **k: calls.append("alerts")
    )

    assert cli_main(["recover-aged-out", "rec-preview-no-logging-alerts"]) == 0
    capsys.readouterr()

    assert calls == []


# --- --confirm: gateway not paid --------------------------------------------


def test_recover_aged_out_confirm_gateway_not_paid_stays_unverified(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-confirm-not-paid", gateway_order_id=220004
    )
    stub.verify_result = httpx.Response(200, json={"status": "error", "message": "not paid yet"})

    assert cli_main(["recover-aged-out", "rec-confirm-not-paid", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: gateway_not_paid ---" in out
    assert "NOT paid" in out

    assert len(stub.verify_requests) == 1
    refetched = get_payment(session_factory, "rec-confirm-not-paid")
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None
    # Never re-inserted into automatic reconciliation polling.
    assert refetched.reconciliation_next_at is None
    assert refetched.reconciliation_attempts == 0

    events = get_events(session_factory, payment.id)
    event_types = [e.event_type for e in events]
    assert "aged_out_recovery_not_paid" in event_types
    assert "bot_notification_queued" not in event_types
    not_paid_event = next(e for e in events if e.event_type == "centralpay_verify_not_paid")
    assert not_paid_event.data is not None
    assert not_paid_event.data["source"] == "aged_out_recovery"


# --- --confirm: financial mismatch -> manual_review -------------------------


def test_recover_aged_out_confirm_amount_mismatch_manual_review(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-confirm-mismatch", gateway_order_id=220005
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount + 1, user_id=payment.gateway_user_id, reference_id="REF-mismatch"
    )

    assert cli_main(["recover-aged-out", "rec-confirm-mismatch", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: manual_review ---" in out

    refetched = get_payment(session_factory, "rec-confirm-mismatch")
    assert refetched.status == PaymentStatus.MANUAL_REVIEW.value
    assert refetched.gateway_verified_at is None

    events = get_events(session_factory, payment.id)
    event_types = [e.event_type for e in events]
    assert "aged_out_recovery_manual_review" in event_types
    assert "verify_payable_amount_mismatch" in event_types
    assert "bot_notification_queued" not in event_types


# --- --confirm: transport/protocol failure ----------------------------------


def test_recover_aged_out_confirm_connection_error_delivery_uncertain(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    """Item C: a connection-level failure (CentralPayConnectionError) can
    never be told apart from "sent, then the response was lost" -- httpx
    gives no way to know whether the request ever reached CentralPay.
    gateway_request_performed must be None (never False, never True), and
    delivery_uncertain must be True."""
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-confirm-transport", gateway_order_id=220006
    )
    stub.verify_result = httpx.ConnectError("boom")

    assert cli_main(["recover-aged-out", "rec-confirm-transport", "--confirm", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "transport_failed"
    assert payload["gateway_request_performed"] is None
    assert payload["delivery_uncertain"] is True
    assert payload["transport_error_code"] == "centralpay_connection_error"

    assert cli_main(["recover-aged-out", "rec-confirm-transport", "--confirm"]) == 1
    out = capsys.readouterr().out
    assert "--- --confirm: transport_failed ---" in out
    assert "error code:" in out
    assert "delivery uncertain:      the request may or may not have reached the gateway" in out
    assert "No local settlement was applied." in out
    # Never claims a gateway-side fact this command cannot prove.
    assert "request reached gateway: yes" not in out
    # Codex follow-up: must never casually suggest an immediate retry
    # without acknowledging CentralPay's own unconfirmed verify-after-verify
    # behavior for a request whose delivery is uncertain.
    assert "does not auto-retry" in out
    assert "never been confirmed safe" in out
    assert "STAGING_VALIDATION.md" in out

    refetched = get_payment(session_factory, "rec-confirm-transport")
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None

    events = get_events(session_factory, payment.id)
    event_types = [e.event_type for e in events]
    assert "aged_out_recovery_transport_failed" in event_types
    assert "bot_notification_queued" not in event_types


def test_recover_aged_out_confirm_non_connection_transport_error_request_proven_performed(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    """Item D: a non-connection CentralPayError (here: a non-200 HTTP
    status, which maps to CentralPayRejectedError) PROVES the request was
    transmitted and answered -- gateway_request_performed must be True and
    delivery_uncertain must be False, never the connection-level
    "uncertain" state."""
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory,
        settings,
        bot_order_id="rec-confirm-rejected-status",
        gateway_order_id=220007,
    )
    stub.verify_result = httpx.Response(500, text="internal error")

    assert cli_main(["recover-aged-out", "rec-confirm-rejected-status", "--confirm", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "transport_failed"
    assert payload["gateway_request_performed"] is True
    assert payload["delivery_uncertain"] is False
    assert payload["transport_error_code"] == "centralpay_rejected"

    assert cli_main(["recover-aged-out", "rec-confirm-rejected-status", "--confirm"]) == 1
    out = capsys.readouterr().out
    assert "request reached gateway: yes (response could not be used)" in out
    assert "delivery uncertain" not in out
    assert "No local settlement was applied." in out
    # Codex follow-up: an unusable response (HTTP 500 / malformed body)
    # proves only that SOME peer answered -- never that CentralPay's own
    # processing of this verify request did not already succeed before
    # the failure occurred. That is the SAME verify-after-verify ambiguity
    # a connection-level failure carries, so the safety caveat applies
    # here too, not only to the delivery-uncertain case.
    assert "does not auto-retry" in out
    assert "never been confirmed safe" in out
    assert "STAGING_VALIDATION.md" in out

    refetched = get_payment(session_factory, "rec-confirm-rejected-status")
    assert refetched.status == PaymentStatus.LINK_CREATED.value
    assert refetched.gateway_verified_at is None

    events = get_events(session_factory, payment.id)
    assert "aged_out_recovery_transport_failed" in [e.event_type for e in events]


# --- --confirm: refused, zero HTTP -------------------------------------------


def test_recover_aged_out_confirm_refused_not_aged_out_zero_http(
    cli_env, client, settings, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    response = create_order(client, settings, order_id="rec-confirm-fresh", amount=5000)
    assert response.status_code == 200
    payment = get_payment(session_factory, "rec-confirm-fresh")
    before = _snapshot(payment)

    assert cli_main(["recover-aged-out", "rec-confirm-fresh", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: refused ---" in out
    assert "has not aged out" in out
    assert "Zero gateway requests were made." in out

    assert len(stub.verify_requests) == 0
    # Only the lock-holding transaction's own audit events change; the
    # financial/status/reconciliation fields are untouched.
    _assert_payment_unchanged(session_factory, "rec-confirm-fresh", before)


def test_recover_aged_out_confirm_refused_manual_review_zero_http(
    cli_env, session_factory, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    _make_manual_review_payment(
        session_factory, bot_order_id="rec-confirm-manual-review", gateway_order_id=230001
    )

    assert cli_main(["recover-aged-out", "rec-confirm-manual-review", "--confirm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "refused"
    assert payload["pre_attempt"]["refusal_reason"] == "manual_review_owned"
    assert payload["gateway_request_performed"] is False
    assert payload["delivery_uncertain"] is False
    assert len(stub.verify_requests) == 0


def test_recover_aged_out_confirm_refused_already_verified_zero_http(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    _make_status_payment(
        session_factory,
        bot_order_id="rec-confirm-already-verified",
        gateway_order_id=230002,
        status=PaymentStatus.BOT_NOTIFY_PENDING.value,
        gateway_verified_at=datetime.now(UTC) - timedelta(minutes=5),
        age_seconds=settings.reconciliation_max_age_seconds + 120,
    )

    assert (
        cli_main(["recover-aged-out", "rec-confirm-already-verified", "--confirm", "--json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["pre_attempt"]["refusal_reason"] == "already_gateway_verified"
    assert payload["gateway_request_performed"] is False
    assert payload["delivery_uncertain"] is False
    assert len(stub.verify_requests) == 0


def test_recover_aged_out_confirm_refused_records_requested_and_refused_events(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_status_payment(
        session_factory,
        bot_order_id="rec-confirm-refused-events",
        gateway_order_id=230003,
        status=PaymentStatus.LINK_CREATED.value,
        gateway_verified_at=None,
        age_seconds=60,  # not aged out
    )

    assert cli_main(["recover-aged-out", "rec-confirm-refused-events", "--confirm"]) == 0
    capsys.readouterr()

    events = get_events(session_factory, payment.id)
    event_types = [e.event_type for e in events]
    assert event_types == ["aged_out_recovery_requested", "aged_out_recovery_refused"]
    refused_event = events[-1]
    assert refused_event.data is not None
    assert refused_event.data["reason"] == "not_aged_out"
    assert refused_event.level == "warning"


# --- race: reload under lock sees a settlement landed between lookup+lock --


def test_recover_aged_out_confirm_rereads_under_lock_after_race_before_lock_acquired(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    """A settlement that lands AFTER the CLI's non-locking order-id lookup
    but BEFORE the recovery's row lock is acquired must be visible to the
    reload -- recovery refuses using the fresh state and makes zero
    gateway calls of its own. (Real-PostgreSQL, genuinely-blocking-lock
    version: tests/integration/test_aged_out_recovery_pg.py.)"""
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-race-reload", gateway_order_id=240001
    )

    import app.cli as cli_module

    original_find_payment = cli_module._find_payment
    raced = {"done": False}

    def racing_find_payment(db, order_id):
        found = original_find_payment(db, order_id)
        if found is not None and not raced["done"]:
            raced["done"] = True
            with session_factory() as settle_db:
                row = settle_db.execute(select(Payment).where(Payment.id == found.id)).scalar_one()
                row.status = PaymentStatus.BOT_NOTIFY_PENDING.value
                row.gateway_verified_at = datetime.now(UTC)
                row.reference_id = "REF-raced-settlement"
                settle_db.commit()
        return found

    monkeypatch.setattr(cli_module, "_find_payment", racing_find_payment)

    assert cli_main(["recover-aged-out", "rec-race-reload", "--confirm", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "refused"
    assert payload["pre_attempt"]["refusal_reason"] == "already_gateway_verified"
    assert len(stub.verify_requests) == 0  # zero calls from recovery itself

    refetched = get_payment(session_factory, "rec-race-reload")
    assert refetched.reference_id == "REF-raced-settlement"  # the race's write stands
    assert payment.id == refetched.id


# --- secrets never leak -----------------------------------------------------


def test_recover_aged_out_output_never_leaks_secrets_or_card_or_raw_user_id(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    _patch_centralpay_client(monkeypatch, stub)
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-leak-check", gateway_order_id=250001
    )
    raw_card_number = "6037991234567890"
    stub.verify_result = verify_ok_response(
        amount=payment.amount,
        user_id=payment.gateway_user_id,
        reference_id="REF-leak-check",
        card_number=raw_card_number,
    )

    all_output = []
    assert cli_main(["recover-aged-out", "rec-leak-check"]) == 0
    all_output.append(capsys.readouterr().out)
    assert cli_main(["recover-aged-out", "rec-leak-check", "--json"]) == 0
    all_output.append(capsys.readouterr().out)
    assert cli_main(["recover-aged-out", "rec-leak-check", "--confirm"]) == 0
    all_output.append(capsys.readouterr().out)

    combined = "\n".join(all_output)
    for secret in _ALL_SECRETS:
        assert secret not in combined
    assert raw_card_number not in combined
    assert raw_card_number[-4:] not in combined
    assert str(payment.gateway_user_id) not in combined
    assert settings.centralpay_base_url not in combined


# --- static safety guards: must call verify_and_settle, must not duplicate -


def test_recover_aged_out_static_calls_only_verify_and_settle_for_settlement():
    """Static guard: app.services.aged_out_recovery's actual CODE (not its
    prose docstrings, which legitimately discuss these names when
    explaining the safety contract) must reference verify_and_settle, and
    must NEVER reference process_callback, run_reconciliation_pass,
    queue_notification, or _validate_and_apply_verification as an
    identifier -- not as an import, not as a call -- and must never call
    `.verify(` on the CentralPayClient it holds."""
    import ast

    import app.services.aged_out_recovery as recovery_module

    source = inspect.getsource(recovery_module)
    tree = ast.parse(source)
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "verify_and_settle" in identifiers

    forbidden = (
        "process_callback",
        "run_reconciliation_pass",
        "queue_notification",
        "_validate_and_apply_verification",
    )
    for name in forbidden:
        assert name not in identifiers, (
            f"app.services.aged_out_recovery must never reference {name}"
        )

    verify_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "verify"
    ]
    assert not verify_calls, (
        "app.services.aged_out_recovery must never call CentralPayClient.verify directly"
    )


def test_recover_aged_out_static_never_assigns_financial_fields_directly():
    """Static guard: the recovery module must never itself assign a
    financial Payment field -- those are exclusively verify_and_settle's
    job."""
    import app.services.aged_out_recovery as recovery_module

    source = inspect.getsource(recovery_module)
    forbidden_assignments = (
        "payment.gateway_verified_at =",
        "payment.reference_id =",
        "payment.card_last4 =",
        "payment.amount =",
        "payment.fee_amount =",
        "payment.payable_amount =",
    )
    for pattern in forbidden_assignments:
        assert pattern not in source, f"app.services.aged_out_recovery must never assign {pattern}"


def test_recover_aged_out_dynamic_never_invokes_forbidden_functions(
    cli_env, session_factory, settings, stub, monkeypatch, capsys
):
    """Dynamic guard: even if process_callback / run_reconciliation_pass /
    queue_notification are made to explode, `recover-aged-out --confirm`
    (eligible, gateway-success path) must still complete normally --
    proving the recovery path never calls them. queue_notification IS
    expected to run, but only via verify_and_settle's own internal call,
    which this test does not patch (only the module-level references a
    duplicate implementation would use)."""
    import app.services.reconciliation as reconciliation_module
    import app.services.verification as verification_module

    def _boom(*args, **kwargs):
        raise AssertionError("must never be called directly by aged_out_recovery")

    monkeypatch.setattr(verification_module, "process_callback", _boom)
    monkeypatch.setattr(reconciliation_module, "run_reconciliation_pass", _boom)
    _patch_centralpay_client(monkeypatch, stub)

    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-dynamic-guard", gateway_order_id=260001
    )
    stub.verify_result = verify_ok_response(
        amount=payment.amount, user_id=payment.gateway_user_id, reference_id="REF-dynamic-guard"
    )

    assert cli_main(["recover-aged-out", "rec-dynamic-guard", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "--- --confirm: verified ---" in out


# --- no bulk mode: only ORDER_ID (single positional) ------------------------


def test_recover_aged_out_parser_rejects_multiple_order_ids():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["recover-aged-out", "a", "b"])


def test_recover_aged_out_parser_has_no_bulk_or_all_flag():
    """No --all / bulk-mode flag exists anywhere on the subparser -- only
    ORDER_ID, --confirm, and --json."""
    subparsers_action = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    recover_parser = subparsers_action.choices["recover-aged-out"]
    all_option_strings = {
        opt for action in recover_parser._actions for opt in action.option_strings
    }
    assert all_option_strings == {"-h", "--help", "--confirm", "--json"}


# --- duplicate-downstream safety: invariant regression tests ---------------


def test_gateway_verified_at_assigned_in_exactly_one_place_in_app_package():
    """Pin the invariant app.services.aged_out_recovery's module docstring
    relies on: gateway_verified_at is assigned in exactly one place in the
    whole app package (the single settlement path's success branch). If a
    future change adds a second assignment site, this recovery command's
    "a link_created + unverified row was never queued for bot delivery"
    reasoning must be re-audited -- this test fails loudly instead of that
    silently going stale."""
    import ast
    import pathlib

    import app

    app_dir = pathlib.Path(app.__file__).parent
    sites: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "gateway_verified_at"
                    and isinstance(target.value, ast.Name)
                ):
                    sites.append(f"{path.relative_to(app_dir.parent)}:{node.lineno}")
    assert sites == ["app/services/verification.py:186"], (
        f"expected exactly one gateway_verified_at assignment site, found: {sites}"
    )


def test_link_created_status_assigned_in_exactly_one_place_in_app_package():
    """Pin the companion invariant: payment.status is set to link_created
    in exactly one place (payment creation). No code path may ever move a
    payment BACK to link_created once it has left that status."""
    import ast
    import pathlib

    import app

    app_dir = pathlib.Path(app.__file__).parent
    sites: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            is_link_created_value = (
                isinstance(value, ast.Attribute)
                and value.attr == "value"
                and isinstance(value.value, ast.Attribute)
                and value.value.attr == "LINK_CREATED"
            )
            if not is_link_created_value:
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and isinstance(target.value, ast.Name)
                ):
                    sites.append(f"{path.relative_to(app_dir.parent)}:{node.lineno}")
    assert sites == ["app/services/payments.py:617"], (
        f"expected exactly one status=LINK_CREATED assignment site, found: {sites}"
    )


def test_check_constraint_forbids_bot_notify_status_without_gateway_verified_at(session_factory):
    """The CHECK constraint (migration 0005,
    ck_payments_delivery_requires_verification) is the database-level half
    of the duplicate-downstream-safety argument: it is impossible to
    persist a row with status IN (bot_notify_pending, bot_notify_accepted)
    while gateway_verified_at IS NULL."""
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="rec-check-constraint",
                gateway_order_id=270001,
                gateway_user_id=1,
                amount=1000,
                payable_amount=1000,
                status=PaymentStatus.BOT_NOTIFY_PENDING.value,
                gateway_verified_at=None,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_eligible_aged_out_payment_has_no_bot_delivery_events(session_factory, settings):
    """Concrete behavioral proof for one payment: a fresh, genuinely
    aged-out link_created row has never had a bot_notification_queued,
    bot_notification_accepted, or gateway_payment_verified event recorded
    against it."""
    payment = _make_aged_out_payment(
        session_factory, settings, bot_order_id="rec-no-delivery-history", gateway_order_id=270002
    )
    events = get_events(session_factory, payment.id)
    event_types = {e.event_type for e in events}
    assert event_types.isdisjoint(
        {"bot_notification_queued", "bot_notification_accepted", "gateway_payment_verified"}
    )
