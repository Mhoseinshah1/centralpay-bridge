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

Status update (Phase 3): deployment tooling added (Docker Compose, Caddy,
installer, management command, backups, CI workflows). Focused Phase 3
deployment tests, the full quick suite, Ruff, mypy, ShellCheck, and
`docker compose config` validation pass. The Docker image build could not
run in the development sandbox (registry access blocked by network policy)
and is delegated to the CI `docker` job. The multi-agent adversarial
review remains outstanding.

Status update (Phase 4): optional read-only administrator Telegram bot
added (alert outbox, health monitor, daily report, worker DB heartbeats,
hardened admin-bot container). Focused Phase 4 tests, the full quick
suite, PostgreSQL integration (migration 0003), Ruff, mypy, ShellCheck,
and compose validation pass. Live Telegram delivery was not exercised
(mocked in tests, per instruction). The multi-agent adversarial review
remains outstanding.

Status update (Phase 5, 0.5.0-rc1): every topic below has been formally
triaged in `RELEASE_RISK_REGISTER.md` (fixed / accepted risk / release
blocker / post-release backlog). Fixed in 0.5.0-rc1: callback replay
protection (topic 1 — one-time stored tokens), verify success detection
hardening (topic 4 — explicit allowlist + typed field parsing; real-schema
confirmation remains a release blocker), malformed integer conversion
(topic 6), manual-review resolution tooling (topics 12/25 — host CLI),
application-level rate limiting (topic 17), and update-channel integrity
(topic 19 — pinned release tag + checksum verification). Fault-injection
tests now prove the crash-window behavior of topic 5. Release blockers B1
(real-host installer run, topic 20), B2 (staging/real gateway, topics
4/5), B3 (live Telegram, topic 22), B4 (adversarial review), and B5
(release workflow green) remain open — see the risk register for the
authoritative status of every item.

Status update (payment-creation audit, audit/payment-creation-idempotency):
the creation path was audited end to end. Fixed: lax Pydantic coercion
(bool True→1, integral floats, numeric strings were silently accepted as
amounts) and control characters/NUL in order_id (NUL reached PostgreSQL
and produced a 500) — the schema is now strict with no side effects for
malformed requests. Confirmed safe and now regression-tested: idempotency
under 10-way identical concurrency (one row, one getLink, one link
event), conflicting-amount races, unique random gateway-id allocation
(DB-unique, restore-safe, guess-resistant), crash recovery before and
after getLink with atomic token+URL persistence, and duplicate handling
for verified and manual-review payments. The getLink transaction model is
documented as model A (row lock held across getLink, bounded by
CENTRALPAY_TIMEOUT_SECONDS). Topics 8 and part of 10 are substantially
narrowed by these tests; the full adversarial review (B4) remains open.

Status update (worker audit, audit/worker-retry-and-recovery): the
worker, retry engine, claim/stale-claim recovery, and crash paths were
audited. Fixed: interrupted attempts now count against the retry limit in
idempotent mode (previously a delivery whose worker died on every attempt
requeued forever); attempt results are recorded only when the row still
carries the recording worker's claim at the same attempt number
(stragglers can no longer write against a successor's claim, and the
discard is audited); stale-claim recovery batches are bounded. Proven by
new deterministic tests: four workers drain a queue with exactly-once
delivery on real PostgreSQL, manual review survives restarts and both
retry modes, scheduled retries survive process restarts, and recovery
batch bounds. Topics 13 and 15 are narrowed accordingly; load testing
(15) and the full adversarial review (B4) remain open.

Status update (backup audit, audit/backup-restore-integrity): backups now
have SHA-256 manifests (atomic sidecar; verified before restore, with an
explicit RESTORE-LEGACY path for pre-manifest files that --yes cannot
bypass), an exclusive lock shared between backup and restore, magic-byte
and zero-size validation before the .ok marker, and retention that can
never convert a successful backup into a reported failure. Restores
refuse symlinks, stop every writer including the admin bot, run
pg_restore --exit-on-error, and gate service startup behind
`centralpay db-check --repair-sequences`; mid-restore failures leave
services stopped with printed recovery steps. Full-state restore fidelity
and sequence safety are proven on real PostgreSQL. Off-site replication
(topic 21) remains manual — a local backup is NOT disaster recovery.

Status update (deployment audit, audit/deployment-installer-security):
Docker/Compose/Caddy/installer/CLI were audited. Fixed: the Caddy access
log now redacts the one-time callback token (ct) alongside sig; the
Compose networks are split (Caddy on an edge network with no route to
PostgreSQL); api/worker/migrate now run the same hardening profile as the
admin bot (read-only root fs, tmpfs, cap_drop ALL, no-new-privileges —
every service denies privilege escalation); the worker masks CentralPay
keys, the inbound API key, and the callback HMAC secret; .dockerignore
excludes credentials, dumps, key material, and local databases; the logs
commands use a component allowlist. Verified and now policy-tested: no
Docker socket/privileged/host namespaces, only 80/443 published, no
archive extraction in the update path, non-root fixed-UID image, no
secrets in image layers. Installer posture confirmed (keyring-based apt,
/dev/tty input, silent secret reads, UFW never enabled silently,
umask 077 config writes). Real-host installer execution remains blocker
B1; base-image digest pinning remains topic 18.

