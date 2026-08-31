"""app.services.attention: durable, non-financial closure of stale failures.

The motivating production row (2026-08-01, ``bot_order_id=12ca60ac8c``) sat in
``getlink_failed`` after a ``getLink.php`` ReadTimeout: no payment link, no
gateway verification, no reference id, no downstream delivery — yet
``centralpay stuck`` classified it ``needs_attention /
unexpected_status:getlink_failed`` forever, so the only way to clear the
worklist was to delete it and destroy audit history.

These tests pin the resolution mechanism's safety contract:

* what it writes (four operational columns) and, exhaustively, what it does
  NOT write (every financial, gateway, identity, and status field);
* the strict status/resolution allowlist;
* every financially-meaningful refusal, including the two that carry the real
  weight — a payment whose ``redirect_url``/``callback_token_hash`` proves a
  link WAS issued is refused even in an otherwise-eligible status;
* durability of actor/time/reason/note and the audit event;
* refusal of a duplicate (second) operator resolution;
* preservation of the Payment row, all PaymentEvents, and all AdminAlerts.

Concurrency (row locking under a real racing operator) lives in
``tests/integration/test_attention_pg.py`` — SQLite cannot prove it.
"""

import pathlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.models import AdminAlert, Payment, PaymentEvent, PaymentStatus
from app.services import attention
from tests.conftest import create_order, event_types, get_events, get_payment


def _make_getlink_failed(client, settings, session_factory, stub, *, order_id="gl-fail-1"):
    """A payment shaped exactly like the production row: getLink failed, so no
    redirect URL and no callback token were ever issued."""
    # Exactly the production failure shape: a getLink.php ReadTimeout.
    stub.getlink_result = httpx.ReadTimeout("read timed out")
    response = create_order(client, settings, order_id=order_id, amount=230000)
    assert response.status_code >= 400
    payment = get_payment(session_factory, order_id)
    assert payment.status == PaymentStatus.GETLINK_FAILED.value
    assert payment.redirect_url is None
    # NOTE: callback_token_hash IS set — the signed return URL is generated
    # and hashed BEFORE the getLink request that carries it. That is exactly
    # why it must not be an eligibility guard (see app.services.attention).
    assert payment.callback_token_hash is not None
    assert payment.gateway_verified_at is None
    assert payment.reference_id is None
    assert payment.bot_notify_attempts == 0
    return payment


def _resolve(session_factory, payment_id, *, resolution="stale_getlink_failure", note="note"):
    with session_factory() as db:
        return attention.resolve_attention(
            db,
            payment_id=payment_id,
            resolution=resolution,
            note=note,
            actor="host-cli",
            now=datetime.now(UTC),
        )


# --- allowlist shape ------------------------------------------------------


def test_allowlist_never_includes_a_financially_meaningful_status():
    """`gateway_verified`, `link_created`, both notification states, and
    `manual_review` must never become attention-resolvable. Each one either
    carries a real gateway verification or means a payer was handed a link."""
    forbidden = {
        PaymentStatus.GATEWAY_VERIFIED.value,
        PaymentStatus.LINK_CREATED.value,
        PaymentStatus.BOT_NOTIFY_PENDING.value,
        PaymentStatus.BOT_NOTIFY_ACCEPTED.value,
        PaymentStatus.MANUAL_REVIEW.value,
    }
    assert attention.RESOLVABLE_STATUSES.isdisjoint(forbidden)
    assert {
        PaymentStatus.CREATED.value,
        PaymentStatus.GETLINK_FAILED.value,
    } == attention.RESOLVABLE_STATUSES


def test_every_resolution_code_maps_to_at_least_one_real_status():
    valid = {status.value for status in PaymentStatus}
    for code, statuses in attention.ATTENTION_RESOLUTIONS.items():
        assert statuses, f"{code} maps to no status"
        assert statuses <= valid, f"{code} maps to an unknown status"


def test_every_refusal_reason_has_a_message():
    for refusal in attention.AttentionRefusal:
        assert refusal in attention.REFUSAL_MESSAGE


# --- the happy path -------------------------------------------------------


def test_resolve_records_actor_time_reason_and_note(
    client, settings, session_factory, stub
):
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    before = datetime.now(UTC) - timedelta(seconds=1)

    outcome = _resolve(session_factory, payment.id, note="ReadTimeout; link was never issued")
    assert outcome.resolved is True
    assert outcome.resolution == "stale_getlink_failure"
    assert outcome.refusal is None

    stored = get_payment(session_factory, payment.bot_order_id)
    assert stored.attention_resolution == "stale_getlink_failure"
    assert stored.attention_resolved_by == "host-cli"
    assert stored.attention_resolution_note == "ReadTimeout; link was never issued"
    resolved_at = stored.attention_resolved_at
    assert resolved_at is not None
    if resolved_at.tzinfo is None:
        resolved_at = resolved_at.replace(tzinfo=UTC)
    assert resolved_at >= before


