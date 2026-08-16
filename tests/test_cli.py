"""app.cli: read-only inspection commands.

Focuses on the new `stuck` command's argparse wiring and output formatting
(human-readable by default, --json for automation, --limit for display
truncation with exact summary counts). Categorization correctness itself is
covered by tests/test_stuck_payments.py — this file only exercises the CLI
layer on top of it, following the same monkeypatched-entrypoint pattern
tests/test_fee_flow.py uses for `python -m app.ops`.
"""

import json

import pytest

from app.cli import build_parser
from app.cli import main as cli_main
from tests.conftest import create_order


@pytest.fixture
def cli_env(settings, session_factory, monkeypatch):
    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "Settings", lambda: settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: session_factory)
    return settings


def test_stuck_parser_defaults():
    args = build_parser().parse_args(["stuck"])
    assert args.command == "stuck"
    assert args.limit == 20
    assert args.as_json is False


def test_stuck_parser_json_and_limit_flags():
    args = build_parser().parse_args(["stuck", "--limit", "5", "--json"])
    assert args.limit == 5
    assert args.as_json is True


def test_stuck_human_readable_output_with_nothing_stuck(cli_env, capsys):
    assert cli_main(["stuck"]) == 0
    out = capsys.readouterr().out
    assert "🚨 Stuck Payments" in out
    assert "🔴 Need attention: 0" in out
    assert "🟡 Waiting gateway: 0" in out
    assert "⚫ Expired links: 0" in out
    assert "Nothing needs attention." in out


def test_stuck_human_readable_shows_waiting_gateway_entry(
    cli_env, client, settings, session_factory, stub, capsys
):
    assert create_order(client, settings, order_id="cli-waiting-1").status_code == 200
    assert cli_main(["stuck"]) == 0
    out = capsys.readouterr().out
    assert "🟡 Waiting gateway: 1" in out
    assert "cli-waiting-1" in out
    assert "Status:" in out
    assert "link_created" in out
    # Zero reconciliation attempts yet: nothing actionable to show.
    assert "No action needed" in out


