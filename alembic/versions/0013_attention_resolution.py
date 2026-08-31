"""Operational attention resolution for stale, non-financial failures.

Production kept a 2026-08-01 payment in ``getlink_failed`` (the CentralPay
``getLink.php`` call timed out, so no payment link was ever issued, no
gateway verification ever happened, no reference id was ever assigned, and
the downstream bot was never notified). ``app.services.stuck_payments``
classifies such a row ``needs_attention / unexpected_status:getlink_failed``
FOREVER, because nothing in the schema could express "an operator looked at
this and it needs no further action." The only way to clear the operator's
worklist was to delete the payment — destroying permanent financial/audit
history, which this project explicitly forbids.

This migration adds the four columns that record that decision durably:

* ``attention_resolved_at``      — when the operator resolved it;
* ``attention_resolution``       — the allowlisted machine-readable reason
  (``app.services.attention.ATTENTION_RESOLUTIONS``), never free text;
* ``attention_resolved_by``      — the acting operator/actor label;
* ``attention_resolution_note``  — the operator's mandatory justification.

plus two CHECK constraints and one partial index:

* ``ck_payments_attention_resolution_consistent`` — all four columns are set
  together or not at all, so a row can never claim to be resolved without
  recording by whom, when, on what grounds, and why;
* ``ck_payments_attention_resolution_fields_not_empty`` — and none of those
  recorded fields is blank. The consistency check alone rejects only NULL, so
  an empty-string note would satisfy it while recording no justification at
  all. Same shape as the existing ``ck_fee_policies_note_not_empty``;
* ``ix_payments_attention_unresolved`` — partial index on
  ``(status, created_at) WHERE attention_resolved_at IS NULL``, matching the
  canonical unresolved-attention predicate every operator surface now shares.

NO constraint ties ``attention_resolved_at`` to ``gateway_verified_at``, and
that omission is deliberate. ``app.services.verification.process_callback``
does NOT gate on ``status == link_created``: a payment whose ``getLink`` call
timed out still holds the ``callback_token_hash`` for the signed return URL
that WAS delivered to CentralPay before the timeout, so if CentralPay did
create the link and a payer paid it, a valid late browser callback settles the
payment normally — a deliberate safety net. A
``attention_resolved_at IS NULL OR gateway_verified_at IS NULL`` constraint
would turn that legitimate settlement into an ``IntegrityError`` and fail a
real customer payment in order to keep an operator worklist tidy. Attention
resolution is an operator OPINION and never constrains the financial path.

NO FINANCIAL FACT IS INVENTED OR ALTERED. Every existing row gets NULL in
all four columns, which means exactly "not resolved" — the same operational
meaning those rows already had. ``amount``, ``payable_amount``, the fee
snapshot, ``gateway_verified_at``, ``reference_id``, ``gateway_order_id``,
``gateway_user_id``, payer identity, ``status``, ``payment_events``, and
``admin_alerts`` are all untouched. No row is deleted, and no status is
rewritten.

Idempotent / recovery-safe (house style, matching 0010/0011/0012):
``upgrade`` no-ops for objects that already exist.

DOWNGRADE LIMITATION (honest statement): ``downgrade`` is NON-destructive by
default — it only moves the Alembic pointer back to 0012 and PRESERVES the
columns, because the pre-0013 application simply ignores them and dropping
them would permanently destroy operator resolution history (actor, time,
reason, note) that cannot be reconstructed from anywhere else. The
``payment_events`` audit trail retains a ``payment_attention_resolved`` event
for each resolution, so the *fact* of a resolution survives a destructive
drop, but the structured columns the operator worklist filters on do not.
An operator who genuinely wants the columns gone must opt in explicitly with
``CENTRALPAY_DROP_ATTENTION_RESOLUTION=1``; doing so makes every previously
resolved row reappear in ``needs_attention``, which is the correct
fail-visible direction (never fail-silent).

Revision ID: 0013
Revises: 0012
"""

import os

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None

_TABLE = "payments"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("attention_resolved_at", sa.DateTime(timezone=True)),
    ("attention_resolution", sa.String(length=64)),
    ("attention_resolved_by", sa.String(length=128)),
    ("attention_resolution_note", sa.Text()),
)
_CK_CONSISTENT = "ck_payments_attention_resolution_consistent"
_CK_NOT_EMPTY = "ck_payments_attention_resolution_fields_not_empty"
_INDEX = "ix_payments_attention_unresolved"

_CONSISTENT_SQL = (
    "(attention_resolved_at IS NULL AND attention_resolution IS NULL"
    " AND attention_resolved_by IS NULL AND attention_resolution_note IS NULL)"
    " OR (attention_resolved_at IS NOT NULL AND attention_resolution IS NOT NULL"
    " AND attention_resolved_by IS NOT NULL AND attention_resolution_note IS NOT NULL)"
)

_NOT_EMPTY_SQL = (
    "attention_resolved_at IS NULL"
    " OR (attention_resolution <> '' AND attention_resolved_by <> ''"
    " AND attention_resolution_note <> '')"
)


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_constraint(bind: sa.engine.Connection, table: str, name: str) -> bool:
    inspector = sa.inspect(bind)
    return any(
        c.get("name") == name for c in inspector.get_check_constraints(table)
    )


def _has_index(bind: sa.engine.Connection, table: str, name: str) -> bool:
    return any(i["name"] == name for i in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    for name, type_ in _COLUMNS:
        if not _has_column(bind, _TABLE, name):
            # Nullable with no server default: every existing row becomes
            # NULL == "not resolved", which is exactly its current meaning.
            # No backfill, no invented financial or operational fact.
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))

    if not _has_constraint(bind, _TABLE, _CK_CONSISTENT):
        op.create_check_constraint(_CK_CONSISTENT, _TABLE, sa.text(_CONSISTENT_SQL))
    if not _has_constraint(bind, _TABLE, _CK_NOT_EMPTY):
        op.create_check_constraint(_CK_NOT_EMPTY, _TABLE, sa.text(_NOT_EMPTY_SQL))

    if not _has_index(bind, _TABLE, _INDEX):
        if bind.dialect.name == "postgresql":
            op.create_index(
                _INDEX,
                _TABLE,
                ["status", "created_at"],
                postgresql_where=sa.text("attention_resolved_at IS NULL"),
            )
        else:  # SQLite (unit tests): partial indexes use the same syntax
            op.create_index(
                _INDEX,
                _TABLE,
                ["status", "created_at"],
                sqlite_where=sa.text("attention_resolved_at IS NULL"),
            )


def downgrade() -> None:
    # NON-DESTRUCTIVE BY DEFAULT: preserve the operator resolution history
    # (actor/time/reason/note) that nothing else stores in structured form.
    # See the module docstring's DOWNGRADE LIMITATION note.
    if os.environ.get("CENTRALPAY_DROP_ATTENTION_RESOLUTION") != "1":
        return
    bind = op.get_bind()
    if _has_index(bind, _TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    for name in (_CK_NOT_EMPTY, _CK_CONSISTENT):
        if _has_constraint(bind, _TABLE, name):
            op.drop_constraint(name, _TABLE, type_="check")
    for name, _type in reversed(_COLUMNS):
        if _has_column(bind, _TABLE, name):
            op.drop_column(_TABLE, name)
