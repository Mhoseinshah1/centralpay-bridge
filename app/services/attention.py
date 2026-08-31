"""Operational resolution of stale, NON-FINANCIAL payment failures.

Motivating production case: a payment created 2026-08-01 whose CentralPay
``getLink.php`` call timed out. It has an ``amount``, a fee snapshot, and a
``gateway_order_id``, but it never obtained a payment link, was never gateway
verified, has no ``reference_id``, and was never delivered to the downstream
bot. ``app.services.stuck_payments`` therefore classified it
``needs_attention / unexpected_status:getlink_failed`` permanently, and the
only way to clear it from the operator's worklist was to DELETE it —
destroying permanent financial/audit history the project deliberately keeps
(and which restrictive foreign keys deliberately protect).

This module is the alternative: a durable, audited, strictly non-financial
"an operator has looked at this and it needs no further action" marker.

What it is NOT
--------------
* It is NOT a status change. ``payments.status`` is never written here — a
  ``getlink_failed`` row stays ``getlink_failed`` forever. Nothing is turned
  into a fake successful state.
* It is NOT manual-review resolution. ``manual_review`` is a FINANCIAL
  ambiguity workflow with its own vocabulary and its own commands
  (``app.ops review ...``); an open manual review is never eligible here, so
  the two mechanisms can never be used to launder each other's semantics.
* It is NOT a deletion, an archive, or a hide. The ``Payment`` row, every
  ``PaymentEvent``, and every ``AdminAlert`` survive untouched and stay
  fully inspectable (``centralpay payment ORDER_ID``,
  ``centralpay attention show``, ``centralpay attention list --resolved``).
* It NEVER performs gateway or downstream-bot HTTP. Nothing in this module
  imports a client; resolution is a pure local database decision.

What it changes
---------------
Exactly four columns, written together, exactly once, never cleared:
``attention_resolved_at``, ``attention_resolution``, ``attention_resolved_by``,
``attention_resolution_note`` (migration 0013). Every financial and identity
fact — ``amount``, ``payable_amount``, ``fee_policy_id``/``fee_rate_bps``/
``fee_amount``, ``gateway_verified_at``, ``reference_id``, ``gateway_order_id``,
``gateway_user_id``, ``payer_identity_id``/``payer_identity_type``,
``redirect_url``, ``card_last4`` — is untouched, and this module contains no
assignment to any of them.

Eligibility is a STRICT ALLOWLIST, not a status filter
------------------------------------------------------
``ATTENTION_RESOLUTIONS`` maps each allowlisted resolution code to the exact
set of statuses it may be applied to. Only two statuses are ever eligible:

* ``getlink_failed`` — the ``getLink`` call demonstrably failed, so CentralPay
  never returned a usable redirect URL;
* ``created`` — the payment row was committed but ``getLink`` never completed,
  so no redirect URL was ever returned to the caller either.

``gateway_verified`` is deliberately NOT eligible even though
``stuck_payments`` also treats it as an unexpected status: a row in that state
carries a real gateway verification and is financially meaningful by
definition. ``link_created``, ``bot_notify_pending``, ``bot_notify_accepted``,
and ``manual_review`` are likewise never eligible.

Beyond the status allowlist, :func:`refuse_reason` requires the payment to be
provably financially inert on OUR side: not gateway verified, no
``reference_id``, not referred to manual review, no bot notification ever
attempted, and — the condition carrying the real weight — ``redirect_url IS
NULL``. That last one is what proves this bridge never held, and therefore
never returned to the calling bot, a usable payment URL for the order: the
create-payment response serves ``{"url": ...}`` from exactly this column.
Without it, "status is getlink_failed" alone would not be enough, since a row
could in principle have obtained a link and failed later.

``callback_token_hash`` is deliberately NOT a guard
---------------------------------------------------
It would look like a natural second proof, and it is the wrong one: the signed
return URL is generated and its hash stored BEFORE the ``getLink`` request is
sent (the URL is part of that request), so EVERY ``getlink_failed`` row has
one. Guarding on it would refuse the exact production case this module exists
for.

It also points at a real residual risk that this module states honestly rather
than pretends away. A ``ReadTimeout`` means the request WAS delivered and only
the response was lost, so CentralPay may hold a link for the order that we
never received. If a payer somehow reached it and paid,
``app.services.verification.process_callback`` would settle the payment
normally — it does NOT gate on ``status == link_created``, and the
``callback_token_hash`` still matches the return URL CentralPay was given.
That safety net is deliberate and MUST keep working.

So resolving an attention item asserts only: *this bridge never delivered a
payment link for this order and has nothing further to do about it.* It does
NOT assert that CentralPay has no record, and it changes nothing about what
the financial path may later do:

* no status is written, so the callback path behaves identically;
* no database constraint ties ``attention_resolved_at`` to
  ``gateway_verified_at`` (migration 0013 explains why such a constraint would
  be a hazard: it would fail a legitimate late settlement with an
  ``IntegrityError``);
* a resolved payment that later settles simply leaves the resolvable statuses
  and is picked up by the ordinary notification/manual-review surfaces, which
  do not consult the attention filter at all.

ACCEPTED RESIDUAL: a retry killed mid-``getLink``
-------------------------------------------------
``create_payment`` records ``centralpay_getlink_failed`` only inside its
``CentralPayError`` handler, and the fresh ``gateway_order_id`` and
``callback_token_hash`` it assigns for a retry are uncommitted while the
network call runs. If the process is killed (or the host restarts) after the
request is sent but before either commit path, PostgreSQL rolls the whole
attempt back: no failure event is written, so :func:`unresolved_attention_
condition` does not reopen a previously resolved row.

What that does and does not mean:

* The row reverts to EXACTLY its pre-retry state — the new gateway order id and
  token hash roll back too — so no new local state is hidden. What is hidden is
  only the fact that another attempt was made and abandoned.
* CentralPay may hold a link for a gateway order id our database rolled back.
  That orphan is pre-existing crash-window behaviour of the retry path,
  identical with or without attention resolution, and the rolled-back token
  hash means a callback for it could not validate anyway. It is the same
  residual this module already states above: resolution never claims CentralPay
  has no record.
* The window closes itself in practice. The upstream bot retrying again either
  succeeds (the row leaves the resolvable statuses) or fails with a caught
  error (which writes the event and reopens the item). Staying hidden requires
  the crash AND no further retry ever — at which point the order is abandoned
  upstream too.

The obvious fix — persisting an attempt marker BEFORE the gateway call — is
deliberately NOT taken: it would add a commit to the payment-creation path,
ahead of a network call, changing that path's crash semantics for the sake of
an operator-worklist signal. AGENTS.md orders financial correctness above
observability, and this module's whole design rule is that attention
resolution never reaches into the financial path. Recorded as a bounded,
accepted residual instead (RELEASE_RISK_REGISTER.md topic 56); an operator who
needs certainty for a specific order has ``centralpay reconcile ORDER_ID``.

Concurrency
-----------
:func:`resolve_attention` re-reads the payment under ``SELECT ... FOR UPDATE``
with ``populate_existing=True`` (AGENTS.md's SQLAlchemy identity-map rule) and
re-evaluates the FULL refusal guard against that freshly-locked state before
writing anything. A payment that became financially meaningful between the
operator's preview and their confirmation — a late reconciliation, a racing
callback, a concurrent manual-review transition — is refused, not resolved.
Resolution is idempotent-safe in the strict direction: an already-resolved
payment is refused rather than re-stamped, so a duplicate operator action can
never overwrite the first actor/time/reason/note or append a second audit
event.

Nothing here ever blocks, delays, or rolls back a payment transaction: the
only lock taken is on the single payment being resolved, held for the duration
of one small local transaction with no network call inside it.
"""

