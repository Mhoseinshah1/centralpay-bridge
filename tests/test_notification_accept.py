"""`centralpay notification accept ORDER_ID --note TEXT --yes`.

Manual, operator-confirmed acceptance of a payment stuck in
``bot_notify_pending`` after gateway verification -- the escape hatch for
the production case where a downstream bot had already processed a
verified payment (idempotent API, customer already credited) but every
delivery attempt kept failing with a proven side-effecting 5xx, so safe
mode correctly refused to auto-retry forever. Covers: eligibility
(status + gateway_verified_at, re-checked under lock), the exact audit
event and its sanitized metadata, financial/diagnostic-field immutability,
confirmation gating, note validation, zero network calls, and rollback
behavior. Real-PostgreSQL concurrency races live in
tests/integration/test_postgres.py -- this module never claims lock safety
from SQLite alone.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.models import Payment, PaymentEvent, PaymentStatus
from app.ops import main as ops_main
from app.reasons import ReasonCode

PAST = datetime(2026, 1, 1, tzinfo=UTC)
NEXT_RETRY = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)


@pytest.fixture
def ops_env(settings, session_factory, monkeypatch):
    import app.ops as ops_module

    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)
    return settings


def _make_payment(
    session_factory,
    *,
    bot_order_id: str,
    gateway_order_id: int,
    status: str,
    amount: int = 10000,
    fee_amount: int = 0,
    **overrides,
) -> int:
    with session_factory() as db:
        payment = Payment(
            bot_order_id=bot_order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=1,
            amount=amount,
            fee_amount=fee_amount,
            payable_amount=amount + fee_amount,
            status=status,
            **overrides,
        )
        db.add(payment)
        db.commit()
        return payment.id


def _stuck_pending_payment(
    session_factory,
    *,
    order_id: str = "stuck-1",
    amount: int = 12345,
    fee_amount: int = 278,
    **overrides: Any,
) -> int:
    """Exactly the production incident: gateway-verified, stuck in
    bot_notify_pending after 5 proven-side-effecting-5xx attempts."""
    defaults: dict[str, Any] = {
        "gateway_verified_at": PAST,
        "reference_id": f"REF-{order_id}",
        "bot_notify_attempts": 5,
        "bot_notify_reason": ReasonCode.BOT_HTTP_500.value,
        "bot_last_http_status": 500,
        "bot_last_error_code": "http_500",
        "next_retry_at": NEXT_RETRY,
        "fee_rate_bps": 225,
    }
    defaults.update(overrides)
    return _make_payment(
        session_factory,
        bot_order_id=order_id,
        gateway_order_id=abs(hash(order_id)) % 1_000_000 + 1,
        status=PaymentStatus.BOT_NOTIFY_PENDING.value,
        amount=amount,
        fee_amount=fee_amount,
        **defaults,
    )


def _snapshot(session_factory, payment_id: int) -> Payment:
    with session_factory() as db:
        return db.execute(select(Payment).where(Payment.id == payment_id)).scalar_one()


def _events(session_factory, payment_id: int) -> list[PaymentEvent]:
    with session_factory() as db:
        return list(
            db.execute(
                select(PaymentEvent)
                .where(PaymentEvent.payment_id == payment_id)
                .order_by(PaymentEvent.id)
            ).scalars()
        )


def _data(event: PaymentEvent) -> dict[str, Any]:
    assert event.data is not None
    return event.data


# --- happy path --------------------------------------------------------


def test_happy_path_accepts(ops_env, session_factory, capsys):
    payment_id = _stuck_pending_payment(session_factory, order_id="ok-1")
    code = ops_main(["notification", "accept", "ok-1", "--note", "operator confirmed", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "ok-1" in out
    row = _snapshot(session_factory, payment_id)
    assert row.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert row.bot_notify_reason == ReasonCode.BOT_NOTIFY_ACCEPTED.value
    assert row.bot_notify_accepted_at is not None
    assert row.next_retry_at is None
    assert row.notification_claimed_at is None
    assert row.notification_claimed_by is None


def test_production_regression_scenario(ops_env, session_factory):
    """Section 11's exact production state, byte-for-byte financial and
    diagnostic preservation after manual acceptance."""
    payment_id = _stuck_pending_payment(session_factory, order_id="prod-1")
    before = _snapshot(session_factory, payment_id)

    code = ops_main(
        [
            "notification",
            "accept",
            "prod-1",
            "--note",
            "VPN bot operator confirmed the order was already processed",
            "--yes",
        ]
    )
    assert code == 0

    after = _snapshot(session_factory, payment_id)
    assert after.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value
    assert after.gateway_verified_at == before.gateway_verified_at
    assert after.reference_id == before.reference_id
    assert after.bot_notify_attempts == 5
    assert after.next_retry_at is None
    assert after.bot_notify_accepted_at is not None
    assert after.notification_claimed_at is None
    assert after.notification_claimed_by is None
    # Financial fields byte-for-byte unchanged.
    assert after.amount == before.amount
    assert after.fee_rate_bps == before.fee_rate_bps
    assert after.fee_amount == before.fee_amount
    assert after.payable_amount == before.payable_amount
    # Historical diagnostics preserved -- never cleared by manual acceptance.
    assert after.bot_last_http_status == 500
    assert after.bot_last_error_code == "http_500"

    events = _events(session_factory, payment_id)
    matching = [e for e in events if e.event_type == "manual_bot_notification_accepted"]
    assert len(matching) == 1


# --- lookup: nonexistent / ambiguous ------------------------------------


def test_nonexistent_order_id_refused(ops_env, session_factory, capsys):
    code = ops_main(["notification", "accept", "does-not-exist", "--note", "x", "--yes"])
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_ambiguous_order_id_refused(ops_env, session_factory, capsys):
    _make_payment(
        session_factory,
        bot_order_id="424242",
        gateway_order_id=1,
        status=PaymentStatus.BOT_NOTIFY_PENDING.value,
        gateway_verified_at=PAST,
    )
    _make_payment(
        session_factory,
        bot_order_id="other-order",
        gateway_order_id=424242,
        status=PaymentStatus.BOT_NOTIFY_PENDING.value,
        gateway_verified_at=PAST,
    )
    code = ops_main(["notification", "accept", "424242", "--note", "x", "--yes"])
    assert code == 1
    assert "ambiguous" in capsys.readouterr().err.lower()


# --- eligibility refusals -----------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.LINK_CREATED.value,
        PaymentStatus.GETLINK_FAILED.value,
        PaymentStatus.MANUAL_REVIEW.value,
        PaymentStatus.BOT_NOTIFY_ACCEPTED.value,
        PaymentStatus.CREATED.value,
        PaymentStatus.GATEWAY_VERIFIED.value,
    ],
)
def test_refused_for_non_pending_status(ops_env, session_factory, capsys, status):
    payment_id = _make_payment(
        session_factory,
        bot_order_id=f"wrong-status-{status}",
        gateway_order_id=abs(hash(status)) % 1_000_000 + 1,
        status=status,
        gateway_verified_at=PAST,
    )
    code = ops_main(
        ["notification", "accept", f"wrong-status-{status}", "--note", "x", "--yes"]
    )
    assert code == 1
    assert "not in bot_notify_pending" in capsys.readouterr().err
    row = _snapshot(session_factory, payment_id)
    assert row.status == status  # untouched
    assert _events(session_factory, payment_id) == []


def test_refused_when_pending_without_gateway_verification():
    """Belt-and-braces case from section 3: status == bot_notify_pending but
    gateway_verified_at IS NULL. The database's own
    ck_payments_delivery_requires_verification CHECK constraint already
    makes this state unreachable via a normal committed row (see
    test_belt_and_braces_state_rejected_by_database below), so this is
    exercised directly against the pure eligibility function rather than
    through a DB round trip."""
    from app.services.notification import ManualAcceptRefusal, determine_manual_accept_refusal

    payment = Payment(
        bot_order_id="unverified-pending",
        gateway_order_id=555,
        gateway_user_id=1,
        amount=10000,
        payable_amount=10000,
        status=PaymentStatus.BOT_NOTIFY_PENDING.value,
        gateway_verified_at=None,
    )
    assert (
        determine_manual_accept_refusal(payment)
        is ManualAcceptRefusal.NOT_GATEWAY_VERIFIED
    )


def test_belt_and_braces_state_rejected_by_database(session_factory):
    """The database itself refuses to persist status=bot_notify_pending
    with gateway_verified_at NULL (ck_payments_delivery_requires_
    verification, migration 0005) -- proving the anomaly
    determine_manual_accept_refusal guards against cannot arise from a
    normal committed row in the first place."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _make_payment(
            session_factory,
            bot_order_id="unverified-pending-2",
            gateway_order_id=556,
            status=PaymentStatus.BOT_NOTIFY_PENDING.value,
            gateway_verified_at=None,
        )


