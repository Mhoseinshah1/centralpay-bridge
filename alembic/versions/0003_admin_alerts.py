"""Admin alert outbox and worker heartbeats.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-18

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSONColumn = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "admin_alerts",
        sa.Column("id", BigIntPK, primary_key=True, autoincrement=True),
        sa.Column("alert_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column(
            "payment_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("payments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("deduplication_key", sa.String(160), nullable=True),
        sa.Column("payload", JSONColumn, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_admin_alerts_alert_type", "admin_alerts", ["alert_type"])
    op.create_index("ix_admin_alerts_payment_id", "admin_alerts", ["payment_id"])
    op.create_index("ix_admin_alerts_deduplication_key", "admin_alerts", ["deduplication_key"])
    op.create_index("ix_admin_alerts_status", "admin_alerts", ["status"])
    op.create_index("ix_admin_alerts_created_at", "admin_alerts", ["created_at"])
    op.create_index("ix_admin_alerts_due", "admin_alerts", ["status", "next_retry_at"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("id", BigIntPK, primary_key=True, autoincrement=True),
        sa.Column("worker_name", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_cycle_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("version", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_worker_heartbeats_worker_name", "worker_heartbeats", ["worker_name"])
    op.create_index(
        "ix_worker_heartbeats_instance_id", "worker_heartbeats", ["instance_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("admin_alerts")
