# Final financial correctness audit

Branch: `audit/final-financial-correctness`. Scope: the complete merged
payment bridge, re-verified independently of prior audit conclusions.
Companion documents: `FINANCIAL_INVARIANTS.md`,
`CENTRALPAY_CONTRACT_ASSUMPTIONS.md`, `FINANCIAL_CRASH_MATRIX.md`,
`FINANCIAL_TEST_MATRIX.md`.

## Verdict

```
CODE_FINANCIALLY_SOUND

PRODUCTION_VALIDATION_STATUS: INCOMPLETE
```

**Meaning of the verdict:** against the source code and the automated
test evidence (343 tests, including deterministic PostgreSQL concurrency,
fault-injection, crash-recovery, and backup/restore proofs), no known
code path can double-credit, silently lose a credit, convert an unknown
delivery outcome into success, bypass manual review, or erase financial
audit history. Every financial ambiguity terminates in a visible state.

**Meaning of INCOMPLETE:** the code has never been exercised against the
real CentralPay gateway, a real bot, real Telegram, or a real Ubuntu
host. Soundness against the *real world* is exactly what the remaining
blockers cover. **No release may be tagged and no real payment may be
processed until they are closed.**

## Payment state machine (audited)

Every `Payment.status` writer in the repository (exhaustive grep, 8
sites) conforms to this table. No other code assigns the field.

| From | To | Writer | Conditions | Audit event | Txn |
|---|---|---|---|---|---|
| — | created | payments.py (insert) | new order id | payment_created | own commit |
| created / getlink_failed | link_created | payments.py | getLink success, fresh token, under row lock | payment_link_created | single commit (token+URL atomic) |
| created / getlink_failed | getlink_failed | payments.py | getLink failure/timeout | centralpay_getlink_failed | single commit |
| link_created | bot_notify_pending | verification.py → queue_notification | strict verify success + amount/user/reference validation + collision check, under row lock | gateway_payment_verified + bot_notification_queued | ONE commit with verified fact |
| link_created | manual_review | verification.py | verify field/mismatch/collision failure | mismatch event + manual_review_required | single commit |
| bot_notify_pending | bot_notify_accepted | notification.py | classified 2xx, row lock, claim ownership (worker id + attempt) | bot_notification_accepted | result txn |
| bot_notify_pending | bot_notify_pending (retry) | notification.py | retryable failure below limit / idempotent stale below limit | bot_notification_retry_scheduled / notification_recovered_after_restart | result txn |
| bot_notify_pending | manual_review | notification.py | ambiguous (safe), permanent 4xx, retry limit (both paths) | bot_timeout_ambiguous / bot_notification_failed + manual_review_required | result txn |
| manual_review | bot_notify_pending | ops.py resend | idempotent mode + gateway-verified + `--confirm-idempotent-bot --yes` (host CLI, root) | manual_review_resend_requested | single commit |

**Forbidden transitions verified absent:** verified→unverified (no
writer), any→created (no writer), manual_review reset by
callback/create/worker/admin-bot (all short-circuit; race-tested),
accepted→pending except nothing (accepted is terminal — no writer
selects accepted rows), amount/reference_id/bot_order_id reassignment
(single assignment sites only; reference assigned once post-collision
check), fabricated `gateway_verified_at` (single writer, post-validation,
plus DB CHECK `ck_payments_delivery_requires_verification`).

## Findings of this final audit

**No confirmed financial-correctness bug was found in the merged code.**
The prior audits' fixes were independently re-verified as present and
correct. Hardening added by this audit (defense-in-depth, not bug
fixes):

1. Database CHECK constraints (migration **0005**): positive amounts,
   non-negative attempt counters, and delivery-states-require-
   verification — the F1/F9 invariant now holds even against buggy
   future application code or manual SQL.
2. `claim_next_due` now also requires `gateway_verified_at IS NOT NULL`
   in its WHERE clause: an anomalous row could never be delivered.