import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import ScalarSelect

from app.audit import record_event
from app.models import Payment, PaymentEvent, PaymentStatus

logger = logging.getLogger("app.services.attention")

# Bounds mirroring the existing operator-note conventions in app.ops.
NOTE_MAX_LENGTH = 500
ACTOR_MAX_LENGTH = 128

# Default actor label for the host CLI, matching app.ops' existing
# "operator": "host-cli" audit convention.
ACTOR_HOST_CLI = "host-cli"

# How long a payment may sit in a status nothing automatically revisits before
# it counts as a genuine anomaly rather than an in-flight request.
#
# Lives HERE, not in `app.services.stuck_payments`, purely for import
# direction: stuck_payments imports this module, so the reverse would be a
# cycle. It re-exports the name, which is where the read-only worklist reads
# it from and where tests import it. Both the worklist predicate and the
# mutating guard below therefore share one threshold and cannot disagree about
# when an item becomes attention-worthy.
UNEXPECTED_STATE_GRACE_SECONDS = 60


# THE allowlist. Each key is a machine-readable resolution code; each value is
# the exact set of payment statuses that code may be applied to. Broadening
# either side is a financial-review-grade change: a status only belongs here
# once it is provably impossible for the payment to have been paid.
ATTENTION_RESOLUTIONS: Mapping[str, frozenset[str]] = {
    # The motivating case: getLink failed (timeout / transport error), so
    # CentralPay never returned a usable redirect URL for this order.
    "stale_getlink_failure": frozenset({PaymentStatus.GETLINK_FAILED.value}),
    # The payment row was committed but the getLink round trip never
    # completed (e.g. the process died mid-call), leaving the row in
    # `created` with no redirect URL ever returned to the caller.
    "stale_incomplete_creation": frozenset({PaymentStatus.CREATED.value}),
}