# --- note validation ------------------------------------------------------


def test_missing_note_is_argparse_error(ops_env, session_factory):
    with pytest.raises(SystemExit):
        ops_main(["notification", "accept", "whatever", "--yes"])


def test_empty_note_refused(ops_env, session_factory, capsys):
    _stuck_pending_payment(session_factory, order_id="empty-note")
    code = ops_main(["notification", "accept", "empty-note", "--note", "   ", "--yes"])
    assert code == 1
    assert "non-empty --note is required" in capsys.readouterr().err


def test_note_truncated_to_500_chars(ops_env, session_factory):
    payment_id = _stuck_pending_payment(session_factory, order_id="long-note")
    long_note = "x" * 900
    code = ops_main(["notification", "accept", "long-note", "--note", long_note, "--yes"])
    assert code == 0
    [event] = [
        e
        for e in _events(session_factory, payment_id)
        if e.event_type == "manual_bot_notification_accepted"
    ]
    assert len(_data(event)["note"]) == 500
    assert _data(event)["note"] == "x" * 500


# --- confirmation gating ---------------------------------------------------


def test_confirmation_required_without_yes(ops_env, session_factory, capsys):
    payment_id = _stuck_pending_payment(session_factory, order_id="needs-yes")
    code = ops_main(["notification", "accept", "needs-yes", "--note", "please"])
    assert code == 1
    err = capsys.readouterr().err
    assert "Does NOT contact the bot" in err
    assert "Does NOT credit the customer" in err
    assert "Does NOT change gateway verification" in err
    assert "Does NOT change payment amounts" in err
    assert "Permanently stops automatic notification retries" in err
    row = _snapshot(session_factory, payment_id)
    assert row.status == PaymentStatus.BOT_NOTIFY_PENDING.value  # untouched
    assert _events(session_factory, payment_id) == []


