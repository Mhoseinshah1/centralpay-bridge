"""Operator CLI surfaces for attention resolution and review resolution.

Covers three separate operator-UX problems:

* `centralpay attention list|show|resolve` — the new command group, its
  confirmation gate, its refusal rendering, and the guarantee that
  `centralpay payment ORDER_ID` still shows the ORIGINAL financial and
  failure facts alongside the resolution afterwards.
* `centralpay manual-review` — previously filtered on `status ==
  manual_review` ALONE, so every review an operator had already resolved kept
  printing as if it were still active (because `review resolve` deliberately
  keeps that status as permanent history). Now unresolved-only, with `--all`
  as the explicit historical view.
* `centralpay review resolve-many` — explicit-list, preview-first,
  all-or-nothing bulk resolution. Never "resolve all", never any gateway or
  downstream-bot request, never a financial mutation.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.models import Payment, PaymentEvent, PaymentStatus
from app.ops import main as ops_main
from app.services.stuck_payments import UNEXPECTED_STATE_GRACE_SECONDS
from tests.conftest import as_utc, create_order

PAST = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
def ops_env(settings, session_factory, monkeypatch):
    import app.ops as ops_module

    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)
    return settings


@pytest.fixture
def cli_env(settings, session_factory, monkeypatch):
    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "Settings", lambda: settings)
    monkeypatch.setattr(cli_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(cli_module, "configure_logging", lambda s: None)
    return settings


def _make_review(
    session_factory,
    *,
    order_id: str,
    gateway_order_id: int,
    resolved: bool = False,
    gateway_verified: bool = True,
    reason: str = "retry_limit_reached",
) -> int:
    with session_factory() as db:
        payment = Payment(
            bot_order_id=order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=55501234,
            amount=10000,
            payable_amount=10000,
            status=PaymentStatus.MANUAL_REVIEW.value,
            manual_review_at=PAST,
            bot_notify_reason=reason,
            bot_notify_attempts=5,
            bot_last_http_status=500,
            gateway_verified_at=PAST if gateway_verified else None,
            reference_id=f"REF-{order_id}" if gateway_verified else None,
            review_resolved_at=PAST if resolved else None,
            review_resolution="confirmed_by_bot_operator" if resolved else None,
        )
        db.add(payment)
        db.commit()
        return payment.id


def _stale_getlink_failed(client, settings, session_factory, stub, *, order_id):
    stub.getlink_result = httpx.ReadTimeout("read timed out")
    assert create_order(client, settings, order_id=order_id, amount=230000).status_code >= 400
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = datetime.now(UTC) - timedelta(
            seconds=UNEXPECTED_STATE_GRACE_SECONDS + 3600
        )
        db.commit()
        return payment.id


def _json_lines(capsys) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]


# --- centralpay attention -------------------------------------------------


def test_attention_resolve_requires_explicit_confirmation(
    ops_env, client, settings, session_factory, stub, capsys
):
    """Same strong-confirmation semantics as `notification accept`: without
    --yes the command REFUSES and explains, and writes nothing."""
    _stale_getlink_failed(client, settings, session_factory, stub, order_id="att-1")

    code = ops_main(
        [
            "attention",
            "resolve",
            "att-1",
            "--resolution",
            "stale_getlink_failure",
            "--note",
            "stale",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Re-run with --yes to confirm" in err
    assert "Does NOT contact CentralPay" in err
    assert "Does NOT change the payment status" in err
    # The honest residual-risk statement must be in the operator's face.
    assert "Does NOT block a later legitimate settlement" in err

    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "att-1")
        ).scalar_one()
        assert payment.attention_resolved_at is None


def test_attention_resolve_requires_a_non_empty_note(
    ops_env, client, settings, session_factory, stub, capsys
):
    _stale_getlink_failed(client, settings, session_factory, stub, order_id="att-note")
    code = ops_main(
        [
            "attention",
            "resolve",
            "att-note",
            "--resolution",
            "stale_getlink_failure",
            "--note",
            "   ",
            "--yes",
        ]
    )
    assert code == 1
    assert "non-empty --note is required" in capsys.readouterr().err


def test_attention_resolve_happy_path_then_list_and_show(
    ops_env, client, settings, session_factory, stub, capsys
):
    _stale_getlink_failed(client, settings, session_factory, stub, order_id="att-2")

    # Open before.
    assert ops_main(["attention", "list"]) == 0
    assert [row["bot_order_id"] for row in _json_lines(capsys)] == ["att-2"]

    assert (
        ops_main(
            [
                "attention",
                "resolve",
                "att-2",
                "--resolution",
                "stale_getlink_failure",
                "--note",
                "getLink ReadTimeout; no link was ever issued",
                "--yes",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "resolved attention for att-2" in out
    assert "status unchanged: getlink_failed" in out

    # Gone from the open list...
    assert ops_main(["attention", "list"]) == 0
    open_output = capsys.readouterr().out
    assert [line for line in open_output.splitlines() if line.startswith("{")] == []
    assert "no open attention items" in open_output

    # ...and present in the historical one.
    assert ops_main(["attention", "list", "--resolved"]) == 0
    rows = _json_lines(capsys)
    assert [row["bot_order_id"] for row in rows] == ["att-2"]
    assert rows[0]["attention_resolution"] == "stale_getlink_failure"
    assert rows[0]["attention_resolved_by"] == "host-cli"


def test_attention_show_reports_original_facts_and_never_the_redirect_url(
    ops_env, client, settings, session_factory, stub, capsys
):
    _stale_getlink_failed(client, settings, session_factory, stub, order_id="att-3")
    assert ops_main(["attention", "show", "att-3"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert payload["status"] == "getlink_failed"
    assert payload["amount"] == 230000
    assert payload["gateway_verified"] is False
    assert payload["reference_id"] is None
    assert payload["resolvable"] is True
    assert payload["eligible_resolutions"] == ["stale_getlink_failure"]
    # Booleans only — a full payment redirect URL must never be printed.
    assert payload["redirect_url_present"] is False
    assert "https://" not in out


def test_attention_show_explains_a_refusal(ops_env, client, settings, session_factory, capsys):
    """A live link_created payment is not resolvable, and `show` says why in
    words, not just a code."""
    assert create_order(client, settings, order_id="att-live").status_code == 200
    assert ops_main(["attention", "show", "att-live"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolvable"] is False
    assert payload["refusal"] == "payment_link_issued"
    assert "redirect_url is set" in payload["refusal_message"]


def test_attention_resolve_refusal_is_reported_without_a_second_lookup(
    ops_env, client, settings, session_factory, stub, capsys
):
    _stale_getlink_failed(client, settings, session_factory, stub, order_id="att-4")
    args = [
        "attention",
        "resolve",
        "att-4",
        "--resolution",
        "stale_getlink_failure",
        "--note",
        "first",
        "--yes",
    ]
    assert ops_main(args) == 0
    capsys.readouterr()
    assert ops_main(args) == 1
    err = capsys.readouterr().err
    assert "already resolved" in err
    assert "stale_getlink_failure" in err
    assert "host-cli" in err


def test_payment_command_still_shows_financial_facts_plus_resolution_history(
    ops_env, cli_env, client, settings, session_factory, stub, capsys
):
    """Problem 1's explicit acceptance criterion: after resolution,
    `centralpay payment ORDER_ID` must still show the ORIGINAL financial and
    failure facts PLUS the resolution and audit history."""
    _stale_getlink_failed(client, settings, session_factory, stub, order_id="att-5")
    assert (
        ops_main(
            [
                "attention",
                "resolve",
                "att-5",
                "--resolution",
                "stale_getlink_failure",
                "--note",
                "closed by operator",
                "--yes",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert cli_main(["payment", "att-5"]) == 0
    lines = _json_lines(capsys)
    summary, events = lines[0], lines[1:]

    # Original financial + failure facts, unchanged.
    assert summary["status"] == "getlink_failed"
    assert summary["original_bot_invoice"] == 230000
    assert summary["paid_through_gateway"] == 230000
    assert summary["gateway_verified"] is False
    assert summary["gateway_verified_at"] is None
    assert summary["reference_id"] is None
    # Plus the resolution.
    assert summary["attention_resolution"] == "stale_getlink_failure"
    assert summary["attention_resolved_by"] == "host-cli"
    assert summary["attention_resolution_note"] == "closed by operator"
    # Plus the full audit history, with the original failure preserved.
    types = [event["event_type"] for event in events]
    assert "payment_created" in types
    assert "payment_fee_snapshotted" in types
    assert "centralpay_getlink_failed" in types
    assert types[-1] == "payment_attention_resolved"


# --- centralpay manual-review (legacy, deprecated) ------------------------


def test_manual_review_lists_only_unresolved_by_default(
    cli_env, session_factory, capsys
):
    """The bug: a resolved review printed exactly like an active one, because
    `review resolve` keeps status='manual_review' as permanent history."""
    _make_review(session_factory, order_id="mr-open", gateway_order_id=930000000001)
    _make_review(
        session_factory,
        order_id="mr-resolved",
        gateway_order_id=930000000002,
        resolved=True,
    )

    assert cli_main(["manual-review"]) == 0
    orders = [row["bot_order_id"] for row in _json_lines(capsys)]
    assert orders == ["mr-open"]


def test_manual_review_all_includes_resolved_and_labels_them(
    cli_env, session_factory, capsys
):
    _make_review(session_factory, order_id="mr-open2", gateway_order_id=930000000003)
    _make_review(
        session_factory,
        order_id="mr-res2",
        gateway_order_id=930000000004,
        resolved=True,
    )

    assert cli_main(["manual-review", "--all"]) == 0
    rows = {row["bot_order_id"]: row for row in _json_lines(capsys)}
    assert set(rows) == {"mr-open2", "mr-res2"}
    # Each resolved row carries its outcome, so the two are never confusable.
    assert rows["mr-res2"]["review_resolved_at"] is not None
    assert rows["mr-res2"]["review_resolution"] == "confirmed_by_bot_operator"
    assert rows["mr-open2"]["review_resolved_at"] is None
    assert rows["mr-open2"]["review_resolution"] is None


def test_manual_review_agrees_with_review_list(
    cli_env, ops_env, session_factory, capsys
):
    """The two commands must never disagree about which reviews are active —
    that disagreement was the whole defect."""
    for index in range(3):
        _make_review(
            session_factory,
            order_id=f"agree-open-{index}",
            gateway_order_id=930000001000 + index,
        )
    for index in range(4):
        _make_review(
            session_factory,
            order_id=f"agree-done-{index}",
            gateway_order_id=930000002000 + index,
            resolved=True,
        )

    assert cli_main(["manual-review"]) == 0
    legacy = {row["bot_order_id"] for row in _json_lines(capsys)}
    assert ops_main(["review", "list"]) == 0
    canonical = {row["bot_order_id"] for row in _json_lines(capsys)}

    assert legacy == canonical
    assert legacy == {f"agree-open-{i}" for i in range(3)}


# --- centralpay review resolve-many --------------------------------------


def test_resolve_many_previews_without_writing(ops_env, session_factory, capsys):
    ids = [
        _make_review(
            session_factory, order_id=f"bulk-{i}", gateway_order_id=940000000000 + i
        )
        for i in range(3)
    ]

    code = ops_main(
        [
            "review",
            "resolve-many",
            "bulk-0",
            "bulk-1",
            "bulk-2",
            "--resolution",
            "confirmed_by_bot_operator",
            "--note",
            "bot operator confirmed",
        ]
    )
    assert code == 1  # preview always exits non-zero: nothing was done
    captured = capsys.readouterr()
    assert "preview" in captured.out
    assert "orders listed: 3" in captured.out
    assert "Re-run with --yes to confirm" in captured.err
    assert "Does NOT contact the selling bot" in captured.err

    with session_factory() as db:
        for payment_id in ids:
            assert db.get(Payment, payment_id).review_resolved_at is None


def test_resolve_many_resolves_the_whole_explicit_batch(
    ops_env, session_factory, capsys
):
    """The exact production scenario: 15 gateway-verified reviews, all
    `retry_limit_reached`, all confirmed credited by the bot operator."""
    ids = [
        _make_review(
            session_factory, order_id=f"prod-{i}", gateway_order_id=950000000000 + i
        )
        for i in range(15)
    ]

    code = ops_main(
        [
            "review",
            "resolve-many",
            *[f"prod-{i}" for i in range(15)],
            "--resolution",
            "confirmed_by_bot_operator",
            "--note",
            "VPN bot records confirm all 15 orders were credited",
            "--yes",
        ]
    )
    assert code == 0
    assert "resolved 15 manual review(s)" in capsys.readouterr().out

    with session_factory() as db:
        for payment_id in ids:
            payment = db.get(Payment, payment_id)
            assert payment.review_resolution == "confirmed_by_bot_operator"
            assert payment.review_resolved_at is not None
            # Status kept as permanent history; financial facts untouched.
            assert payment.status == PaymentStatus.MANUAL_REVIEW.value
            assert payment.amount == 10000
            # as_utc: SQLite returns naive datetimes for our UTC writes.
            assert as_utc(payment.gateway_verified_at) == PAST
            assert payment.reference_id == f"REF-{payment.bot_order_id}"
        events = db.execute(
            select(PaymentEvent.event_type).where(
                PaymentEvent.event_type.in_(
                    ("manual_review_resolved", "manual_review_bulk_resolved")
                )
            )
        ).scalars()
        counts: dict[str, int] = {}
        for event_type in events:
            counts[event_type] = counts.get(event_type, 0) + 1
        assert counts == {"manual_review_resolved": 15, "manual_review_bulk_resolved": 1}


def test_resolve_many_rejects_a_batch_mixing_verified_and_unverified(
    ops_env, session_factory, capsys
):
    """A financial-mismatch set: one shared justification cannot honestly
    cover a gateway-verified payment and one that was never verified."""
    _make_review(session_factory, order_id="mix-v", gateway_order_id=960000000001)
    _make_review(
        session_factory,
        order_id="mix-u",
        gateway_order_id=960000000002,
        gateway_verified=False,
    )

    code = ops_main(
        [
            "review",
            "resolve-many",
            "mix-v",
            "mix-u",
            "--resolution",
            "false_positive",
            "--note",
            "mixed",
            "--yes",
        ]
    )
    assert code == 1
    out = capsys.readouterr()
    assert "mixed_verification_set" in out.out
    with session_factory() as db:
        for order in ("mix-v", "mix-u"):
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order)
            ).scalar_one()
            assert payment.review_resolved_at is None


def test_resolve_many_rejects_a_bot_credited_claim_for_an_unverified_payment(
    ops_env, session_factory, capsys
):
    """`confirmed_by_bot_operator` asserts the downstream bot processed the
    order. That cannot be true for a payment CentralPay never verified."""
    _make_review(
        session_factory,
        order_id="unver-1",
        gateway_order_id=960000000010,
        gateway_verified=False,
    )
    code = ops_main(
        [
            "review",
            "resolve-many",
            "unver-1",
            "--resolution",
            "confirmed_by_bot_operator",
            "--note",
            "claiming",
            "--yes",
        ]
    )
    assert code == 1
    assert "requires_gateway_verified" in capsys.readouterr().out


def test_resolve_many_rejects_duplicates_not_found_and_wrong_status(
    ops_env, session_factory, capsys
):
    _make_review(session_factory, order_id="dup-1", gateway_order_id=960000000020)
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="not-review",
                gateway_order_id=960000000021,
                gateway_user_id=55501234,
                amount=1000,
                payable_amount=1000,
                status=PaymentStatus.LINK_CREATED.value,
            )
        )
        db.commit()

    code = ops_main(
        [
            "review",
            "resolve-many",
            "dup-1",
            "dup-1",
            "ghost-order",
            "not-review",
            "--resolution",
            "false_positive",
            "--note",
            "n",
            "--yes",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "duplicate_order_id" in out
    assert "not_found" in out
    assert "not_manual_review" in out
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "dup-1")
        ).scalar_one()
        assert payment.review_resolved_at is None


def test_resolve_many_has_no_resolve_all_mode(ops_env, session_factory):
    """There must be no way to invoke it without naming every order."""
    with pytest.raises(SystemExit):
        ops_main(
            ["review", "resolve-many", "--resolution", "false_positive", "--note", "n", "--yes"]
        )


def test_resolve_many_refuses_an_oversized_batch(ops_env, session_factory, capsys):
    from app.services.review_resolution import MAX_BULK_SIZE

    orders = [f"huge-{i}" for i in range(MAX_BULK_SIZE + 1)]
    code = ops_main(
        [
            "review",
            "resolve-many",
            *orders,
            "--resolution",
            "false_positive",
            "--note",
            "n",
            "--yes",
        ]
    )
    assert code == 1
    assert "batch_too_large" in capsys.readouterr().out


def test_resolve_many_never_weakens_the_single_payment_command(
    ops_env, session_factory, capsys
):
    """The single-payment path keeps its existing behavior, including the
    deliberate ability to re-record one previously-resolved review (which the
    stricter bulk path refuses)."""
    payment_id = _make_review(
        session_factory, order_id="single-1", gateway_order_id=960000000030, resolved=True
    )
    assert (
        ops_main(
            [
                "review",
                "resolve",
                "single-1",
                "--resolution",
                "bot_not_credited",
                "--note",
                "correcting an earlier mistake",
            ]
        )
        == 0
    )
    with session_factory() as db:
        assert db.get(Payment, payment_id).review_resolution == "bot_not_credited"

    # ...while bulk refuses exactly that.
    capsys.readouterr()
    assert (
        ops_main(
            [
                "review",
                "resolve-many",
                "single-1",
                "--resolution",
                "false_positive",
                "--note",
                "n",
                "--yes",
            ]
        )
        == 1
    )
    assert "already_resolved" in capsys.readouterr().out
