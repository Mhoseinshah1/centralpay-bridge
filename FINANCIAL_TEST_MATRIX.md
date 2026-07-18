# Financial test matrix (final audit)

Maps every financial invariant (`FINANCIAL_INVARIANTS.md`) to its proving
tests, and records the test-quality review of section 15.

Suite size at this audit: **343 tests** (unit on SQLite + integration on
real PostgreSQL 16). CI runs both matrices (ubuntu-22.04/24.04) with
`TEST_DATABASE_URL` always set; `tests/integration/test_ci_guard.py`
fails the build if CI ever lacks the database (no silent skips).

| Inv. | Proving tests (primary) | DB engine |
|---|---|---|
| F1 | test_centralpay_client.py (explicit-success/rejection/field-error suites); test_callback.py::test_verify_success; test_fault_injection.py | SQLite + PG |
| F2 | test_verify_amount_mismatch_moves_to_manual_review; test_creation_hardening.py amount matrix; ck_ constraint (0005, PG) | both |
| F3 | test_verify_user_id_mismatch_moves_to_manual_review | SQLite |
| F4 | test_verify_missing_reference_id…; test_reference_id_collision_goes_to_manual_review; uq constraint (PG) | both |
| F5 | test_concurrent_creates_return_one_link; test_many_identical_concurrent_creates; test_concurrent_conflicting_amounts_single_row | PG |
| F6 | test_duplicate_order_with_different_amount_rejected; conflicting-amount race | both |
| F7 | test_concurrent_callbacks_verify_exactly_once; test_duplicate_callback_does_not_verify_again; test_concurrent_replays_after_verification_never_reverify; test_race_duplicate_callback_against_worker | PG |
| F8 | test_duplicate_callback_cannot_enqueue_duplicate_notification; test_four_workers_drain_queue_exactly_once | both/PG |
| F9 | test_2xx_becomes_bot_notify_accepted (3 variants) | SQLite |
| F10 | test_ambiguous_read_timeout_safe_mode_manual_review; test_review_resend_refused_in_safe_mode; stale-claim safe-mode tests | SQLite |
| F11 | test_callback_after_manual_review_does_not_verify_again; test_create_for_verified/unverified_manual_review…; test_manual_review_survives_restart_and_worker_passes; test_race_review_acknowledge_against_callback; test_admin_auth.py | both/PG |
| F12 | test_crash_during_verification_commit_is_recoverable; test_full_state_round_trip_and_sequence_safety | both/PG |
| F13 | test_straggler_result_never_recorded_against_reowned_claim; test_restart_recovers_unclaimed_pending_payment | SQLite |
| F14 | test_retry_limit_reached_becomes_manual_review; test_stale_claim_at_retry_limit_goes_to_manual_review_in_idempotent_mode | SQLite |
| F15 | review-CLI financial-fields-preserved assertions; FK RESTRICT (schema); no-delete grep (audited) | both |
| F16 | test_full_state_round_trip_and_sequence_safety; test_pg_dump_restore_round_trip; corrupted/zero-byte/plain-SQL rejection | PG |
| F17 | test_restore_preflight_and_failure_safety (shell content); test_db_check_detects_and_repairs_sequence_drift | static + PG |
| F18 | test_logging_redaction.py (sentinel + real sig/ct); test_callback_responses_never_echo…; test_get_link/verify_never_exposes_gateway_text; deployment sig+ct redaction test; gitleaks (CI) | both |
| F19 | the whole PG race suite (11 race tests incl. the three final-audit races) | PG |
| F20 | reason-code assertions across every failure-path test; manual_review event assertions; alert mapping tests | both |

## Test-quality review (section 15)

- **SQLite vs PostgreSQL:** all locking/concurrency/constraint-critical
  behavior is proven on real PostgreSQL (races, SKIP LOCKED, unique
  constraints, CHECK constraints via migration tests, backup/restore).
  SQLite covers logic-level paths only. The 0005 CHECK constraints are
  also declared on the models, so SQLite unit databases enforce them too.
- **Timing:** no arbitrary sleeps anywhere in the suite; the only
  `verify_delay_seconds` usage widens a race window behind a barrier and
  the assertions are order-independent. The one historical wall-clock
  dependency (FIXED_NOW time bomb) was found and fixed in the worker
  audit; both clock sides are injected now.
- **Silent skips:** PG suites skip only without TEST_DATABASE_URL;
  CI cannot silently skip them (test_ci_guard.py fails the build).
- **State assertions:** financial tests assert DB state (status,
  amounts, reference ids, attempt counters, event counts), not just HTTP
  codes; negative assertions are used throughout (no verify calls, no
  bot requests, no new events, secrets absent).
- **Shared state:** stubs are per-fixture; module-level limiter/tracker
  state has explicit reset() and per-test instances where raced.
- **Weakening check:** no test has been removed, skipped, or xfailed in
  any audit; counts only grew (215 → 343 across Phases 4→final).
- **Known gaps (external):** real-gateway behavior, real bot credit
  semantics, real Telegram, real-host installer/systemd/TLS — these are
  validation blockers (B1–B3), not test-suite defects.