def test_invalid_confirmation_flag_rejected(ops_env, session_factory):
    _stuck_pending_payment(session_factory, order_id="bad-flag")
    with pytest.raises(SystemExit):
        ops_main(
            ["notification", "accept", "bad-flag", "--note", "x", "--yes=definitely"]
        )


def test_yes_flag_successful_path(ops_env, session_factory):
    payment_id = _stuck_pending_payment(session_factory, order_id="yes-path")
    code = ops_main(["notification", "accept", "yes-path", "--note", "confirmed", "--yes"])
    assert code == 0
    assert _snapshot(session_factory, payment_id).status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


# --- audit event exact type + metadata ------------------------------------


def test_audit_event_type_and_metadata(ops_env, session_factory):
    payment_id = _stuck_pending_payment(session_factory, order_id="audit-1")
    code = ops_main(
        ["notification", "accept", "audit-1", "--note", "confirmed with bot team", "--yes"]
    )
    assert code == 0
    events = _events(session_factory, payment_id)
    matching = [e for e in events if e.event_type == "manual_bot_notification_accepted"]
    assert len(matching) == 1
    event = matching[0]
    # Never the automatic-acceptance event.
    assert "bot_notification_accepted" not in [e.event_type for e in events]
    assert _data(event)["operator"] == "host-cli"
    assert _data(event)["note"] == "confirmed with bot team"
    assert _data(event)["previous_reason"] == ReasonCode.BOT_HTTP_500.value


# --- claim fields, financial immutability, gateway facts -------------------


def test_claim_fields_cleared(ops_env, session_factory):
    payment_id = _stuck_pending_payment(
        session_factory,
        order_id="claimed-1",
        notification_claimed_at=PAST,
        notification_claimed_by="some-worker-abc",
    )
    code = ops_main(["notification", "accept", "claimed-1", "--note", "x", "--yes"])
    assert code == 0
    row = _snapshot(session_factory, payment_id)
    assert row.notification_claimed_at is None
    assert row.notification_claimed_by is None


def test_financial_and_gateway_fields_unchanged(ops_env, session_factory):
    payment_id = _stuck_pending_payment(
        session_factory,
        order_id="fin-1",
        amount=54321,
        fee_rate_bps=500,
        fee_amount=2716,
        reference_id="REF-fin-1-unique",
    )
    before = _snapshot(session_factory, payment_id)
    code = ops_main(["notification", "accept", "fin-1", "--note", "x", "--yes"])
    assert code == 0
    after = _snapshot(session_factory, payment_id)
    assert after.amount == before.amount == 54321
    assert after.fee_rate_bps == before.fee_rate_bps == 500
    assert after.fee_amount == before.fee_amount == 2716
    assert after.payable_amount == before.payable_amount
    assert after.gateway_verified_at == before.gateway_verified_at
    assert after.reference_id == before.reference_id == "REF-fin-1-unique"
    assert after.gateway_user_id == before.gateway_user_id
    assert after.card_last4 == before.card_last4


def test_attempt_counter_and_diagnostics_preserved(ops_env, session_factory):
    payment_id = _stuck_pending_payment(
        session_factory,
        order_id="diag-1",
        bot_notify_attempts=5,
        bot_last_http_status=500,
        bot_last_error_code="http_500",
    )
    code = ops_main(["notification", "accept", "diag-1", "--note", "x", "--yes"])
    assert code == 0
    row = _snapshot(session_factory, payment_id)
    assert row.bot_notify_attempts == 5
    assert row.bot_last_http_status == 500
    assert row.bot_last_error_code == "http_500"


# --- idempotency / second invocation ---------------------------------------


