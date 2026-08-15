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
