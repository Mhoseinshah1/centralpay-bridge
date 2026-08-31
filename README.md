# CentralPay Bridge

Production-grade payment bridge between a Telegram bot custom-payment API and CentralPay.

Current application version: **0.6.0-rc4**. Current Alembic head on this branch: **0012**.

The project is intentionally conservative: **financial correctness > security > reliability > recoverability > observability > availability**. If the system cannot prove that a payment is safe to continue, it fails closed or moves the payment to manual review instead of guessing.

The authoritative engineering contract is [AGENTS.md](AGENTS.md). The documentation map is [DOCUMENTATION.md](DOCUMENTATION.md).

## What the bridge does

1. The selling bot calls `POST /api/custom-payment` with an API key, amount, and `order_id`.
2. CentralPay Bridge validates the request, snapshots the active fee policy, creates an idempotent payment record, derives an isolated gateway payer identity, and requests a CentralPay payment link.
3. CentralPay returns the payer to the signed callback URL.
4. The bridge verifies the callback HMAC/token, calls CentralPay `verify`, and validates the gateway-reported amount, payer identity, and reference ID.
5. Only after successful verification does the bridge queue the notification to the selling bot.
6. Delivery, retries, reconciliation, manual review, audit history, backups, and administrator alerts remain recoverable from PostgreSQL.

The selling-bot notification payload intentionally contains no amount or fee fields; the bot continues to credit its own original invoice.

## Public surface

