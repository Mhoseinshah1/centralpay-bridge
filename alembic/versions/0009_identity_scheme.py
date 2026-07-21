"""Explicit payer-identity scheme column (raw-Telegram-userId revision).

Adds ``centralpay_payer_identities.identity_scheme`` so the derivation scheme
of every mapping row is EXPLICIT and never inferred from the numeric value:

* ``telegram_raw_v1`` — gateway_user_id IS the exact Telegram id (product
  requirement, matching the reference mirza-cpanel behavior);
* ``order_hmac_v1``   — keyed-HMAC id inside the reserved fallback range
  (strictly above every valid Telegram id);
* ``historical_hmac_v1`` — every row that already exists when this migration
  runs: created by the retired keyed-HMAC derivations (customer_id-era or
  v1 tg/order). Applied via the column's server_default during ADD COLUMN —
  a non-destructive backfill that is accurate for all pre-0009 rows and for
  any row a not-yet-updated (0008-era) application inserts afterwards.

Existing mappings, payment snapshots, and live links are untouched: their
``gateway_user_id`` values stay exactly as stored, so callbacks keep
verifying. Production is at revision 0008; this is a forward-only follow-up
(0007 and 0008 are applied and are never edited).

Idempotent / recovery-safe: ``upgrade`` no-ops for objects that already
exist, and ``downgrade`` is NON-destructive by default — it only moves the
Alembic pointer back to 0008 and preserves the column + CHECK, so a code
rollback never forces a schema downgrade or loses scheme labels. To actually
drop the column an operator opts in with ``CENTRALPAY_DROP_PAYER_IDENTITY=1``.

Revision ID: 0009
Revises: 0008
"""

import os

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None

_TABLE = "centralpay_payer_identities"
_CHECK_NAME = "ck_payer_identities_identity_scheme_valid"
_CHECK_SQL = (
    "identity_scheme IN ('telegram_raw_v1', 'order_hmac_v1', 'historical_hmac_v1')"
)


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_check(bind, table: str, name: str) -> bool:
    return any(c["name"] == name for c in sa.inspect(bind).get_check_constraints(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, _TABLE, "identity_scheme"):
        # NOT NULL with a server_default: existing rows are labeled
        # historical_hmac_v1 in place (non-destructive, instant on PG11+).
        op.add_column(
            _TABLE,
            sa.Column(
                "identity_scheme",
                sa.String(length=32),
                nullable=False,
                server_default="historical_hmac_v1",
            ),
        )
    if not _has_check(bind, _TABLE, _CHECK_NAME):
        op.create_check_constraint(_CHECK_NAME, _TABLE, _CHECK_SQL)


def downgrade() -> None:
    # NON-destructive by default: keep the column (and its CHECK) so rolling
    # the application back and forward again never loses scheme labels or
    # requires a destructive schema change. Alembic still moves the version
    # pointer to 0008. Opt in to drop the schema explicitly.
    if os.environ.get("CENTRALPAY_DROP_PAYER_IDENTITY") != "1":
        return
    bind = op.get_bind()
    if _has_check(bind, _TABLE, _CHECK_NAME):
        op.drop_constraint(_CHECK_NAME, _TABLE, type_="check")
    if _has_column(bind, _TABLE, "identity_scheme"):
        op.drop_column(_TABLE, "identity_scheme")
