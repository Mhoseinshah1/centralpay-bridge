# Security Policy

## Reporting vulnerabilities

Report suspected vulnerabilities privately through GitHub Security Advisories for this repository. Do not publish secrets, production credentials, callback tokens/signatures, or payment data in public issues.

## Security priority

CentralPay Bridge handles payment state. Financial correctness outranks availability. A bug that can create false verification, duplicate credit, overwrite payment identity, hide an ambiguous delivery, or corrupt recovery state is treated as a security issue even if no classic remote-code-execution primitive exists.

The engineering baseline is [AGENTS.md](AGENTS.md). Historical security reviews are listed in [DOCUMENTATION.md](DOCUMENTATION.md).

## Current security posture

### Verification before trust

A payment is not trusted merely because the payer returned to the callback URL.

The callback path requires:

1. non-duplicated security parameters
2. valid HMAC signature
3. current one-time callback token
4. CentralPay `verify` success using the strict response parser
5. exact `payable_amount` match
6. exact payment `gateway_user_id` match
7. valid, non-conflicting `referenceId`

Only then can the payment enter the verified/notification flow.

Already verified payments short-circuit under the financial lock and are not sent to CentralPay verify again merely because callback/reconciliation repeats.

### Callback replay protection

Each payment link contains a callback token (`ct`) covered by the callback HMAC signature (`sig`). Only the token hash is stored. Regenerating a link supersedes the previous token, and a stale token is rejected before gateway verification.

Successful payment state remains replay-safe: a later return can render the final result without re-settlement.

### Strict gateway parsing

CentralPay response bodies are attacker-influenceable external data.

The client layer accepts success only through the explicit supported success vocabulary and converts gateway data to typed internal values. Raw gateway error/message text does not propagate into logs, stored errors, audit events, Telegram alerts, or API responses; callers receive fixed internal reason codes.

Invalid/missing financial fields fail closed or route to manual review.

### Amount and fee integrity

Money uses integer TOMAN arithmetic.

For each new payment, the active fee policy is snapshotted into immutable per-payment fields:

- `fee_policy_id`
- `fee_rate_bps`
- `fee_amount`
- `payable_amount`

`payable_amount = amount + fee_amount` is enforced by application logic and database constraints. CentralPay is asked to charge the payable amount, and verify must report that same value.

The original `payments.amount` remains the selling bot's original invoice. The selling-bot notification payload contains no fee or amount fields.

Fee policy changes are explicit host-CLI operations and affect new payments only.

### Payer identity isolation

New payment flows use the repository's payer-identity scheme rather than a shared gateway user ID. The gateway user ID stored on the payment is reused for link retry and verification.

Safe replay of an existing payment is allowed to bypass create limiting only when the request's identity shape exactly matches the stored identity. Changing Telegram-user presence/value is conservatively treated as real work and remains rate-limited.

### Request validation

`POST /api/custom-payment` has a bounded body and a strict normalized contract.

Canonical `amount` is a JSON integer. A legacy ASCII-decimal string matching `[0-9]+` may be normalized before validation. Other numeric-looking forms are rejected, including floats, booleans, signed strings, whitespace-padded strings, separators, exponents, and non-ASCII digits.

`order_id` is opaque, bounded, and not silently normalized.

Authentication occurs before order lookup so unauthenticated requests cannot use response differences to enumerate existing orders.

Legacy body compatibility is bounded by body size, parser depth, and form-field count; it does not weaken authentication, amount checks, idempotency, fee handling, or gateway validation.

### Reference ID boundary

Gateway `referenceId` is validated before query/assignment/log/audit use. The parser and database storage contract share the same maximum length. NUL/control characters and invalid shapes are rejected; invalid data is never silently truncated.

Non-null reference IDs are unique. A collision routes to manual review rather than overwriting another payment.

### Redirect URL policy

Gateway payment redirects must pass URL validation before being stored/returned. HTTPS is required, userinfo is rejected, malformed ports/hosts are rejected, control characters/whitespace are rejected, and length is bounded.

### Outbound transport

CentralPay transport is HTTPS-only; TLS verification is never disabled.

Selling-bot notification is HTTPS by default. Cleartext HTTP is permitted only through the explicit insecure-notify opt-in and only for syntactically private/internal destinations intended for isolated development/test networks. Public cleartext destinations remain rejected.

### Rate limiting and proxy trust

The application uses bounded in-memory sliding-window rate limiting.

Current dimensions include:

- payment creation: per-IP + global
- invalid callback signatures: per-IP + global
- invalid API-key attempts: global