def test_resolve_emits_an_audit_event_recording_the_unchanged_status(
    client, settings, session_factory, stub
):
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    before = event_types(get_events(session_factory, payment.id))

    _resolve(session_factory, payment.id, note="operator reviewed")

    events = get_events(session_factory, payment.id)
    assert event_types(events) == [*before, "payment_attention_resolved"]
    data = events[-1].data
    assert data is not None
    assert data["resolution"] == "stale_getlink_failure"
    assert data["note"] == "operator reviewed"
    assert data["operator"] == "host-cli"
    # Proof in the trail itself that the status was NOT rewritten.
    assert data["status"] == PaymentStatus.GETLINK_FAILED.value
    assert data["gateway_verified"] is False


def test_resolve_changes_nothing_financial_and_nothing_about_status(
    client, settings, session_factory, stub
):
    """Exhaustive before/after comparison: EVERY column except the four
    attention columns (and the bookkeeping `updated_at`) must be byte-identical
    after resolution."""
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    attention_columns = {
        "attention_resolved_at",
        "attention_resolution",
        "attention_resolved_by",
        "attention_resolution_note",
        "updated_at",
    }
    columns = [c.name for c in Payment.__table__.columns if c.name not in attention_columns]
    with session_factory() as db:
        before = db.execute(
            select(*[getattr(Payment, name) for name in columns]).where(
                Payment.id == payment.id
            )
        ).one()

    _resolve(session_factory, payment.id)

    with session_factory() as db:
        after = db.execute(
            select(*[getattr(Payment, name) for name in columns]).where(
                Payment.id == payment.id
            )
        ).one()
    assert dict(zip(columns, before, strict=True)) == dict(
        zip(columns, after, strict=True)
    )


def test_resolution_preserves_the_payment_every_event_and_every_alert(
    client, settings, session_factory, stub
):
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    with session_factory() as db:
        events_before = db.execute(select(func.count(PaymentEvent.id))).scalar_one()
        alerts_before = db.execute(select(func.count(AdminAlert.id))).scalar_one()

    _resolve(session_factory, payment.id)

    with session_factory() as db:
        # The row itself still exists, and nothing was deleted: the event count
        # only GREW (by the one resolution event).
        assert db.get(Payment, payment.id) is not None
        assert (
            db.execute(select(func.count(PaymentEvent.id))).scalar_one()
            == events_before + 1
        )
        assert (
            db.execute(select(func.count(AdminAlert.id))).scalar_one() >= alerts_before
        )


# --- refusals -------------------------------------------------------------


def test_refuses_a_second_resolution_and_keeps_the_first(
    client, settings, session_factory, stub
):
    """Duplicate operator action must never overwrite the original actor,
    time, reason, or note, and must never append a second audit event."""
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    assert _resolve(session_factory, payment.id, note="first").resolved is True
    first = get_payment(session_factory, payment.bot_order_id)

    outcome = _resolve(session_factory, payment.id, note="second")
    assert outcome.resolved is False
    assert outcome.refusal is attention.AttentionRefusal.ALREADY_RESOLVED
    assert outcome.existing_resolution == "stale_getlink_failure"

    after = get_payment(session_factory, payment.bot_order_id)
    assert after.attention_resolution_note == "first"
    assert after.attention_resolved_at == first.attention_resolved_at
    assert (
        event_types(get_events(session_factory, payment.id)).count(
            "payment_attention_resolved"
        )
        == 1
    )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "gateway_verified_at",
            datetime(2026, 8, 1, tzinfo=UTC),
            attention.AttentionRefusal.GATEWAY_VERIFIED,
        ),
        ("reference_id", "REF-XYZ", attention.AttentionRefusal.HAS_REFERENCE_ID),
        (
            "manual_review_at",
            datetime(2026, 8, 1, tzinfo=UTC),
            attention.AttentionRefusal.UNDER_MANUAL_REVIEW,
        ),
        (
            "bot_notify_attempts",
            1,
            attention.AttentionRefusal.BOT_NOTIFICATION_ATTEMPTED,
        ),
        (
            "redirect_url",
            "https://gateway.test/pay/tok",
            attention.AttentionRefusal.PAYMENT_LINK_ISSUED,
        ),
    ],
)
def test_refuses_a_payment_that_became_financially_meaningful(
    client, settings, session_factory, stub, field, value, expected
):
    """Each guard independently disqualifies an otherwise-eligible
    `getlink_failed` row. `redirect_url` matters most: it is the proof this
    bridge never held, and so never returned, a usable payment URL."""
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    with session_factory() as db:
        row = db.get(Payment, payment.id)
        setattr(row, field, value)
        db.commit()

    outcome = _resolve(session_factory, payment.id)
    assert outcome.resolved is False
    assert outcome.refusal is expected

    stored = get_payment(session_factory, payment.bot_order_id)
    assert stored.attention_resolved_at is None
    assert "payment_attention_resolved" not in event_types(
        get_events(session_factory, payment.id)
    )


