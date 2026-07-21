"""Hybrid payer-identity scope column (follow-up to incident 2026-07 / 0007).

Adds ``payments.payer_identity_type`` — the identity scope
(``telegram_user``/``order_fallback``) introduced by the upstream-bot
compatibility revision — as a NEW migration on top of the already-deployed
0007. Production executed the ORIGINAL 0007 (mapping table +
``payer_identity_id``/``payer_derivation_version`` only) and its
``alembic_version`` is ``0007``; Alembic never re-runs an applied revision, so
this column MUST ship as 0008, never by editing 0007.

Backfill: intentionally NONE. Rows written while 0007-era code was live carry a
non-NULL ``payer_identity_id`` whose mapping was keyed by the retired
``customer_id`` scheme; the raw identity is (by design) not stored, so their
scope is NOT determinable — guessing ``telegram_user`` would be wrong. They stay
``NULL`` = "historical, untyped", exactly like pre-0007 legacy rows, and the
application handles both explicitly (see ``_reconcile_identity`` in
``app/services/payments.py``). The CHECK below therefore allows NULL.

Idempotent / recovery-safe: ``upgrade`` no-ops for objects that already exist
(e.g. a staging database built while the column briefly lived in a modified
0007), and ``downgrade`` is NON-destructive by default — it only moves the
Alembic pointer back to 0007 and preserves the column, so a code rollback never
forces a schema downgrade or loses identity scopes. To actually drop the
column an operator opts in with ``CENTRALPAY_DROP_PAYER_IDENTITY=1``.

Revision ID: 0008
Revises: 0007
"""

import os

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None

_CHECK_NAME = "ck_payments_payer_identity_type_valid"
# NULL passes (historical/untyped rows); new rows must use a known scope.
_CHECK_SQL = (
    "payer_identity_type IS NULL "
    "OR payer_identity_type IN ('telegram_user', 'order_fallback')"
)


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_check(bind, table: str, name: str) -> bool:
    return any(c["name"] == name for c in sa.inspect(bind).get_check_constraints(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "payments", "payer_identity_type"):
        op.add_column(
            "payments",
            sa.Column("payer_identity_type", sa.String(length=16), nullable=True),
        )
    if not _has_check(bind, "payments", _CHECK_NAME):
        op.create_check_constraint(_CHECK_NAME, "payments", _CHECK_SQL)
    # No backfill (see module docstring): existing rows — both pre-0007 legacy
    # and 0007-era customer-scoped — keep NULL as their explicit historical
    # marker; the scope of a 0007-era row is not determinable from stored data.


def downgrade() -> None:
    # NON-destructive by default: keep the column (and its CHECK) so rolling
    # the application back and forward again never loses identity scopes or
    # requires a destructive schema change. Alembic still moves the version
    # pointer to 0007. Opt in to drop the schema explicitly.
    if os.environ.get("CENTRALPAY_DROP_PAYER_IDENTITY") != "1":
        return
    bind = op.get_bind()
    if _has_check(bind, "payments", _CHECK_NAME):
        op.drop_constraint(_CHECK_NAME, "payments", type_="check")
    if _has_column(bind, "payments", "payer_identity_type"):
        op.drop_column("payments", "payer_identity_type")
