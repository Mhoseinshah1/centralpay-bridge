"""Regressions for five defects found by review on PR #89.

Each was verified against source before fixing; each test states the concrete
failure mode so the reason survives independently of the review thread.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.adminbot import queries
from app.cli import _cmd_stuck
from app.cli import main as cli_main
from app.models import Payment, PaymentEvent, PaymentStatus
from app.ops import main as ops_main
from app.services import attention
from app.services.stuck_payments import (
    _QUERY_CAP,
    UNEXPECTED_STATE_GRACE_SECONDS,
    stuck_payments_overview,
)
from tests.conftest import create_order


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


def _json_lines(capsys) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]


def _age_created(session_factory, order_id: str, *, seconds: int) -> None:
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == order_id)
        ).scalar_one()
        payment.created_at = datetime.now(UTC) - timedelta(seconds=seconds)
        db.commit()


# --- Finding 5: a NEW failure after a resolution must reopen -------------


def test_a_second_getlink_failure_reopens_a_resolved_attention_item(
    client, settings, session_factory, stub
):
    """THE serious one.

    `create_payment` deliberately RETRIES getLink for an existing
    `getlink_failed` row (allocating a fresh gateway_order_id first). If that
    retry also fails, the row returns to `getlink_failed` while
    `attention_resolved_at` still holds the operator's judgment about the
    PREVIOUS incident. With a plain `IS NULL` predicate the new failure was
    hidden from every attention surface forever, AND the operator could not
    record a new resolution because the service refused already-resolved rows
    — a real problem made permanently invisible.
    """
    stub.getlink_result = httpx.ReadTimeout("read timed out")
    assert create_order(client, settings, order_id="reopen-1").status_code >= 400
    _age_created(session_factory, "reopen-1", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)

    with session_factory() as db:
        payment_id = db.execute(
            select(Payment).where(Payment.bot_order_id == "reopen-1")
        ).scalar_one().id
        assert attention.resolve_attention(
            db,
            payment_id=payment_id,
            resolution="stale_getlink_failure",
            note="first incident",
            actor="host-cli",
            now=datetime.now(UTC),
        ).resolved is True

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert overview.total_counts["needs_attention"] == 0

    # The upstream bot retries the same order; getLink fails AGAIN.
    assert create_order(client, settings, order_id="reopen-1").status_code >= 400
    _age_created(session_factory, "reopen-1", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)

    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "reopen-1")
        ).scalar_one()
        assert payment.status == PaymentStatus.GETLINK_FAILED.value
        # The earlier resolution record is NOT destroyed...
        assert payment.attention_resolution == "stale_getlink_failure"
        overview = stuck_payments_overview(db, settings)
    # ...but the NEW failure is visible again on every current surface.
    assert overview.total_counts["needs_attention"] == 1
    assert "reopen-1" in [e.payment.bot_order_id for e in overview.ordered()]


def test_the_operator_can_resolve_the_reopened_incident(
    client, settings, session_factory, stub
):
    """Reopening is useless if the operator cannot then close the new
    incident. The refusal guard must apply the SAME supersession rule the
    worklist predicate does."""
    stub.getlink_result = httpx.ReadTimeout("read timed out")
    assert create_order(client, settings, order_id="reopen-2").status_code >= 400
    _age_created(session_factory, "reopen-2", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)
    with session_factory() as db:
        payment_id = db.execute(
            select(Payment).where(Payment.bot_order_id == "reopen-2")
        ).scalar_one().id
        attention.resolve_attention(
            db,
            payment_id=payment_id,
            resolution="stale_getlink_failure",
            note="first",
            actor="host-cli",
            now=datetime.now(UTC),
        )
    assert create_order(client, settings, order_id="reopen-2").status_code >= 400
    _age_created(session_factory, "reopen-2", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)

    with session_factory() as db:
        outcome = attention.resolve_attention(
            db,
            payment_id=payment_id,
            resolution="stale_getlink_failure",
            note="second incident, also reviewed",
            actor="host-cli-2",
            now=datetime.now(UTC),
        )
    assert outcome.resolved is True

    with session_factory() as db:
        payment = db.get(Payment, payment_id)
        assert payment.attention_resolution_note == "second incident, also reviewed"
        events = list(
            db.execute(
                select(PaymentEvent).where(
                    PaymentEvent.payment_id == payment_id,
                    PaymentEvent.event_type == "payment_attention_resolved",
                ).order_by(PaymentEvent.id)
            ).scalars()
        )
    # Both resolutions are permanently recorded; the second says it superseded.
    assert len(events) == 2
    assert events[0].data["note"] == "first"
    assert events[1].data["superseded_previous_resolution"] is True
    assert events[1].data["previous_resolution"] == "stale_getlink_failure"


def test_a_resolution_with_no_later_failure_is_still_refused(
    client, settings, session_factory, stub
):
    """Supersession must not weaken the duplicate-action guard in the ordinary
    case: with no NEW failure, a second resolve is still refused."""
    stub.getlink_result = httpx.ReadTimeout("read timed out")
    assert create_order(client, settings, order_id="reopen-3").status_code >= 400
    _age_created(session_factory, "reopen-3", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)
    with session_factory() as db:
        payment_id = db.execute(
            select(Payment).where(Payment.bot_order_id == "reopen-3")
        ).scalar_one().id
        attention.resolve_attention(
            db, payment_id=payment_id, resolution="stale_getlink_failure",
            note="first", actor="host-cli", now=datetime.now(UTC),
        )
        outcome = attention.resolve_attention(
            db, payment_id=payment_id, resolution="stale_getlink_failure",
            note="second", actor="host-cli", now=datetime.now(UTC),
        )
    assert outcome.resolved is False
    assert outcome.refusal is attention.AttentionRefusal.ALREADY_RESOLVED


# --- Finding 3: the open listing must use the canonical predicate --------


def test_attention_list_does_not_show_an_in_flight_payment_creation(
    ops_env, client, settings, session_factory, capsys
):
    """`create_payment` commits the `created` row BEFORE attempting getLink, so
    a plain `status IN (...) AND attention_resolved_at IS NULL` read sees every
    in-flight creation. `centralpay stuck` and the admin bot deliberately
    exclude such a row for UNEXPECTED_STATE_GRACE_SECONDS; `attention list`
    must agree with them."""
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="inflight-1",
                gateway_order_id=970000000001,
                gateway_user_id=55501234,
                amount=1000,
                payable_amount=1000,
                status=PaymentStatus.CREATED.value,
                created_at=datetime.now(UTC),  # brand new: inside the grace period
            )
        )
        db.commit()

    assert ops_main(["attention", "list"]) == 0
    assert _json_lines(capsys) == []

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert overview.total_counts["needs_attention"] == 0

    # Past the grace period both surfaces show it, still in agreement.
    _age_created(session_factory, "inflight-1", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)
    assert ops_main(["attention", "list"]) == 0
    assert [r["bot_order_id"] for r in _json_lines(capsys)] == ["inflight-1"]
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    assert overview.total_counts["needs_attention"] == 1


# --- Finding 2: the historical listing must not filter by status --------


def test_attention_list_resolved_keeps_a_payment_that_settled_afterwards(
    ops_env, client, settings, session_factory, stub, capsys
):
    """A resolved payment can legitimately settle later through a late
    callback, which moves it OUT of the resolvable statuses while it keeps its
    resolution columns. Scoping the historical listing by status dropped
    exactly that case — the most interesting one — contradicting the
    durability this feature promises."""
    from tests.conftest import valid_callback_path, verify_ok_response

    stub.getlink_result = httpx.ReadTimeout("read timed out")
    assert create_order(client, settings, order_id="late-1").status_code >= 400
    _age_created(session_factory, "late-1", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "late-1")
        ).scalar_one()
        payment_id, gw_order_id = payment.id, payment.gateway_order_id
        payable, gw_user = payment.payable_amount, payment.gateway_user_id
        attention.resolve_attention(
            db, payment_id=payment_id, resolution="stale_getlink_failure",
            note="closed", actor="host-cli", now=datetime.now(UTC),
        )

    stub.verify_result = verify_ok_response(
        amount=payable, user_id=gw_user, reference_id="REF-LATE-HIST"
    )
    assert client.get(valid_callback_path(stub, gw_order_id)).status_code == 200

    with session_factory() as db:
        settled = db.get(Payment, payment_id)
        assert settled.status not in attention.RESOLVABLE_STATUSES

    assert ops_main(["attention", "list", "--resolved"]) == 0
    rows = _json_lines(capsys)
    assert [r["bot_order_id"] for r in rows] == ["late-1"]
    assert rows[0]["attention_resolution"] == "stale_getlink_failure"
    assert rows[0]["gateway_verified"] is True


# --- Finding 1: two aliases naming the same payment ---------------------


def test_resolve_many_rejects_two_aliases_of_the_same_payment(
    ops_env, session_factory, capsys
):
    """A payment can be named by its bot_order_id AND by its numeric
    gateway_order_id. Those strings differ, so a string-only duplicate check
    passed: the preview reported two eligible reviews, execution mutated one
    locked row, and the CLI printed two success lines above `resolved 1`."""
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="alias-1",
                gateway_order_id=980000000001,
                gateway_user_id=55501234,
                amount=10000,
                payable_amount=10000,
                status=PaymentStatus.MANUAL_REVIEW.value,
                manual_review_at=datetime(2026, 8, 1, tzinfo=UTC),
                bot_notify_reason="retry_limit_reached",
                bot_notify_attempts=5,
                gateway_verified_at=datetime(2026, 8, 1, tzinfo=UTC),
                reference_id="REF-alias-1",
            )
        )
        db.commit()

    code = ops_main(
        [
            "review", "resolve-many", "alias-1", "980000000001",
            "--resolution", "confirmed_by_bot_operator",
            "--note", "aliases", "--yes",
        ]
    )
    assert code == 1
    assert "duplicate_order_id" in capsys.readouterr().out
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "alias-1")
        ).scalar_one()
        assert payment.review_resolved_at is None


# --- Finding 4: needs_attention must be exact past the cap --------------


def test_needs_attention_is_exact_beyond_the_materialization_cap(
    client, settings, session_factory, capsys
):
    """`stuck_payments_overview` derived `needs_attention` from
    `len(reused_attention)`, and that list is capped at `_QUERY_CAP`. Once
    `stuck --json` publishes the category counts as exact totals (and derives
    `total`/`truncated` from them), a saturating value understates the
    worklist and can even report `truncated: false` while rows are hidden."""
    over_cap = _QUERY_CAP + 25
    review_at = datetime.now(UTC) - timedelta(hours=1)
    with session_factory() as db:
        for index in range(over_cap):
            db.add(
                Payment(
                    bot_order_id=f"mr-{index}",
                    gateway_order_id=990000000000 + index,
                    gateway_user_id=55501234,
                    amount=10000,
                    payable_amount=10000,
                    status=PaymentStatus.MANUAL_REVIEW.value,
                    manual_review_at=review_at,
                    bot_notify_reason="retry_limit_reached",
                    bot_notify_attempts=5,
                    gateway_verified_at=review_at,
                    reference_id=f"REF-mr-{index}",
                )
            )
        db.commit()

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
        snapshot = queries.open_attention_snapshot(
            db, now=datetime.now(UTC), limit=_QUERY_CAP
        )
    # The total is exact and unbounded; only the ENTRIES are capped.
    assert snapshot.total == over_cap
    assert len(snapshot.entries) == _QUERY_CAP
    assert overview.total_counts["needs_attention"] == over_cap  # not _QUERY_CAP

    with session_factory() as db:
        assert _cmd_stuck(db, settings, limit=20, as_json=True) == 0
    summary = _json_lines(capsys)[0]
    assert summary["needs_attention"] == over_cap
    assert summary["total"] == over_cap
    assert summary["truncated"] is True


def test_the_cli_and_the_admin_bot_still_agree_beyond_the_cap(
    client, settings, session_factory
):
    """The two surfaces must agree at every scale, not just below the cap —
    the admin bot's number was already exact, so only the CLI's was wrong."""
    from app.services.stuck_payments import count_other_attention

    over_cap = _QUERY_CAP + 7
    review_at = datetime.now(UTC) - timedelta(hours=1)
    with session_factory() as db:
        for index in range(over_cap):
            db.add(
                Payment(
                    bot_order_id=f"agree-{index}",
                    gateway_order_id=991000000000 + index,
                    gateway_user_id=55501234,
                    amount=10000,
                    payable_amount=10000,
                    status=PaymentStatus.MANUAL_REVIEW.value,
                    manual_review_at=review_at,
                    bot_notify_reason="retry_limit_reached",
                    bot_notify_attempts=5,
                    gateway_verified_at=review_at,
                    reference_id=f"REF-agree-{index}",
                )
            )
        db.commit()

    now = datetime.now(UTC)
    with session_factory() as db:
        cli_total = stuck_payments_overview(db, settings).total_counts["needs_attention"]
        bot_total = (
            queries.bot_delivery_snapshot(db, now=now).total
            + count_other_attention(db, settings, now=now)
        )
    assert cli_total == bot_total == over_cap


# --- entries and their exact total must come from ONE statement ----------


def test_delivery_attention_entries_and_total_come_from_one_statement(
    session_factory,
):
    """`queries.bot_delivery_snapshot` documents this hazard at length and
    solves it with a window function; the overview's reused bucket briefly
    reintroduced it as a capped list PLUS a separate COUNT.

    Two statements are two READ COMMITTED snapshots, so a worker delivering a
    stale pending payment (or an operator resolving a review) between them
    could leave the overview carrying a detail entry while reporting
    `needs_attention: 0` — self-contradictory now that those fields are
    published as exact. Asserted structurally: the snapshot's total and its
    entries are produced by a single call, and the total is exact while the
    entries are capped.
    """
    review_at = datetime.now(UTC) - timedelta(hours=1)
    with session_factory() as db:
        for index in range(_QUERY_CAP + 12):
            db.add(
                Payment(
                    bot_order_id=f"one-stmt-{index}",
                    gateway_order_id=992000000000 + index,
                    gateway_user_id=55501234,
                    amount=10000,
                    payable_amount=10000,
                    status=PaymentStatus.MANUAL_REVIEW.value,
                    manual_review_at=review_at,
                    bot_notify_reason="retry_limit_reached",
                    bot_notify_attempts=5,
                    gateway_verified_at=review_at,
                    reference_id=f"REF-one-stmt-{index}",
                )
            )
        db.commit()

    with session_factory() as db:
        snapshot = queries.open_attention_snapshot(
            db, now=datetime.now(UTC), limit=_QUERY_CAP
        )
    assert snapshot.total == _QUERY_CAP + 12  # exact, unbounded
    assert len(snapshot.entries) == _QUERY_CAP  # capped


def test_the_overview_never_reports_fewer_than_it_renders(
    client, settings, session_factory
):
    """The concrete self-contradiction the fused statement rules out: a
    rendered NEEDS_ATTENTION entry that the summary count does not include."""
    review_at = datetime.now(UTC) - timedelta(hours=1)
    with session_factory() as db:
        for index in range(5):
            db.add(
                Payment(
                    bot_order_id=f"consistent-{index}",
                    gateway_order_id=993000000000 + index,
                    gateway_user_id=55501234,
                    amount=10000,
                    payable_amount=10000,
                    status=PaymentStatus.MANUAL_REVIEW.value,
                    manual_review_at=review_at,
                    bot_notify_reason="retry_limit_reached",
                    bot_notify_attempts=5,
                    gateway_verified_at=review_at,
                    reference_id=f"REF-consistent-{index}",
                )
            )
        db.commit()

    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
    rendered = [
        entry for entry in overview.ordered() if entry.category.value == "needs_attention"
    ]
    assert len(rendered) == 5
    assert overview.total_counts["needs_attention"] >= len(rendered)
    assert overview.total_counts["needs_attention"] == 5


# --- historical attention listing shows the NEWEST resolutions -----------


def test_attention_list_resolved_shows_the_most_recent_decisions_first(
    ops_env, session_factory, capsys
):
    """Ordering history by `created_at` ascending meant that past `--limit`
    resolutions an operator only ever saw the OLDEST payments by creation date
    and could never reach the decisions just made — with no pagination to get
    there. "What did we just close?" is the question this view answers."""
    base = datetime.now(UTC) - timedelta(days=10)
    with session_factory() as db:
        for index in range(8):
            db.add(
                Payment(
                    bot_order_id=f"hist-{index}",
                    gateway_order_id=994000000000 + index,
                    gateway_user_id=55501234,
                    amount=1000,
                    payable_amount=1000,
                    status=PaymentStatus.GETLINK_FAILED.value,
                    # Created oldest-first...
                    created_at=base + timedelta(hours=index),
                    # ...but resolved in the REVERSE order.
                    attention_resolved_at=base + timedelta(days=1, hours=8 - index),
                    attention_resolution="stale_getlink_failure",
                    attention_resolved_by="host-cli",
                    attention_resolution_note=f"note {index}",
                )
            )
        db.commit()

    assert ops_main(["attention", "list", "--resolved", "--limit", "3"]) == 0
    rows = _json_lines(capsys)
    # Newest RESOLUTION first -> hist-0, hist-1, hist-2 were resolved last.
    assert [row["bot_order_id"] for row in rows] == ["hist-0", "hist-1", "hist-2"]

    resolved_times = [row["attention_resolved_at"] for row in rows]
    assert resolved_times == sorted(resolved_times, reverse=True)


def test_the_open_attention_listing_stays_oldest_first(
    ops_env, session_factory, capsys
):
    """The worklist keeps most-urgent-first ordering: only the HISTORICAL
    branch was reordered."""
    base = datetime.now(UTC) - timedelta(days=5)
    with session_factory() as db:
        for index in range(3):
            db.add(
                Payment(
                    bot_order_id=f"open-order-{index}",
                    gateway_order_id=995000000000 + index,
                    gateway_user_id=55501234,
                    amount=1000,
                    payable_amount=1000,
                    status=PaymentStatus.GETLINK_FAILED.value,
                    created_at=base + timedelta(hours=index),
                )
            )
        db.commit()

    assert ops_main(["attention", "list"]) == 0
    rows = _json_lines(capsys)
    assert [row["bot_order_id"] for row in rows] == [
        "open-order-0",
        "open-order-1",
        "open-order-2",
    ]


# --- a blank note must never produce an unauditable resolution -----------


def test_the_service_refuses_a_blank_note_or_actor(
    client, settings, session_factory, stub
):
    """The CLI already rejected `--note "   "`, but `resolve_attention` only
    TRUNCATED it. This module claims to own every safety decision, and the
    consistency CHECK rejects only NULL — an empty string satisfied it and
    recorded a resolution with no stated justification, even though the note
    is one of the four fields whose purpose is to say WHY."""
    stub.getlink_result = httpx.ReadTimeout("read timed out")
    assert create_order(client, settings, order_id="blank-1").status_code >= 400
    _age_created(session_factory, "blank-1", seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60)
    with session_factory() as db:
        payment_id = db.execute(
            select(Payment).where(Payment.bot_order_id == "blank-1")
        ).scalar_one().id

    for note, actor in (("", "host-cli"), ("   ", "host-cli"), ("ok", "  ")):
        with session_factory() as db:
            outcome = attention.resolve_attention(
                db,
                payment_id=payment_id,
                resolution="stale_getlink_failure",
                note=note,
                actor=actor,
                now=datetime.now(UTC),
            )
        assert outcome.resolved is False
        assert outcome.refusal is attention.AttentionRefusal.EMPTY_NOTE

    with session_factory() as db:
        assert db.get(Payment, payment_id).attention_resolved_at is None


# --- the mutating path honours the same grace period as the worklist -----


def test_a_brand_new_row_cannot_be_resolved_before_it_is_stale(session_factory):
    """`app.services.payments._ensure_payment_row` COMMITS the `created` row
    and releases its lock before `create_payment` re-acquires it to attempt
    getLink, so a brand-new row is briefly visible and lock-free.

    `attention list` and `centralpay stuck` both hide it for the grace period.
    The mutating path must agree: otherwise it could close an incident that has
    not happened yet, and if creation then died hard without writing a
    `centralpay_getlink_failed` event, the supersession rule would never fire
    and the abandoned row would stay hidden."""
    with session_factory() as db:
        payment = Payment(
            bot_order_id="fresh-1",
            gateway_order_id=996000000001,
            gateway_user_id=55501234,
            amount=1000,
            payable_amount=1000,
            status=PaymentStatus.CREATED.value,
            created_at=datetime.now(UTC),  # just committed, mid-creation
        )
        db.add(payment)
        db.commit()
        payment_id = payment.id

    with session_factory() as db:
        outcome = attention.resolve_attention(
            db,
            payment_id=payment_id,
            resolution="stale_incomplete_creation",
            note="closing early",
            actor="host-cli",
            now=datetime.now(UTC),
        )
    assert outcome.resolved is False
    assert outcome.refusal is attention.AttentionRefusal.NOT_YET_STALE
    with session_factory() as db:
        assert db.get(Payment, payment_id).attention_resolved_at is None

    # Past the grace period the SAME payment becomes resolvable, and the
    # worklist agrees it is attention-worthy.
    with session_factory() as db:
        db.get(Payment, payment_id).created_at = datetime.now(UTC) - timedelta(
            seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60
        )
        db.commit()
    with session_factory() as db:
        assert attention.resolve_attention(
            db,
            payment_id=payment_id,
            resolution="stale_incomplete_creation",
            note="now genuinely stale",
            actor="host-cli",
            now=datetime.now(UTC),
        ).resolved is True


def test_the_resolve_guard_and_the_worklist_share_one_grace_period(
    client, settings, session_factory
):
    """Not merely 'both have a grace period' — the SAME constant, so they can
    never drift. `stuck_payments` re-exports it from `attention`."""
    from app.services import stuck_payments as stuck_service

    assert (
        stuck_service.UNEXPECTED_STATE_GRACE_SECONDS
        is attention.UNEXPECTED_STATE_GRACE_SECONDS
    )

    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="agree-grace-1",
                gateway_order_id=996000000002,
                gateway_user_id=55501234,
                amount=1000,
                payable_amount=1000,
                status=PaymentStatus.GETLINK_FAILED.value,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()

    # Invisible to the worklist AND unresolvable, together.
    with session_factory() as db:
        assert stuck_payments_overview(db, settings).total_counts["needs_attention"] == 0
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "agree-grace-1")
        ).scalar_one()
        assert (
            attention.refuse_reason(payment, now=datetime.now(UTC))
            is attention.AttentionRefusal.NOT_YET_STALE
        )


# --- historical review listings filter on HISTORY, not current status ----


def _resend_eligible_review(session_factory, *, order_id: str, gateway_order_id: int):
    with session_factory() as db:
        payment = Payment(
            bot_order_id=order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=55501234,
            amount=10000,
            payable_amount=10000,
            status=PaymentStatus.MANUAL_REVIEW.value,
            manual_review_at=datetime(2026, 8, 1, tzinfo=UTC),
            bot_notify_reason="retry_limit_reached",
            bot_notify_attempts=5,
            gateway_verified_at=datetime(2026, 8, 1, tzinfo=UTC),
            reference_id=f"REF-{order_id}",
            review_resolved_at=datetime(2026, 8, 2, tzinfo=UTC),
            review_resolution="confirmed_by_bot_operator",
        )
        db.add(payment)
        db.commit()
        return payment.id


@pytest.mark.parametrize("command", ["cli", "ops"])
def test_history_keeps_a_resolved_review_that_was_later_resent(
    ops_env, cli_env, session_factory, capsys, command
):
    """`review resend` moves a review to `bot_notify_pending` while KEEPING
    its `review_resolved_at`/`review_resolution`. A history view filtered on
    the current status therefore dropped exactly the rows an operator most
    wants to look back at — a review that was resolved and then successfully
    redelivered — while its own docs promised to print resolved rows.

    Same class as `attention list --resolved` filtering by status: a
    historical view filters on what HAPPENED, never on where the row is now.
    """
    payment_id = _resend_eligible_review(
        session_factory, order_id=f"resent-{command}", gateway_order_id=997000000000
        + (0 if command == "cli" else 1)
    )
    # The resend outcome: status moves on, review history stays.
    with session_factory() as db:
        payment = db.get(Payment, payment_id)
        payment.status = PaymentStatus.BOT_NOTIFY_PENDING.value
        db.commit()

    if command == "cli":
        assert cli_main(["manual-review", "--all"]) == 0
    else:
        assert ops_main(["review", "list", "--all"]) == 0
    orders = [row["bot_order_id"] for row in _json_lines(capsys)]
    assert orders == [f"resent-{command}"]


@pytest.mark.parametrize("command", ["cli", "ops"])
def test_the_open_listing_still_excludes_a_resent_review(
    ops_env, cli_env, session_factory, capsys, command
):
    """Widening HISTORY must not widen the active worklist: once resent, the
    row is the notification queue's problem, not an open review."""
    payment_id = _resend_eligible_review(
        session_factory, order_id=f"open-resent-{command}",
        gateway_order_id=997100000000 + (0 if command == "cli" else 1),
    )
    with session_factory() as db:
        db.get(Payment, payment_id).status = PaymentStatus.BOT_NOTIFY_PENDING.value
        db.commit()

    if command == "cli":
        assert cli_main(["manual-review"]) == 0
    else:
        assert ops_main(["review", "list"]) == 0
    assert _json_lines(capsys) == []


def test_both_history_listings_share_one_predicate():
    """`app.cli manual-review --all` and `app.ops review list --all` compose
    the SAME builder, so they can never disagree about what history is."""
    import inspect

    from app.adminbot import queries as q

    for module_source in (
        inspect.getsource(__import__("app.cli", fromlist=["_cmd_manual_review"])
                          ._cmd_manual_review),
        inspect.getsource(__import__("app.ops", fromlist=["_cmd_review"])._cmd_review),
    ):
        assert "manual_review_history_conditions()" in module_source
    # And it selects on history, never on the current status.
    rendered = " ".join(
        str(c.compile(compile_kwargs={"literal_binds": True}))
        for c in q.manual_review_history_conditions()
    )
    assert "manual_review_at IS NOT NULL" in rendered
    assert "status" not in rendered
