# Deferred review — unresolved topics

Automated adversarial verification and the full test suite were deferred and
have not yet been completed. This implementation must not be considered
production-ready until those checks are completed.

An in-depth multi-agent adversarial review of Phase 1 was started and then
intentionally stopped before completion. This document records every visible
unresolved review topic so nothing is lost. Each item must be reviewed (and
fixed or explicitly accepted) before production deployment.

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
Payments can be left in `created` (crash before getLink) or `getlink_failed`
states with no automated sweep; recovery currently relies on the bot
re-requesting the same `order_id`. There is no operator runbook or tooling
for enumerating and resolving stale rows. Phase 3+ management commands are
expected to cover this; until then the gap is undocumented.

### 11. Bot notification ambiguity
Phase 2 scope, recorded here for continuity: the bot API defines no response
schema and no idempotency guarantee. AGENTS.md mandates conservative
semantics (HTTP 2xx → `bot_notify_accepted` only, ambiguous timeout →
`manual_review` in safe mode, no automatic retry of ambiguous deliveries).
Nothing in Phase 1 sends bot notifications; any interim manual notification
process must follow the same rules.

### 12. Manual review workflow
Payments reaching `manual_review` are preserved and recoverable (append-only
audit trail, no destructive transitions), but there is no administrator
tooling to inspect or resolve them yet (planned for Phases 3–4). Until that
exists, resolving a `manual_review` payment requires direct, audited
database work by an administrator — there is no documented procedure.

## Deferred checks

The following were intentionally not run for the preservation snapshot on
this branch and must be completed before production:

- full pytest suite (unit + PostgreSQL integration)
- Ruff lint and mypy type checking as release gates
- multi-agent adversarial review (financial correctness, security,
  contract compliance, test coverage)
- dependency vulnerability scan and secret scan (CI, Phase 5)
- ShellCheck, Docker build, end-to-end installer test (later phases)
