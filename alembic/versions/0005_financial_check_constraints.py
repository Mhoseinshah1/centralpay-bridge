"""Financial invariant CHECK constraints (final financial audit).

All three constraints are valid for every state the application can have
produced, so this migration is safe on existing data:

- amounts are validated positive at the API before insert;
- attempt counters only ever increment from zero;
- bot_notify_pending/accepted are only ever set in the same transaction
  as (or after) gateway_verified_at.

Adding a CHECK constraint takes a brief ACCESS EXCLUSIVE lock while the
table is scanned; on the row counts this system produces the impact is
negligible. Forward-only for financial data as usual — the downgrade
only drops the constraints, never data.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_payments_amount_positive", "payments", "amount > 0"
    )
    op.create_check_constraint(
        "ck_payments_attempts_non_negative", "payments", "bot_notify_attempts >= 0"
    )
    op.create_check_constraint(
        "ck_payments_delivery_requires_verification",
        "payments",
        "status NOT IN ('bot_notify_pending', 'bot_notify_accepted')"
        " OR gateway_verified_at IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_payments_delivery_requires_verification", "payments", type_="check"
    )
    op.drop_constraint("ck_payments_attempts_non_negative", "payments", type_="check")
    op.drop_constraint("ck_payments_amount_positive", "payments", type_="check")
