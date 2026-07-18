# Deferred review — unresolved topics

Automated adversarial verification has not yet been completed. This
implementation must not be considered production-ready until that review
(and the remaining checks listed at the end of this document) are completed.

An in-depth multi-agent adversarial review of Phase 1 was started and then
intentionally stopped before completion; it has not been re-run for Phase 1
or Phase 2. This document records every visible unresolved review topic so
nothing is lost. Each item must be reviewed (and fixed or explicitly
accepted) before production deployment.

Status update (Phase 2): the focused Phase 2 tests, the full quick unit
suite, PostgreSQL integration tests, Ruff, mypy, and Alembic migration
validation were run and pass. The multi-agent adversarial review remains
outstanding.

## Unresolved review topics

### 1. Callback replay protection
The callback signature (`app/security.py`) is a static HMAC over
`orderId=<gateway_order_id>`. A captured callback URL stays valid forever and
can be replayed. Replays are currently absorbed by row locking plus the
already-verified/duplicate handling in `app/services/verification.py`, but
replay of a *not-yet-verified* order triggers a fresh outbound verify call.
Review whether a timestamp/nonce should be added to the signed message and
what expiry policy CentralPay's redirect flow tolerates.

### 2. Gateway-controlled error text handling
`_safe_reason()` in `app/centralpay.py` copies gateway-supplied message text
(truncated to 200 chars) into exception messages, audit event data, and API
error responses. This is attacker-influenceable-by-gateway content flowing
into logs and client-visible responses. Review sanitization/encoding and
whether gateway text should be stored but never echoed to API callers.

### 3. Untrusted X-Request-ID handling
`app/middleware.py` accepts `X-Request-ID` from any client (sanitized to
`[A-Za-z0-9._-]{1,64}`). Arbitrary clients can therefore inject chosen
request IDs into logs and the audit trail (`payment_events.request_id`),
enabling confusion or collision with proxy-issued IDs. Review trusting the
header only from the reverse proxy (e.g. strip/replace at Caddy, or a
trusted-proxy allowlist).

### 4. Verify success detection
`CentralPayClient.verify()` uses heuristic failure markers (`success=false`,
`status` in an error set, `error` key, missing `data` object) because the
real CentralPay response schema is not fully documented. A response shape not
matching these heuristics could be misclassified. The exact success/failure
contract must be confirmed against real CentralPay documentation or sandbox
traffic before production.

### 5. Crash window after gateway verification
In `process_callback()`, if the process crashes after CentralPay verify
succeeds but before the transaction committing `gateway_verified` completes,
the gateway considers the payment verified while the bridge still shows
`link_created`. A later callback retry re-runs verify (AGENTS.md forbids
re-verifying only after a *successfully recorded* verification — confirm
CentralPay tolerates verify-after-verify for the same order). Document the
recovery procedure and confirm idempotency of `verify.php`.

### 6. Malformed integer conversion
`_to_int()` in `app/centralpay.py` coerces digit strings but has edge cases
to review: leading zeros, `"-0"`, values exceeding BIGINT range (Python ints
are unbounded; the DB column is BIGINT), float-typed JSON amounts (currently
rejected → mismatch → manual_review), and whitespace variants. Confirm the
conservative fallbacks (None → manual_review) are the desired behavior for
every malformed shape.

### 7. Configurable minimum and maximum payment amount
`POST /api/custom-payment` accepts any positive integer amount
(`Field(gt=0)` in `app/api/payments.py`). There is no configurable
minimum/maximum bound, so absurd amounts (1 TOMAN or 10^18 TOMAN) reach
CentralPay. Add `PAYMENT_MIN_AMOUNT` / `PAYMENT_MAX_AMOUNT` settings and
validation, or explicitly accept gateway-side enforcement.

