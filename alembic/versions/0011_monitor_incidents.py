"""Monitor incident state (app.monitor / app.services.monitor_incidents).

Adds ``monitor_incidents``: durable, cross-restart incident state for the
monitoring subsystem's checks (see AGENTS.md Phase 6-equivalent monitoring
work). A check transitioning from healthy to warning/critical opens a row;
the SAME condition staying unhealthy across polling cycles never opens a
second row; recovery resolves it. At most one OPEN row can exist per
``check_key`` at a time — enforced by a partial unique index
(``status = 'open'``) rather than application logic alone, so two racing
monitor instances can never both "win" opening the same incident (the loser
gets an IntegrityError and treats the winner's row as authoritative — the
same idiom ``app.services.payments`` already uses for ``bot_order_id``
races).

Idempotent / recovery-safe (house style, matching 0010): ``upgrade`` no-ops
for objects that already exist and ``downgrade`` is NON-destructive by
default — it only moves the Alembic pointer back to 0010 and preserves the
table, so a code rollback never forces a schema downgrade (the previous
application simply never queries this table). To actually drop the table an
operator opts in with ``CENTRALPAY_DROP_MONITOR_INCIDENTS=1``.

Revision ID: 0011
Revises: 0010
"""

import os

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None

_TABLE = "monitor_incidents"
_INDEX_CHECK_KEY_STATUS = "ix_monitor_incidents_check_key_status"
_INDEX_OPEN_UNIQUE = "uq_monitor_incidents_open_check_key"

# Self-contained, matching every prior migration's convention: migrations
# never import app.models, so a later change to that module can never alter
# what an already-applied migration creates.
BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSONColumn = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _has_index(bind: sa.engine.Connection, table: str, name: str) -> bool:
    return any(i["name"] == name for i in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", BigIntPK, primary_key=True, autoincrement=True),
            sa.Column("check_key", sa.String(length=64), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="open"
            ),
            sa.Column(
                "opened_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_alerted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("details", JSONColumn, nullable=True),
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
    if not _has_index(bind, _TABLE, _INDEX_CHECK_KEY_STATUS):
        op.create_index(_INDEX_CHECK_KEY_STATUS, _TABLE, ["check_key", "status"])
    if not _has_index(bind, _TABLE, _INDEX_OPEN_UNIQUE):
        op.create_index(
            _INDEX_OPEN_UNIQUE,
            _TABLE,
            ["check_key"],
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
            sqlite_where=sa.text("status = 'open'"),
        )


def downgrade() -> None:
    if os.environ.get("CENTRALPAY_DROP_MONITOR_INCIDENTS") != "1":
        return
    bind = op.get_bind()
    if _has_table(bind, _TABLE):
        op.drop_table(_TABLE)
