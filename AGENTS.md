# CentralPay Bridge — Engineering Contract

This file is the authoritative engineering contract for the repository. It defines invariants and required behavior. It is intentionally more stable than audit reports, release notes, or implementation snapshots.

## Mission and priorities

Build and operate a payment bridge between a Telegram selling bot custom-payment API and CentralPay.

Priority order:

1. Financial correctness
2. Security
3. Reliability
4. Recoverability
5. Observability
6. Availability

Availability must never be improved by weakening financial correctness.

## Supported deployment

- Ubuntu Server 22.04 LTS, 24.04 LTS, 26.04 LTS
- amd64 and arm64
- Docker Engine + Docker Compose plugin
- PostgreSQL 16 production database
- Caddy as the public TLS reverse proxy

The installer and management tooling store configuration/secrets outside the repository under `/etc/centralpay-bridge/`.

## Public API contract

### Create payment

`POST /api/custom-payment`

Canonical JSON body:

```json
{
  "api_key": "...",
  "amount": 100000,
  "order_id": "opaque-string"
}
```

Rules:

- `api_key` is a secret and must never be logged or echoed.
- `amount` is TOMAN.
- Canonical amount representation is a JSON integer.
- A legacy ASCII-decimal string matching exactly `[0-9]+` may be normalized before validation.
- Floats, booleans, signs, separators, whitespace padding, exponents, and non-ASCII digits are rejected.
- `order_id` is opaque, bounded, non-empty, and must not be silently normalized.
- Authentication occurs before order lookup or behavior that can reveal order existence.
- Existing-order replay may bypass creation rate limiting only if the replay is provably work-free and matches the stored amount and payer-identity shape.

Successful response:

```json
{"url": "https://..."}
```

### Callback

`GET /api/centralpay/callback?orderId=...&ct=...&sig=...`

Requirements:

- reject duplicated security parameters
- validate HMAC signature before database/gateway work
- bind the callback token (`ct`) into the signature
- store only the token hash
- superseded tokens fail before gateway verify
- already verified payments never call verify again

### Health

- `GET /health/live`
- `GET /health/ready`

`/health/details` is internal-only and must not be publicly routed by Caddy.

## CentralPay transport contract

CentralPay calls use HTTPS only.

### getLink

Send JSON to the configured getLink endpoint with the gateway-required fields including:

- API key
- type `deposit`
- payable amount in TOMAN
- isolated gateway user ID
- unique numeric gateway order ID
- signed return URL

The returned redirect URL must satisfy the configured HTTPS/storage safety policy before it is stored or returned.

### verify

Verification is trusted only after:

- an explicit accepted success marker
- valid reference ID
- exact amount match against `payable_amount`
- exact user ID match against the payment's `gateway_user_id`

Raw gateway-controlled error/message content must not escape the CentralPay client boundary into logs, audit payloads, Telegram, or API errors. Translate external failures into the repository's fixed internal reason-code vocabulary.

## Order and payer identity

- Preserve the original bot `order_id` exactly.
- Use a separate unique numeric `gateway_order_id` for CentralPay.
- `bot_order_id` and `gateway_order_id` are database-unique.
- Gateway payer identity must be isolated per payer/order according to the current identity scheme; never fall back to a shared legacy payer ID for new flows.
- Identity shape is part of safe-replay eligibility.
- Identity reconciliation must never silently reinterpret a different Telegram user as the same payer.

## Payment states

The implementation may add operational detail, but the core state model includes:

- `created`
- `link_created`
- `getlink_failed`
- `gateway_verified`
- `bot_notify_pending`
- `bot_notify_accepted`
- `manual_review`

State names are operational facts, not customer-balance claims.

In particular, `bot_notify_accepted` means the selling-bot API returned an accepted HTTP response. It does **not** prove that the customer balance was credited.

## Financial invariants

The following are non-negotiable:

1. Never mark a payment verified before CentralPay verify succeeds.
2. Never notify the selling bot before gateway verification is committed.
3. Verify amount must equal the immutable `payable_amount` snapshot.
4. Verify `userId` must equal the payment's immutable gateway payer identity.
5. Reference IDs must satisfy the storage contract and must not overwrite another payment's reference ID.
6. One `bot_order_id` maps to one payment row.
7. A duplicate order with a different amount must not mutate the original payment.
8. A successfully verified payment is never re-verified merely because callback/reconciliation repeats.
9. Dynamic fee arithmetic uses integers only and is snapshotted once per payment.
10. `payments.amount` remains the original selling-bot invoice amount.
11. The selling-bot notification payload does not carry fee/payable amounts.
12. Ambiguous delivery is never silently converted into success.
13. In `safe` retry mode, an ambiguous send is never automatically resent.
14. Automatic notification retries are bounded.
15. Reconciliation retries are bounded/aged and use the same canonical verify/settle logic as callbacks.
16. `manual_review` cannot be bypassed by ordinary callback/create/worker paths.
17. Operator review resolution must not rewrite gateway verification facts or financial amounts.
18. Every financial state transition is appended to `payment_events`.
19. A failed administrator-alert side effect must not abort the financial transaction that triggered it.
20. Backup/restore must preserve financial rows and audit history and must fail closed if restore integrity cannot be established.

Detailed audit snapshots may extend this list; this contract remains the baseline.

## Concurrency rules

Financial concurrency semantics are PostgreSQL semantics.

Use:

- database uniqueness constraints for identity/idempotency boundaries
- `SELECT ... FOR UPDATE` for financial state transitions that require serialization
- `FOR UPDATE SKIP LOCKED` for competing queue workers when appropriate
- `populate_existing=True` or equivalent refresh discipline when SQLAlchemy identity-map staleness could defeat a lock/re-check
- explicit re-check under the lock before committing a mutation

Do not replace database-enforced correctness with process-local locks.

Tests that claim concurrency correctness must use real PostgreSQL.

## Notification delivery

After verified settlement, notify the selling bot using the documented order-only payload and Token header.

Supported retry modes:

### `safe`

Default conservative mode.

- safe pre-send/connect failures may retry
- selected HTTP 5xx may retry within the bounded policy
- explicit unsafe/invalid responses do not loop forever
- ambiguous post-send timeout/outcome moves to manual review rather than automatic resend

### `idempotent`

May be enabled only when the selling bot is known to process duplicate `order_id` delivery idempotently.

This mode permits recovery operations that can re-deliver an already verified order. The operator must understand that re-delivery is not itself proof of balance state.

## Manual review and operator recovery

`manual_review` exists to stop automation at ambiguity.

Allowed operator actions are explicit and audited:

- acknowledge a review
- resolve with an allowlisted non-financial resolution
- gated resend for a verified payment when idempotent mode is configured
- manual notification acceptance only when the operator has independently confirmed the selling bot already processed the order
- bounded aged-out reconciliation recovery through the canonical verification path

No operator command may fabricate gateway verification, rewrite amount/fee snapshots, or silently delete audit history.

## Administrator Telegram bot

The optional admin bot is an operations plane, never part of customer payment correctness.

Authorization requirements:

- numeric Telegram user IDs only
- private chats only
- unauthorized access receives a generic denial and is audited
- usernames are never authorization identities

Most commands are read-only inspection. A mutating Telegram command may exist only when:

- its financial scope is explicitly constrained
- it is strongly gated
- it operates only on already gateway-verified records when re-delivery is involved
- it is auditable
- concurrency is proven safe on PostgreSQL
- it cannot forge settlement or change financial snapshots

Currently `/resend_failed confirm` is the approved mutating pattern and is allowed only in idempotent notification mode for eligible verified delivery failures.

Telegram delivery outages must never block or roll back payment processing.

Never send secrets, callback signatures/tokens, raw external error text, full card data, or full payment redirect URLs through Telegram.

## Reconciliation

The reconciliation worker exists to recover paid orders when browser callback delivery is absent/delayed.

Rules:

- reuse the canonical verification/settlement service
- do not invent a second financial interpretation of gateway verify
- normal `gateway_not_paid` is not itself an incident
- retry scheduling is bounded and respects link age / max-age policy
- terminal/exhausted/aged-out cases remain visible to the operator
- already verified/manual-review payments are not re-settled
- reconciliation must not create double notifications

## Fees

Fee policy is stored in PostgreSQL, not mutable environment state.

