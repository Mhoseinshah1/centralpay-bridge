"""The shipped reconciliation schedule: defaults, config surfaces, load.

Production observed many unverified `link_created` payments with ~100-180
reconciliation attempts, ages of 1-2 hours, and a next retry roughly 60
SECONDS after the last check — while the documented default slow interval is
300 seconds. That raised four candidate explanations: (A) an intentional
deployment override, (B) a bad shipped default/template, (C) stale
compatibility config, or (D) a scheduler bug.

These tests settle (B) and (D) in the repository, which is the only place a
repository can settle them:

* the SCHEDULING MATH is exercised directly at and around the tier boundary,
  including the specific 1-2 hour ages production reported — a payment that
  old must be scheduled at the SLOW interval, never at 60 s;
* the ATTEMPT ARITHMETIC each candidate slow interval implies is computed, so
  the observed 100-180 attempts can be attributed to a configuration value
  rather than guessed at;
* the shipped DEFAULTS are pinned, and `app/config.py`, `.env.example`, and
  `deploy/centralpay.env.template` are asserted to agree with each other.

That last one is the load-bearing regression. The three surfaces currently
agree, and a silent divergence between them is exactly how a deployment ends
up polling far harder than the documentation claims — i.e. how (B) would come
true later even though it is not true today.

Nothing here contacts a gateway.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models import Payment, PaymentStatus
from app.services.reconciliation import reconciliation_retry_delay_seconds

REPO_ROOT = Path(__file__).resolve().parents[1]

# The schedule this repository ships and documents. Changing any of these is a
# deliberate load/financial-coverage decision, not a refactor.
SHIPPED_DEFAULTS = {
    "RECONCILIATION_ENABLED": "true",
    "RECONCILIATION_MIN_AGE_SECONDS": "10",
    "RECONCILIATION_INTERVAL_SECONDS": "5",
    "RECONCILIATION_BATCH_SIZE": "10",
    "RECONCILIATION_SLOW_TIER_RESERVED_SLOTS": "1",
    "RECONCILIATION_FAST_WINDOW_SECONDS": "900",
    "RECONCILIATION_FAST_INTERVAL_SECONDS": "10",
    "RECONCILIATION_SLOW_INTERVAL_SECONDS": "300",
    "RECONCILIATION_MAX_AGE_SECONDS": "7200",
    "RECONCILIATION_MAX_ATTEMPTS": "1000",
}

_SETTINGS_ATTRIBUTE = {
    "RECONCILIATION_ENABLED": "reconciliation_enabled",
    "RECONCILIATION_MIN_AGE_SECONDS": "reconciliation_min_age_seconds",
    "RECONCILIATION_INTERVAL_SECONDS": "reconciliation_interval_seconds",
    "RECONCILIATION_BATCH_SIZE": "reconciliation_batch_size",
    "RECONCILIATION_SLOW_TIER_RESERVED_SLOTS": "reconciliation_slow_tier_reserved_slots",
    "RECONCILIATION_FAST_WINDOW_SECONDS": "reconciliation_fast_window_seconds",
    "RECONCILIATION_FAST_INTERVAL_SECONDS": "reconciliation_fast_interval_seconds",
    "RECONCILIATION_SLOW_INTERVAL_SECONDS": "reconciliation_slow_interval_seconds",
    "RECONCILIATION_MAX_AGE_SECONDS": "reconciliation_max_age_seconds",
    "RECONCILIATION_MAX_ATTEMPTS": "reconciliation_max_attempts",
}


def _env_values(path: Path) -> dict[str, str]:
    """Uncommented KEY=VALUE pairs only — a commented-out line documents a
    deprecated knob and must not be read as a shipped default."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([A-Z0-9_]+)=(.*)", line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _payment(age_seconds: int, now: datetime) -> Payment:
    return Payment(
        bot_order_id="sched",
        gateway_order_id=1,
        gateway_user_id=1,
        amount=1000,
        payable_amount=1000,
        status=PaymentStatus.LINK_CREATED.value,
        callback_token_issued_at=now - timedelta(seconds=age_seconds),
        created_at=now - timedelta(seconds=age_seconds),
    )


# --- the shipped defaults are what the code says they are ----------------


@pytest.mark.parametrize(("key", "expected"), sorted(SHIPPED_DEFAULTS.items()))
def test_code_default_matches_the_shipped_schedule(key, expected, settings):
    """The shared `settings` fixture deliberately sets NO reconciliation value,
    so every attribute read here IS the shipped `app/config.py` default."""
    actual = getattr(settings, _SETTINGS_ATTRIBUTE[key])
    if isinstance(actual, bool):
        assert str(actual).lower() == expected
    else:
        assert int(actual) == int(expected)


@pytest.mark.parametrize(
    "relative_path", [".env.example", "deploy/centralpay.env.template"]
)
def test_config_surface_matches_the_shipped_schedule(relative_path):
    """`.env.example` (documentation) and the installer template (what a fresh
    install actually gets) must both state the same schedule the code
    defaults to.

    A divergence here is the realistic path to the production symptom: a
    template shipping a harder-polling value than the docs claim, or docs
    drifting away from what installs actually run.
    """
    values = _env_values(REPO_ROOT / relative_path)
    for key, expected in SHIPPED_DEFAULTS.items():
        assert values.get(key) == expected, f"{relative_path}: {key}"