3. CI guard test: the PostgreSQL financial-integration suites can never
   silently skip in CI.
4. Three new cross-component race proofs (create-vs-callback,
   callback-vs-worker, review-CLI-vs-callback), completing section 7's
   matrix alongside the eight already covered.

## Transaction models (documented, verified)

- **Creation (model A):** row committed first (durable audit), then row
  lock held across getLink (bounded by the 15s gateway timeout);
  token+URL commit atomically; crash windows recoverable (crash matrix
  rows 1–4).
- **Verification:** one transaction under the row lock: token check →
  duplicate short-circuit → verify (inside lock — accepted and
  documented: bounded by the 15s timeout; pool sizing documented;
  concurrent callbacks for *other* payments unaffected; no deadlock —
  single-row lock ordering) → validation → verified fact + queue +
  events → single commit.
- **Delivery:** claim commit → HTTP with no transaction → result in a
  new transaction gated on claim ownership.
- **Restore:** lock → validate → pre-restore backup → stop writers →
  exit-on-error restore → migrations → db-check gate → health-gated
  start.

## Remaining blockers (section 16 classification)

| Blocker | Class | Evidence needed |
|---|---|---|
| B1 real-host installation (Ubuntu 22.04 + 24.04) | real-host | REAL_HOST_VALIDATION.md filled with run records |
| B2 real CentralPay getLink/verify responses + verify idempotency + TOMAN unit | real-CentralPay | STAGING_VALIDATION.md filled |
| B3 live admin Telegram run | live-Telegram | ADMIN_BOT_VALIDATION.md filled |
| B4 external adversarial review | process | reviewer sign-off recorded |
| B5 release workflow green run (Trivy/SBOM/arm64/artifacts) | process | tag-triggered run on GitHub |
| Real bot credit behavior (2xx semantics, duplicate handling) | real-bot | bot developer confirmation + staging delivery |

**No code blocker remains.** Everything else in
`RELEASE_RISK_REGISTER.md` is classified accepted-risk or post-release
backlog (off-site DR, digest pinning, proxy rate limiting, signed
releases, load testing, payer failure pages, pre-send markers).

## Explicit final statements

- The PR may be merged after human review; merging does not change any
  production system.
- **A release tag may NOT be created** (B1/B2/B5 open).
- **Real payments may NOT be enabled** (B1–B5 + real-bot confirmation
  open; `PRODUCTION_CHECKLIST_FA.md` gates go-live).

## Addendum — dynamic percentage fee (`feat/dynamic-payment-fee`, 2026-07-18)

The dynamic-fee feature changes the money model after this audit's
snapshot. Summary of the financial deltas (full statements: invariants
F21–F24 in `FINANCIAL_INVARIANTS.md`):

- `payments.amount` remains the ORIGINAL bot invoice. New immutable
  snapshot columns `fee_policy_id` / `fee_rate_bps` / `fee_amount` /
  `payable_amount` are written once, at creation, in the same
  transaction as the insert (`payment_fee_snapshotted` event).
  `fee_amount = (amount * fee_rate_bps + 5000) // 10000` — integer
  round-half-up, never floats.
- getLink now charges `payable_amount`; verification compares the
  gateway-reported amount against `payable_amount` (mismatch event
  renamed `verify_payable_amount_mismatch`). The bot notification payload
  is unchanged (exact JSON object and field set, parsed from the raw
  request body) and still carries no amounts.
- `MAX_PAYMENT_AMOUNT_TOMAN` now explicitly bounds the FINAL payable
  amount (rejection code `payable_amount_out_of_range`, before any row or
  gateway call); the minimum still bounds the original amount.
- Fee policies are append-only rows in `fee_policies` (never env vars),
  selected deterministically, mutable only via the root host CLI, fully
  audited, and included in backups. Migration **0006** backfills existing
  payments as fee-less (`payable_amount = amount`) and adds CHECK
  constraints binding `payable = amount + fee` at the storage layer.

