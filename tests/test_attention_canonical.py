"""ONE canonical "unresolved attention" definition, proven across every surface.

Problem 2's requirement: a resolved attention item must disappear from CURRENT
operational alerts EVERYWHERE at the same instant, while remaining visible
historically — and no surface may implement its own subtly-different
predicate.

The surfaces that compute or display operational attention, and what each is
built from:

| surface                                   | built from                                  |
| ----------------------------------------- | ------------------------------------------- |
| `centralpay stuck` (human + `--json`)     | `stuck_payments_overview`                   |
| admin bot `/status` "نیازمند بررسی"       | `count_other_attention` + `bot_delivery_snapshot` |
| admin bot `/stuck` header "other"         | `count_other_attention`                     |
| `centralpay review list` / bot `/manual_review` | `queries.open_manual_review_conditions` |
| `manual_review` monitor check             | `queries.count_open_manual_reviews`         |
| `reconciliation` monitor check            | `reconciliation_*_exhausted_conditions`     |

The unexpected-status half of that table is the only part an attention-
resolvable payment can ever appear in (proven below), and both of its
consumers derive from the single `unexpected_status_conditions` builder.
"""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.adminbot import queries
from app.models import Payment, PaymentStatus
from app.services import attention
from app.services.monitor_checks import check_manual_review, check_reconciliation
from app.services.stuck_payments import (
    UNEXPECTED_STATE_GRACE_SECONDS,
    StuckCategory,
    count_other_attention,
    stuck_payments_overview,
    unexpected_status_conditions,
)
from tests.conftest import create_order, get_payment


def _stale_getlink_failed(client, settings, session_factory, stub, *, order_id="canon-1"):
    """The production shape, aged past the unexpected-status grace period."""
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
    return get_payment(session_factory, order_id)


def _all_surfaces(session_factory, settings):
    """Every current-attention number an operator can see, read at one instant
    from the same session, so they are directly comparable."""
    now = datetime.now(UTC)
    with session_factory() as db:
        overview = stuck_payments_overview(db, settings)
        return {
            # `centralpay stuck` summary + the entry rows it prints
            "stuck_needs_attention": overview.total_counts["needs_attention"],
            "stuck_entry_orders": [
                entry.payment.bot_order_id
                for entry in overview.ordered()
                if entry.category is StuckCategory.NEEDS_ATTENTION
            ],
            # admin bot `/status` and `/stuck` "other" summary number
            "bot_other_attention": count_other_attention(db, settings, now=now),
            # bot-delivery half of both, for completeness
            "bot_delivery_total": queries.bot_delivery_snapshot(db, now=now).total,
            # review surfaces + the manual_review monitor check
            "open_manual_reviews": queries.count_open_manual_reviews(db),
            "monitor_manual_review": check_manual_review(db, settings, now=now).details[
                "count"
            ],
            # the reconciliation monitor check's actionable population
            "monitor_reconciliation": check_reconciliation(db, settings, now=now).details[
                "exhausted_actionable_total"
            ],
        }


# --- the structural invariant --------------------------------------------


def test_every_resolvable_status_is_an_unexpected_status():
    """The load-time assertion in `app.services.stuck_payments`, restated here
    so its REASON is recorded in a test rather than only in a comment.

    `unexpected_status_conditions` is the only NEEDS_ATTENTION predicate that
    composes `unresolved_attention_condition()`. That is sufficient exactly
    as long as no attention-resolvable status can reach a different
    needs-attention predicate. The bot-delivery bucket only selects
    `manual_review`/`bot_notify_pending`; the reconciliation-exhausted bucket
    only selects `link_created`. So containment inside the unexpected-status
    set is what makes the single filter total.
    """
    from app.services.stuck_payments import _UNEXPECTED_STATUSES

    assert set(_UNEXPECTED_STATUSES) >= attention.RESOLVABLE_STATUSES

    bot_delivery_statuses = {
        PaymentStatus.MANUAL_REVIEW.value,
        PaymentStatus.BOT_NOTIFY_PENDING.value,
    }
    reconciliation_statuses = {PaymentStatus.LINK_CREATED.value}
    assert attention.RESOLVABLE_STATUSES.isdisjoint(bot_delivery_statuses)
    assert attention.RESOLVABLE_STATUSES.isdisjoint(reconciliation_statuses)


def test_the_canonical_predicate_includes_the_unresolved_filter():
    """A structural check that the filter is actually in the shared builder —
    so deleting it fails here, not silently in production."""
    rendered = " ".join(
        str(condition.compile(compile_kwargs={"literal_binds": True}))
        for condition in unexpected_status_conditions(now=datetime.now(UTC))
    )
    assert "attention_resolved_at IS NULL" in rendered


# --- the behavioural proof ------------------------------------------------