# Every status any allowlisted resolution can target. `app.services.
# stuck_payments` asserts this stays a SUBSET of its `_UNEXPECTED_STATUSES`,
# which is what proves the canonical unresolved-attention filter covers every
# attention surface a resolvable row can possibly appear in.
RESOLVABLE_STATUSES: frozenset[str] = frozenset(
    status for statuses in ATTENTION_RESOLUTIONS.values() for status in statuses
)


class AttentionRefusal(enum.StrEnum):
    """Exactly why a payment may not be attention-resolved. Machine-readable;
    never free text and never raw external content."""

    ALREADY_RESOLVED = "already_resolved"
    NOT_YET_STALE = "not_yet_stale"
    EMPTY_NOTE = "empty_note"
    STATUS_NOT_ELIGIBLE = "status_not_eligible"
    RESOLUTION_NOT_VALID_FOR_STATUS = "resolution_not_valid_for_status"
    GATEWAY_VERIFIED = "gateway_verified"
    HAS_REFERENCE_ID = "has_reference_id"
    UNDER_MANUAL_REVIEW = "under_manual_review"
    BOT_NOTIFICATION_ATTEMPTED = "bot_notification_attempted"
    PAYMENT_LINK_ISSUED = "payment_link_issued"


REFUSAL_MESSAGE: Mapping[AttentionRefusal, str] = {
    AttentionRefusal.ALREADY_RESOLVED: (
        "refused: this payment's attention item is already resolved "
        "(resolution={resolution}, at={resolved_at}, by={resolved_by}). "
        "Attention resolution is recorded once and never overwritten."
    ),
    AttentionRefusal.NOT_YET_STALE: (
        "refused: this payment is younger than the {grace}s grace period, so "
        "it is still in flight, not a stale failure. `centralpay stuck` and "
        "`centralpay attention list` deliberately do not show it yet — "
        "creation may still be in progress. Wait, then re-check."
    ),
    AttentionRefusal.EMPTY_NOTE: (
        "refused: --note and the actor must be non-empty. The note is one of "
        "the four fields that record WHY this incident was closed; a blank "
        "one would leave an unauditable resolution."
    ),
    AttentionRefusal.STATUS_NOT_ELIGIBLE: (
        "refused: status={status} is not an attention-resolvable state. "
        "Only {eligible} may be resolved this way; a manual_review payment "
        "is resolved with `centralpay review resolve` instead."
    ),
    AttentionRefusal.RESOLUTION_NOT_VALID_FOR_STATUS: (
        "refused: resolution={resolution} does not apply to status={status}."
    ),
    AttentionRefusal.GATEWAY_VERIFIED: (
        "refused: this payment IS gateway verified (gateway_verified_at is "
        "set). It is financially meaningful and must not be closed as a "
        "stale non-financial failure."
    ),
    AttentionRefusal.HAS_REFERENCE_ID: (
        "refused: this payment carries a CentralPay reference_id, so a "
        "gateway transaction exists for it."
    ),
    AttentionRefusal.UNDER_MANUAL_REVIEW: (
        "refused: this payment has been referred to manual review "
        "(manual_review_at is set). Use `centralpay review` for it."
    ),
    AttentionRefusal.BOT_NOTIFICATION_ATTEMPTED: (
        "refused: a downstream bot notification was already attempted for "
        "this payment (bot_notify_attempts > 0)."
    ),
    AttentionRefusal.PAYMENT_LINK_ISSUED: (
        "refused: this bridge holds a payment URL for this order "
        "(redirect_url is set), so it was returned to the calling bot and a "
        "payer could have paid it. Investigate with `centralpay reconcile "
        "ORDER_ID` first."
    ),
}