def test_refuses_a_status_outside_the_allowlist(client, settings, session_factory, stub):
    """A link_created payment — the payer HAS a usable link — is never
    attention-resolvable, even though it is not verified."""
    response = create_order(client, settings, order_id="live-1")
    assert response.status_code == 200
    payment = get_payment(session_factory, "live-1")
    assert payment.status == PaymentStatus.LINK_CREATED.value

    outcome = _resolve(session_factory, payment.id)
    assert outcome.resolved is False
    # The link-issued guard fires before the status guard: it is the more
    # alarming and more specific fact.
    assert outcome.refusal is attention.AttentionRefusal.PAYMENT_LINK_ISSUED


def test_refuses_a_resolution_code_that_does_not_apply_to_the_status(
    client, settings, session_factory, stub
):
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    outcome = _resolve(session_factory, payment.id, resolution="stale_incomplete_creation")
    assert outcome.resolved is False
    assert outcome.refusal is attention.AttentionRefusal.RESOLUTION_NOT_VALID_FOR_STATUS
    assert get_payment(session_factory, payment.bot_order_id).attention_resolved_at is None


def test_a_created_row_uses_its_own_resolution_code(session_factory):
    """`created` and `getlink_failed` are separate codes on purpose, so the
    audit trail records which operational situation was actually closed."""
    with session_factory() as db:
        payment = Payment(
            bot_order_id="created-only-1",
            gateway_order_id=900000000001,
            gateway_user_id=55501234,
            amount=1000,
            payable_amount=1000,
            status=PaymentStatus.CREATED.value,
        )
        db.add(payment)
        db.commit()
        payment_id = payment.id

    assert (
        _resolve(session_factory, payment_id, resolution="stale_getlink_failure").refusal
        is attention.AttentionRefusal.RESOLUTION_NOT_VALID_FOR_STATUS
    )
    assert (
        _resolve(
            session_factory, payment_id, resolution="stale_incomplete_creation"
        ).resolved
        is True
    )


# --- snapshot / reporting -------------------------------------------------


def test_snapshot_never_exposes_the_redirect_url(client, settings, session_factory):
    """A full payment redirect URL must never leave an operator tool
    (AGENTS.md logging contract). The snapshot reports only a boolean."""
    assert create_order(client, settings, order_id="snap-1").status_code == 200
    with session_factory() as db:
        payment = db.execute(
            select(Payment).where(Payment.bot_order_id == "snap-1")
        ).scalar_one()
        snapshot = attention.snapshot(payment)
        assert payment.redirect_url is not None
    assert snapshot.redirect_url_present is True
    for value in vars(snapshot).values():
        assert not (isinstance(value, str) and value.startswith("https://gateway.test"))


def test_snapshot_reports_eligibility_and_a_reason(
    client, settings, session_factory, stub
):
    payment = _make_getlink_failed(client, settings, session_factory, stub)
    with session_factory() as db:
        snapshot = attention.snapshot(db.get(Payment, payment.id))
    assert snapshot.refusal is None
    assert attention.snapshot_refusal_message(snapshot) is None
    assert snapshot.eligible_resolutions == ("stale_getlink_failure",)
    # Original financial facts are reported verbatim, never rounded away.
    assert snapshot.amount == 230000
    assert snapshot.gateway_verified is False
    assert snapshot.reference_id is None


# --- attention resolution must be INERT with respect to settlement --------