def test_the_deprecated_backoff_knobs_are_not_shipped_as_active_config():
    """`RECONCILIATION_INITIAL_BACKOFF_SECONDS` / `_MAX_BACKOFF_SECONDS` are
    accepted for environment compatibility but no longer control the schedule.
    They must stay commented out in `.env.example` and absent from the
    installer template, so a fresh install never looks like it is configuring
    a retry cadence that has no effect."""
    example = _env_values(REPO_ROOT / ".env.example")
    template = _env_values(REPO_ROOT / "deploy/centralpay.env.template")
    for key in (
        "RECONCILIATION_INITIAL_BACKOFF_SECONDS",
        "RECONCILIATION_MAX_BACKOFF_SECONDS",
    ):
        assert key not in example
        assert key not in template


# --- the scheduling math -------------------------------------------------


@pytest.mark.parametrize(
    ("age_seconds", "expected_delay"),
    [
        (0, 10),  # brand new -> fast
        (899, 10),  # just inside the fast window
        (900, 300),  # exactly at the boundary -> slow
        (901, 300),
        (3600, 300),  # 1 hour: a production-observed age
        (5400, 300),  # 1.5 hours
        (7199, 300),  # 2 hours, still inside the lifetime
    ],
)
def test_retry_delay_is_the_documented_two_stage_schedule(
    age_seconds, expected_delay, settings
):
    """The production symptom was a next retry ~60 s after the last check on a
    1-2 hour old payment. With the shipped configuration that is impossible:
    anything at or past the 900 s boundary schedules at 300 s.
    """
    now = datetime.now(UTC)
    delay = reconciliation_retry_delay_seconds(
        settings, payment=_payment(age_seconds, now), now=now
    )
    assert delay == expected_delay


def test_a_sixty_second_cadence_at_one_to_two_hours_requires_an_overridden_slow_interval(
    settings,
):
    """Attribution, not speculation.

    Under the SHIPPED configuration a 1-2 hour old payment is scheduled 300 s
    out; only an overridden `RECONCILIATION_SLOW_INTERVAL_SECONDS` can produce
    the observed ~60 s. This test states that implication as executable
    arithmetic, so the diagnosis in OPERATIONS_FA.md rests on something
    checkable rather than on prose.
    """
    now = datetime.now(UTC)
    shipped = settings
    overridden = settings.model_copy(
        update={"reconciliation_slow_interval_seconds": 60}
    )

    for age in (3600, 5400, 7199):
        payment = _payment(age, now)
        assert reconciliation_retry_delay_seconds(shipped, payment=payment, now=now) == 300
        assert reconciliation_retry_delay_seconds(overridden, payment=payment, now=now) == 60


@pytest.mark.parametrize(
    ("slow_interval", "age_seconds", "expected_attempts"),
    [
        # Shipped 300 s: a 2-hour-old link can only ever have reached ~111.
        (300, 3600, 99),
        (300, 7200, 111),
        # Overridden 60 s: the production-observed 100-180 range.
        (60, 3600, 135),
        (60, 7200, 195),
    ],
)
def test_attempt_arithmetic_attributes_the_observed_attempt_counts(
    slow_interval, age_seconds, expected_attempts
):
    """Maximum attempts a link of a given age can have accumulated:
    `fast_window / fast_interval` in the fast window, then
    `(age - fast_window) / slow_interval` afterwards.

    Production reported 100-180 attempts at ages of 1-2 hours. The shipped
    300 s schedule tops out at 111 for a payment at the very end of its
    2-hour lifetime, so it cannot produce 180; a 60 s slow interval spans
    exactly the observed range. This is the arithmetic behind classifying the
    observation as a deployment override rather than a shipped-default or
    scheduler defect.
    """
    fast_window, fast_interval = 900, 10
    attempts = fast_window // fast_interval
    if age_seconds > fast_window:
        attempts += (age_seconds - fast_window) // slow_interval
    assert attempts == expected_attempts
    # Either way the attempt cap is never the binding limit within the
    # 2-hour lifetime: max age is the primary stop condition, as documented.
    assert attempts < int(SHIPPED_DEFAULTS["RECONCILIATION_MAX_ATTEMPTS"])


def test_average_verify_load_is_bounded_by_batch_size_over_scan_interval():
    """`batch_size / interval` is the documented AVERAGE upper bound on verify
    calls. Pin it so a batch-size or scan-interval change that multiplies
    gateway load has to be an explicit decision."""
    batch = int(SHIPPED_DEFAULTS["RECONCILIATION_BATCH_SIZE"])
    interval = int(SHIPPED_DEFAULTS["RECONCILIATION_INTERVAL_SECONDS"])
    assert batch / interval == 2.0  # at most ~2 verify calls per second, on average


def test_the_expiring_tier_keeps_a_reserved_slot_but_never_the_whole_batch():
    """Fairness between the active and expiring tiers: the reserved quota must
    stay strictly below the batch size, or a historical backlog could consume
    every slot and delay newly-created payments."""
    reserved = int(SHIPPED_DEFAULTS["RECONCILIATION_SLOW_TIER_RESERVED_SLOTS"])
    batch = int(SHIPPED_DEFAULTS["RECONCILIATION_BATCH_SIZE"])
    assert 0 < reserved < batch