### 8. Race conditions
Creation and callback processing hold `SELECT ... FOR UPDATE` row locks
across the external gateway calls (`app/services/payments.py`,
`app/services/verification.py`). Basic concurrency behavior is covered by
the postgres-marked tests, but the full adversarial concurrency review
(crash/timeout while holding locks, lock wait pile-ups under callback
floods, gateway latency at the lock boundary, `_ensure_payment_row` retry
path) was not completed.

### 9. Duplicate callbacks
Duplicate callbacks after successful verification return `already_verified`
without re-verifying, and duplicates for `manual_review` payments return
`under_review` without contacting the gateway. The stopped review had not
finished probing duplicate callbacks arriving in *other* states (e.g. during
`getlink_failed` after gateway_order_id regeneration — the old orderId no
longer resolves to a payment and returns 404).

### 10. Recovery after process crash
Partially addressed in Phase 2: the notification worker recovers
`bot_notify_pending` payments after restart, and stale claims (a worker
crashed mid-attempt) are released on every pass with a
`notification_recovered_after_restart` audit event. Still open: payments
left in `created` (crash before getLink) or `getlink_failed` have no
automated sweep — recovery relies on the bot re-requesting the same
`order_id` — and there is still no operator runbook for those rows
(Phase 3+ management command scope).

### 11. Bot notification ambiguity
Implemented in Phase 2 per the contract: HTTP 2xx → `bot_notify_accepted`
only (never treated as balance credit); ambiguous read/write timeouts →
`manual_review` with reason `bot_timeout_ambiguous` in safe mode; retry of
ambiguous deliveries only in the explicitly configured idempotent mode.
Still open for review: confirmation from the bot developer whether
duplicate `order_id` delivery is idempotent (prerequisite for ever enabling
idempotent mode in production), and the classification boundaries in
`app/bot.py` (which httpx failures count as "clearly before transmission").

### 12. Manual review workflow
Partially addressed in Phase 2: `python -m app.cli manual-review` /
`payment ORDER_ID` provide read-only inspection with reason codes, attempt
counts, and full audit history. Still open: there is no resolution tooling —
resolving a `manual_review` payment still requires direct, audited database
work by an administrator, and no manual retry command exists yet (per
AGENTS.md it must not be added until retry safety and authorization are
separately reviewed). Administrator alerts arrive with the Phase 4 admin
bot.

## New unresolved topics from Phase 2

### 13. Stale-claim conservatism in safe mode
A claim whose worker died is treated as an ambiguous attempt and sent to
manual review in safe mode, even when the crash may have happened *before*
the HTTP request was transmitted (the pre-send window is milliseconds, but
not zero). This is deliberately conservative — availability sacrificed for
financial correctness — but review whether a durable "request about to be
sent" marker could narrow the ambiguity window.

### 14. Retry-After handling is integer-seconds only
HTTP-date `Retry-After` values on 429 responses are ignored (backoff
schedule applies instead). Confirm this is acceptable for the bot API.

### 15. Worker scaling and batch behavior
One worker processes up to 20 payments per pass sequentially. Multiple
workers are safe (SKIP LOCKED), but throughput under a large verified
backlog and lock-wait behavior under callback floods have not been load
tested (load testing was explicitly out of scope).

### 16. Callback pages for non-verified outcomes
Only verified payments get the payer-facing HTML page. Signature failures,
unknown payments, and gateway-declined verifications still return JSON
errors; a payer-friendly failure page is deferred.

## Deferred checks

The following must still be completed before production:

- multi-agent adversarial review (financial correctness, security,
  contract compliance, test coverage) — started for Phase 1, intentionally
  stopped, never completed; not run for Phase 2
- dependency vulnerability scan and secret scan (CI, Phase 5)
- ShellCheck, Docker build, end-to-end installer test (later phases)
- load testing (explicitly out of scope so far)

Completed for Phase 2 (see the Phase 2 pull request for details): focused
Phase 2 tests, full quick unit suite, PostgreSQL integration tests
(migration on an empty database, stepwise 0001→0002 upgrade, SKIP LOCKED
concurrency), Ruff, mypy, and a local end-to-end smoke test (API + worker +
fake gateway + fake bot).
