# Financial invariants — final audit (audit/final-financial-correctness)

The twenty invariants the payment bridge must uphold, each with its
enforcement points, database constraints, transaction boundaries, proving
tests, and known limitations. Test names refer to files under `tests/`.
Independent of prior audit conclusions: every claim below was re-verified
against the merged source at the time of this audit.

Legend: **[DB]** database constraint · **[TXN]** transaction boundary ·
**[T]** proving tests · **[LIMIT]** known limitation.

---

**F1 — No verified state without a strictly parsed, validated verify response.**
Enforced: `app/centralpay.py` (explicit success allowlist, typed field
parsing), `app/services/verification.py` (`_validate_and_apply_verification`
runs before `gateway_verified_at` is set; mismatches divert to manual
review first). [DB] `ck_payments_delivery_requires_verification`
(migration 0005): pending/accepted rows must carry `gateway_verified_at`.
[TXN] verified fact + queue state + audit events commit atomically under
the callback row lock. [T] `test_callback.py` (success/mismatch suites),
`test_centralpay_client.py` (parsing), `test_fault_injection.py`.
[LIMIT] the real CentralPay schema is unobserved (blocker B2).

**F2 — Verified amount equals the original TOMAN amount exactly.**
Enforced: `verification.py` (`result.amount != payment.amount` → manual
review, never credit); `amount` is assigned exactly once, at row creation
(`payments.py`), and no code path reassigns it (verified by exhaustive
grep). [DB] `ck_payments_amount_positive`. [T]
`test_verify_amount_mismatch_moves_to_manual_review`,
`test_creation_hardening.py` strict-amount matrix.

**F3 — Verified userId matches the configured user.** Enforced:
`verification.py` (`result.user_id != payment.gateway_user_id` → manual
review). [T] `test_verify_user_id_mismatch_moves_to_manual_review`.

**F4 — Reference ID non-empty and unique.** Enforced: `centralpay.py`
(`gateway_invalid_reference_id` field error), `verification.py` (missing
→ manual review; collision check before assignment →
`reference_id_collision` manual review, never overwrite). [DB]
`uq_payments_reference_id` (0004; PG allows multiple NULLs). [T]
`test_verify_missing_reference_id_moves_to_manual_review`,
`test_reference_id_collision_goes_to_manual_review`.

**F5 — One bot order_id, one payment row.** Enforced: `payments.py`
`_ensure_payment_row` (IntegrityError → locked re-select). [DB] unique
index `ix_payments_bot_order_id` (0001). [T]
`test_concurrent_creates_return_one_link`,
`test_many_identical_concurrent_creates` (10-way),
`test_concurrent_conflicting_amounts_single_row`.

**F6 — A duplicate order with a different amount never mutates the
original.** Enforced: `payments.py` (mismatch → audited 409 before any
other action, stored row untouched). [T]
`test_duplicate_order_with_different_amount_rejected`, conflicting-amount
race test.

**F7 — Verify called at most once per successfully verified payment.**
Enforced: `verification.py` — the already-verified short-circuit runs
under the row lock before any gateway call; manual-review payments are
never re-verified. [TXN] the entire decision runs inside one
`FOR UPDATE` transaction. [T]
`test_concurrent_callbacks_verify_exactly_once`,
`test_duplicate_callback_does_not_verify_again`,
`test_concurrent_replays_after_verification_never_reverify`,
`test_race_duplicate_callback_against_worker`. [LIMIT] a crash before
the verified commit legitimately re-verifies on retry (allowed:
verification was never recorded; CentralPay verify-idempotency
assumption is B2).

**F8 — At most one logical bot-notification workflow per payment.**
Enforced: `queue_notification` runs only inside the verification
transaction (once — guarded by F7) and via the gated `review resend`;
`claim_next_due` requires status=pending AND `gateway_verified_at`
non-null (final-audit guard) AND unclaimed, under `FOR UPDATE SKIP
LOCKED`. [DB] 0005 CHECK; claim columns. [T]
`test_duplicate_callback_cannot_enqueue_duplicate_notification`,
`test_four_workers_drain_queue_exactly_once`.

**F9 — HTTP 2xx means `bot_notify_accepted`, never proven credit.**
Enforced: `notification.py` (comment + code: accepted state only),
`app/bot.py` classification; no "balance_credited" state exists anywhere.
[T] `test_2xx_becomes_bot_notify_accepted`, README contract section.

**F10 — Safe-mode ambiguity is never auto-resent.** Enforced:
`notification.py` (AMBIGUOUS → manual review in safe mode; stale claims
treated identically), `ops.py` resend gate (idempotent mode + verified +
two explicit flags). [T]
`test_ambiguous_read_timeout_safe_mode_manual_review`,
`test_review_resend_refused_in_safe_mode`, stale-claim suite.

