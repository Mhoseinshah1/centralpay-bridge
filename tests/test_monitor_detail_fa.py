"""app.adminbot.commands._monitor_detail_fa — the short Persian summary
suffix `/monitor` appends to each check line.

Regression: under a non-default MONITOR_RECONCILIATION_EXHAUSTED_RECENT_
WINDOW_SECONDS shorter than RECONCILIATION_MAX_AGE_SECONDS,
exhausted_not_aged_out and exhausted_recent are NOT nested populations --
neither is a subset of the other -- so the summary must render the exact
union count (exhausted_actionable_total), never either population alone and
never max(exhausted_not_aged_out, exhausted_recent) (which can undercount
two disjoint actionable rows as one; see
test_reconciliation_actionable_total_counts_disjoint_rows_correctly in
tests/test_monitor_checks.py for the underlying query-level proof).
"""

from app.adminbot.commands import _monitor_detail_fa
from app.services.monitor_checks import CheckResult


def test_reconciliation_summary_uses_actionable_total_in_the_common_case():
    result = CheckResult(
        "reconciliation",
        "critical",
        "reconciliation_exhausted",
        {
            "exhausted_not_aged_out": 0,
            "exhausted_recent": 3,
            "exhausted_actionable_total": 3,
            "exhausted_historical_total": 3,
            "oldest_overdue_seconds": None,
        },
    )
    assert _monitor_detail_fa(result) == " — اتمام‌یافته: 3"


def test_reconciliation_summary_never_shows_zero_while_actionable_total_is_nonzero():
    """exhausted_not_aged_out > 0 (the actual driver of CRITICAL) but
    exhausted_recent == 0 (a short custom window excluding it) -- the
    summary must surface the nonzero actionable total, never "0"."""
    result = CheckResult(
        "reconciliation",
        "critical",
        "reconciliation_exhausted",
        {
            "exhausted_not_aged_out": 1,
            "exhausted_recent": 0,
            "exhausted_actionable_total": 1,
            "exhausted_historical_total": 1,
            "oldest_overdue_seconds": None,
        },
    )
    assert _monitor_detail_fa(result) == " — اتمام‌یافته: 1"


def test_reconciliation_summary_reports_the_full_union_not_the_max():
    """The exact scenario a naive max(exhausted_not_aged_out, exhausted_recent)
    undercounts: two DISTINCT rows, one in each population, neither
    contained in the other -- max() would report 1, but the real actionable
    total is 2. The summary must show the real total."""
    result = CheckResult(
        "reconciliation",
        "critical",
        "reconciliation_exhausted",
        {
            "exhausted_not_aged_out": 1,
            "exhausted_recent": 1,
            "exhausted_actionable_total": 2,
            "exhausted_historical_total": 2,
            "oldest_overdue_seconds": None,
        },
    )
    assert _monitor_detail_fa(result) == " — اتمام‌یافته: 2"


def test_reconciliation_summary_zero_when_actionable_total_is_zero():
    result = CheckResult(
        "reconciliation",
        "ok",
        "healthy",
        {
            "exhausted_not_aged_out": 0,
            "exhausted_recent": 0,
            "exhausted_actionable_total": 0,
            "exhausted_historical_total": 12,
            "oldest_overdue_seconds": None,
        },
    )
    assert _monitor_detail_fa(result) == " — اتمام‌یافته: 0"
