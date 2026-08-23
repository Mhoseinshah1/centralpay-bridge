"""app.adminbot.commands._monitor_detail_fa — the short Persian summary
suffix `/monitor` appends to each check line.

Regression: under a non-default MONITOR_RECONCILIATION_EXHAUSTED_RECENT_
WINDOW_SECONDS shorter than RECONCILIATION_MAX_AGE_SECONDS, a payment can
be exhausted_not_aged_out (still within the reconciliation lifetime --
always alarms) while falling OUTSIDE the recent window (exhausted_recent
== 0). check_reconciliation correctly stays critical (either population
alarms it), but the summary must never display "critical ... 0" for the
population that is NOT what actually tripped it.
"""

from app.adminbot.commands import _monitor_detail_fa
from app.services.monitor_checks import CheckResult


def test_reconciliation_summary_uses_recent_count_in_the_common_case():
    result = CheckResult(
        "reconciliation",
        "critical",
        "reconciliation_exhausted",
        {
            "exhausted_not_aged_out": 0,
            "exhausted_recent": 3,
            "exhausted_historical_total": 3,
            "oldest_overdue_seconds": None,
        },
    )
    assert _monitor_detail_fa(result) == " — اتمام‌یافته: 3"


def test_reconciliation_summary_never_shows_zero_while_actionable_count_is_nonzero():
    """The exact misconfiguration scenario: exhausted_not_aged_out > 0 (the
    actual driver of CRITICAL) but exhausted_recent == 0 (a short custom
    window excluding it) -- the summary must surface the nonzero count,
    never "0"."""
    result = CheckResult(
        "reconciliation",
        "critical",
        "reconciliation_exhausted",
        {
            "exhausted_not_aged_out": 1,
            "exhausted_recent": 0,
            "exhausted_historical_total": 1,
            "oldest_overdue_seconds": None,
        },
    )
    assert _monitor_detail_fa(result) == " — اتمام‌یافته: 1"


def test_reconciliation_summary_zero_when_both_populations_are_zero():
    result = CheckResult(
        "reconciliation",
        "ok",
        "healthy",
        {
            "exhausted_not_aged_out": 0,
            "exhausted_recent": 0,
            "exhausted_historical_total": 12,
            "oldest_overdue_seconds": None,
        },
    )
    assert _monitor_detail_fa(result) == " — اتمام‌یافته: 0"