@dataclass(frozen=True)
class AttentionSnapshot:
    """Read-only view of one payment's attention state. Financial fields are
    reported verbatim so an operator sees the real facts, never a summary
    that could imply a different outcome."""

    bot_order_id: str
    gateway_order_id: int
    status: str
    amount: int
    fee_rate_bps: int
    fee_amount: int
    payable_amount: int
    gateway_verified: bool
    gateway_verified_at: datetime | None
    reference_id: str | None
    redirect_url_present: bool
    callback_token_issued: bool
    bot_notify_attempts: int
    manual_review_at: datetime | None
    last_error_code: str | None
    created_at: datetime | None
    attention_resolved_at: datetime | None
    attention_resolution: str | None
    attention_resolved_by: str | None
    attention_resolution_note: str | None
    # None == currently eligible for at least one allowlisted resolution.
    refusal: AttentionRefusal | None
    eligible_resolutions: tuple[str, ...]
    # True when a recorded resolution exists but a LATER link-creation failure
    # has superseded it: the item is simultaneously historically-resolved and
    # currently open, and the operator must review the new incident.
    attention_resolution_superseded: bool = False


@dataclass(frozen=True)
class AttentionOutcome:
    resolved: bool
    bot_order_id: str
    status: str
    resolution: str | None
    refusal: AttentionRefusal | None
    # Populated on ALREADY_RESOLVED so the caller can show the first actor.
    existing_resolution: str | None = None
    existing_resolved_at: datetime | None = None
    existing_resolved_by: str | None = None


# The audit event every failed link-creation attempt records, in the SAME
# transaction as the status write (see app.services.payments.create_payment's
# CentralPayError branch), and the event this module records for a resolution.
# `payment_events` is append-only and permanent.
LINK_FAILURE_EVENT_TYPE = "centralpay_getlink_failed"
ATTENTION_RESOLVED_EVENT_TYPE = "payment_attention_resolved"


def _latest_event_id_expression(event_type: str) -> ScalarSelect[int]:
    """Correlated scalar subquery: the highest ``payment_events.id`` of the
    given type for this payment, or NULL if it has none.

    IDS, NOT TIMESTAMPS, deliberately. Two independent problems make
    ``created_at`` the wrong key here:

    * PostgreSQL's ``now()`` (the column's server default) is TRANSACTION
      START time. ``create_payment`` holds the row lock across the ``getLink``
      call, so a slow failure — a ReadTimeout is by definition slow — records
      its event with a timestamp from before the call even began. That can be
      EARLIER than a resolution recorded after it, which would hide the newer
      failure: exactly the bug this supersession rule exists to prevent.
    * SQLite (unit tests) resolves ``CURRENT_TIMESTAMP`` to whole seconds,
      so two events in the same second are indistinguishable.

    ``payment_events.id`` is sequence-allocated and strictly increasing, and
    both writers serialize on the payment row lock (``create_payment`` holds
    it across ``getLink``; :func:`resolve_attention` takes it ``FOR UPDATE``),
    so the two operations can never interleave: whichever commits second
    observes the other's event and is allocated a higher id. Comparing ids is
    therefore exact, with no dependence on any clock.

    Both ``payment_events.payment_id`` and ``.event_type`` are indexed; this
    is the same correlated-subquery shape
    ``app.adminbot.queries._notification_age_anchor`` already uses.
    """
    return (
        select(func.max(PaymentEvent.id))
        .where(
            PaymentEvent.payment_id == Payment.id,
            PaymentEvent.event_type == event_type,
        )
        .correlate(Payment)
        .scalar_subquery()
    )


