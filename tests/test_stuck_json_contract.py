"""`centralpay stuck --json` summary-field contract.

Production emitted this, which is self-contradictory on its face::

    needs_attention: 1  waiting_gateway: 25  expired: 5788  shown: 20  total: 226

1 + 25 + 5788 = 5814, not 226. `total` was `len(overview.ordered())` — the
size of the MATERIALIZED result set, where each bucket is independently capped
at `stuck_payments._QUERY_CAP` (200). So the observed 226 was 1 + 25 + 200, and
an earlier sample's 254 was 16 + 38 + 200. Any machine consumer summing the
categories and comparing against `total` got a number that silently disagreed
once any category exceeded the cap.

`total` now means the true sum; the old value is exposed explicitly as
`materialized_total`; `truncated` is the single boolean a consumer needs.

The decisive tests here deliberately create MORE rows than the internal cap —
the exact condition under which the old field was wrong and under which no
existing test exercised it.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.cli import _STUCK_DISPLAY_LIMIT_MAX, _cmd_stuck, _stuck_summary_dict
from app.models import Payment, PaymentStatus
from app.services.stuck_payments import (
    _QUERY_CAP,
    UNEXPECTED_STATE_GRACE_SECONDS,
    StuckCategory,
    StuckEntry,
    StuckOverview,
    stuck_payments_overview,
)
from tests.conftest import create_order

OVER_CAP = _QUERY_CAP + 37


def _bulk_expired(session_factory, settings, count: int) -> None:
    """`count` link_created payments aged past the reconciliation lifetime —
    the EXPIRED bucket, which is what overflowed the cap in production."""
    anchor = datetime.now(UTC) - timedelta(
        seconds=settings.reconciliation_max_age_seconds + 3600
    )
    with session_factory() as db:
        for index in range(count):
            db.add(
                Payment(
                    bot_order_id=f"expired-{index}",
                    gateway_order_id=920000000000 + index,
                    gateway_user_id=55501234,
                    amount=10000,
                    payable_amount=10000,
                    status=PaymentStatus.LINK_CREATED.value,
                    redirect_url="https://gateway.test/pay/x",
                    callback_token_hash="d" * 64,
                    callback_token_issued_at=anchor,
                    created_at=anchor,
                )
            )
        db.commit()


def _summary_line(capsys, session_factory, settings, *, limit=20) -> dict[str, Any]:
    with session_factory() as db:
        assert _cmd_stuck(db, settings, limit=limit, as_json=True) == 0
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    summary = lines[0]
    assert summary["type"] == "summary"
    return summary


# --- the contract ---------------------------------------------------------


def test_total_is_the_true_sum_when_everything_fits(
    capsys, client, settings, session_factory
):
    _bulk_expired(session_factory, settings, 5)
    summary = _summary_line(capsys, session_factory, settings)

    assert summary["expired"] == 5
    assert summary["total"] == (
        summary["needs_attention"] + summary["waiting_gateway"] + summary["expired"]
    )
    # Below the cap, the two totals coincide — which is exactly why the old
    # bug never showed up in the existing small-fixture tests.
    assert summary["materialized_total"] == summary["total"]


def test_total_is_still_the_true_sum_when_the_internal_cap_binds(
    capsys, client, settings, session_factory
):
    """THE regression. With more expired rows than `_QUERY_CAP`, `total` must
    stay the exact sum while `materialized_total` reveals the capping."""
    _bulk_expired(session_factory, settings, OVER_CAP)
    summary = _summary_line(capsys, session_factory, settings)

    assert summary["expired"] == OVER_CAP
    assert summary["total"] == OVER_CAP
    assert summary["total"] == (
        summary["needs_attention"] + summary["waiting_gateway"] + summary["expired"]
    )
    # The old semantics, now under an honest name.
    assert summary["materialized_total"] == _QUERY_CAP
    assert summary["materialized_total"] < summary["total"]
    assert summary["truncated"] is True


def test_the_json_can_never_be_internally_self_contradictory(
    capsys, client, settings, session_factory
):
    """The invariant a machine consumer actually relies on, asserted directly
    across a range of sizes that straddles the cap."""
    for count in (0, 1, _QUERY_CAP - 1, _QUERY_CAP, _QUERY_CAP + 1, OVER_CAP):
        with session_factory() as db:
            db.query(Payment).delete()
            db.commit()
        _bulk_expired(session_factory, settings, count)
        summary = _summary_line(capsys, session_factory, settings)

        assert summary["total"] == (
            summary["needs_attention"] + summary["waiting_gateway"] + summary["expired"]
        ), count
        assert summary["shown"] <= summary["materialized_total"] <= summary["total"], count
        assert summary["truncated"] == (summary["shown"] < summary["total"]), count


def test_shown_respects_limit_and_truncated_reports_it(
    capsys, client, settings, session_factory
):
    _bulk_expired(session_factory, settings, 30)
    summary = _summary_line(capsys, session_factory, settings, limit=5)
    assert summary["shown"] == 5
    assert summary["total"] == 30
    assert summary["truncated"] is True


def test_nothing_stuck_reports_zeroes_and_not_truncated(
    capsys, client, settings, session_factory
):
    summary = _summary_line(capsys, session_factory, settings)
    assert summary["total"] == 0
    assert summary["materialized_total"] == 0
    assert summary["shown"] == 0
    assert summary["truncated"] is False


def test_the_legacy_category_counts_are_unchanged(
    capsys, client, settings, session_factory, stub
):
    """Backward compatibility: the three category fields keep their exact
    previous meaning and values. Only `total` was redefined, and only in the
    direction every consumer already assumed."""
    import httpx

    stub.getlink_result = httpx.ReadTimeout("read timed out")
    assert create_order(client, settings, order_id="legacy-1").status_code >= 400
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "legacy-1")
        ).scalar_one()
        payment.created_at = datetime.now(UTC) - timedelta(
            seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60
        )
        db.commit()
    _bulk_expired(session_factory, settings, 3)

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    summary = _summary_line(capsys, session_factory, settings)

    for key in ("needs_attention", "waiting_gateway", "expired"):
        assert summary[key] == overview.total_counts[key]


# --- human mode -----------------------------------------------------------


def test_human_footer_tells_the_truth_when_the_cap_is_the_constraint(
    capsys, client, settings, session_factory
):
    """The old footer always said "raise --limit to see more", which is false
    once the per-bucket cap is binding: no --limit can surface a row the
    capped query never materialized."""
    _bulk_expired(session_factory, settings, OVER_CAP)
    with session_factory() as db:
        assert _cmd_stuck(db, settings, limit=_QUERY_CAP, as_json=False) == 0
    out = capsys.readouterr().out
    assert "capped at" in out
    assert "cannot reveal them" in out
    assert "the summary counts above are still exact" in out


def test_human_footer_still_suggests_limit_when_limit_is_the_constraint(
    capsys, client, settings, session_factory
):
    _bulk_expired(session_factory, settings, 30)
    with session_factory() as db:
        assert _cmd_stuck(db, settings, limit=5, as_json=False) == 0
    out = capsys.readouterr().out
    assert "raise --limit to see more" in out
    assert "capped at" not in out


def test_human_footer_never_suggests_a_limit_the_command_would_clamp(
    capsys, client, settings, session_factory
):
    """The other half of the same defect.

    Each of the three buckets materializes up to `_QUERY_CAP` INDEPENDENTLY, so
    `len(overview.ordered())` can exceed the single `--limit` ceiling
    `_cmd_stuck` clamps to. `ordered_count > shown_count` was therefore true
    even at the maximum, and an operator already passing `--limit 200` was told
    to raise a limit the command silently clamps right back.
    """
    _bulk_expired(session_factory, settings, _QUERY_CAP + 10)
    stale = datetime.now(UTC) - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)
    with session_factory() as db:
        for index in range(_QUERY_CAP + 10):
            db.add(
                Payment(
                    bot_order_id=f"attn-{index}",
                    gateway_order_id=921000000000 + index,
                    gateway_user_id=55501234,
                    amount=1000,
                    payable_amount=1000,
                    status=PaymentStatus.GETLINK_FAILED.value,
                    created_at=stale,
                )
            )
        db.commit()

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    # More materialized rows than any single --limit may request.
    assert len(overview.ordered()) > _STUCK_DISPLAY_LIMIT_MAX

    with session_factory() as db:
        assert (
            _cmd_stuck(db, settings, limit=_STUCK_DISPLAY_LIMIT_MAX, as_json=False) == 0
        )
    out = capsys.readouterr().out
    assert "raise --limit to see more" not in out
    assert "cannot reveal them" in out


# --- the helper in isolation ---------------------------------------------


@pytest.mark.parametrize(
    ("counts", "materialized", "shown"),
    [
        ({"needs_attention": 1, "waiting_gateway": 25, "expired": 5788}, 226, 20),
        ({"needs_attention": 16, "waiting_gateway": 38, "expired": 5763}, 254, 20),
    ],
)
def test_the_exact_production_outputs_are_now_coherent(counts, materialized, shown):
    """Replay the two real production summaries. `materialized_total`
    reproduces the old `total` exactly (proving the diagnosis: it was
    needs_attention + waiting_gateway + min(expired, 200)), while `total`
    now reports the truth."""

    # A REAL StuckOverview whose exact category counts and materialized entry
    # list deliberately disagree — precisely the state the per-bucket cap
    # produces, and the state the old `total` reported incorrectly.
    placeholder = Payment(
        bot_order_id="replay",
        gateway_order_id=1,
        gateway_user_id=1,
        amount=1,
        payable_amount=1,
        status=PaymentStatus.LINK_CREATED.value,
    )
    entries = [
        StuckEntry(
            payment=placeholder,
            category=StuckCategory.EXPIRED,
            reason="reconciliation_max_age_exceeded",
        )
    ] * materialized
    overview = StuckOverview(
        needs_attention=[],
        waiting_gateway=[],
        expired=entries,
        total_counts=counts,
    )
    summary = _stuck_summary_dict(overview, shown_count=shown)
    assert summary["materialized_total"] == materialized
    assert summary["total"] == sum(counts.values())
    assert summary["truncated"] is True
    assert (
        counts["needs_attention"] + counts["waiting_gateway"] + _QUERY_CAP == materialized
    )