Status update (final financial audit, audit/final-financial-correctness):
the entire bridge was re-audited end to end, independently of prior audit
conclusions. **No remaining financial-correctness bug was found in the
code.** The twenty financial invariants are documented and mapped to
tests in FINANCIAL_INVARIANTS.md / FINANCIAL_TEST_MATRIX.md; the state
machine, crash matrix (FINANCIAL_CRASH_MATRIX.md), and CentralPay
contract assumptions (CENTRALPAY_CONTRACT_ASSUMPTIONS.md) are documented.
Hardening added: migration 0005 CHECK constraints (positive amounts,
non-negative attempts, delivery-requires-verification), a
verified-fact predicate in the worker claim query, a CI guard against
silently skipped integration suites, and three further cross-component
race proofs. Verdict (FINAL_FINANCIAL_AUDIT.md): CODE_FINANCIALLY_SOUND,
PRODUCTION_VALIDATION_STATUS: INCOMPLETE — blockers B1–B5 and real-bot
confirmation remain; no tag, no real payments.

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
**RESOLVED (audit/gateway-response-hardening).** `_safe_reason()` copied
gateway-supplied message text (truncated to 200 chars) into exception
messages, audit event data, and API error responses. It has been removed:
gateway responses are now classified into a fixed internal reason-code
vocabulary (`gateway_rejected`, `gateway_response_invalid`,
`gateway_missing_data`, `gateway_invalid_redirect_url`,
`gateway_invalid_reference_id`, `gateway_invalid_amount`,
`gateway_invalid_user_id`) inside `app/centralpay.py`, and raw gateway text
never leaves that module. Redirect URLs are additionally validated with a
real URL parser (HTTPS-only, no credentials, no control characters,
bounded length) — see the gateway-controlled data policy in SECURITY.md.
Regression tests assert sentinel gateway text never reaches exceptions,
logs, stored errors, audit data, or API responses.

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

## New unresolved topics from Phase 3

### 17. No rate limiting at the proxy
Stock Caddy has no rate-limit module; adding one requires a custom Caddy
build (plugin) or an alternative mechanism. Until then, unauthenticated
endpoints (callback, health) rely on request-size limits, signature
validation, and upstream capacity. Amount bounds and API-key auth protect
`custom-payment`. Review before production exposure.

### 18. Base images not digest-pinned
`python:3.12-slim`, `postgres:16`, and `caddy:2` are tag-pinned, not
digest-pinned; supply-chain reviewers may want digests plus an update
process for them.

### 19. Update channel is a Git ref without signature verification
`centralpay update` fetches `CENTRALPAY_UPDATE_REF` (default `main` —
development mode) from GitHub over HTTPS but does not verify tags or
commit signatures. Pin a release tag for production and review signed
releases before 1.0.

### 20. Installer end-to-end run not executed on real target OSes
Installer logic is unit-tested (OS/arch rejection, validation, secret
handling) and ShellCheck-clean, but a full `curl | sudo bash` install has
not been executed on live Ubuntu 22.04/24.04/26.04 hosts from this
environment. Required before claiming installer completion (AGENTS.md
completion criteria). Ubuntu 26.04 CI runners are not yet available;
version logic is validated in tests instead.

### 21. Off-site backup replication
Backups are local to the server. Replication to off-site storage is
documented as a manual recommendation only.

## New unresolved topics from Phase 4

### 22. Live Telegram integration untested
All Telegram interaction is exercised against mocks/fakes (per phase
instructions). A supervised test against the real Telegram Bot API
(polling, 429 behavior, HTML rendering of Persian messages) is required
before relying on alerts operationally.

### 23. Duplicate alert delivery on stale-claim recovery
A crashed admin-bot instance releases its `sending` claims back to
pending; the alert may then be sent twice. Accepted deliberately (alerts
are operational, never financial), but reviewers should confirm the
duplicate-message trade-off.

### 24. Health monitor counters are in-memory
Consecutive-failure/recovery counters reset on admin-bot restart, which
can delay (never fabricate) an unhealthy or recovery alert by one cycle.

### 25. Admin bot resolution tooling still absent
The bot is read-only by design (per AGENTS.md a Telegram /retry command
must not exist until retry safety and authorization are separately
reviewed). Resolving manual_review payments still requires direct,
audited database work.

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