def unresolved_attention_condition() -> ColumnElement[bool]:
    """THE canonical "this attention item is still open" predicate.

    Every operator-attention surface that can contain an attention-resolvable
    row composes exactly this expression — never a re-derived
    ``attention_resolved_at == None`` written out locally — so a resolved item
    disappears from CURRENT operational alerts everywhere at once while
    staying fully visible in historical views.

    An item is open when it was never resolved, OR when a link-creation
    failure was recorded AFTER the most recent resolution.

    That second clause is not defensive padding; without it a resolution
    silently suppresses a genuinely NEW failure forever.
    ``app.services.payments.create_payment`` deliberately RETRIES ``getLink``
    for an existing ``created``/``getlink_failed`` row (allocating a fresh
    ``gateway_order_id`` first, in case CentralPay half-registered the old
    one). If that retry also fails, the row returns to ``getlink_failed``
    while ``attention_resolved_at`` still holds the operator's judgment about
    the PREVIOUS incident. A plain ``IS NULL`` predicate would then hide the
    new failure from `centralpay stuck` and every admin summary permanently,
    and :func:`refuse_reason` would refuse to let the operator record a fresh
    resolution — a real problem made invisible, which is exactly what an
    operator-convenience feature must never do.

    Resolution is therefore scoped to the INCIDENT it closed, not to the
    payment forever. A retry that SUCCEEDS needs no special handling: the row
    leaves the resolvable statuses entirely and is owned by the ordinary
    delivery surfaces from then on.

    See ``app.services.stuck_payments.unexpected_status_conditions``, the
    single predicate builder both the ``centralpay stuck`` overview and the
    admin bot's ``needs attention`` count are built from.
    """
    latest_failure = _latest_event_id_expression(LINK_FAILURE_EVENT_TYPE)
    latest_resolution = _latest_event_id_expression(ATTENTION_RESOLVED_EVENT_TYPE)
    return or_(
        Payment.attention_resolved_at.is_(None),
        and_(
            latest_failure.is_not(None),
            or_(
                latest_resolution.is_(None),
                latest_failure > latest_resolution,
            ),
        ),
    )


def _latest_event_id(db: Session, payment_id: int, event_type: str) -> int | None:
    return db.execute(
        select(func.max(PaymentEvent.id)).where(
            PaymentEvent.payment_id == payment_id,
            PaymentEvent.event_type == event_type,
        )
    ).scalar_one_or_none()


def latest_link_failure_codes(
    db: Session, payment_ids: Sequence[int]
) -> dict[int, str | None]:
    """The safe internal error code of each payment's most recent
    ``centralpay_getlink_failed`` event, keyed by payment id.

    ``create_payment`` stores the failure's ``exc.code`` ONLY in that event's
    data; ``Payment.last_error`` holds the gateway's MESSAGE, and
    ``bot_last_error_code`` / ``reconciliation_last_error_code`` are written
    exclusively by the notification and reconciliation flows — neither of
    which an attention-resolvable row ever reaches. Reading those columns for
    this population therefore always yields ``None``, which is how the
    snapshot's ``last_error_code`` came to be structurally empty for exactly
    the payments this module exists for.

    That code is operationally load-bearing, not decoration: a connection
    timeout means the request WAS delivered and CentralPay may hold a link we
    never saw, whereas an explicit rejection means no link exists. That is the
    distinction an operator weighs before recording a resolution, and it is
    the same residual this module documents.

    Only the fixed internal reason-code vocabulary is returned. The gateway's
    raw message text is deliberately NOT surfaced — AGENTS.md forbids
    gateway-controlled content escaping the CentralPay client boundary into
    operator tooling, and a non-string payload value is dropped rather than
    passed through.

    One statement for the whole batch, so ``attention list`` does not issue a
    query per row.
    """
    if not payment_ids:
        return {}
    latest_ids = (
        select(func.max(PaymentEvent.id))
        .where(
            PaymentEvent.payment_id.in_(payment_ids),
            PaymentEvent.event_type == LINK_FAILURE_EVENT_TYPE,
        )
        .group_by(PaymentEvent.payment_id)
    )
    codes: dict[int, str | None] = {}
    for event in db.execute(
        select(PaymentEvent).where(PaymentEvent.id.in_(latest_ids))
    ).scalars():
        if event.payment_id is None:
            continue
        raw = (event.data or {}).get("error_code")
        codes[event.payment_id] = raw if isinstance(raw, str) else None
    return codes


def latest_link_failure_code(db: Session, payment_id: int) -> str | None:
    """Single-payment convenience over :func:`latest_link_failure_codes`."""
    return latest_link_failure_codes(db, [payment_id]).get(payment_id)


def resolution_superseded_in_db(db: Session, payment: Payment) -> bool:
    """Scalar counterpart of the second clause of
    :func:`unresolved_attention_condition` for one payment — used wherever the
    mutating or rendering path must apply the SAME supersession rule the
    worklist predicate applies, against a specific row."""
    if payment.attention_resolved_at is None:
        return False
    latest_failure = _latest_event_id(db, payment.id, LINK_FAILURE_EVENT_TYPE)
    if latest_failure is None:
        return False
    latest_resolution = _latest_event_id(db, payment.id, ATTENTION_RESOLVED_EVENT_TYPE)
    return latest_resolution is None or latest_failure > latest_resolution


