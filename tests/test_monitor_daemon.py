"""app.monitor.run_forever: db_integrity retry-on-failure cadence.

A pass that was due for the expensive db_integrity check but raised before
finishing must retry on the very next tick -- not wait a full
db_integrity_every cycles for the next scheduled slot, which would extend
a transient failure into a near-doubled detection gap.
"""

import contextlib
import threading

from app import monitor as monitor_module
from app.services.monitor_checks import (
    DB_INTEGRITY_CHECK_KEY,
    DB_UNAVAILABLE_REASON,
    CheckResult,
)


def test_failed_integrity_cycle_retries_next_tick(settings, monkeypatch, tmp_path):
    monitor_settings = settings.model_copy(
        update={
            "monitor_interval_seconds": 0.001,
            "monitor_db_integrity_interval_seconds": 0.003,  # db_integrity_every = 3
            "monitor_heartbeat_file": str(tmp_path / "heartbeat"),
        }
    )
    calls: list[bool] = []
    stop_event = threading.Event()

    def fake_run_one_pass(session_factory, s, *, include_db_integrity):
        calls.append(include_db_integrity)
        if len(calls) == 1:
            raise RuntimeError("simulated transient failure during db_integrity's slot")
        if len(calls) >= 4:
            stop_event.set()
        # Real run_one_pass returns whether db_integrity actually ran --
        # here, simulate it genuinely completing whenever it was due.
        return include_db_integrity

    monkeypatch.setattr(monitor_module, "run_one_pass", fake_run_one_pass)
    monkeypatch.setattr(monitor_module, "record_worker_heartbeat", lambda *a, **k: None)

    monitor_module.run_forever(
        monitor_settings,
        lambda: contextlib.nullcontext(None),  # type: ignore[arg-type]
        stop_event=stop_event,
        instance_id="test-instance",
    )

    # Tick 1: due (the very first pass always includes it) -- fails.
    # Tick 2: STILL due (retried immediately, not deferred) -- succeeds.
    # Ticks 3-4: cheap-only until the next scheduled slot, 3 cycles later.
    assert calls == [True, True, False, False]


def test_db_integrity_placeholdered_by_outage_does_not_reset_cadence(
    settings, monkeypatch, tmp_path
):
    """A pass that is due for db_integrity, but where PostgreSQL is
    unavailable for that specific check, PERSISTS SUCCESSFULLY (run_all_
    checks degrades gracefully -- see app.services.monitor_checks) even
    though db_integrity itself never actually ran, only got a
    database_unavailable placeholder. run_one_pass must report that as
    NOT completed, so run_forever retries it on the very next tick rather
    than treating the whole pass "not raising" as proof db_integrity ran."""
    monitor_settings = settings.model_copy(
        update={
            "monitor_interval_seconds": 0.001,
            "monitor_db_integrity_interval_seconds": 0.003,  # db_integrity_every = 3
            "monitor_heartbeat_file": str(tmp_path / "heartbeat"),
        }
    )
    calls: list[bool] = []
    stop_event = threading.Event()

    def fake_run_one_pass(session_factory, s, *, include_db_integrity):
        calls.append(include_db_integrity)
        if len(calls) >= 4:
            stop_event.set()
        # Simulates run_all_checks degrading gracefully: the pass itself
        # never raises, but db_integrity was placeholdered by an outage
        # that happened to recover before record_check_result ran -- so it
        # did NOT actually execute.
        return False

    monkeypatch.setattr(monitor_module, "run_one_pass", fake_run_one_pass)
    monkeypatch.setattr(monitor_module, "record_worker_heartbeat", lambda *a, **k: None)

    monitor_module.run_forever(
        monitor_settings,
        lambda: contextlib.nullcontext(None),  # type: ignore[arg-type]
        stop_event=stop_event,
        instance_id="test-instance",
    )

    # Every tick stays due: db_integrity never genuinely completes, so the
    # cadence counter never resets and every pass keeps retrying it.
    assert calls == [True, True, True, True]


def test_run_one_pass_returns_false_when_db_integrity_is_placeholdered(
    session_factory, settings, monkeypatch
):
    """Direct unit test of run_one_pass's own return value (as opposed to
    the run_forever-level tests above, which exercise a fully mocked
    run_one_pass): when run_all_checks placeholders db_integrity as
    database_unavailable, run_one_pass must report False even though every
    record_check_result call for the pass succeeds without raising."""

    def _fake_run_all_checks(db, s, *, now_fn, include_db_integrity):
        results = [CheckResult("public_ready", "ok", "healthy", {})]
        if include_db_integrity:
            results.append(
                CheckResult(DB_INTEGRITY_CHECK_KEY, "critical", DB_UNAVAILABLE_REASON, {})
            )
        return results

    monkeypatch.setattr(monitor_module, "run_all_checks", _fake_run_all_checks)

    assert (
        monitor_module.run_one_pass(session_factory, settings, include_db_integrity=False)
        is False
    )
    assert (
        monitor_module.run_one_pass(session_factory, settings, include_db_integrity=True)
        is False
    )


def test_run_one_pass_returns_true_when_db_integrity_genuinely_ran(
    session_factory, settings, monkeypatch
):
    def _fake_run_all_checks(db, s, *, now_fn, include_db_integrity):
        results = [CheckResult("public_ready", "ok", "healthy", {})]
        if include_db_integrity:
            results.append(CheckResult(DB_INTEGRITY_CHECK_KEY, "ok", "healthy", {}))
        return results

    monkeypatch.setattr(monitor_module, "run_all_checks", _fake_run_all_checks)

    assert (
        monitor_module.run_one_pass(session_factory, settings, include_db_integrity=True) is True
    )
