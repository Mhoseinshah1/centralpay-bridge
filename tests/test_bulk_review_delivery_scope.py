"""Bulk review resolution is confined to downstream-DELIVERY failures.

The original requirement said bulk resolution must reject financial-mismatch
sets. An earlier version of `refuse_reason` enforced only status,
not-already-resolved, and gateway-verification for the two resolution codes
that assert the bot credited the order. A HOMOGENEOUS batch of
financial/verification manual reviews therefore passed — e.g. with
`resolution=false_positive`, which is not one of those two codes — so an
operator could blanket-close amount, user-id, reference-id, callback, and
configuration mismatches without ever looking at them individually. Those are
precisely the reviews where a wrong blanket judgement has financial
consequences.

`app.adminbot.queries` already owns the authoritative split: an open manual
review is a bot-delivery problem iff `bot_notify_reason IS NOT NULL`
(`non_delivery_manual_review_conditions` is its exact complement). Bulk now
fails CLOSED on the financial half, and narrows the delivery half further to
the canonical allowlist.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from app.adminbot import queries
from app.models import Payment, PaymentEvent, PaymentStatus
from app.ops import main as ops_main
from app.reasons import ReasonCode
from app.services import review_resolution
from app.services.bulk_resend import ELIGIBLE_RESEND_REASONS

PAST = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
def ops_env(settings, session_factory, monkeypatch):
    import app.ops as ops_module

    monkeypatch.setattr(ops_module, "Settings", lambda: settings)
    monkeypatch.setattr(ops_module, "create_session_factory", lambda url: session_factory)
    monkeypatch.setattr(ops_module, "configure_logging", lambda s: None)
    return settings


def _make_review(
    session_factory,
    *,
    order_id: str,
    gateway_order_id: int,
    reason: str | None,
    gateway_verified: bool = True,
) -> int:
    """`reason=None` builds a FINANCIAL/verification manual review — exactly
    what `app.services.verification` produces on a mismatch: it never reaches
    notification, so `bot_notify_reason` stays NULL."""
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
            bot_notify_attempts=5 if reason else 0,
            gateway_verified_at=PAST if gateway_verified else None,
            reference_id=f"REF-{order_id}" if gateway_verified else None,
        )
        db.add(payment)
        db.commit()
        return payment.id


def _assert_nothing_resolved(session_factory, *order_ids: str) -> None:
    with session_factory() as db:
        for order_id in order_ids:
            payment = db.execute(
                select(Payment).where(Payment.bot_order_id == order_id)
            ).scalar_one()
            assert payment.review_resolved_at is None, order_id
            assert payment.review_resolution is None, order_id
        assert (
            db.execute(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.event_type.in_(
                        ("manual_review_resolved", "manual_review_bulk_resolved")
                    )
                )
            ).scalar_one()
            == 0
        )


# --- the allowlist is the canonical one, not a copy ----------------------


def test_the_bulk_allowlist_is_the_canonical_object_not_a_duplicate():
    """Reuse, not restatement: a second literal set of reason codes would
    silently drift the day one side is broadened."""
    assert review_resolution.BULK_ELIGIBLE_DELIVERY_REASONS is ELIGIBLE_RESEND_REASONS
    assert {
        ReasonCode.RETRY_LIMIT_REACHED.value,
        ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value,
    } == review_resolution.BULK_ELIGIBLE_DELIVERY_REASONS


def test_every_refusal_code_has_a_message():
    for refusal in review_resolution.BulkReviewRefusal:
        assert refusal in review_resolution.REFUSAL_MESSAGE


def test_the_bulk_scope_matches_the_authoritative_delivery_split(session_factory):
    """`queries` partitions open manual review on `bot_notify_reason IS NOT
    NULL`. Bulk's eligible population must be a strict SUBSET of the delivery
    half — never overlapping the financial half."""
    _make_review(
        session_factory, order_id="split-delivery", gateway_order_id=810000000001,
        reason=ReasonCode.RETRY_LIMIT_REACHED.value,
    )
    _make_review(
        session_factory, order_id="split-financial", gateway_order_id=810000000002,
        reason=None,
    )
    with session_factory() as db:
        financial = list(
            db.execute(
                select(Payment.bot_order_id).where(
                    *queries.non_delivery_manual_review_conditions()
                )
            ).scalars()
        )
        rows = list(db.execute(select(Payment)).scalars())
    assert financial == ["split-financial"]

    eligible = [
        row.bot_order_id
        for row in rows
        if review_resolution.refuse_reason(row, resolution="false_positive") is None
    ]
    assert eligible == ["split-delivery"]


# --- required regression cases -------------------------------------------


def test_a_homogeneous_financial_review_batch_is_refused(
    ops_env, session_factory, capsys
):
    """THE gap. Two financial/verification reviews with the SAME verification
    shape and `resolution=false_positive` passed every earlier check."""
    _make_review(
        session_factory, order_id="fin-1", gateway_order_id=810000001001, reason=None
    )
    _make_review(
        session_factory, order_id="fin-2", gateway_order_id=810000001002, reason=None
    )

    code = ops_main(
        [
            "review", "resolve-many", "fin-1", "fin-2",
            "--resolution", "false_positive",
            "--note", "blanket close", "--yes",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert out.count("financial_review_requires_individual_resolution") == 2
    assert "resolve it individually" in out
    _assert_nothing_resolved(session_factory, "fin-1", "fin-2")


def test_one_financial_row_rejects_the_entire_batch(ops_env, session_factory, capsys):
    """All-or-nothing: a single financial review blocks otherwise-eligible
    delivery failures, and NOTHING is mutated."""
    _make_review(
        session_factory, order_id="mixed-del-1", gateway_order_id=810000002001,
        reason=ReasonCode.RETRY_LIMIT_REACHED.value,
    )
    _make_review(
        session_factory, order_id="mixed-fin-1", gateway_order_id=810000002002,
        reason=None,
    )

    code = ops_main(
        [
            "review", "resolve-many", "mixed-del-1", "mixed-fin-1",
            "--resolution", "confirmed_by_bot_operator",
            "--note", "bot operator confirmed", "--yes",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "financial_review_requires_individual_resolution" in out
    _assert_nothing_resolved(session_factory, "mixed-del-1", "mixed-fin-1")


@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.BOT_HTTP_403.value,
        ReasonCode.BOT_HTTP_422.value,
        ReasonCode.BOT_INVALID_CONFIGURATION.value,
    ],
)
def test_a_non_allowlisted_delivery_reason_is_refused(
    ops_env, session_factory, capsys, reason
):
    """An allowlist, not merely `IS NOT NULL`: an explicit bot 4xx or a
    misconfiguration is a delivery reason the 'the bot already credited these'
    workflow was never designed around."""
    _make_review(
        session_factory, order_id=f"nal-{reason}", gateway_order_id=810000003001,
        reason=reason,
    )
    code = ops_main(
        [
            "review", "resolve-many", f"nal-{reason}",
            "--resolution", "confirmed_by_bot_operator",
            "--note", "n", "--yes",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "delivery_reason_not_bulk_eligible" in out
    assert reason in out
    _assert_nothing_resolved(session_factory, f"nal-{reason}")


@pytest.mark.parametrize(
    "reason",
    [ReasonCode.RETRY_LIMIT_REACHED.value, ReasonCode.BOT_TIMEOUT_AMBIGUOUS.value],
)
def test_an_allowlisted_delivery_batch_still_succeeds(
    ops_env, session_factory, capsys, reason
):
    """The production workflow must keep working: the narrowing must not break
    the case bulk resolution exists for."""
    ids = [
        _make_review(
            session_factory, order_id=f"ok-{reason}-{index}",
            gateway_order_id=810000004000 + index, reason=reason,
        )
        for index in range(3)
    ]
    code = ops_main(
        [
            "review", "resolve-many",
            *[f"ok-{reason}-{index}" for index in range(3)],
            "--resolution", "confirmed_by_bot_operator",
            "--note", "bot records confirm all were credited", "--yes",
        ]
    )
    assert code == 0
    assert "resolved 3 manual review(s)" in capsys.readouterr().out
    with session_factory() as db:
        for payment_id in ids:
            payment = db.get(Payment, payment_id)
            assert payment.review_resolution == "confirmed_by_bot_operator"
            # Financial facts and the permanent status are untouched.
            assert payment.status == PaymentStatus.MANUAL_REVIEW.value
            assert payment.amount == 10000
            assert payment.gateway_verified_at is not None


def test_the_preview_also_refuses_a_financial_review(ops_env, session_factory, capsys):
    """Requirement: the rule applies during PREVIEW as well as under lock, so
    an operator is told before they add --yes."""
    _make_review(
        session_factory, order_id="prev-fin", gateway_order_id=810000005001, reason=None
    )
    code = ops_main(
        [
            "review", "resolve-many", "prev-fin",
            "--resolution", "false_positive", "--note", "n",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "financial_review_requires_individual_resolution" in captured.out
    # And the preview states the scope up front.
    assert "allowlisted downstream-DELIVERY failures only" in captured.out
    assert "Re-run with --yes" not in captured.err
    _assert_nothing_resolved(session_factory, "prev-fin")


# --- the single-payment path is untouched --------------------------------


def test_single_payment_resolve_still_handles_a_financial_review(
    ops_env, session_factory, capsys
):
    """Requirement 2. Financial/verification reviews must remain resolvable
    individually after investigation — bulk is narrowed, the one-payment
    workflow is not."""
    payment_id = _make_review(
        session_factory, order_id="single-fin", gateway_order_id=810000006001, reason=None
    )
    code = ops_main(
        [
            "review", "resolve", "single-fin",
            "--resolution", "false_positive",
            "--note", "investigated: gateway double-reported, no customer impact",
        ]
    )
    assert code == 0
    with session_factory() as db:
        payment = db.get(Payment, payment_id)
        assert payment.review_resolution == "false_positive"
        assert payment.review_resolved_at is not None
        # Still no financial mutation from the single-payment path either.
        assert payment.status == PaymentStatus.MANUAL_REVIEW.value
        assert payment.amount == 10000


def test_single_payment_resolve_still_handles_a_non_allowlisted_delivery_reason(
    ops_env, session_factory
):
    payment_id = _make_review(
        session_factory, order_id="single-403", gateway_order_id=810000006002,
        reason=ReasonCode.BOT_HTTP_403.value,
    )
    assert (
        ops_main(
            [
                "review", "resolve", "single-403",
                "--resolution", "configuration_fixed",
                "--note", "bot token corrected",
            ]
        )
        == 0
    )
    with session_factory() as db:
        assert db.get(Payment, payment_id).review_resolution == "configuration_fixed"


def test_review_list_still_shows_financial_reviews_as_open(
    ops_env, session_factory, capsys
):
    """Narrowing BULK must not hide financial reviews from the worklist —
    they still need an operator, just individually."""
    _make_review(
        session_factory, order_id="list-fin", gateway_order_id=810000007001, reason=None
    )
    assert ops_main(["review", "list"]) == 0
    rows: list[dict[str, Any]] = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert [row["bot_order_id"] for row in rows] == ["list-fin"]
