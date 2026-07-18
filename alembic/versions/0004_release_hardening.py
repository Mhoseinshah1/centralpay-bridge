"""Release hardening: callback tokens, reference uniqueness, review metadata.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-18

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments", sa.Column("callback_token_hash", sa.String(64), nullable=True)
    )
    op.add_column(
        "payments",
        sa.Column("callback_token_issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("review_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments", sa.Column("review_resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("payments", sa.Column("review_resolution", sa.String(64), nullable=True))
    # reference_id uniqueness: PostgreSQL allows multiple NULLs, so unpaid
    # payments are unaffected. Existing data predates any release, so no
    # duplicate cleanup step is required.
    op.create_unique_constraint("uq_payments_reference_id", "payments", ["reference_id"])


def downgrade() -> None:
    op.drop_constraint("uq_payments_reference_id", "payments", type_="unique")
    for column in (
        "review_resolution",
        "review_resolved_at",
        "review_acknowledged_at",
        "callback_token_issued_at",
        "callback_token_hash",
    ):
        op.drop_column("payments", column)
