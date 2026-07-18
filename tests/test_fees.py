"""Dynamic fee arithmetic, rate parsing, and deterministic policy selection.

Pure integer money math (never floats) and the append-only fee_policies
lifecycle: selection, scheduling, cancellation, permanent history.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import FeePolicy
from app.services.fees import (
    MAX_RATE_BPS,
    calculate_fee,
    cancel_policy,
    create_policy,
    format_rate_percent,
    next_scheduled_policy,
    parse_rate_percent,
    select_effective_policy,
)
from tests.conftest import event_types, get_events

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
PAST = NOW - timedelta(days=30)
FUTURE = NOW + timedelta(days=30)


# --- fee arithmetic ---------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "rate_bps", "expected_fee", "expected_payable"),
    [
        (500_000, 1000, 50_000, 550_000),  # the canonical business example: 10%
        (500_000, 750, 37_500, 537_500),  # 7.5%
        (500_000, 250, 12_500, 512_500),  # 2.5%
        (101, 1000, 10, 111),  # 10.1 rounds DOWN (below half)
        (1005, 1000, 101, 1106),  # 100.5 rounds UP (half up)
        (999, 50, 5, 1004),  # 4.995 rounds UP (above half)
        (1000, 25, 3, 1003),  # 2.5 exactly: half rounds UP
        (500_000, 0, 0, 500_000),  # zero fee
        (500_000, 10_000, 500_000, 1_000_000),  # 100%
        (1, 1, 0, 1),  # smallest inputs: 0.0001 -> 0
        (10**12, 1, 100_000_000, 10**12 + 100_000_000),  # huge amount, no overflow
    ],
)
def test_calculate_fee_exact_integer_cases(amount, rate_bps, expected_fee, expected_payable):
    fee, payable = calculate_fee(amount, rate_bps)
    assert fee == expected_fee
    assert payable == expected_payable
    assert payable == amount + fee
    assert isinstance(fee, int) and isinstance(payable, int)


def test_calculate_fee_is_deterministic_pure_integer():
    """(amount * rate_bps + 5000) // 10000 — same inputs, same output, always."""
    results = {calculate_fee(123_457, 333) for _ in range(100)}
    assert results == {((123_457 * 333 + 5_000) // 10_000, 123_457 + 4_111)}


@pytest.mark.parametrize("amount", [0, -1, -500_000])
def test_calculate_fee_rejects_non_positive_amount(amount):
    with pytest.raises(ValueError, match="amount must be positive"):
        calculate_fee(amount, 1000)


@pytest.mark.parametrize("rate_bps", [-1, MAX_RATE_BPS + 1, 10**6])
def test_calculate_fee_rejects_out_of_range_rate(rate_bps):
    with pytest.raises(ValueError, match="rate_bps"):
        calculate_fee(500_000, rate_bps)


# --- rate parsing (operator input hardening) --------------------------------


@pytest.mark.parametrize(
    ("value", "expected_bps"),
    [
        ("0", 0),
        ("10", 1000),
        ("7.5", 750),
        ("2.25", 225),
        ("100", 10_000),
        ("0.01", 1),
        ("0.1", 10),
        ("99.99", 9_999),
        ("007", 700),  # leading zeros are harmless decimal notation
    ],
)
def test_parse_rate_percent_accepts_valid_rates(value, expected_bps):
    assert parse_rate_percent(value) == expected_bps


@pytest.mark.parametrize(
    "value",
    [
        "10.555",  # more than two decimals
        "-5",  # signs rejected
        "+5",
        "101",  # above 100
        "100.01",
        "999",
        "1e2",  # scientific notation rejected
        "1E2",
        "10,5",  # comma separators rejected
        "1,000",
        " 10",  # whitespace rejected
        "10 ",
        "10\n",
        "",
        ".5",  # must have a whole part
        "10.",  # dot without decimals
        "abc",
        "NaN",
        "nan",
        "Infinity",
        "inf",
        "0x10",
        "١٠",  # noqa: RUF001 — non-ASCII (Arabic-Indic) digits rejected
        "10; rm -rf /",  # injection-shaped input is just an invalid rate
        "10' OR '1'='1",
        "$(reboot)",
        "`id`",
    ],
)
def test_parse_rate_percent_rejects_malformed_input(value):
    with pytest.raises(ValueError):
        parse_rate_percent(value)


@pytest.mark.parametrize(
    ("rate_bps", "expected"),
    [
        (0, "0%"),
        (1000, "10%"),
        (750, "7.5%"),
        (225, "2.25%"),
        (10_000, "100%"),
        (1, "0.01%"),
        (10, "0.1%"),
        (505, "5.05%"),
    ],
)
def test_format_rate_percent(rate_bps, expected):
    assert format_rate_percent(rate_bps) == expected


def test_parse_and_format_round_trip():
    for text in ("0", "10", "7.5", "2.25", "100", "0.01"):
        assert format_rate_percent(parse_rate_percent(text)) == f"{text}%"


# --- policy selection determinism -------------------------------------------


def _add_policy(
    session_factory, *, rate_bps, effective_at, cancelled=False
) -> int:
    with session_factory() as db:
        policy = FeePolicy(
            rate_bps=rate_bps,
            effective_at=effective_at,
            created_by="test",
            note="test policy",
            cancelled_at=NOW if cancelled else None,
            cancelled_by="test" if cancelled else None,
            cancellation_note="cancelled in test" if cancelled else None,
        )
        db.add(policy)
        db.commit()
        return policy.id


def test_no_policy_means_no_fee(session_factory):
    with session_factory() as db:
        assert select_effective_policy(db, now=NOW) is None
        assert next_scheduled_policy(db, now=NOW) is None


def test_selection_prefers_highest_effective_at(session_factory):
    _add_policy(session_factory, rate_bps=500, effective_at=PAST)
    newer = _add_policy(session_factory, rate_bps=1000, effective_at=PAST + timedelta(days=1))
    with session_factory() as db:
        selected = select_effective_policy(db, now=NOW)
    assert selected is not None
    assert selected.id == newer
    assert selected.rate_bps == 1000


def test_selection_tie_breaks_on_highest_id(session_factory):
    _add_policy(session_factory, rate_bps=500, effective_at=PAST)
    later_row = _add_policy(session_factory, rate_bps=1000, effective_at=PAST)
    with session_factory() as db:
        selected = select_effective_policy(db, now=NOW)
    assert selected is not None
    assert selected.id == later_row


def test_scheduled_policy_is_not_selected_early(session_factory):
    active = _add_policy(session_factory, rate_bps=1000, effective_at=PAST)
    scheduled = _add_policy(session_factory, rate_bps=250, effective_at=FUTURE)
    with session_factory() as db:
        selected = select_effective_policy(db, now=NOW)
        upcoming = next_scheduled_policy(db, now=NOW)
    assert selected is not None and selected.id == active
    assert upcoming is not None
    assert upcoming.id == scheduled


def test_scheduled_policy_activates_at_exact_effective_at(session_factory):
    _add_policy(session_factory, rate_bps=1000, effective_at=PAST)
    scheduled = _add_policy(session_factory, rate_bps=250, effective_at=FUTURE)
    with session_factory() as db:
        # One microsecond before: still the old policy.
        before = select_effective_policy(db, now=FUTURE - timedelta(microseconds=1))
        # At the exact effective_at instant: the new policy, no restart needed.
        at = select_effective_policy(db, now=FUTURE)
    assert before is not None and before.rate_bps == 1000
    assert at is not None
    assert at.id == scheduled
    assert at.rate_bps == 250


def test_cancelled_policy_is_never_selected(session_factory):
    _add_policy(session_factory, rate_bps=9000, effective_at=PAST, cancelled=True)
    with session_factory() as db:
        assert select_effective_policy(db, now=NOW) is None
    _add_policy(session_factory, rate_bps=9000, effective_at=FUTURE, cancelled=True)
    with session_factory() as db:
        assert next_scheduled_policy(db, now=NOW) is None


def test_cancelled_scheduled_policy_falls_back_to_active(session_factory):
    active = _add_policy(session_factory, rate_bps=1000, effective_at=PAST)
    _add_policy(session_factory, rate_bps=250, effective_at=FUTURE, cancelled=True)
    with session_factory() as db:
        # Even after the cancelled policy's effective_at passes, it never wins.
        selected = select_effective_policy(db, now=FUTURE + timedelta(days=1))
    assert selected is not None and selected.id == active


# --- policy lifecycle (append-only, audited) --------------------------------


def test_create_policy_records_audit_event(session_factory):
    with session_factory() as db:
        policy = create_policy(
            db, rate_bps=1000, effective_at=NOW, actor="ops-test", note="launch fee",
            scheduled=False,
        )
        db.commit()
        policy_id = policy.id
    events = get_events(session_factory)
    assert event_types(events) == ["fee_policy_created"]
    data = events[0].data
    assert data is not None
    assert data["policy_id"] == policy_id
    assert data["rate_bps"] == 1000
    assert data["actor"] == "ops-test"


def test_schedule_policy_records_scheduled_event(session_factory):
    with session_factory() as db:
        create_policy(
            db, rate_bps=250, effective_at=FUTURE, actor="ops-test", note="seasonal",
            scheduled=True,
        )
        db.commit()
    assert event_types(get_events(session_factory)) == ["fee_policy_scheduled"]


def test_create_policy_requires_timezone_and_note(session_factory):
    with session_factory() as db:
        with pytest.raises(ValueError, match="timezone"):
            create_policy(
                db, rate_bps=1000, effective_at=NOW.replace(tzinfo=None),
                actor="t", note="x", scheduled=False,
            )
        with pytest.raises(ValueError, match="note"):
            create_policy(
                db, rate_bps=1000, effective_at=NOW, actor="t", note="   ",
                scheduled=False,
            )
        with pytest.raises(ValueError, match="500"):
            create_policy(
                db, rate_bps=1000, effective_at=NOW, actor="t", note="x" * 501,
                scheduled=False,
            )


def test_cancel_preserves_history_permanently(session_factory):
    policy_id = _add_policy(session_factory, rate_bps=250, effective_at=FUTURE)
    with session_factory() as db:
        cancel_policy(db, policy_id=policy_id, actor="ops-test", note="wrong rate", now=NOW)
        db.commit()
    with session_factory() as db:
        row = db.get(FeePolicy, policy_id)
        # Append-only: the row survives with full cancellation metadata.
        assert row is not None
        assert row.rate_bps == 250
        assert row.cancelled_by == "ops-test"
        assert row.cancellation_note == "wrong rate"
        assert row.cancelled_at is not None
    assert "fee_policy_cancelled" in event_types(get_events(session_factory))


def test_cancel_rejects_missing_and_double_cancel(session_factory):
    with session_factory() as db, pytest.raises(ValueError, match="does not exist"):
        cancel_policy(db, policy_id=424242, actor="t", note="x", now=NOW)
    policy_id = _add_policy(session_factory, rate_bps=250, effective_at=FUTURE)
    with session_factory() as db:
        cancel_policy(db, policy_id=policy_id, actor="t", note="first", now=NOW)
        db.commit()
    with session_factory() as db, pytest.raises(ValueError, match="already cancelled"):
        cancel_policy(db, policy_id=policy_id, actor="t", note="second", now=NOW)