Caddy explicitly overwrites `X-Forwarded-For` with the resolved immediate peer. The application accepts only a single syntactically valid IP value; malformed/multiple values fall back rather than becoming attacker-selected identity.

Valid signed callbacks are not rate-limited merely because they repeat. Health endpoints remain unthrottled for orchestration.

The limiter is intentionally process-local for the current single-API-container topology. Redis is not part of the current architecture.

### Bot-notification ambiguity

HTTP 2xx from the selling-bot API means `bot_notify_accepted`; it does not prove customer balance credit.

In `BOT_NOTIFY_RETRY_MODE=safe`, ambiguous post-send outcomes are not automatically resent. They are surfaced for operator review.

`idempotent` mode may be used only when duplicate `order_id` delivery is known to be safe on the selling bot.

Manual/gated resend operations never fabricate CentralPay verification and may only operate on already gateway-verified eligible records.

### Reconciliation

The worker can reconcile `link_created` payments when browser callback delivery is absent/delayed. Reconciliation calls the same canonical verification/settlement service used by callback processing and does not implement a second financial decision path.

Ordinary `gateway_not_paid` results are retryable operational states, not proof of an outage. Reconciliation is bounded by configured aging/max-age semantics and does not retry forever.

### SQL and transaction safety

Production uses PostgreSQL with SQLAlchemy parameterized queries.

Financial state transitions use database transactions, row locking, uniqueness constraints, and check constraints. Queue consumers use row locks/`SKIP LOCKED` where appropriate. SQLAlchemy identity-map refresh discipline is used where a locked re-read must defeat stale cached state.

Process-local synchronization is never accepted as the only protection for a financial invariant.

### Audit trail

`payment_events` is the permanent append-only financial audit history. Financial state changes must produce explicit events and reason codes. Ordinary application operations do not delete payment audit history.

Administrator alert creation is isolated with a database savepoint so a failed non-financial alert side effect cannot abort the outer payment transaction.

### Admin Telegram bot

The admin bot is optional and isolated from the customer payment path.

Authorization uses configured numeric Telegram user IDs and private chats; usernames are never authorization credentials. Unauthorized attempts receive generic denial and are audited.

Most commands are read-only. The current approved mutating Telegram operation is `/resend_failed confirm`, which is strongly gated: it can only requeue eligible already gateway-verified delivery failures and only when notification mode is `idempotent`. It cannot modify gateway verification facts or financial snapshots.

Telegram outage/failure must never block or roll back customer payment processing.

Never send secrets, callback token/signature, raw gateway text, full card data, or full redirect URLs to Telegram.

### Monitoring

The optional monitor service (`MONITOR_ENABLED=false` by default) is isolated from the customer payment path, exactly like the admin bot. It only reads existing tables and the filesystem: it never writes a payment row, never resolves a manual review, and never mutates financial state. It introduces no new secrets and delivers alerts through the existing admin-bot Telegram pipeline rather than a separate channel.

If PostgreSQL itself is unreachable, database-independent checks (public readiness, backup, disk space) keep running and database-dependent checks degrade to a `database_unavailable` result instead of raising — a real, unrelated code bug in a check is not mislabeled as a database outage. Backup manifest validation reads only the small `.manifest` sidecar, never the dump file's own bytes, so it does not need read access to backup contents to certify recoverability metadata. Gateway/bot failure-burst counting only counts genuine transport/protocol-level failures; an ordinary payer declining or abandoning a payment never trips it.

Disabling the monitor (the default) cannot change payment, notification, or reconciliation behavior in any way.

### Secret handling

Secrets live outside git under `/etc/centralpay-bridge/` with restrictive permissions.

The repository must never contain production:

- CentralPay API keys
- inbound API key
- callback HMAC secret
- selling-bot Token
- admin-bot Token
- database password

Per-service Compose overrides mask secrets a service does not need.

### Logging and callback-secret redaction

Application logs are structured and must not include request query strings or raw secret-bearing request bodies.

Caddy access logging redacts callback `ct` and `sig` in both locations where they can appear:

- request URI query string
- `Referer` header on follow-up static-asset requests

This logging-boundary redaction is required even though callback responses use restrictive Referrer Policy; client behavior must not be trusted to protect log secrecy.

Uvicorn must not be run with a callback-leaking access-log configuration in environments that handle real callback URLs.

