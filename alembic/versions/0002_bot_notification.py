"""Bot notification delivery tracking.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-18

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments", sa.Column("gateway_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("payments", sa.Column("bot_notify_reason", sa.String(64), nullable=True))
    op.add_column(
        "payments",
        sa.Column("bot_notify_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("payments", sa.Column("bot_last_http_status", sa.Integer(), nullable=True))
    op.add_column("payments", sa.Column("bot_last_error_code", sa.String(64), nullable=True))
    op.add_column(
        "payments", sa.Column("bot_notify_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payments", sa.Column("bot_notify_accepted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("payments", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "payments", sa.Column("manual_review_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payments",
        sa.Column("notification_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments", sa.Column("notification_claimed_by", sa.String(128), nullable=True)
    )
    op.create_index("ix_payments_notify_due", "payments", ["status", "next_retry_at"])

    # Backfill: rows verified under Phase 1 get their verification timestamp
    # from updated_at. They stay in their current status; the worker only
    # processes bot_notify_pending rows.
    op.execute(
        "UPDATE payments SET gateway_verified_at = updated_at "
        "WHERE status IN ('gateway_verified', 'bot_notify_pending', 'bot_notify_accepted') "
        "AND gateway_verified_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_payments_notify_due", table_name="payments")
    for column in (
        "notification_claimed_by",
        "notification_claimed_at",
        "manual_review_at",
        "next_retry_at",
        "bot_notify_accepted_at",
        "bot_notify_started_at",
        "bot_last_error_code",
        "bot_last_http_status",
        "bot_notify_attempts",
        "bot_notify_reason",
        "gateway_verified_at",
    ):
        op.drop_column("payments", column)
