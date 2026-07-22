"""Server-side payment reconciliation state (stuck link_created recovery).

Production observed a payment that CentralPay showed as successful while the
bridge kept it in ``link_created`` with ``gateway_verified_at IS NULL``: the
payer completed payment but the browser callback never reached the API. The
worker now reconciles such payments server-side through the SAME shared
verification path the callback uses; this migration adds only the
reconciliation bookkeeping:

* ``reconciliation_attempts``        — bounded attempt counter;
* ``reconciliation_next_at``         — next due time (NULL on a link_created
  row means "never attempted yet": due as soon as it is old enough);
* ``reconciliation_last_at``         — last attempt time;
* ``reconciliation_last_error_code`` — fixed internal reason code only, never
  a raw gateway response;
* ``reconciliation_claimed_at`` / ``reconciliation_claimed_by`` — operational
  visibility of the claiming worker (correctness comes from row locks);
* index ``ix_payments_reconciliation_due (status, reconciliation_next_at)``.

No data migration is required: existing ``link_created`` rows have NULL
``reconciliation_next_at`` and zero attempts, which makes them due
immediately once they pass the configured minimum age. Financial columns and
existing rows are untouched.

Idempotent / recovery-safe (house style): ``upgrade`` no-ops for objects that
already exist and ``downgrade`` is NON-destructive by default — it only moves
the Alembic pointer back to 0009 and preserves the columns, so a code
rollback never forces a schema downgrade (the previous application ignores
the extra columns). To actually drop the schema an operator opts in with
``CENTRALPAY_DROP_RECONCILIATION=1``.

Revision ID: 0010
Revises: 0009
"""

import os

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None

_INDEX = "ix_payments_reconciliation_due"
_COLUMN_NAMES = (
    "reconciliation_attempts",
    "reconciliation_next_at",
    "reconciliation_last_at",
    "reconciliation_last_error_code",
    "reconciliation_claimed_at",
    "reconciliation_claimed_by",
)


def _fresh_columns() -> "list[sa.Column[object]]":
    return [
        sa.Column(
            "reconciliation_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("reconciliation_next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_last_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_last_error_code", sa.String(length=64), nullable=True),
        sa.Column("reconciliation_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_claimed_by", sa.String(length=128), nullable=True),
    ]


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_index(bind, table: str, name: str) -> bool:
    return any(i["name"] == name for i in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    for column in _fresh_columns():
        if not _has_column(bind, "payments", str(column.name)):
            op.add_column("payments", column)
    if not _has_index(bind, "payments", _INDEX):
        op.create_index(_INDEX, "payments", ["status", "reconciliation_next_at"])


def downgrade() -> None:
    # NON-destructive by default: keep the columns and index so rolling the
    # application back and forward again never loses reconciliation state or
    # requires a destructive schema change. Alembic still moves the version
    # pointer to 0009. Opt in to drop the schema explicitly.
    if os.environ.get("CENTRALPAY_DROP_RECONCILIATION") != "1":
        return
    bind = op.get_bind()
    if _has_index(bind, "payments", _INDEX):
        op.drop_index(_INDEX, table_name="payments")
    for column_name in reversed(_COLUMN_NAMES):
        if _has_column(bind, "payments", column_name):
            op.drop_column("payments", column_name)