def test_stuck_json_output_is_one_object_per_line(
    cli_env, client, settings, session_factory, stub, capsys
):
    assert create_order(client, settings, order_id="cli-json-1").status_code == 200
    assert cli_main(["stuck", "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    summary = lines[0]
    assert summary["type"] == "summary"
    assert summary["waiting_gateway"] == 1
    entries = [line for line in lines[1:] if line["type"] == "entry"]
    assert any(entry["order"] == "cli-json-1" for entry in entries)


def test_stuck_limit_truncates_display_but_summary_stays_exact(
    cli_env, client, settings, session_factory, stub, capsys
):
    assert create_order(client, settings, order_id="cli-limit-1").status_code == 200
    assert create_order(client, settings, order_id="cli-limit-2").status_code == 200
    assert cli_main(["stuck", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "🟡 Waiting gateway: 2" in out
    assert out.count("Order:") == 1
    assert "more not shown" in out


def test_stuck_displayed_age_matches_link_issuance_not_row_creation(
    cli_env, client, settings, session_factory, stub, capsys
):
    """Regression: the displayed Age must use the same link-age anchor
    reconciliation itself categorizes by (callback_token_issued_at, falling
    back to created_at) — never bare created_at — or an EXPIRED entry could
    misleadingly show an age far below the max-age cutoff that put it there."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.models import Payment

    assert create_order(client, settings, order_id="cli-age-expired").status_code == 200
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "cli-age-expired")
        ).scalar_one()
        # created_at stays "now"; only the link-issuance anchor is backdated,
        # mirroring how a real link ages relative to a (near-identical) row
        # creation time.
        payment.callback_token_issued_at = datetime.now(UTC) - timedelta(
            seconds=settings.reconciliation_max_age_seconds + 300
        )
        db.commit()

    assert cli_main(["stuck"]) == 0
    out = capsys.readouterr().out
    assert "⚫ Expired links: 1" in out
    assert "less than a minute" not in out


def test_stuck_ordering_is_attention_then_waiting_then_expired(
    cli_env, client, settings, session_factory, stub, capsys
):
    assert create_order(client, settings, order_id="cli-order-waiting").status_code == 200
    assert cli_main(["stuck", "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    categories = [line["category"] for line in lines if line["type"] == "entry"]
    # Only a waiting_gateway payment exists here; the fixed cross-category
    # ordering itself is covered by
    # test_stuck_payments.py::test_ordered_priority_is_attention_then_waiting_then_expired.
    assert categories == ["waiting_gateway"]


# --- reconciliation status ---------------------------------------------------


def test_reconciliation_status_parser_defaults():
    args = build_parser().parse_args(["reconciliation", "status"])
    assert args.command == "reconciliation"
    assert args.reconciliation_command == "status"
    assert args.as_json is False


def test_reconciliation_status_parser_json_flag():
    args = build_parser().parse_args(["reconciliation", "status", "--json"])
    assert args.as_json is True


def test_reconciliation_status_human_output(cli_env, capsys):
    assert cli_main(["reconciliation", "status"]) == 0
    out = capsys.readouterr().out
    assert "🔄 Reconciliation Status" in out
    assert "config source:" in out
    assert "enabled:                  yes" in out
    assert "Effective configuration:" in out
    assert "Payment buckets" in out
    assert "Queue health" in out
    assert "Recent activity" in out
    assert "exhausted (within auto-reconciliation lifetime)" in out


def test_reconciliation_status_json_output_shape(cli_env, capsys):
    assert cli_main(["reconciliation", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1  # exactly one JSON object
    payload = json.loads(out)
    assert set(payload) == {"generated_at", "runtime", "config", "buckets", "queue", "recent"}
    assert payload["runtime"]["enabled"] is True
    assert payload["config"]["fast_window_seconds"] == cli_env.reconciliation_fast_window_seconds
    assert payload["buckets"] == {
        "total_unverified": 0,
        "active": 0,
        "expiring": 0,
        "aged_out": 0,
    }
    assert payload["queue"]["exhausted_not_aged_out"] == 0
    assert payload["recent"]["window_hours"] == 24


def test_reconciliation_status_disabled_shows_heartbeat_not_applicable(
    cli_env, monkeypatch, capsys
):
    import app.cli as cli_module

    disabled = cli_env.model_copy(update={"reconciliation_enabled": False})
    monkeypatch.setattr(cli_module, "Settings", lambda: disabled)

    assert cli_main(["reconciliation", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["enabled"] is False
    assert payload["runtime"]["heartbeat_fresh"] is None


def test_reconciliation_status_recent_exhausted_label_is_distinct_from_queue(
    cli_env, session_factory, capsys
):
    """Regression: recent.exhausted (a `reconciliation_exhausted` EVENT count
    within the window) must never be rendered with the queue's
    `exhausted_not_aged_out` label — a payment can have raised that event and
    since aged out, so the two counts are not the same guarantee and can
    legitimately diverge (here: 1 recent event, 0 still-not-aged-out)."""
    from datetime import UTC, datetime, timedelta

    from app.models import Payment, PaymentEvent, PaymentStatus

    now = datetime.now(UTC)
    aged_out_at = now - timedelta(seconds=cli_env.reconciliation_max_age_seconds + 60)
    with session_factory() as db:
        payment = Payment(
            bot_order_id="cli-exhausted-recent",
            gateway_order_id=999001,
            gateway_user_id=1,
            amount=10000,
            payable_amount=10000,
            status=PaymentStatus.LINK_CREATED.value,
            created_at=aged_out_at,
            callback_token_issued_at=aged_out_at,
            reconciliation_attempts=cli_env.reconciliation_max_attempts,
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        db.add(
            PaymentEvent(
                payment_id=payment.id,
                event_type="reconciliation_exhausted",
                created_at=now - timedelta(hours=1),
            )
        )
        db.commit()

    assert cli_main(["reconciliation", "status"]) == 0
    out = capsys.readouterr().out
    assert "exhausted (within auto-reconciliation lifetime): 0" in out
    assert "  exhausted:                1  (attention)" in out
    assert "exhausted_not_aged_out" not in out
