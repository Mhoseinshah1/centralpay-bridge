"""Lightweight periodic health monitoring with consecutive-failure thresholds.

An unhealthy alert fires only after N consecutive failures; a recovery alert
fires after M consecutive successes following an alert. Counters live in
memory: a restart resets them, which at worst delays one alert cycle.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adminbot import queries
from app.adminbot.alerts import create_alert
from app.config import Settings
from app.models import AdminAlert, AlertStatus, Payment

logger = logging.getLogger("app.adminbot.health")

ApiProbe = Callable[[], dict[str, bool]]


@dataclass(frozen=True)
class CheckResult:
    check: str
    ok: bool
    detail: str = ""


def run_health_checks(
    db: Session, settings: Settings, api_probe: ApiProbe
) -> list[CheckResult]:
    now = datetime.now(UTC)
    results: list[CheckResult] = []

    try:
        api = api_probe()
    except Exception:
        api = {"live": False, "ready": False}
    results.append(CheckResult("api_ready", bool(api.get("ready")), "readiness probe"))

    results.append(CheckResult("database", queries.database_ok(db), "SELECT 1"))

    heartbeat_age = queries.worker_heartbeat_age_seconds(db)
    worker_ok = heartbeat_age is not None and heartbeat_age < max(
        settings.bot_notify_worker_interval_seconds * 6, 120
    )
    results.append(
        CheckResult(
            "worker_heartbeat",
            worker_ok,
            f"age={int(heartbeat_age)}s" if heartbeat_age is not None else "no heartbeat",
        )
    )

    # Retry queue stalled: due items untouched for a long time.
    stall_cutoff = now - timedelta(minutes=10)
    stalled = db.execute(
        select(func.count(Payment.id)).where(
            Payment.status == "bot_notify_pending",
            Payment.notification_claimed_at.is_(None),
            Payment.next_retry_at <= stall_cutoff,
        )
    ).scalar_one()
    results.append(CheckResult("retry_queue", stalled == 0, f"stalled={stalled}"))

    # Backup overdue: no successful backup within ~26h.
    backup = queries.latest_backup_alert(db, "backup_succeeded")
    backup_ok = False
    if backup is not None:
        created = backup.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        backup_ok = now - created < timedelta(hours=26)
    results.append(
        CheckResult("backup", backup_ok, "recent backup found" if backup_ok else "overdue")
    )

    # Alert queue stalled: pending alerts that never get delivered.
    alert_cutoff = now - timedelta(minutes=15)
    stuck_alerts = db.execute(
        select(func.count(AdminAlert.id)).where(
            AdminAlert.status == AlertStatus.PENDING.value,
            AdminAlert.created_at <= alert_cutoff,
        )
    ).scalar_one()
    results.append(CheckResult("alert_queue", stuck_alerts == 0, f"stalled={stuck_alerts}"))

    return results


@dataclass
class HealthMonitor:
    settings: Settings
    session_factory: sessionmaker[Session]
    api_probe: ApiProbe
    failure_counts: dict[str, int] = field(default_factory=dict)
    success_counts: dict[str, int] = field(default_factory=dict)
    alerted: set[str] = field(default_factory=set)

    def run_once(self) -> list[CheckResult]:
        with self.session_factory() as db:
            results = run_health_checks(db, self.settings, self.api_probe)
            # Backup checks only alert when backup alerts are wanted; the
            # backup check result stays visible either way.
            for result in results:
                self._process(db, result)
            db.commit()
        return results

    def _process(self, db: Session, result: CheckResult) -> None:
        name = result.check
        if result.ok:
            self.failure_counts[name] = 0
            self.success_counts[name] = self.success_counts.get(name, 0) + 1
            if (
                name in self.alerted
                and self.success_counts[name]
                >= self.settings.admin_bot_health_recovery_threshold
            ):
                self.alerted.discard(name)
                if self.settings.admin_bot_health_alerts:
                    create_alert(
                        db,
                        alert_type="service_recovered",
                        severity="info",
                        payload={"check": name, "detail": result.detail},
                    )
                logger.info("admin_health_recovered", extra={"check": name})
        else:
            self.success_counts[name] = 0
            self.failure_counts[name] = self.failure_counts.get(name, 0) + 1
            if (
                name not in self.alerted
                and self.failure_counts[name]
                >= self.settings.admin_bot_health_failure_threshold
            ):
                self.alerted.add(name)
                if self.settings.admin_bot_health_alerts:
                    create_alert(
                        db,
                        alert_type="service_unhealthy",
                        severity="error",
                        deduplication_key=f"service_unhealthy:{name}",
                        payload={"check": name, "detail": result.detail},
                    )
                logger.error(
                    "admin_health_alert", extra={"check": name, "detail": result.detail}
                )