def resolved_attention_condition() -> ColumnElement[bool]:
    """Every payment carrying a recorded resolution, for HISTORICAL views
    (``centralpay attention list --resolved``). Resolved items never vanish;
    they only leave the current worklist.

    Deliberately NOT the boolean complement of
    :func:`unresolved_attention_condition`: a resolution superseded by a
    newer failure is BOTH currently-open and historically-resolved, and the
    history view must keep showing it. Callers must also not add a status
    filter here — see ``app.ops``' ``attention list --resolved``.
    """
    return Payment.attention_resolved_at.is_not(None)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def eligible_resolutions_for_status(status: str) -> tuple[str, ...]:
    """Allowlisted resolution codes applicable to ``status``, sorted for a
    stable operator-facing listing."""
    return tuple(
        sorted(
            code
            for code, statuses in ATTENTION_RESOLUTIONS.items()
            if status in statuses
        )
    )


def refuse_reason(
    payment: Payment,
    *,
    now: datetime,
    resolution: str | None = None,
    superseded: bool = False,
) -> AttentionRefusal | None:
    """The COMPLETE eligibility guard, as a pure function of a Payment row.

    Returns ``None`` when the payment may be attention-resolved, otherwise the
    exact machine-readable reason it may not. Pure and side-effect free, so
    the preview path and the mutating path evaluate literally the same
    predicate — the mutating path just re-runs it against a freshly locked,
    ``populate_existing`` row (see :func:`resolve_attention`).

    Ordering is deliberate: the FINANCIAL refusals are evaluated before the
    status/resolution ones, so a payment that has become financially
    meaningful reports that fact rather than a less alarming "wrong status"
    message.

    ``resolution`` is optional: pass ``None`` to ask "is this payment
    resolvable at all?" (used by ``attention list``/``show``), or a specific
    allowlisted code to additionally check that code applies to this status.
    """
    # An already-resolved item is refused — UNLESS a newer link-creation
    # failure has superseded that resolution, in which case this is a fresh,
    # never-reviewed incident the operator must be able to close. Passing
    # `superseded=False` (the default, used only where the caller has no
    # event context) keeps the strict old behaviour.
    if payment.attention_resolved_at is not None and not superseded:
        return AttentionRefusal.ALREADY_RESOLVED

    # --- financially-meaningful guards (any one of these is disqualifying) ---
    if payment.gateway_verified_at is not None:
        return AttentionRefusal.GATEWAY_VERIFIED
    if payment.reference_id is not None:
        return AttentionRefusal.HAS_REFERENCE_ID
    if payment.manual_review_at is not None:
        return AttentionRefusal.UNDER_MANUAL_REVIEW
    if payment.bot_notify_attempts > 0:
        return AttentionRefusal.BOT_NOTIFICATION_ATTEMPTED
    # The decisive guard: proof this bridge never held (and so never returned
    # to the calling bot) a usable payment URL for this order. Deliberately
    # NOT also checking callback_token_hash — see the module docstring's
    # "``callback_token_hash`` is deliberately NOT a guard" section.
    if payment.redirect_url is not None:
        return AttentionRefusal.PAYMENT_LINK_ISSUED

    # --- allowlist guards ---
    if payment.status not in RESOLVABLE_STATUSES:
        return AttentionRefusal.STATUS_NOT_ELIGIBLE

    # The SAME grace period the canonical worklist predicate applies
    # (`app.services.stuck_payments.unexpected_status_conditions`). Without it
    # the mutating path could close an item no read-only surface even shows:
    # `app.services.payments._ensure_payment_row` COMMITS the `created` row
    # and releases its lock before `create_payment` re-acquires it to attempt
    # getLink, so a brand-new row is briefly visible and lock-free. Resolving
    # in that window records a judgment about an incident that has not
    # happened yet — and if creation then dies hard, without writing a
    # `centralpay_getlink_failed` event, the supersession rule never fires and
    # the abandoned row stays hidden. Refusing here keeps the mutating path
    # and the worklist in exact agreement about what is attention-worthy.
    if _as_utc(payment.created_at) > now - timedelta(
        seconds=UNEXPECTED_STATE_GRACE_SECONDS
    ):
        return AttentionRefusal.NOT_YET_STALE
    if resolution is not None and payment.status not in ATTENTION_RESOLUTIONS.get(
        resolution, frozenset()
    ):
        return AttentionRefusal.RESOLUTION_NOT_VALID_FOR_STATUS
    return None


