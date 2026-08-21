"""Configuration validation for the monitor service's alert thresholds."""

import pytest

from app.config import ConfigurationError, validate_monitor_settings


def test_settings_construction_survives_bad_monitor_thresholds(settings):
    """A malformed MONITOR_* pair must never crash Settings() for api/worker.

    The threshold-ordering checks used to live in the shared
    _validate_bot_settings model_validator, so a typo here could break
    payment processing even with MONITOR_ENABLED=false. They now live in
    validate_monitor_settings, which only the monitor service calls.
    """
    broken = settings.model_copy(
        update={
            "monitor_worker_heartbeat_warning_seconds": 999_999,
            "monitor_worker_heartbeat_critical_seconds": 1,
        }
    )
    assert broken.monitor_worker_heartbeat_warning_seconds == 999_999


def test_valid_monitor_configuration_passes(settings):
    enabled = settings.model_copy(update={"monitor_enabled": True})
    validate_monitor_settings(enabled)


def test_monitor_disabled_is_rejected(settings):
    with pytest.raises(ConfigurationError, match="MONITOR_ENABLED"):
        validate_monitor_settings(settings.model_copy(update={"monitor_enabled": False}))


@pytest.mark.parametrize(
    ("update", "match"),
    [
        (
            {
                "monitor_worker_heartbeat_warning_seconds": 999_999,
                "monitor_worker_heartbeat_critical_seconds": 1,
            },
            "MONITOR_WORKER_HEARTBEAT_WARNING_SECONDS",
        ),
        (
            {
                "monitor_notification_warning_count": 999,
                "monitor_notification_critical_count": 1,
            },
            "MONITOR_NOTIFICATION_WARNING_COUNT",
        ),
        (
            {
                "monitor_manual_review_warning_count": 999,
                "monitor_manual_review_critical_count": 1,
            },
            "MONITOR_MANUAL_REVIEW_WARNING_COUNT",
        ),
        (
            {
                "monitor_backup_warning_age_seconds": 999_999,
                "monitor_backup_critical_age_seconds": 1,
            },
            "MONITOR_BACKUP_WARNING_AGE_SECONDS",
        ),
        (
            {
                "monitor_disk_critical_percent": 50.0,
                "monitor_disk_warning_percent": 10.0,
            },
            "MONITOR_DISK_CRITICAL_PERCENT",
        ),
        (
            {
                "monitor_gateway_failure_warning_count": 999,
                "monitor_gateway_failure_critical_count": 1,
            },
            "MONITOR_GATEWAY_FAILURE_WARNING_COUNT",
        ),
        (
            {
                "monitor_bot_failure_warning_count": 999,
                "monitor_bot_failure_critical_count": 1,
            },
            "MONITOR_BOT_FAILURE_WARNING_COUNT",
        ),
    ],
)
def test_monitor_threshold_ordering_is_enforced(settings, update, match):
    broken = settings.model_copy(update={**update, "monitor_enabled": True})
    with pytest.raises(ConfigurationError, match=match):
        validate_monitor_settings(broken)