Caddy exposes only the intended public routes:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/custom-payment` | create or safely replay a payment link |
| `GET` | `/api/centralpay/callback` | signed CentralPay callback |
| `GET` | `/health/live` | liveness |
| `GET` | `/health/ready` | readiness + database connectivity |
| `GET` | `/static/*` | callback-page static assets |

`/health/details` is application-internal and is deliberately not routed by public Caddy configuration.

## Current architecture

```text
Internet
   |
   v
Caddy :80/:443
   |
   v
API :8000 --------> PostgreSQL 16
                       ^
                       |
          +------------+----+----------+
          |                 |          |
        Worker            Admin bot   Monitor (optional)
   notification +         Telegram    health/incident checks,
   reconciliation         ops         Telegram alerts
```

Only Caddy publishes host ports. PostgreSQL is on the internal network only. Caddy has no route to PostgreSQL. Application containers run non-root with a read-only root filesystem, dropped capabilities, `no-new-privileges`, and per-service secret masking.

The `admin-bot` service is optional and profile-gated. Most Telegram commands are read-only. The only currently supported mutating Telegram operation is the heavily gated `/resend_failed confirm`, which can only requeue already gateway-verified delivery failures when `BOT_NOTIFY_RETRY_MODE=idempotent` is configured.

The `monitor` service is also optional and profile-gated (`MONITOR_ENABLED=false` by default). Its checks are read-only against the database and filesystem and never write payment rows; its durable incident lifecycle does write a separate, non-financial `monitor_incidents` table and an alert-outbox row per open/escalate/resolve transition, delivered through the same admin-bot Telegram pipeline. See [Monitoring](#monitoring) below.

## Financial safety guarantees

- A payment is never marked verified before CentralPay verification succeeds.
- Gateway amount must match the snapshotted `payable_amount` exactly.
- Gateway `userId` must match the payment's isolated payer identity.
- Reference IDs are validated against the storage contract and must be unique when present.
- `bot_order_id` and `gateway_order_id` are unique at the database layer.
- Successful verification is row-locked and duplicate callbacks do not re-verify an already verified payment.
- Ambiguous bot delivery is not silently treated as credit.
- In `safe` notification mode, ambiguous delivery is never automatically resent.
- `manual_review` is sticky until an explicit operator action resolves or safely requeues it.
- Financial state transitions are permanently appended to `payment_events`.
- Dynamic fee arithmetic is integer-only and snapshotted once per payment.
- Reconciliation only uses the canonical verification/settlement path and ages out rather than retrying forever.
- Monitoring's checks are read-only against financial state: no check writes a payment row, resolves a manual review, or mutates financial state. (Its own incident-tracking table is a separate, non-financial write — see [Monitoring](#monitoring).)

See [FINANCIAL_INVARIANTS.md](FINANCIAL_INVARIANTS.md) and [FINANCIAL_TEST_MATRIX.md](FINANCIAL_TEST_MATRIX.md) for detailed audit/test snapshots.

## Request contract

`POST /api/custom-payment` accepts the canonical JSON object:

```json
{
  "api_key": "...",
  "amount": 100000,
  "order_id": "opaque-string"
}
```

`amount` is TOMAN. A JSON integer is canonical; a legacy ASCII-decimal string matching exactly `[0-9]+` is also normalized before validation. Floats, booleans, signed strings, whitespace-padded strings, separators, exponents, and non-ASCII digits are rejected.

`order_id` is an opaque, non-empty string with a bounded length. It is not trimmed, case-folded, or Unicode-normalized.

Legacy clients using `application/x-www-form-urlencoded`, `text/plain`, or one extra JSON-string wrapper are supported only through bounded normalization; authentication, amount rules, idempotency, fee handling, and gateway validation remain identical.

## Rate limiting and client IP

The application uses bounded in-process sliding-window limiters. Payment creation has both per-IP and global ceilings; invalid callback signatures have per-IP and global ceilings; invalid API-key attempts retain a global ceiling. Caddy explicitly overwrites `X-Forwarded-For` with its resolved peer address, and the application accepts only one syntactically valid IP value before using it for limiter identity.

Safe, work-free replay of an already linked order may bypass the creation limiter only when amount and payer-identity shape exactly match the stored payment. Any request that could create data, call the gateway, mutate state, or represent an identity mismatch consumes limiter budget.

See [RATE_LIMITING_ARCHITECTURE.md](RATE_LIMITING_ARCHITECTURE.md).

## Administrator operations

Installations expose the host command `centralpay` for service lifecycle, backups, migration, review, reconciliation, fee policy, inspection, monitoring, and admin-bot control.

Common commands:

```text
centralpay status
centralpay logs [api|worker|db|caddy]
centralpay logs-errors [COMPONENT]
centralpay diagnose
centralpay version

centralpay payment ORDER_ID
centralpay recent
centralpay stuck
centralpay retry-queue
centralpay manual-review [--all]          # deprecated; use `review list`

centralpay review list [--all]
centralpay review show ORDER_ID
centralpay review acknowledge ORDER_ID --note TEXT
centralpay review resolve ORDER_ID --resolution VALUE --note TEXT
centralpay review resolve-many ORDER_ID [ORDER_ID ...] \
    --resolution VALUE --note TEXT [--yes]   # delivery failures only
centralpay review resend ORDER_ID --confirm-idempotent-bot --yes
centralpay notification accept ORDER_ID --note TEXT --yes

centralpay attention list [--resolved]
centralpay attention show ORDER_ID
centralpay attention resolve ORDER_ID --resolution VALUE --note TEXT --yes

centralpay reconciliation status
centralpay reconcile ORDER_ID
centralpay recover-aged-out ORDER_ID

centralpay fee status
centralpay fee set RATE --note TEXT
centralpay fee schedule RATE --at ISO --note TEXT
centralpay fee history
centralpay fee cancel POLICY_ID --note TEXT

centralpay monitor enable
centralpay monitor disable
centralpay monitor check --json
centralpay monitor incidents
centralpay monitor status
centralpay monitor logs
centralpay monitor restart

centralpay backup
centralpay backups
centralpay restore FILE
centralpay db-check --details --json

centralpay update --check
centralpay update
centralpay rollback
```

Detailed Persian runbooks: [OPERATIONS_FA.md](OPERATIONS_FA.md), [BACKUP_RESTORE_FA.md](BACKUP_RESTORE_FA.md), and [ADMIN_BOT_FA.md](ADMIN_BOT_FA.md).

### Operational attention resolution

Some payments never reach a payment link and are never gateway verified — a
`getLink` timeout leaves the row in `getlink_failed`, which nothing
automatically revisits, so it stays in `centralpay stuck`'s `needs_attention`
category forever. Payment and audit history is permanent and is never deleted
to clear an operator worklist, so `centralpay attention resolve` records that
decision instead:

- writes only four operational columns (time, actor, allowlisted reason,
  mandatory note — migration `0013`) and appends a `payment_attention_resolved`
  audit event;
- never changes `status`, `amount`, `payable_amount`, the fee snapshot,
  `gateway_verified_at`, `reference_id`, `gateway_order_id`, `gateway_user_id`,
  or payer identity, and never deletes a payment, event, or admin alert;
- uses a strict resolution→status allowlist (`stale_getlink_failure` for
  `getlink_failed`, `stale_incomplete_creation` for `created`) and refuses a
  payment that has become financially meaningful, re-checked under the row
  lock;
- is scoped to the incident it closed: a later failed link-creation attempt for
  the same order reopens the item everywhere and lets the operator record a
  fresh resolution, so a new failure is never silently suppressed;
- removes the item from CURRENT attention counts on every surface at once (one
  shared predicate, `app.services.stuck_payments.unexpected_status_conditions`)
  while leaving it fully visible historically;
- never contacts CentralPay or the selling bot, and never blocks a later
  legitimate settlement — if the gateway did create a link the bridge never
  received, the normal callback path still settles the payment.

### `stuck --json` summary fields

`total` is the TRUE sum of `needs_attention + waiting_gateway + expired`. It
previously reported the size of the internally capped result set, which made
the JSON self-contradictory once any category exceeded that cap (a real
production line read `needs_attention: 1, waiting_gateway: 25, expired: 5788,
total: 226`). The capped size is still available, explicitly, as
`materialized_total`, and `truncated` reports whether any matching payment is
missing from the entry lines.

## Production installation

Supported targets:

- Ubuntu Server 22.04 LTS, 24.04 LTS, or 26.04 LTS
- amd64 or arm64
- Docker Engine + Docker Compose plugin
- PostgreSQL 16 container

One-line installer:

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

Secrets and generated credentials are stored outside the repository under `/etc/centralpay-bridge/` with restrictive permissions. Backups default to `/var/backups/centralpay-bridge/`.

See [INSTALL_FA.md](INSTALL_FA.md) and [PRODUCTION_CHECKLIST_FA.md](PRODUCTION_CHECKLIST_FA.md).

## Production update policy

Production updates are **release-tag only by default**.

`CENTRALPAY_UPDATE_REF` must normally be a release tag matching `vX.Y.Z` or `vX.Y.Z-rcN`. For a release tag, the updater verifies the release artifact, `SOURCE_COMMIT`, and `SHA256SUMS`, and requires the fetched tag commit to equal the verified `SOURCE_COMMIT` before checkout, backup/deploy, migration, or restart.

Plain branch refs such as `main` are rejected unless the operator explicitly sets:

```env
CENTRALPAY_UPDATE_ALLOW_DEV_REF=true
```

That setting is for local/development use and disables release integrity binding for the selected branch ref. `CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` is a separate emergency escape hatch for a release tag whose verification assets are unavailable; it is not the normal production path.

## Backups and restore

The host backup job creates PostgreSQL custom-format dumps, validates them with `pg_restore --list`, writes SHA-256 manifest metadata, and applies retention while always keeping the newest valid backup. Restore verifies the selected file/manifest, creates a pre-restore backup, stops writers, restores with `--exit-on-error`, runs migrations and the canonical integrity checker, and only then restarts application services.

A backup on the same host is **not** disaster recovery. Off-site replication remains an operator responsibility unless separately implemented.

## Monitoring

An optional, dedicated monitoring process (`MONITOR_ENABLED=false` by default, separate from the worker) checks public readiness, database connectivity, worker heartbeats, notification/manual-review backlog, reconciliation health, backup freshness and manifest integrity, disk space, DB integrity, and gateway/bot failure bursts. Incidents are durable — backed by the `monitor_incidents` table, so state survives a restart — with deduplicated open/escalation/recovery alerts queued through the existing admin-bot Telegram pipeline (a disabled alert category or a permanently failed delivery can add a later catch-up row for the same still-open incident, so queuing is not a strict one-row-per-transition guarantee); delivery to Telegram itself is at-least-once (a lost response after Telegram accepts the message can produce a duplicate), never exactly-once.

```bash
centralpay monitor enable            # start the monitor service
centralpay monitor check --json      # run every check now
centralpay monitor incidents         # currently open incidents
```

An admin can also run `/monitor` in Telegram for the same live snapshot (read-only). Gateway/bot failure-burst counting only counts genuine transport/protocol-level failures — an ordinary payer declining or abandoning a payment never trips it. Backup validation checks manifest metadata (filename, size, checksum shape) without ever hashing the full dump file. If PostgreSQL itself is unreachable, database-independent checks keep running and database-dependent checks degrade to a `database_unavailable` result instead of crashing the pass.

See [MONITORING.md](MONITORING.md) for the full architecture, check/threshold reference, incident-lifecycle design, operator runbook, and known limitations (including PostgreSQL-outage behavior).

## Security

Important controls include:

- HMAC-signed callback + one-time callback token hash
- constant-time comparison for inbound secrets/signatures
- strict gateway response parsing
- HTTPS-only CentralPay transport
- bounded redirect/reference validation
- least-privilege container secret distribution
- structured logs with secret redaction
- Caddy callback query and `Referer` redaction for `ct` and `sig`
- application rate limiting
- PostgreSQL row locks, uniqueness constraints, and check constraints
- gitleaks/dependency scans in CI

See [SECURITY.md](SECURITY.md) and [SECURITY_HARDENING_AUDIT.md](SECURITY_HARDENING_AUDIT.md).

## Verification and historical audit documents

The repository contains several audit, validation, incident, and release-candidate documents produced at specific commits. They are retained as evidence and history; they are **not automatically kept synchronized with later code**. When an old audit says a review was still pending, treat that as the state at that audit's commit, not necessarily the current repository state.

[DOCUMENTATION.md](DOCUMENTATION.md) classifies every Markdown document as current/living, historical snapshot, validation evidence, or archive/legacy material.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Production and concurrency-sensitive integration tests use PostgreSQL. SQLite is allowed only for isolated tests that do not depend on PostgreSQL semantics.

Apply migrations, then run the API and worker locally:

```bash
alembic upgrade head
uvicorn app.asgi:app --host 127.0.0.1 --port 8000 --no-access-log
python -m app.worker
```

`--no-access-log` is required in every environment that handles real callbacks: uvicorn's default access log prints full request lines including query strings, which would leak callback signatures. See [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) for the migration chain.

Typical validation:

```bash
pytest -q
ruff check .
mypy app tests
shellcheck install.sh scripts/backup.sh scripts/centralpay
bash -n install.sh scripts/backup.sh scripts/centralpay
docker compose config --quiet
```

GitHub Actions additionally builds the image and runs secret/dependency scans.

## Release state

`0.6.0-rc4` is a pre-release. Do not infer go-live readiness from an old checklist or audit snapshot; use current source, current CI, the current operational checklist, and the cumulative risk history together.

For documentation ownership and which files are authoritative, start with [DOCUMENTATION.md](DOCUMENTATION.md).