The verdict statement above is extended by 120 new deterministic tests
(463 total) covering fee arithmetic, snapshot immutability under races,
CLI/admin-bot authorization, migration backfill, db-check corruption
reporting, and backup/restore of policy history. The blockers table is
UNCHANGED and gains fee-specific staging evidence requirements under B2:
the real gateway must be observed charging the payable amount and
reporting it back in verify (including the TOMAN-unit and
verify-idempotency checks already listed), and the real messages
("payment is paid", "payment type invalid") must be captured with a
fee-bearing payment. **PRODUCTION_VALIDATION_STATUS remains INCOMPLETE;
no tag, no real payments, and the live customer bot must stay disabled
until B1–B5 are closed.** The payer must be shown the final payable
amount before paying — fee disclosure in the bot's purchase flow is an
operator go-live requirement in `PRODUCTION_CHECKLIST_FA.md`.

## 0.6.0-rc1 release re-audit (`release/0.6.0-rc1`, 2026-07-18)

One final independent pass over the complete merged codebase (fee
feature included), re-verifying each release-audit point against the
source and the test evidence:

1. `payments.amount` is assigned exactly once, at row insert, and no
   code path reassigns it (exhaustive grep; F2/F21/F22). ✓
2. getLink is called with `amount=payment.payable_amount`
   (`app/services/payments.py`). ✓
3. verify compares `result.amount != payment.payable_amount` — never
   the original amount (`app/services/verification.py`). ✓
4. Fee arithmetic is exactly `(amount * fee_rate_bps + 5000) // 10000`
   — integer round half up, no floats anywhere in money paths. ✓
5–8. Snapshot immutability: written once inside the creation
   transaction; duplicates, policy changes, and getlink-failed retries
   preserve it (unit + PG race proofs). ✓
9. Migration 0006 backfills pre-existing payments as
   `fee_rate_bps=0, fee_amount=0, payable_amount=amount` (proven with a
   populated 0005 database). ✓
10–11. The bot notification is exactly the JSON object
   `{"order_id", "actions": "custom_payment_verify"}` — exact field
   set, parsed from the raw request body — with the `Token` header and
   no amount or fee field (regression test; byte-level serialization
   of the JSON encoder is deliberately not pinned). ✓
12. A payable mismatch moves to `manual_review`, is never verified, and
   the worker can never deliver it (claim requires
   `bot_notify_pending` AND `gateway_verified_at IS NOT NULL`; new
   regression `test_payable_mismatch_never_notifies_bot`). ✓
13. Concurrent fee change vs creation cannot produce a mixed snapshot
   (single policy read; PG barrier race proof). ✓
14. Backup/restore preserves active, scheduled, and cancelled policies
   and payment snapshots field-for-field, ids included. ✓
15. db-check reports fee corruption read-only; it never rewrites
   financial history. ✓
16. `scripts/backup.sh` is executable in Git (mode 100755) and
   installed root:root 0750 by the installer. ✓
17. Secret/callback-parameter redaction re-verified (logging suite,
   audit-event secret checks, Caddy sig+ct redaction). ✓
18. No previously established invariant (F1–F24) was weakened; no test
   was removed, skipped, or xfailed — the suite only grew (343 → 465).

New regression tests added by this re-audit:
`test_payable_mismatch_never_notifies_bot`,
`test_delivered_fee_payment_retains_exact_snapshot`.

### Verdict (0.6.0-rc1)

```
CODE_FINANCIALLY_SOUND

PRODUCTION_VALIDATION_STATUS: INCOMPLETE
```

No real CentralPay payment (with or without a fee), no real
payable-amount report from the gateway, no mock-bot delivery outside
the test stubs, and no real bot integration have been observed — none
of those external tests are claimed to have occurred. The blockers
table above stands. The 0.6.0-rc1 PR may be merged after human review;
**the `v0.6.0-rc1` tag may be created only after merge, as an explicit
human decision** (the tag-triggered workflow produces a draft-only
release); real payments and the live customer bot remain prohibited
until B1–B5 close.
