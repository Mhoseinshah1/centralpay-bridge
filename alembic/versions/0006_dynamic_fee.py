"""Dynamic percentage fee: fee_policies table and payment fee snapshots.

Backfill for existing payments: fee_policy_id NULL, fee_rate_bps 0,
fee_amount 0, payable_amount = amount — historical payments are exactly
"zero fee", so every existing amount, order id, status, reference id,
callback hash, notification state, and audit row is preserved untouched
and all new CHECK constraints hold for them.

Forward-only for financial data as usual: the downgrade drops the new
columns/table but is never run automatically.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fee_policies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.String(length=128), nullable=True),
        sa.Column("cancellation_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_fee_policies_effective_at", "fee_policies", ["effective_at"])
    op.create_check_constraint(
        "ck_fee_policies_rate_bps_range", "fee_policies", "rate_bps >= 0 AND rate_bps <= 10000"
    )
    op.create_check_constraint("ck_fee_policies_note_not_empty", "fee_policies", "note <> ''")
    op.create_check_constraint(
        "ck_fee_policies_cancellation_consistent",
        "fee_policies",
        "(cancelled_at IS NULL AND cancelled_by IS NULL AND cancellation_note IS NULL)"
        " OR (cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL"
        " AND cancellation_note IS NOT NULL)",
    )

    op.add_column(
        "payments", sa.Column("fee_policy_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_payments_fee_policy_id",
        "payments",
        "fee_policies",
        ["fee_policy_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "payments",
        sa.Column("fee_rate_bps", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "payments",
        sa.Column("fee_amount", sa.BigInteger(), nullable=False, server_default="0"),
    )
    # Backfill in two steps so existing rows get payable_amount = amount.
    op.add_column("payments", sa.Column("payable_amount", sa.BigInteger(), nullable=True))
    op.execute("UPDATE payments SET payable_amount = amount WHERE payable_amount IS NULL")
    op.alter_column("payments", "payable_amount", nullable=False)

    op.create_check_constraint(
        "ck_payments_fee_rate_bps_range",
        "payments",
        "fee_rate_bps >= 0 AND fee_rate_bps <= 10000",
    )
    op.create_check_constraint(
        "ck_payments_fee_amount_non_negative", "payments", "fee_amount >= 0"
    )
    op.create_check_constraint(
        "ck_payments_payable_positive", "payments", "payable_amount > 0"
    )
    op.create_check_constraint(
        "ck_payments_payable_equals_amount_plus_fee",
        "payments",
        "payable_amount = amount + fee_amount",
    )


def downgrade() -> None:
    for name in (
        "ck_payments_payable_equals_amount_plus_fee",
        "ck_payments_payable_positive",
        "ck_payments_fee_amount_non_negative",
        "ck_payments_fee_rate_bps_range",
    ):
        op.drop_constraint(name, "payments", type_="check")
    op.drop_constraint("fk_payments_fee_policy_id", "payments", type_="foreignkey")
    for column in ("payable_amount", "fee_amount", "fee_rate_bps", "fee_policy_id"):
        op.drop_column("payments", column)
    op.drop_table("fee_policies")