**Managed Caddy config upgrades.** The installed `/etc/centralpay-bridge/Caddyfile` has exactly two operator-supplied values (payment domain, TLS/ACME email) — everything security-relevant (the redaction rules above, security headers, request-size cap, route allowlist, `admin off`) is application-managed and always re-rendered from the currently checked-out `deploy/caddy/Caddyfile.template`, never hand-merged. `scripts/render-caddy-config.sh` runs on both `centralpay update` and every `install.sh` invocation (including a "keep existing configuration" rerun) — a mandatory security-directive change in a new release therefore always reaches an already-running installation, closing the gap where a security fix could ship in source but never reach a production host that only ever ran `centralpay update`. The candidate is validated with the real `caddy:2` image before anything on disk changes; the previous file is backed up first; a failed validation leaves the running configuration completely untouched. Caddy's admin API is intentionally disabled and unpublished, so activation is always a container restart (gated on the config having actually changed), never a hot reload.

### Container/network isolation

Only Caddy publishes host ports.

Current trust zones:

- `edge`: Caddy + API
- `internal`: API + PostgreSQL + worker + migrate + optional admin-bot + optional monitor

Caddy has no database route and no application secrets. PostgreSQL has no published host port.

Application services run non-root and use a read-only root filesystem, tmpfs for required temporary/heartbeat files, `cap_drop: ALL`, and `no-new-privileges`. The deployment does not mount the Docker socket, use privileged containers, host networking, or host PID/IPC namespaces.

### Backup/restore safety

Current backups use PostgreSQL custom format, atomic creation, archive validation, and SHA-256 manifest metadata.

Restore rejects unsafe inputs, verifies the selected backup, obtains the backup/restore lock, creates a pre-restore backup, stops writers, restores with `--exit-on-error`, runs migrations and the canonical DB integrity checker, and restarts services only after those checks pass.

A failed restore intentionally leaves writers stopped rather than serving against a half-restored database.

Backups stored only on the same host do not constitute full disaster recovery.

### Production update integrity

Production update refs are release tags by default (`vX.Y.Z` or `vX.Y.Z-rcN`).

For a release tag, the updater downloads the source artifact, `SOURCE_COMMIT`, and `SHA256SUMS`; verifies checksums; validates the commit grammar; resolves the fetched tag; and requires tag commit == verified `SOURCE_COMMIT` before checkout/deploy/migration/restart.

Non-release branch refs fail closed by default. `CENTRALPAY_UPDATE_ALLOW_DEV_REF=true` explicitly restores unverified branch-update behavior for development and must not be presented as normal production operation.

`CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` is a separate emergency release-asset escape hatch, not the standard production path.

Database migrations are forward-only. Application rollback does not downgrade the schema.

## CI/security gates

CI includes the project's test suites and security/quality gates such as:

- unit/integration tests
- PostgreSQL financial/concurrency tests
- Ruff
- mypy
- ShellCheck
- Docker build/compose validation
- secret scanning
- dependency vulnerability scanning
- release-flow checks
- runtime Caddy redaction verification for callback secrets
- the managed Caddy config upgrade path (old-config -> current-template), validated against the real `caddy:2` image

A required failing check must be fixed, not bypassed.

## Known/accepted architectural limitations

These are not equivalent to confirmed vulnerabilities:

- rate limiting is process-local and assumes the current single API-container topology
- off-site backup replication is not part of the built-in backup job
- during a full PostgreSQL outage, the monitor cannot durably record its own "database is down" incident or Telegram alert (persisting either requires the very database that is unreachable), and the admin bot's `/monitor` command cannot run at all (its command-audit commit needs the database before any handler runs); `centralpay monitor check` stays usable through the CLI, but only if the `monitor` container was already running before the outage started — see [MONITORING.md](MONITORING.md)
- release trust binds artifacts to a commit through checksums/`SOURCE_COMMIT`; this is not the same thing as a complete signed-artifact provenance system
- some audit/review documents in the repository are historical snapshots and intentionally retain the findings/status of their original commit
- `centralpay rollback` deliberately does NOT re-sync the Caddyfile: rolling application files back to an older commit must never also downgrade a security-relevant Caddy directive that shipped in a version between the rollback target and the version being rolled back from. The installed Caddyfile stays at whatever the most recent successful `centralpay update`/install left it, one-way, the same way database migrations are forward-only

The current risk register is [RELEASE_RISK_REGISTER.md](RELEASE_RISK_REGISTER.md). Do not derive current risk status solely from an older audit snapshot.

## Historical reviews

The repository retains security/adversarial audit reports as evidence. They are not rewritten after every later PR. [DOCUMENTATION.md](DOCUMENTATION.md) identifies which files are current policy and which are historical snapshots.

## Supported versions

The project is currently pre-1.0. Security fixes are developed on `main` and shipped through the release-tag workflow.