def snapshot(
    payment: Payment,
    *,
    now: datetime,
    superseded: bool = False,
    link_failure_code: str | None = None,
) -> AttentionSnapshot:
    """Build the read-only operator view of one payment's attention state.

    ``superseded`` lets the caller report whether a newer link failure has
    obsoleted this payment's recorded resolution (see
    :func:`resolution_superseded_in_db`, or the equivalent computed in bulk by
    ``app.ops``' ``attention list``), so eligibility rendered here matches the
    worklist predicate exactly.
    """
    return AttentionSnapshot(
        bot_order_id=payment.bot_order_id,
        gateway_order_id=payment.gateway_order_id,
        status=payment.status,
        amount=payment.amount,
        fee_rate_bps=payment.fee_rate_bps,
        fee_amount=payment.fee_amount,
        payable_amount=payment.payable_amount,
        gateway_verified=payment.gateway_verified_at is not None,
        gateway_verified_at=payment.gateway_verified_at,
        reference_id=payment.reference_id,
        # Booleans, never the URL itself: a full redirect URL must never be
        # printed by an operator tool (AGENTS.md logging/secret contract).
        redirect_url_present=payment.redirect_url is not None,
        callback_token_issued=payment.callback_token_hash is not None,
        bot_notify_attempts=payment.bot_notify_attempts,
        manual_review_at=payment.manual_review_at,
        # The getLink failure's safe internal code, supplied by the caller from
        # `latest_link_failure_codes` (it lives in the audit event, not on the
        # row). The bot/reconciliation columns remain as fallbacks for
        # completeness, but never carry a value for a resolvable status.
        last_error_code=link_failure_code
        or payment.bot_last_error_code
        or payment.reconciliation_last_error_code,
        created_at=payment.created_at,
        attention_resolved_at=payment.attention_resolved_at,
        attention_resolution=payment.attention_resolution,
        attention_resolved_by=payment.attention_resolved_by,
        attention_resolution_note=payment.attention_resolution_note,
        refusal=refuse_reason(payment, now=now, superseded=superseded),
        eligible_resolutions=eligible_resolutions_for_status(payment.status),
        attention_resolution_superseded=superseded,
    )


def snapshot_refusal_message(snapshot: AttentionSnapshot) -> str | None:
    """Human-readable reason this snapshot's payment is not resolvable, or
    ``None`` when it is. Interpolates only already-safe local fields (status,
    allowlisted resolution codes, timestamps) — never raw external content,
    never a redirect URL, never a secret."""
    if snapshot.refusal is None:
        return None
    return REFUSAL_MESSAGE[snapshot.refusal].format(
        grace=UNEXPECTED_STATE_GRACE_SECONDS,
        status=snapshot.status,
        resolution=snapshot.attention_resolution,
        resolved_at=(
            snapshot.attention_resolved_at.isoformat()
            if snapshot.attention_resolved_at
            else None
        ),
        resolved_by=snapshot.attention_resolved_by,
        eligible=", ".join(sorted(RESOLVABLE_STATUSES)),
    )


def outcome_refusal_message(outcome: AttentionOutcome) -> str:
    """Render the refusal text for a :func:`resolve_attention` outcome.

    The outcome carries everything the message templates interpolate
    (``status`` and, for ALREADY_RESOLVED, the FIRST actor/time/resolution
    read under the row lock), so the caller never needs a second database
    round trip — and can never render a message from a row that changed again
    after the refusal was decided.
    """
    assert outcome.refusal is not None
    return REFUSAL_MESSAGE[outcome.refusal].format(
        grace=UNEXPECTED_STATE_GRACE_SECONDS,
        status=outcome.status,
        resolution=outcome.existing_resolution,
        resolved_at=(
            outcome.existing_resolved_at.isoformat()
            if outcome.existing_resolved_at
            else None
        ),
        resolved_by=outcome.existing_resolved_by,
        eligible=", ".join(sorted(RESOLVABLE_STATUSES)),
    )


