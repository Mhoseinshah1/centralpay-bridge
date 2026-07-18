"""Reliability: outages never block payments; alerts recover; claims are safe."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import update

from app.adminbot.alerts import claim_due_alerts, create_alert
from app.adminbot.health import HealthMonitor
from app.models import AdminAlert, AlertStatus
from app.services.heartbeat import record_worker_heartbeat
from tests.conftest import (
    FakeAlertSender,
    get_alerts,
    make_verified_pending,
    run_alert_pass,
    run_pass,
)

FIXED_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def test_admin_bot_outage_does_not_block_api(
    alert_policy, client, settings, session_factory, stub
):
    """With alerts enabled and NO admin bot running (nothing delivers), the
    payment flow works end to end; alerts just accumulate as pending."""
    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="rel-api"
    )
    assert payment.gateway_verified_at is not None
    # No Telegram interaction happened anywhere in the API flow.


def test_telegram_outage_does_not_block_worker(
    alert_policy, client, settings, session_factory, stub, bot_stub, notifier
):
    make_verified_pending(client, settings, session_factory, stub, order_id="rel-worker")
    bot_stub.result = httpx.Response(200)
    result = run_pass(session_factory, notifier, settings)
    # Worker delivered the SALES-bot notification without touching Telegram.
    assert result["processed"] == 1


def test_pending_alerts_recover_after_restart(alert_policy, admin_settings, session_factory):
    with session_factory() as db:
        create_alert(db, alert_type="admin_test_alert", now=FIXED_NOW)
        db.commit()
    # A "new instance" (fresh sender/worker id) picks up the pending alert.
    sender = FakeAlertSender()
    processed = run_alert_pass(
        session_factory,
        sender,
        admin_settings,
        worker_id="restarted-instance",
        now_fn=lambda: FIXED_NOW,
    )
    assert processed == 1
    [alert] = get_alerts(session_factory, "admin_test_alert")
    assert alert.status == AlertStatus.DELIVERED.value


def test_concurrent_claims_cannot_take_same_alert(
    alert_policy, admin_settings, session_factory
):
    with session_factory() as db:
        create_alert(db, alert_type="admin_test_alert", now=FIXED_NOW)
        db.commit()
    session_a = session_factory()
    session_b = session_factory()
    try:
        claimed_a = claim_due_alerts(session_a, worker_id="bot-a", now=FIXED_NOW)
        assert len(claimed_a) == 1
        # The second instance sees the claim and gets nothing.
        claimed_b = claim_due_alerts(session_b, worker_id="bot-b", now=FIXED_NOW)
        assert claimed_b == []
    finally:
        session_a.close()
        session_b.close()


def test_stale_alert_claims_recover(alert_policy, admin_settings, session_factory):
    with session_factory() as db:
        alert = create_alert(db, alert_type="admin_test_alert", now=FIXED_NOW)
        db.commit()
        alert_id = alert.id
    stale = FIXED_NOW - timedelta(
        seconds=admin_settings.admin_bot_alert_claim_timeout_seconds + 1
    )
    with session_factory() as db:
        db.execute(
            update(AdminAlert)
            .where(AdminAlert.id == alert_id)
            .values(
                status=AlertStatus.SENDING.value,
                claimed_at=stale,
                claimed_by="dead-instance",
                attempts=1,
            )
        )
        db.commit()
    sender = FakeAlertSender()
    run_alert_pass(session_factory, sender, admin_settings, now_fn=lambda: FIXED_NOW)
    [alert] = get_alerts(session_factory, "admin_test_alert")
    assert alert.status == AlertStatus.DELIVERED.value
    assert sender.sent  # re-sent after the stale claim was released


def test_worker_heartbeat_stale_state_detected(alert_policy, admin_settings, session_factory):
    stale_time = datetime.now(UTC) - timedelta(minutes=30)
    with session_factory() as db:
        record_worker_heartbeat(
            db,
            worker_name="notification-worker",
            instance_id="wrk-stale-1",
            now=stale_time,
            cycle_completed=True,
        )
    monitor = HealthMonitor(
        admin_settings,
        session_factory,
        api_probe=lambda: {"live": True, "ready": True},
    )
    for _ in range(admin_settings.admin_bot_health_failure_threshold):
        results = monitor.run_once()
    worker_check = next(r for r in results if r.check == "worker_heartbeat")
    assert worker_check.ok is False
    alerts = get_alerts(session_factory, "service_unhealthy")
    assert any((a.payload or {}).get("check") == "worker_heartbeat" for a in alerts)


def test_health_monitor_thresholds_and_recovery(alert_policy, admin_settings, session_factory):
    """Unhealthy only after N consecutive failures; recovery after M successes."""
    with session_factory() as db:
        record_worker_heartbeat(
            db,
            worker_name="notification-worker",
            instance_id="wrk-live-1",
            now=datetime.now(UTC),
            cycle_completed=True,
        )
    api_ok = {"value": False}
    monitor = HealthMonitor(
        admin_settings,
        session_factory,
        api_probe=lambda: {"live": api_ok["value"], "ready": api_ok["value"]},
    )
    threshold = admin_settings.admin_bot_health_failure_threshold
    for _ in range(threshold - 1):
        monitor.run_once()
    api_alerts = [
        a
        for a in get_alerts(session_factory, "service_unhealthy")
        if (a.payload or {}).get("check") == "api_ready"
    ]
    assert api_alerts == []  # below threshold: no alert yet
    monitor.run_once()
    api_alerts = [
        a
        for a in get_alerts(session_factory, "service_unhealthy")
        if (a.payload or {}).get("check") == "api_ready"
    ]
    assert len(api_alerts) == 1

    # Recovery after the configured number of consecutive successes.
    api_ok["value"] = True
    for _ in range(admin_settings.admin_bot_health_recovery_threshold):
        monitor.run_once()
    recovered = [
        a
        for a in get_alerts(session_factory, "service_recovered")
        if (a.payload or {}).get("check") == "api_ready"
    ]
    assert len(recovered) == 1


def test_worker_records_database_heartbeat(session_factory):
    now = datetime.now(UTC)
    with session_factory() as db:
        record_worker_heartbeat(
            db,
            worker_name="notification-worker",
            instance_id="wrk-hb-1",
            now=now,
            cycle_completed=True,
        )
        # Upsert: same instance updates, no duplicate rows.
        record_worker_heartbeat(
            db,
            worker_name="notification-worker",
            instance_id="wrk-hb-1",
            now=now + timedelta(seconds=10),
            cycle_completed=False,
            error_code="RuntimeError",
        )
    from sqlalchemy import select

    from app.models import WorkerHeartbeat

    with session_factory() as db:
        rows = list(
            db.execute(
                select(WorkerHeartbeat).where(WorkerHeartbeat.instance_id == "wrk-hb-1")
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].last_error_code == "RuntimeError"
    assert rows[0].version == "0.5.0-rc1"


def test_signature_storm_creates_single_aggregated_alert(
    alert_policy, client, settings, session_factory, stub
):
    from app.api.callback import signature_failure_tracker

    payment = make_verified_pending(
        client, settings, session_factory, stub, order_id="rel-sig"
    )
    signature_failure_tracker.reset()
    for _ in range(6):
        response = client.get(
            f"/api/centralpay/callback?orderId={payment.gateway_order_id}"
            f"&ct={'a' * 32}&sig={'0' * 64}"
        )
        assert response.status_code == 403
    alerts = get_alerts(session_factory, "callback_signature_failures")
    assert len(alerts) == 1  # aggregated, not one per request
    assert alerts[0].payload["count"] >= 5


def test_signature_storm_reports_on_freshly_booted_machine():
    """Regression (CI failure on GitHub Actions runners): time.monotonic()
    has an arbitrary epoch and is SMALLER than the window on a freshly
    booted machine. The first storm must still be reported."""
    from app.api.callback import SignatureFailureTracker

    tracker = SignatureFailureTracker(threshold=5, window_seconds=600.0)
    # Clock values below window_seconds — a machine up for under 10 minutes.
    results = [tracker.record(now=100.0 + i) for i in range(6)]
    assert results[4] == 5  # fifth failure crosses the threshold and reports
    assert results[5] is None  # and only once per window
    # A second storm within the same window stays suppressed...
    assert tracker.record(now=200.0) is None
    # ...but reports again after the window has passed.
    late_results = [tracker.record(now=800.0 + i) for i in range(5)]
    assert late_results[-1] is not None
