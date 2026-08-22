"""Monitor incident alert-delivery outcome tracking.

Adds ``MonitorIncident.last_alert_id`` — an FK to ``admin_alerts.id``
recording which outbox row ``last_alerted_at`` refers to. Without it, an
incident whose opening/escalation alert PERMANENTLY failed delivery (every
Telegram retry exhausted, ``admin_alerts.status == 'failed'``) would look
"already alerted" forever: ``app.services.monitor_incidents``'s catch-up
path only re-checked ``last_alerted_at is None``, which stays set from the
moment the row was queued for delivery — regardless of whether delivery
ever actually succeeded. This column lets that catch-up path look up the
referenced alert's actual status and re-queue a fresh one if it failed.

Idempotent / recovery-safe (house style, matching 0010/0011): ``upgrade``
no-ops for objects that already exist and ``downgrade`` is NON-destructive
by default — it only moves the Alembic pointer back to 0011 and preserves
the column, so a code rollback never forces a schema downgrade. To
actually drop it an operator opts in with
``CENTRALPAY_DROP_MONITOR_INCIDENT_LAST_ALERT=1``.

Revision ID: 0012
Revises: 0011
"""

import os

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None

_TABLE = "monitor_incidents"
_COLUMN = "last_alert_id"
_FK = "fk_monitor_incidents_last_alert_id"


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_fk(bind: sa.engine.Connection, table: str, name: str) -> bool:
    return any(fk["name"] == name for fk in sa.inspect(bind).get_foreign_keys(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, _TABLE, _COLUMN):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.BigInteger(), nullable=True))
    if not _has_fk(bind, _TABLE, _FK):
        op.create_foreign_key(
            _FK, _TABLE, "admin_alerts", [_COLUMN], ["id"], ondelete="RESTRICT"
        )


def downgrade() -> None:
    if os.environ.get("CENTRALPAY_DROP_MONITOR_INCIDENT_LAST_ALERT") != "1":
        return
    bind = op.get_bind()
    if _has_fk(bind, _TABLE, _FK):
        op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    if _has_column(bind, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