- fee policies are append-only history with explicit cancellation rules
- fee arithmetic is basis-point/integer based
- every payment snapshots policy ID, rate, fee amount, and payable amount once
- later fee-policy changes never alter an existing payment
- maximum-payment enforcement applies to the final payable amount
- changing fee policy affects new payments only

## Rate limiting

Rate limiting is abuse protection, not a financial correctness mechanism.

- health endpoints remain reachable
- valid callback signatures are not dropped merely to satisfy a limiter
- payment creation keeps global and per-client protection
- invalid callback signatures keep global and per-client protection
- client IP trust depends on the explicit Caddy single-hop overwrite boundary
- limiter storage must stay bounded
- limiter internal failure should not corrupt payment state

Do not introduce Redis or another stateful dependency solely for rate limiting without an explicit architecture decision.

## Logging and secret handling

Use structured logs with request IDs.

Never log or expose:

- inbound API key
- CentralPay API key
- selling-bot Token
- admin-bot token
- database password
- callback HMAC secret
- callback `ct` token
- callback `sig`
- full card number
- full redirect URL
- raw gateway-controlled error body/text

Caddy access logging must redact `ct` and `sig` from both the request URI and `Referer` header. Referrer policy is defense-in-depth, not a substitute for log-boundary redaction.

## Deployment and container security

Only Caddy may publish public host ports.

Required architecture:

- Caddy on edge network
- API on edge + internal networks
- PostgreSQL on internal network only
- worker/admin-bot/migrate on internal network only
- no Docker socket
- no privileged mode
- no host network/PID/IPC
- application containers non-root
- read-only root filesystem where compatible
- all capabilities dropped for application services
- `no-new-privileges`
- secrets masked/omitted per service role

## Backups and restore

Backups must be validated before being declared good.

Required properties:

- PostgreSQL custom-format dump
- atomic creation
- non-empty / format validation
- `pg_restore --list` validation
- SHA-256 manifest for current backups
- bounded retention while retaining the newest valid backup
- restore checksum validation
- pre-restore backup
- all writers stopped during destructive restore
- `pg_restore --exit-on-error`
- migrations and canonical DB integrity check before application restart
- failed restore leaves writers stopped with recovery instructions

Local backups are not off-site disaster recovery.

## Production update integrity

Normal production updates use release tags matching:

- `vX.Y.Z`
- `vX.Y.Z-rcN`

For release-tag updates, the updater must verify the release artifact and `SOURCE_COMMIT` through `SHA256SUMS`, then require the fetched tag commit to equal the verified `SOURCE_COMMIT` before deployment work begins.

A non-release branch ref fails closed by default. Development branch updates require explicit `CENTRALPAY_UPDATE_ALLOW_DEV_REF=true` and must not be presented as verified production updates.

`CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` is an emergency escape hatch and must never be the normal documented production path.

Database migrations are forward-only. Application rollback must not pretend to downgrade the database schema.

## Testing and CI

Required validation categories:

- unit tests
- real PostgreSQL integration/concurrency tests
- financial fault-injection tests
- backup/restore tests
- migration tests
- admin-bot authorization and mutation-gate tests
- rate-limit and proxy-boundary tests
- secret/log-redaction tests
- Ruff
- mypy
- ShellCheck + `bash -n`
- Docker build and compose validation
- secret scan
- dependency vulnerability scan

Do not weaken, skip, or delete a required test merely to obtain green CI.

Infrastructure CI hangs may be retried once when plausibly transient; repeated infrastructure failure must be fixed rather than rerun indefinitely.

## Documentation policy

- `README.md`, `README_FA.md`, this file, `SECURITY.md`, and operational runbooks are living documentation.
- Audit/review/release-validation files are evidence snapshots and may intentionally describe an older commit.
- Historical snapshots must not be treated as the current implementation contract.
- [DOCUMENTATION.md](DOCUMENTATION.md) is the map of document ownership/status.

## Change policy

Prefer small, reviewable changes.

Before merging a behavior change:

1. identify financial/security invariants affected
2. add/adjust the smallest authoritative tests
3. run real PostgreSQL tests when DB locking/uniqueness matters
4. inspect the complete diff
5. ensure no secret or generated local tooling artifact is included
6. require current-head CI to be green

Graph/navigation tools may help find code, but source and real database behavior remain authoritative.
