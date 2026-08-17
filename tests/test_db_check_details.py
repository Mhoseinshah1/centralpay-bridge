"""`centralpay db-check --details`: bounded, read-only anomaly drill-down.

Focuses on the `invalid_payment_status` check -- the production anomaly this
feature exists to inspect. Verifies: the plain `db-check` report and exit
code are byte-for-byte unaffected by this feature, `--details` finds the
exact row(s) behind a failing check without reinterpreting the raw legacy
status, the audit trail is chronological and strips every field outside the
explicit safe allowlist (no secrets, no Telegram ids, no operator free
text), output is bounded/truncated/deterministically ordered, and the whole
mode performs zero writes and zero network calls.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.models import Payment, PaymentEvent
from app.ops import build_parser
from app.ops import main as ops_main

PAST = datetime(2024, 1, 1, tzinfo=UTC)


def _seed_alembic_version(session_factory, revision: str = "test_revision") -> None:
    """The SQLite unit-test schema (Base.metadata.create_all) has no
    alembic_version table, so db-check's alembic_revision check always fails
    on this fixture regardless of payment data. Seed a fake row so tests that
    want a genuinely "healthy" baseline can get exit code 0."""
    with session_factory() as db:
        db.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        db.execute(text("DELETE FROM alembic_version"))
        db.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": revision}
        )
        db.commit()


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
    created_at: datetime | None = None,
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
        if created_at is not None:
            payment.created_at = created_at
            payment.updated_at = created_at
        db.add(payment)
        db.commit()
        return payment.id


def _add_event(
    session_factory,
    *,
    payment_id: int,
    event_type: str,
    level: str = "info",
    request_id: str | None = None,
    data: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> None:
    with session_factory() as db:
        event = PaymentEvent(
            payment_id=payment_id,
            event_type=event_type,
            level=level,
            request_id=request_id,
            data=data,
        )
        if created_at is not None:
            event.created_at = created_at
        db.add(event)
        db.commit()


def _snapshot(session_factory, bot_order_id: str) -> Payment:
    with session_factory() as db:
        return db.execute(
            select(Payment).where(Payment.bot_order_id == bot_order_id)
        ).scalar_one()


# --- argparse wiring ---------------------------------------------------------


def test_parser_defaults_are_off():
    args = build_parser().parse_args(["db-check"])
    assert args.repair_sequences is False
    assert args.details is False
    assert args.details_json is False


def test_parser_accepts_details_and_json_flags():
    args = build_parser().parse_args(["db-check", "--details", "--json"])
    assert args.details is True
    assert args.details_json is True


# --- plain db-check: unchanged contract -------------------------------------


def test_plain_db_check_unchanged_on_healthy_database(ops_env, session_factory, capsys):
    _seed_alembic_version(session_factory)
    assert ops_main(["db-check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["failures"] == []
    assert report["checks"]["invalid_payment_status"] == 0
    assert "details" not in report


def test_plain_db_check_reports_invalid_status_count_without_details(
    ops_env, session_factory, capsys
):
    _make_payment(
        session_factory, bot_order_id="legacy-1", gateway_order_id=1, status="bot_notified"
    )
    assert ops_main(["db-check"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["invalid_payment_status"] == 1
    assert "invalid_payment_status" in report["failures"]
    # Behavior contract: no --details means no drill-down key, ever.
    assert "details" not in report


def test_plain_db_check_output_still_indented(ops_env, session_factory, capsys):
    _seed_alembic_version(session_factory)
    assert ops_main(["db-check"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("{\n")  # indent=2 pretty-printing, unchanged


# --- --details: finds the exact row -----------------------------------------


def test_details_identifies_exact_invalid_status_row(ops_env, session_factory, capsys):
    _make_payment(
        session_factory,
        bot_order_id="legacy-order-42",
        gateway_order_id=999042,
        status="bot_notified",
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    detail = report["details"]["invalid_payment_status"]
    assert detail["total"] == 1
    assert detail["shown"] == 1
    assert detail["truncated"] is False
    row = detail["rows"][0]
    assert row["bot_order_id"] == "legacy-order-42"
    assert row["gateway_order_id"] == 999042


def test_raw_legacy_status_is_never_reinterpreted(ops_env, session_factory, capsys):
    """A row saying `bot_notified` must be printed exactly as `bot_notified`
    -- never mapped onto a known PaymentStatus value like
    `bot_notify_accepted`."""
    _make_payment(
        session_factory, bot_order_id="legacy-2", gateway_order_id=2, status="bot_notified"
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    row = report["details"]["invalid_payment_status"]["rows"][0]
    assert row["status"] == "bot_notified"
    assert row["status"] != "bot_notify_accepted"


def test_relevant_safe_operational_fields_are_shown(ops_env, session_factory, capsys):
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    _make_payment(
        session_factory,
        bot_order_id="legacy-3",
        gateway_order_id=3,
        status="pending_legacy",
        gateway_verified_at=now,
        bot_notify_reason="bot_http_500",
        bot_notify_attempts=4,
        bot_last_http_status=500,
        bot_notify_started_at=now,
        next_retry_at=now + timedelta(minutes=5),
        manual_review_at=now,
        review_acknowledged_at=now,
        review_resolved_at=now,
        review_resolution="false_positive",
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    row = report["details"]["invalid_payment_status"]["rows"][0]
    assert row["id"] > 0
    assert row["gateway_verified"] is True
    assert row["gateway_verified_at"] is not None
    assert row["bot_notify_reason"] == "bot_http_500"
    assert row["bot_notify_attempts"] == 4
    assert row["bot_last_http_status"] == 500
    assert row["bot_notify_started_at"] is not None
    assert row["bot_notify_accepted_at"] is None
    assert row["next_retry_at"] is not None
    assert row["manual_review_at"] is not None
    assert row["review_acknowledged_at"] is not None
    assert row["review_resolved_at"] is not None
    assert row["review_resolution"] == "false_positive"
    assert row["created_at"] is not None
    assert row["updated_at"] is not None


# --- audit trail: chronology and the safe allowlist -------------------------


def test_audit_event_chronology_is_correct(ops_env, session_factory, capsys):
    payment_id = _make_payment(
        session_factory, bot_order_id="legacy-4", gateway_order_id=4, status="bot_notified"
    )
    # Inserted out of chronological order; output must still be time-ordered.
    _add_event(
        session_factory,
        payment_id=payment_id,
        event_type="bot_notification_failed",
        created_at=PAST + timedelta(hours=2),
    )
    _add_event(
        session_factory,
        payment_id=payment_id,
        event_type="payment_created",
        created_at=PAST,
    )
    _add_event(
        session_factory,
        payment_id=payment_id,
        event_type="manual_review_required",
        created_at=PAST + timedelta(hours=1),
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    events = report["details"]["invalid_payment_status"]["rows"][0]["audit_events"]["events"]
    assert [e["event_type"] for e in events] == [
        "payment_created",
        "manual_review_required",
        "bot_notification_failed",
    ]


def test_audit_event_request_id_and_reason_fields_pass_through(
    ops_env, session_factory, capsys
):
    payment_id = _make_payment(
        session_factory, bot_order_id="legacy-5", gateway_order_id=5, status="bot_notified"
    )
    _add_event(
        session_factory,
        payment_id=payment_id,
        event_type="bot_notification_failed",
        level="warning",
        request_id="req-safe-123",
        data={
            "reason_code": "bot_http_500",
            "http_status": 500,
            "attempt": 2,
            "duration_ms": 42.5,
            "error_code": "bot_http_500",
        },
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    event = report["details"]["invalid_payment_status"]["rows"][0]["audit_events"]["events"][0]
    assert event["event_type"] == "bot_notification_failed"
    assert event["level"] == "warning"
    assert event["request_id"] == "req-safe-123"
    assert event["reason_fields"] == {
        "reason_code": "bot_http_500",
        "http_status": 500,
        "attempt": 2,
        "duration_ms": 42.5,
        "error_code": "bot_http_500",
    }


def test_no_secret_or_untrusted_event_data_leaks(ops_env, session_factory, capsys):
    payment_id = _make_payment(
        session_factory, bot_order_id="legacy-6", gateway_order_id=6, status="bot_notified"
    )
    _add_event(
        session_factory,
        payment_id=payment_id,
        event_type="admin_command_received",
        data={
            "telegram_user_id": 987654321,
            "chat_id": 123456789,
            "note": "operator free text should never appear",
            "operator": "host-cli",
            "command": "/secretcmd",
            "api_key": "sk-should-never-appear",
            "callback_token": "should-never-appear",
            "raw_response": "<html>gateway body should never appear</html>",
        },
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    raw_out = capsys.readouterr().out
    report = json.loads(raw_out)
    event = report["details"]["invalid_payment_status"]["rows"][0]["audit_events"]["events"][0]
    assert event["reason_fields"] == {}
    forbidden = [
        "987654321",
        "123456789",
        "operator free text",
        "/secretcmd",
        "sk-should-never-appear",
        "should-never-appear",
        "gateway body should never appear",
        "telegram_user_id",
        "chat_id",
        "callback_token",
        "api_key",
        "raw_response",
    ]
    for needle in forbidden:
        assert needle not in raw_out, f"leaked forbidden content: {needle!r}"


def test_unsafe_scalar_types_are_dropped_not_leaked(ops_env, session_factory, capsys):
    """Defense in depth: a value under an ALLOWED key that isn't a plain
    scalar (or list of scalars) must be dropped, never serialized."""
    payment_id = _make_payment(
        session_factory, bot_order_id="legacy-7", gateway_order_id=7, status="bot_notified"
    )
    _add_event(
        session_factory,
        payment_id=payment_id,
        event_type="bot_notification_failed",
        data={"reason_code": "bot_http_500", "stage": {"nested": "should not leak"}},
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    event = report["details"]["invalid_payment_status"]["rows"][0]["audit_events"]["events"][0]
    assert event["reason_fields"] == {"reason_code": "bot_http_500"}
    assert "nested" not in json.dumps(report)


# --- bounded / truncated / deterministic ordering ---------------------------


def test_bounded_and_truncated_output(ops_env, session_factory, capsys):
    for i in range(25):
        _make_payment(
            session_factory,
            bot_order_id=f"legacy-bulk-{i:02d}",
            gateway_order_id=1000 + i,
            status="bot_notified",
        )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    detail = report["details"]["invalid_payment_status"]
    assert detail["total"] == 25
    assert detail["shown"] == 20
    assert detail["truncated"] is True
    assert detail["limit"] == 20
    assert len(detail["rows"]) == 20


def test_deterministic_ordering_oldest_id_first(ops_env, session_factory, capsys):
    ids_in_insertion_order = []
    for i in range(5):
        pid = _make_payment(
            session_factory,
            bot_order_id=f"legacy-order-{i}",
            gateway_order_id=2000 + i,
            status="bot_notified",
        )
        ids_in_insertion_order.append(pid)
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    detail = report["details"]["invalid_payment_status"]
    assert detail["ordering"] == "id_ascending"
    returned_ids = [row["id"] for row in detail["rows"]]
    assert returned_ids == sorted(ids_in_insertion_order)

    # Re-run: ordering must be stable across repeated invocations.
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report_again = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in report_again["details"]["invalid_payment_status"]["rows"]] == (
        returned_ids
    )


def test_audit_trail_bounded_and_truncated(ops_env, session_factory, capsys):
    payment_id = _make_payment(
        session_factory,
        bot_order_id="legacy-many-events",
        gateway_order_id=8,
        status="bot_notified",
    )
    for i in range(60):
        _add_event(
            session_factory,
            payment_id=payment_id,
            event_type="bot_notification_failed",
            created_at=PAST + timedelta(minutes=i),
        )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    trail = report["details"]["invalid_payment_status"]["rows"][0]["audit_events"]
    assert trail["total"] == 60
    assert trail["shown"] == 50
    assert trail["truncated"] is True


def test_healthy_database_details_is_empty_dict(ops_env, session_factory, capsys):
    _seed_alembic_version(session_factory)
    assert ops_main(["db-check", "--details", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["details"] == {}


def test_unsupported_check_gets_explicit_marker_not_silent_omission(
    ops_env, session_factory, capsys
):
    # Corrupt a check with no row-level detail implementation yet: a claim
    # timestamp on a payment that is not bot_notify_pending.
    _make_payment(
        session_factory,
        bot_order_id="claim-anomaly-1",
        gateway_order_id=9,
        status="gateway_verified",
        notification_claimed_at=PAST,
        notification_claimed_by="worker-1",
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["claims_on_non_pending_payments"] == 1
    marker = report["details"]["claims_on_non_pending_payments"]
    assert marker["supported"] is False
    assert marker["total"] == 1
    assert "invalid_payment_status" not in report["details"]  # that check passed, no entry


# --- read-only / zero side effects ------------------------------------------


def test_details_mode_performs_zero_writes(ops_env, session_factory, capsys):
    _make_payment(
        session_factory,
        bot_order_id="legacy-8",
        gateway_order_id=10,
        status="bot_notified",
        bot_notify_attempts=2,
    )
    payment_id = _snapshot(session_factory, "legacy-8").id
    _add_event(session_factory, payment_id=payment_id, event_type="payment_created")

    before = _snapshot(session_factory, "legacy-8")
    with session_factory() as db:
        events_before = db.execute(
            select(PaymentEvent).where(PaymentEvent.payment_id == payment_id)
        ).scalars().all()
        event_count_before = len(events_before)

    assert ops_main(["db-check", "--details", "--json"]) == 1
    capsys.readouterr()

    after = _snapshot(session_factory, "legacy-8")
    assert after.status == before.status == "bot_notified"
    assert after.bot_notify_attempts == before.bot_notify_attempts == 2
    assert after.updated_at == before.updated_at
    with session_factory() as db:
        event_count_after = len(
            db.execute(
                select(PaymentEvent).where(PaymentEvent.payment_id == payment_id)
            ).scalars().all()
        )
    assert event_count_after == event_count_before


def test_details_mode_makes_zero_network_calls(ops_env, session_factory, monkeypatch, capsys):
    _make_payment(
        session_factory, bot_order_id="legacy-9", gateway_order_id=11, status="bot_notified"
    )

    def _forbidden_send(self, request, **kwargs):
        raise AssertionError(f"unexpected network call: {request.method} {request.url}")

    monkeypatch.setattr(httpx.Client, "send", _forbidden_send)
    # Must complete successfully with the network layer poisoned -- proves
    # no HTTP call was attempted anywhere in the --details code path.
    assert ops_main(["db-check", "--details", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["details"]["invalid_payment_status"]["total"] == 1


# --- flag interactions -------------------------------------------------------


def test_details_and_repair_sequences_is_rejected(ops_env, session_factory, capsys):
    _make_payment(
        session_factory, bot_order_id="legacy-10", gateway_order_id=12, status="bot_notified"
    )
    before = _snapshot(session_factory, "legacy-10")
    assert ops_main(["db-check", "--details", "--repair-sequences"]) == 1
    captured = capsys.readouterr()
    assert "cannot be combined" in captured.err
    assert captured.out == ""  # refused before any report was ever built
    after = _snapshot(session_factory, "legacy-10")
    assert after.status == before.status  # untouched


def test_json_without_details_is_rejected(ops_env, capsys):
    assert ops_main(["db-check", "--json"]) == 1
    captured = capsys.readouterr()
    assert "--json requires --details" in captured.err
    assert captured.out == ""


def test_repair_sequences_alone_still_works(ops_env, session_factory, capsys):
    """Existing --repair-sequences behavior is untouched by this feature."""
    _seed_alembic_version(session_factory)
    assert ops_main(["db-check", "--repair-sequences"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "details" not in report


# --- JSON stability -----------------------------------------------------------


def test_details_json_is_stable_across_repeated_calls(ops_env, session_factory, capsys):
    _make_payment(
        session_factory, bot_order_id="legacy-11", gateway_order_id=13, status="bot_notified"
    )
    assert ops_main(["db-check", "--details", "--json"]) == 1
    first = capsys.readouterr().out
    assert ops_main(["db-check", "--details", "--json"]) == 1
    second = capsys.readouterr().out
    assert first == second
    assert "\n" not in first.strip()  # single compact line


def test_details_without_json_flag_is_still_indented(ops_env, session_factory, capsys):
    _make_payment(
        session_factory, bot_order_id="legacy-12", gateway_order_id=14, status="bot_notified"
    )
    assert ops_main(["db-check", "--details"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("{\n")
    parsed = json.loads(out)
    assert parsed["details"]["invalid_payment_status"]["total"] == 1
