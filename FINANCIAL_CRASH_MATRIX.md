# Financial crash matrix (final audit)

Every crash point in the payment lifecycle: what is persisted, who
recovers it, whether recovery is automatic, and the duplicate/lost-credit
risk. "Tested" names the proving test where one exists; shell-level
points are content-audited (real-host execution is blocker B1).

Key design facts that bound every row below:
1. Money can only move through a payment link the bridge **durably
   committed and returned** — an invoice whose URL was never returned to
   anyone is unpayable.
2. The verified fact, queue state, and audit events commit in **one
   transaction**.
3. The worker sends HTTP with **no transaction open**, and results are
   recorded only against the sender's own claim.

| # | Crash point | Persisted state | Recovery owner | Auto? | Duplicate risk | Lost-credit risk | Operator action | Tested |
|---|---|---|---|---|---|---|---|---|
| 1 | Before payment insert | nothing | bot (re-request) | yes | none | none | none | trivial |
| 2 | After insert, before creation commit | nothing (single txn) | bot | yes | none | none | none | `_ensure` semantics |
| 3 | Before getLink call | row `created`, no token | bot | yes | none | none | none | `test_crash_before_getlink_is_recoverable` |
| 4 | After getLink success, before commit | nothing new (rollback); orphan invoice at CentralPay, URL never disclosed → unpayable | bot | yes | none | none | none | `test_crash_after_getlink_before_commit_is_atomic_and_recoverable` |
| 5 | Before callback row lock | payment unchanged | payer/CentralPay (callback retry) | yes | none | none | none | replay suite |
| 6 | After verify success, before DB commit | nothing (atomic rollback: no verified fact, no queue, no events) | callback retry (re-verify allowed — never recorded) | yes | none — verify re-runs, credit happens once | none if verify is idempotent (B2); else payment stays link_created → visible | investigate via `centralpay payment` if payer reports paid | `test_crash_during_verification_commit_is_recoverable` |
| 7 | After verified commit, before worker claim | verified + `bot_notify_pending` durable | worker (any instance, any restart) | yes | none | none | none | `test_scheduled_retry_survives_restart`, drain test |
| 8 | After claim commit, before HTTP send | claimed row | stale-claim recovery | yes | none | none | safe mode: resolve the manual review | stale-claim suite |
| 9 | During HTTP send | claimed row; bot may or may not have processed | stale-claim recovery → **ambiguous**: safe mode → manual review; idempotent → bounded requeue | yes (to a visible state) | only in idempotent mode (declared acceptable) | none — payment never lost | safe mode: confirm with bot operator, resolve | stale-claim + ownership suites |
| 10 | After HTTP send, before result commit | same as 9 | same as 9; a late straggler result is discarded + audited if the claim was re-owned | yes | same as 9 | none | same as 9 | `test_straggler_result_never_recorded_against_reowned_claim` |
| 11 | After retry-schedule commit | pending + next_retry_at durable | worker after restart | yes | none | none | none | `test_scheduled_retry_survives_restart` |
| 12 | During stale-claim recovery | per-payment: either recovered (committed) or still stale (next pass) | next worker pass | yes | none | none | none | bounded-batch test |
| 13 | During manual-review resolution | either acknowledged/resolved (committed) or unchanged; review fields only | operator re-runs command | manual | none | none | re-run `centralpay review` | review CLI suite + race test |
| 14 | During backup | `.partial` file only; never `.ok`, never a manifest | next scheduled backup | yes | n/a | n/a (previous backups intact) | none | backup.sh atomicity (content-audited) |
| 15 | During restore | database possibly partial; **services deliberately stopped** | operator | manual by design | none (writers stopped) | none (pre-restore backup exists and is preserved) | follow printed recovery steps: restore the pre-restore backup | restore flow content-audited; db-check gate tested |
| 16 | After restore, before readiness | restored DB; services not yet started (health-gated `compose up --wait`) | compose health gating / operator | yes | none | none | `centralpay status`, `db-check` | deployment tests |

## Summary

- **No crash point can double-credit** in safe mode. The only
  duplicate-delivery windows (rows 9–10) exist solely in idempotent mode,
  which is opt-in and requires the bot developer's written confirmation
  that duplicates are safe.
- **No crash point silently loses a credit.** The worst outcomes are
  visible states: `link_created` awaiting a callback retry (row 6 under a
  non-idempotent gateway — a B2 staging question), or `manual_review`.
- **No crash point converts an unknown outcome into success** (rows
  8–10: ownership-checked result recording plus ambiguous-claim
  handling).
