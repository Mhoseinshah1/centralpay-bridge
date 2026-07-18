"""Operational hooks: python -m app.ops COMMAND

Used by host-side scripts (backup.sh) and the centralpay management command
to record operational events in the database. These are append-only
operational records — never financial mutations.

Commands:
  backup-event {success|failure} [--size TEXT] [--file-name TEXT]
                                 [--retention-days N] [--detail TEXT]
  test-alert
"""

import argparse
import sys

from app.adminbot.alerts import configure_alert_creation, create_alert
from app.audit import record_event
from app.config import Settings
from app.db import create_session_factory
from app.logging_setup import configure_logging


def _cmd_backup_event(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    configure_alert_creation(settings)
    session_factory = create_session_factory(settings.database_url)
    event_type = "backup_succeeded" if args.outcome == "success" else "backup_failed"
    data: dict[str, object] = {}
    if args.size:
        data["size"] = args.size[:64]
    if args.file_name:
        # Base name only: full paths are unnecessary disclosure.
        data["file_name"] = args.file_name.rsplit("/", 1)[-1][:128]
    if args.retention_days:
        data["retention_days"] = args.retention_days
    if args.detail:
        data["detail"] = args.detail[:200]
    with session_factory() as db:
        record_event(
            db,
            payment_id=None,
            event_type=event_type,
            level="info" if args.outcome == "success" else "error",
            data=data,
        )
        db.commit()
    print(f"recorded {event_type}")
    return 0


def _cmd_test_alert(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(settings)
    if not settings.admin_bot_enabled:
        print("admin bot is disabled (ADMIN_BOT_ENABLED=false)", file=sys.stderr)
        return 1
    session_factory = create_session_factory(settings.database_url)
    with session_factory() as db:
        alert = create_alert(
            db,
            alert_type="admin_test_alert",
            severity="info",
            payload={"detail": "پیام آزمایشی — این فقط یک تست است / test message"},
        )
        db.commit()
        print(f"test alert queued (id={alert.id}); delivery within the poll interval")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ops", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup-event", help="record a backup outcome")
    backup.add_argument("outcome", choices=["success", "failure"])
    backup.add_argument("--size", default="")
    backup.add_argument("--file-name", default="")
    backup.add_argument("--retention-days", type=int, default=0)
    backup.add_argument("--detail", default="")
    sub.add_parser("test-alert", help="queue a clearly marked test alert")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "backup-event":
        return _cmd_backup_event(args)
    return _cmd_test_alert(args)


if __name__ == "__main__":
    sys.exit(main())
