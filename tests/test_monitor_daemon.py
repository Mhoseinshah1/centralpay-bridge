"""app.monitor.run_forever: db_integrity retry-on-failure cadence.

A pass that was due for the expensive db_integrity check but raised before
finishing must retry on the very next tick -- not wait a full
db_integrity_every cycles for the next scheduled slot, which would extend
a transient failure into a near-doubled detection gap.
"""

import contextlib
import threading

from app import monitor as monitor_module


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

    monkeypatch.setattr(monitor_module, "run_one_pass", fake_run_one_pass)
    monkeypatch.setattr(monitor_module, "record_worker_heartbeat", lambda *a, **k: None)

    monitor_module.run_forever(
        monitor_settings,
        lambda: contextlib.nullcontext(None),
        stop_event=stop_event,
        instance_id="test-instance",
    )

    # Tick 1: due (the very first pass always includes it) -- fails.
    # Tick 2: STILL due (retried immediately, not deferred) -- succeeds.
    # Ticks 3-4: cheap-only until the next scheduled slot, 3 cycles later.
    assert calls == [True, True, False, False]