def resolve_attention(
    db: Session,
    *,
    payment_id: int,
    resolution: str,
    note: str,
    actor: str,
    now: datetime,
) -> AttentionOutcome:
    """Durably record an operational attention resolution for ONE payment.

    Locks the row (``FOR UPDATE``, ``populate_existing=True`` so a stale
    identity-map copy can never satisfy the guard the locked row would fail),
    re-runs the COMPLETE :func:`refuse_reason` guard against that fresh state,
    and only then writes the four attention columns and appends the
    ``payment_attention_resolved`` audit event — both in the SAME transaction,
    so the resolution and its audit record commit atomically or not at all.

    Makes NO gateway call, NO downstream-bot call, and NO financial mutation.
    The caller owns the surrounding transaction boundary: this function
    commits on success and rolls back on refusal, matching the single-payment
    convention in ``app.services.notification.execute_manual_accept``.

    ``resolution`` MUST already be an ``ATTENTION_RESOLUTIONS`` key — the CLI
    enforces that through ``argparse`` ``choices`` before reaching here — but
    an unknown code still fails closed via
    ``RESOLUTION_NOT_VALID_FOR_STATUS`` rather than being written.

    ``payment_id`` must name an existing payment; the caller is expected to
    have resolved it (``app.ops`` does so through ``_find_payment`` on this
    same session and transaction). Payments are never deleted, so the
    ``scalar_one()`` below cannot legitimately miss; if it ever did, raising
    is the correct fail-closed outcome rather than silently reporting success.
    """
    # Validated BEFORE any lock or read: a blank note or actor is a caller
    # error, not a payment state, and there is no reason to hold a row lock to
    # discover it. The CLI already refuses these, but this module is the one
    # that claims to own every safety decision, and the database CHECK only
    # rejects NULL -- an empty string would otherwise satisfy it and record a
    # resolution with no recorded justification.
    if not note.strip() or not actor.strip():
        return AttentionOutcome(
            resolved=False,
            bot_order_id="",
            status="",
            resolution=None,
            refusal=AttentionRefusal.EMPTY_NOTE,
        )

    payment = db.execute(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()

    # Evaluated under the SAME lock, from the same append-only event trail the
    # worklist predicate reads, so a superseded resolution is re-openable here
    # exactly when the worklist says it is open.
    superseded = resolution_superseded_in_db(db, payment)
    previous_resolution = payment.attention_resolution
    refusal = refuse_reason(
        payment, now=now, resolution=resolution, superseded=superseded
    )
    if refusal is not None:
        outcome = AttentionOutcome(
            resolved=False,
            bot_order_id=payment.bot_order_id,
            status=payment.status,
            resolution=None,
            refusal=refusal,
            existing_resolution=payment.attention_resolution,
            existing_resolved_at=payment.attention_resolved_at,
            existing_resolved_by=payment.attention_resolved_by,
        )
        db.rollback()
        return outcome

    previous_status = payment.status
    # The ONLY writes in this module. `status` and every financial/identity
    # column are deliberately absent.
    payment.attention_resolved_at = now
    payment.attention_resolution = resolution
    # STRIP BEFORE TRUNCATING. Validating `note.strip()` above and then storing
    # `note[:NOTE_MAX_LENGTH]` checks a different string than it writes: a note
    # of NOTE_MAX_LENGTH spaces followed by a real character passes the
    # non-blank guard, but truncation keeps only the spaces, and the database's
    # `<> ''` constraint accepts whitespace. The resolution would then commit
    # with no usable justification in either the column or the audit event.
    # Stripping first makes the validated value and the stored value the same
    # string.
    safe_actor = actor.strip()[:ACTOR_MAX_LENGTH]
    safe_note = note.strip()[:NOTE_MAX_LENGTH]
    payment.attention_resolved_by = safe_actor
    payment.attention_resolution_note = safe_note

    record_event(
        db,
        payment_id=payment.id,
        event_type="payment_attention_resolved",
        level="warning",
        data={
            "resolution": resolution,
            "note": safe_note,
            "operator": safe_actor,
            # Recorded so the audit trail proves the status was NOT changed.
            "status": previous_status,
            "gateway_verified": False,
            "gateway_order_id": payment.gateway_order_id,
            # True when this closes a NEW failure that superseded an earlier
            # resolution; `superseded_resolution` names the one replaced. The
            # earlier resolution's own event stays in the trail permanently.
            "superseded_previous_resolution": superseded,
            "previous_resolution": previous_resolution if superseded else None,
        },
    )
    db.commit()
    logger.warning(
        "payment_attention_resolved",
        extra={
            "payment_id": payment_id,
            "gateway_order_id": payment.gateway_order_id,
            "resolution": resolution,
            "operator": safe_actor,
        },
    )
    return AttentionOutcome(
        resolved=True,
        bot_order_id=payment.bot_order_id,
        status=previous_status,
        resolution=resolution,
        refusal=None,
    )
