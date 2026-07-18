"""Initial schema: payments and the permanent payment_events audit trail.

Revision ID: 0001
Revises:
Create Date: 2026-07-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSONColumn = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", BigIntPK, primary_key=True, autoincrement=True),
        sa.Column("bot_order_id", sa.String(length=128), nullable=False),
        sa.Column("gateway_order_id", sa.BigInteger(), nullable=False),
        sa.Column("gateway_user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("redirect_url", sa.Text(), nullable=True),
        sa.Column("reference_id", sa.String(length=128), nullable=True),
        sa.Column("card_last4", sa.String(length=4), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        "ix_payments_bot_order_id", "payments", ["bot_order_id"], unique=True
    )
    op.create_index(
        "ix_payments_gateway_order_id", "payments", ["gateway_order_id"], unique=True
    )
    op.create_index("ix_payments_status", "payments", ["status"])

    op.create_table(
        "payment_events",
        sa.Column("id", BigIntPK, primary_key=True, autoincrement=True),
        sa.Column(
            "payment_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("payments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("data", JSONColumn, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_payment_events_payment_id", "payment_events", ["payment_id"])
    op.create_index("ix_payment_events_event_type", "payment_events", ["event_type"])
    op.create_index("ix_payment_events_created_at", "payment_events", ["created_at"])


def downgrade() -> None:
    # The audit trail must never be silently deleted; downgrading the initial
    # schema is intentionally not supported.
    raise RuntimeError(
        "Downgrading the initial schema would delete payments and the "
        "permanent payment_events audit trail; this is not supported."
    )