**F11 — Manual review cannot be bypassed.** Enforced: creation raises
`order_under_review`/`order_already_verified`; callback returns
`under_review` without touching state; `claim_next_due` selects only
pending; admin bot is read-only; ordinary CLI has no mutation path —
the only exits are the authorized host-CLI resolution (non-financial
fields only) and gated resend. [T]
`test_callback_after_manual_review_does_not_verify_again`,
`test_create_for_*_manual_review_*`, `test_manual_review_survives_restart_and_worker_passes`,
`test_race_review_acknowledge_against_callback`, admin-bot auth suite.

**F12 — A crash cannot erase a committed verified payment.** Enforced:
single-commit verification transaction (all-or-nothing); PostgreSQL
durability; backups. [T] `test_crash_during_verification_commit_is_recoverable`
(nothing persisted before commit → retry verifies; after commit the fact
is durable), `test_full_state_round_trip_and_sequence_safety`.

**F13 — A crash cannot convert an unknown delivery outcome into success.**
Enforced: `notification.py` — accepted state is written only by
`record_attempt_result` on a classified 2xx, under the row lock, and only
when the row still carries the recording worker's claim at the same
attempt; interrupted attempts become stale claims → ambiguous handling.
[T] `test_straggler_result_never_recorded_against_reowned_claim`,
`test_restart_recovers_unclaimed_pending_payment`, stale-claim suite.

**F14 — Retry counts bounded in every path.** Enforced: attempt counter
increments on claim; limit enforced on classified failures AND on
stale-claim recovery in idempotent mode (worker audit fix). [DB]
`ck_payments_attempts_non_negative`. [T]
`test_retry_limit_reached_becomes_manual_review`,
`test_stale_claim_at_retry_limit_goes_to_manual_review_in_idempotent_mode`.

**F15 — Financial records and audit events cannot be deleted/overwritten
by ordinary operations.** Enforced: no `.delete()` exists in `app/`
(verified by grep); `payment_events.payment_id` and
`admin_alerts.payment_id` are `ondelete=RESTRICT`; `record_event` is
append-only; review resolution writes only `review_*` fields. [T]
review-CLI suite (financial fields asserted unchanged), audit-order
tests. [LIMIT] a superuser at the database can do anything — see
compromise model in SECURITY.md.

**F16 — Backup/restore preserves every financially significant field and
event.** Enforced: pg_dump custom format of the whole database; restore
gates (checksum, `--exit-on-error`, db-check). [T]
`test_full_state_round_trip_and_sequence_safety` (field-level equality
across every state + events + alerts), `test_pg_dump_restore_round_trip`.

**F17 — Restore never restarts services against a bad database.**
Enforced: `scripts/centralpay cmd_restore` — services stopped before
restore, `--exit-on-error`, migrations + `db-check --repair-sequences`
gate `compose up`; failures leave services stopped with printed recovery
steps. [T] shell content proofs in `test_deployment.py`;
db-check behavior in `test_backup_restore.py`. [LIMIT] shell-level flow
is content-tested, not executed end-to-end in CI (needs real host, B1).

**F18 — Secrets and tokens never appear in logs, responses, manifests,
alerts, or diagnostics.** Enforced: redaction pipeline
(`logging_setup.py`), token hash-only storage, reason-code-only gateway
handling, Caddy `sig`+`ct` redaction, no-secret manifest fields,
admin-alert payload policy. [T] `test_logging_redaction.py` (sentinel
extraction incl. real sig/ct), `test_callback_hardening.py` response
redaction, deployment policy tests, gitleaks in CI.

**F19 — Concurrency cannot violate F1–F18.** Enforced: `FOR UPDATE` on
create/callback/result-recording, `SKIP LOCKED` claims, DB unique
indexes, CHECK constraints. [T] the full PG race suite: identical-create
(2, 10), conflicting amounts, concurrent callbacks, stale+valid token
race, replay storms, 2- and 4-worker claims/drain, create-vs-callback,
callback-vs-worker, review-vs-callback.

**F20 — Every unresolved ambiguity is visible.** Enforced: every
non-success path lands in `manual_review` with a reason code, or raises
an explicit coded error, or (deployment-level) leaves services stopped
with instructions; critical events map to never-deduplicated admin
alerts. [T] reason-code assertions across all suites;
`_move_to_manual_review` audit events; alert-mapping tests.

---

## Enforcement summary of database constraints

| Constraint | Migration | Invariants |
|---|---|---|
| `ix_payments_bot_order_id` UNIQUE | 0001 | F5 |
| `ix_payments_gateway_order_id` UNIQUE | 0001 | F5/F7 |
| `uq_payments_reference_id` UNIQUE | 0004 | F4 |
| FK `payment_events.payment_id` RESTRICT | 0001 | F15 |
| FK `admin_alerts.payment_id` RESTRICT | 0003 | F15 |
| `ck_payments_amount_positive` CHECK | 0005 | F2 |
| `ck_payments_attempts_non_negative` CHECK | 0005 | F14 |
| `ck_payments_delivery_requires_verification` CHECK | 0005 | F1/F8/F9 |