def test_a_resolved_payment_can_still_be_settled_by_a_late_callback(
    client, settings, session_factory, stub
):
    """THE most important test in this file.

    A ``getLink`` ReadTimeout means the request WAS delivered and only the
    response was lost, so CentralPay may hold a link we never received. If a
    payer reaches it and pays, the browser callback must still settle the
    payment: ``process_callback`` deliberately does not gate on
    ``status == link_created``, and the ``callback_token_hash`` still matches
    the return URL CentralPay was given.

    An earlier draft of this feature added a CHECK constraint
    (``attention_resolved_at IS NULL OR gateway_verified_at IS NULL``) as a
    "backstop". It would have turned this legitimate settlement into an
    IntegrityError and FAILED A REAL CUSTOMER PAYMENT in order to keep an
    operator worklist tidy. This test exists so that constraint can never come
    back.
    """
    from tests.conftest import valid_callback_path, verify_ok_response

    payment = _make_getlink_failed(client, settings, session_factory, stub)
    assert _resolve(session_factory, payment.id, note="stale, closing").resolved is True

    # CentralPay had in fact created the link; the payer paid it.
    stub.verify_result = verify_ok_response(
        amount=payment.payable_amount,
        user_id=payment.gateway_user_id,
        reference_id="REF-LATE-1",
    )
    response = client.get(valid_callback_path(stub, payment.gateway_order_id))
    assert response.status_code == 200

    settled = get_payment(session_factory, payment.bot_order_id)
    # Settlement completed normally, financial facts recorded.
    assert settled.gateway_verified_at is not None
    assert settled.reference_id == "REF-LATE-1"
    assert settled.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    # The operator's resolution record survives untouched and un-consulted.
    assert settled.attention_resolution == "stale_getlink_failure"
    assert settled.attention_resolution_note == "stale, closing"


def test_a_settled_previously_resolved_payment_is_visible_to_delivery_surfaces(
    client, settings, session_factory, stub
):
    """Once such a payment settles it leaves the attention-resolvable statuses
    entirely, so the attention filter can never hide it from the ordinary
    notification/manual-review surfaces that own it from then on."""
    from tests.conftest import valid_callback_path, verify_ok_response

    payment = _make_getlink_failed(client, settings, session_factory, stub)
    _resolve(session_factory, payment.id)
    stub.verify_result = verify_ok_response(
        amount=payment.payable_amount,
        user_id=payment.gateway_user_id,
        reference_id="REF-LATE-2",
    )
    assert client.get(valid_callback_path(stub, payment.gateway_order_id)).status_code == 200

    settled = get_payment(session_factory, payment.bot_order_id)
    assert settled.status not in attention.RESOLVABLE_STATUSES


# --- structural: the resolution paths cannot make a network call ----------


def test_resolution_modules_import_no_gateway_or_bot_client():
    """A structural guarantee, not a behavioural one.

    "Makes no gateway call" and "makes no downstream-bot call" are asserted by
    every behavioural test in this file only for the paths those tests happen
    to walk. Checking that neither module can even REACH a client — no import
    of `app.centralpay`, `app.bot`, `httpx`, or any settlement/notification
    service — makes it impossible for a future edit to add one without this
    failing first.
    """
    import ast

    forbidden_modules = {
        "httpx",
        "requests",
        "urllib",
        "urllib.request",
        "http.client",
        "socket",
        "app.centralpay",
        "app.bot",
        "app.services.verification",
        "app.services.notification",
        "app.services.reconciliation",
        "app.services.aged_out_recovery",
        "app.services.reconcile_inspect",
    }
    for module_path in (
        "app/services/attention.py",
        "app/services/review_resolution.py",
    ):
        tree = ast.parse(pathlib.Path(module_path).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offending = imported & forbidden_modules
        assert not offending, f"{module_path} imports {sorted(offending)}"


def test_resolution_modules_never_assign_a_financial_field():
    """Every attribute the resolution modules assign on a Payment, checked
    against an explicit allowlist.

    The behavioural test above compares a full before/after row, which proves
    the happy path. This proves the same thing STRUCTURALLY for every code
    path in both modules, including ones no test reaches — a future edit that
    writes `gateway_verified_at`, `reference_id`, `amount`, `status`, or any
    fee field fails here immediately.
    """
    import ast

    allowed = {
        # attention resolution
        "attention_resolved_at",
        "attention_resolution",
        "attention_resolved_by",
        "attention_resolution_note",
        # review resolution (identical to the single-payment path)
        "review_acknowledged_at",
        "review_resolved_at",
        "review_resolution",
    }
    for module_path in (
        "app/services/attention.py",
        "app/services/review_resolution.py",
    ):
        tree = ast.parse(pathlib.Path(module_path).read_text())
        assigned: set[str] = set()
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    assigned.add(target.attr)
        assert assigned <= allowed, (
            f"{module_path} assigns non-allowlisted attribute(s): "
            f"{sorted(assigned - allowed)}"
        )