def test_all_surfaces_agree_before_and_after_resolution(
    client, settings, session_factory, stub
):
    """One payment, every surface, before and after: the resolved item leaves
    all CURRENT attention numbers together, and no other number moves."""
    payment = _stale_getlink_failed(client, settings, session_factory, stub)

    before = _all_surfaces(session_factory, settings)
    assert before["stuck_needs_attention"] == 1
    assert before["bot_other_attention"] == 1
    assert payment.bot_order_id in before["stuck_entry_orders"]

    with session_factory() as db:
        assert (
            attention.resolve_attention(
                db,
                payment_id=payment.id,
                resolution="stale_getlink_failure",
                note="never obtained a link",
                actor="host-cli",
                now=datetime.now(UTC),
            ).resolved
            is True
        )

    after = _all_surfaces(session_factory, settings)
    assert after["stuck_needs_attention"] == 0
    assert after["bot_other_attention"] == 0
    assert payment.bot_order_id not in after["stuck_entry_orders"]

    # Nothing else moved: this is an unexpected-status item, so the delivery,
    # review, and reconciliation surfaces must be completely unaffected.
    for key in (
        "bot_delivery_total",
        "open_manual_reviews",
        "monitor_manual_review",
        "monitor_reconciliation",
    ):
        assert after[key] == before[key], key


def test_the_cli_and_the_admin_bot_never_disagree_across_many_rows(
    client, settings, session_factory, stub
):
    """The specific drift `unexpected_status_conditions` exists to prevent:
    `centralpay stuck`'s overview and the admin bot's `count_other_attention`
    were previously two separately-written copies of the same predicate.

    Resolve them one at a time and assert the two numbers move in lockstep at
    every single step.
    """
    payments = [
        _stale_getlink_failed(
            client, settings, session_factory, stub, order_id=f"canon-multi-{i}"
        )
        for i in range(5)
    ]

    for resolved_so_far, payment in enumerate(payments):
        surfaces = _all_surfaces(session_factory, settings)
        assert surfaces["stuck_needs_attention"] == len(payments) - resolved_so_far
        assert surfaces["bot_other_attention"] == surfaces["stuck_needs_attention"]
        with session_factory() as db:
            attention.resolve_attention(
                db,
                payment_id=payment.id,
                resolution="stale_getlink_failure",
                note=f"batch {resolved_so_far}",
                actor="host-cli",
                now=datetime.now(UTC),
            )

    final = _all_surfaces(session_factory, settings)
    assert final["stuck_needs_attention"] == 0
    assert final["bot_other_attention"] == 0


def test_resolution_hides_from_current_alerts_but_never_from_history(
    client, settings, session_factory, stub
):
    """"Disappears from current alerts" must never mean "disappears". The row,
    its status, its financial facts, and its events all remain queryable."""
    payment = _stale_getlink_failed(client, settings, session_factory, stub)
    with session_factory() as db:
        attention.resolve_attention(
            db,
            payment_id=payment.id,
            resolution="stale_getlink_failure",
            note="closed",
            actor="host-cli",
            now=datetime.now(UTC),
        )

    with session_factory() as db:
        # Absent from the CURRENT worklist predicate...
        current = db.execute(
            select(Payment).where(*unexpected_status_conditions(now=datetime.now(UTC)))
        ).scalars()
        assert payment.bot_order_id not in [p.bot_order_id for p in current]

        # ...but present in the HISTORICAL one, with every fact intact.
        historical = db.execute(
            select(Payment).where(
                Payment.status.in_(sorted(attention.RESOLVABLE_STATUSES)),
                attention.resolved_attention_condition(),
            )
        ).scalars()
        rows = list(historical)
        assert [p.bot_order_id for p in rows] == [payment.bot_order_id]
        assert rows[0].status == PaymentStatus.GETLINK_FAILED.value
        assert rows[0].amount == 230000
        assert rows[0].gateway_verified_at is None


def test_an_unresolved_row_of_another_unexpected_status_still_needs_attention(
    client, settings, session_factory, stub
):
    """The filter must narrow attention only by RESOLUTION, never by status.
    A `gateway_verified` row (an unexpected status that is deliberately NOT
    attention-resolvable) keeps alarming."""
    with session_factory() as db:
        db.add(
            Payment(
                bot_order_id="canon-gv-1",
                gateway_order_id=900000000777,
                gateway_user_id=55501234,
                amount=1000,
                payable_amount=1000,
                status=PaymentStatus.GATEWAY_VERIFIED.value,
                created_at=datetime.now(UTC)
                - timedelta(seconds=UNEXPECTED_STATE_GRACE_SECONDS + 60),
            )
        )
        db.commit()

    surfaces = _all_surfaces(session_factory, settings)
    assert surfaces["stuck_needs_attention"] == 1
    assert surfaces["bot_other_attention"] == 1
    assert "canon-gv-1" in surfaces["stuck_entry_orders"]
