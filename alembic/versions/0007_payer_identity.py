"""Per-identity CentralPay payer isolation (incident 2026-07).

Adds the ``centralpay_payer_identities`` mapping table (stable per-identity
gateway ``userId``) and snapshot columns on ``payments``: ``payer_identity_id``
(FK; NULL == created under the legacy shared payer id, the historical marker),
``payer_identity_type`` (telegram_user/order_fallback), and
``payer_derivation_version``.

Non-destructive: existing payment rows are untouched. Their ``gateway_user_id``
snapshot (the old shared CENTRALPAY_USER_ID) is preserved, ``payer_identity_id``
stays NULL, and their callbacks keep verifying against that snapshot, so active
payment links remain valid.

Rollback-safe / re-entrant: an application rollback can leave the database at
revision 0007 while the code is back at 0006's expectations. Rolling the CODE
forward again must not fail, so ``upgrade`` is idempotent (IF NOT EXISTS checks)
and ``downgrade`` is NON-destructive by default — it only moves the alembic
pointer to 0006 and preserves the mapping table + columns, so no payer identity
is lost and no destructive schema change is required. To actually drop the
schema an operator opts in with ``CENTRALPAY_DROP_PAYER_IDENTITY=1`` (see the
incident doc's recovery procedure).

Revision ID: 0007
Revises: 0006
"""

import os

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "centralpay_payer_identities"):
        op.create_table(
            "centralpay_payer_identities",
            sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
            sa.Column("customer_key_hash", sa.String(length=64), nullable=False),
            sa.Column("gateway_user_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "derivation_version", sa.Integer(), nullable=False, server_default="1"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_centralpay_payer_identities_customer_key_hash",
            "centralpay_payer_identities",
            ["customer_key_hash"],
            unique=True,
        )
        op.create_unique_constraint(
            "uq_payer_identities_gateway_user_id",
            "centralpay_payer_identities",
            ["gateway_user_id"],
        )
        op.create_check_constraint(
            "ck_payer_identities_gateway_user_id_positive",
            "centralpay_payer_identities",
            "gateway_user_id > 0",
        )
        op.create_check_constraint(
            "ck_payer_identities_derivation_version_positive",
            "centralpay_payer_identities",
            "derivation_version >= 1",
        )

    if not _has_column(bind, "payments", "payer_identity_id"):
        op.add_column(
            "payments", sa.Column("payer_identity_id", sa.BigInteger(), nullable=True)
        )
        op.create_index(
            "ix_payments_payer_identity_id", "payments", ["payer_identity_id"]
        )
        op.create_foreign_key(
            "fk_payments_payer_identity_id",
            "payments",
            "centralpay_payer_identities",
            ["payer_identity_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _has_column(bind, "payments", "payer_identity_type"):
        op.add_column(
            "payments",
            sa.Column("payer_identity_type", sa.String(length=16), nullable=True),
        )
    if not _has_column(bind, "payments", "payer_derivation_version"):
        op.add_column(
            "payments", sa.Column("payer_derivation_version", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    # NON-destructive by default: preserve the mapping table + snapshot columns
    # so rolling the application back to 0006 and forward again never loses
    # payer identities or requires a destructive schema change. Alembic still
    # moves the version pointer to 0006. Opt in to drop the schema explicitly.
    if os.environ.get("CENTRALPAY_DROP_PAYER_IDENTITY") != "1":
        return
    bind = op.get_bind()
    if _has_column(bind, "payments", "payer_identity_id"):
        op.drop_constraint("fk_payments_payer_identity_id", "payments", type_="foreignkey")
        op.drop_index("ix_payments_payer_identity_id", table_name="payments")
    for column in ("payer_derivation_version", "payer_identity_type", "payer_identity_id"):
        if _has_column(bind, "payments", column):
            op.drop_column("payments", column)
    if _has_table(bind, "centralpay_payer_identities"):
        op.drop_table("centralpay_payer_identities")