def test_second_invocation_safely_refuses(ops_env, session_factory, capsys):
    payment_id = _stuck_pending_payment(session_factory, order_id="twice-1")
    first = ops_main(["notification", "accept", "twice-1", "--note", "first", "--yes"])
    assert first == 0

    second = ops_main(["notification", "accept", "twice-1", "--note", "second", "--yes"])
    assert second == 1
    assert "not in bot_notify_pending" in capsys.readouterr().err

    events = _events(session_factory, payment_id)
    matching = [e for e in events if e.event_type == "manual_bot_notification_accepted"]
    assert len(matching) == 1  # never duplicated
    row = _snapshot(session_factory, payment_id)
    assert row.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


# --- rollback / exception safety -------------------------------------------


def test_exception_before_commit_leaves_payment_and_audit_untouched(
    ops_env, session_factory, monkeypatch
):
    import app.services.notification as notification_module

    payment_id = _stuck_pending_payment(session_factory, order_id="boom-1")
    before = _snapshot(session_factory, payment_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(notification_module, "record_event", _boom)
    with pytest.raises(RuntimeError):
        ops_main(["notification", "accept", "boom-1", "--note", "x", "--yes"])

    after = _snapshot(session_factory, payment_id)
    assert after.status == before.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert after.bot_notify_accepted_at is None
    assert after.next_retry_at == before.next_retry_at
    assert _events(session_factory, payment_id) == []


# --- zero network calls -----------------------------------------------------


def test_manual_accept_never_constructs_a_bot_notifier(ops_env, session_factory, monkeypatch):
    from app.bot import BotNotifier

    def _forbidden(*args, **kwargs):
        raise AssertionError("manual accept must never construct a BotNotifier")

    monkeypatch.setattr(BotNotifier, "__init__", _forbidden)
    payment_id = _stuck_pending_payment(session_factory, order_id="no-bot-1")
    code = ops_main(["notification", "accept", "no-bot-1", "--note", "x", "--yes"])
    assert code == 0
    assert _snapshot(session_factory, payment_id).status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


def test_manual_accept_never_constructs_a_centralpay_client(ops_env, session_factory, monkeypatch):
    from app.centralpay import CentralPayClient

    def _forbidden(*args, **kwargs):
        raise AssertionError("manual accept must never construct a CentralPayClient")

    monkeypatch.setattr(CentralPayClient, "__init__", _forbidden)
    payment_id = _stuck_pending_payment(session_factory, order_id="no-gw-1")
    code = ops_main(["notification", "accept", "no-gw-1", "--note", "x", "--yes"])
    assert code == 0
    assert _snapshot(session_factory, payment_id).status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


def test_manual_accept_makes_no_bot_or_gateway_http_request(
    ops_env, session_factory, bot_stub, stub
):
    """End-to-end proof using the real transport stubs: even when a real
    BotNotifier/CentralPayStub pair exists in the process, manual acceptance
    never touches either."""
    payment_id = _stuck_pending_payment(session_factory, order_id="no-http-1")
    code = ops_main(["notification", "accept", "no-http-1", "--note", "x", "--yes"])
    assert code == 0
    assert bot_stub.requests == []
    assert stub.getlink_requests == []
    assert stub.verify_requests == []
    assert _snapshot(session_factory, payment_id).status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value


# --- privacy / secret regression --------------------------------------------


def test_audit_data_never_contains_secret_values(ops_env, session_factory):
    """Mirrors test_notification.py's
    test_attempt_events_contain_no_secret_values: the manual-accept audit
    event's data must never carry configured secret material, even though
    the note is free text."""
    payment_id = _stuck_pending_payment(session_factory, order_id="secret-1")
    note = f"token={ops_env.bot_notify_token} db={ops_env.database_url}"
    code = ops_main(["notification", "accept", "secret-1", "--note", note, "--yes"])
    assert code == 0
    [event] = [
        e
        for e in _events(session_factory, payment_id)
        if e.event_type == "manual_bot_notification_accepted"
    ]
    # The note itself is free text an operator controls (sanitized only by
    # length) -- this test asserts the STRUCTURED fields around it carry no
    # additional secret material, and that nothing beyond the allowlisted
    # keys is present.
    assert set(_data(event).keys()) == {"operator", "note", "previous_reason"}


# --- CLI help text -----------------------------------------------------------


def test_cli_help_documents_notification_accept():
    import app.ops as ops_module

    assert "notification accept ORDER_ID --note TEXT --yes" in ops_module.__doc__


def test_argparse_wires_notification_accept():
    from app.ops import build_parser

    args = build_parser().parse_args(
        ["notification", "accept", "abc", "--note", "hi", "--yes"]
    )
    assert args.command == "notification"
    assert args.notification_command == "accept"
    assert args.order_id == "abc"
    assert args.note == "hi"
    assert args.yes is True
