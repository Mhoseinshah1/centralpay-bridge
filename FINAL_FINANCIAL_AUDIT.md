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
