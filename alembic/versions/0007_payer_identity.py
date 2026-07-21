"""Per-customer CentralPay payer identity isolation (incident 2026-07).

Adds the ``centralpay_payer_identities`` mapping table (stable per-customer
gateway ``userId``) and two snapshot columns on ``payments``:
``payer_identity_id`` (FK; NULL == created under the legacy shared payer id,
the historical marker) and ``payer_derivation_version``.

Non-destructive: existing payment rows are untouched. Their ``gateway_user_id``
snapshot (the old shared CENTRALPAY_USER_ID) is preserved, ``payer_identity_id``
stays NULL, and their callbacks keep verifying against that snapshot, so active
payment links remain valid. The downgrade is reversible but never run
automatically (forward-only financial history).

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.add_column(
        "payments", sa.Column("payer_derivation_version", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_constraint("fk_payments_payer_identity_id", "payments", type_="foreignkey")
    op.drop_index("ix_payments_payer_identity_id", table_name="payments")
    for column in ("payer_derivation_version", "payer_identity_id"):
        op.drop_column("payments", column)
    op.drop_table("centralpay_payer_identities")
