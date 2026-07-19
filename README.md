# CentralPay Bridge

Production-grade payment bridge between a Telegram bot custom gateway API and
[CentralPay]. Priorities, in order: financial correctness, security,
reliability, recoverability, observability.

The authoritative project contract is [AGENTS.md](AGENTS.md). The delivery
roadmap is GitHub issue #1.

## Verification status

> Automated adversarial verification has not yet been completed. This
> implementation must not be considered production-ready until that review
> and the remaining checks are completed.

The full test suite (unit + PostgreSQL integration + fault injection +
backup/restore round-trip), lint, type checking, and migration validation
pass; the multi-agent adversarial review remains outstanding. Every
deferred topic has been triaged for 0.6.0-rc1 in
[RELEASE_RISK_REGISTER.md](RELEASE_RISK_REGISTER.md) — **open release
blockers**: real-host installer validation
([REAL_HOST_VALIDATION.md](REAL_HOST_VALIDATION.md)), staging validation
against the real gateway ([STAGING_VALIDATION.md](STAGING_VALIDATION.md)),
live Telegram validation ([ADMIN_BOT_VALIDATION.md](ADMIN_BOT_VALIDATION.md)),
the adversarial review, and a green release workflow. Original topics:
[DEFERRED_REVIEW.md](DEFERRED_REVIEW.md).

## Status

**Phase 1 — Core payment API:**

- `POST /api/custom-payment` — payment creation for the bot, idempotent by
  `order_id`, authenticated with a constant-time API key comparison
- CentralPay `getLink` integration
- `GET /api/centralpay/callback` — HMAC-signed return URL, row-locked
  processing, CentralPay `verify` with amount / userId / referenceId
  validation
- `GET /health/live` and `GET /health/ready`
- Permanent `payment_events` audit trail
- Structured JSON logs with request IDs and secret redaction

**Phase 2 — Bot notification and recovery:**

- Safe delivery of verified payments to the bot API with explicit reason
  codes for every non-success state (no generic "stuck")
- Notification worker (`python -m app.worker`) with `FOR UPDATE SKIP LOCKED`
  claims, bounded exponential backoff with jitter, and stale-claim recovery
- `safe` (default) and `idempotent` retry modes
- Payer-facing callback status pages (verified+accepted / verified+pending /
  under review)
- Read-only inspection CLI (`python -m app.cli`)

**Phase 3 — Deployment and operations** (this code):

- Production Dockerfile (multi-stage, non-root fixed UID, amd64/arm64) and
  Docker Compose stack: `caddy` (TLS) → `api` → `db`, plus `worker` and a
  one-shot `migrate` service that gates API/worker startup on successful
  migrations. Since the deployment audit: split edge/internal networks
  (Caddy has no route to PostgreSQL), read-only hardened app containers
  (`cap_drop: ALL`, `no-new-privileges`, tmpfs), per-service secret
  masking, and `sig`+`ct` redaction in access logs
- One-line interactive installer for Ubuntu 22.04 / 24.04 / 26.04
- `centralpay` management command (status, logs, diagnose, backup, restore,
  update, ssl, uninstall, …)
- Validated daily PostgreSQL backups via a host systemd timer — atomic
  creation, SHA-256 manifest sidecars, checksum-verified restores with a
  post-restore integrity check (`centralpay db-check`)
- Configurable payment amount bounds; application version in `/health/live`
- GitHub Actions CI (tests, lint, types, ShellCheck, Docker build, compose
  validation, secret and dependency scanning)

**Phase 4 — Administrator Telegram bot** (this code):

- Optional, read-only, admin-only Telegram bot (`admin-bot` Compose
  service behind a profile — never started unless explicitly enabled)
- Authorization by numeric Telegram ID only, private chats only; generic
  denial for everyone else
- 12 inspection commands (`/status`, `/health`, `/recent`, `/stuck`,
  `/manual_review`, `/errors`, `/payment`, `/retry_queue`,
  `/backup_status`, `/version`, `/start`, `/help`)
- Durable alert outbox (`admin_alerts` table): alert rows are created
  inside payment transactions — but inside a database SAVEPOINT, so a
  failed alert INSERT rolls back only the savepoint and can never abort
  the financial transaction; the admin-bot service delivers them
  best-effort with bounded retries, dedup windows, and stale-claim
  recovery — a Telegram outage can never block payment processing.
  Delivery results carry claim ownership: a result is persisted only
  while the alert row still holds the same worker ID and attempt number
  (checked under the row lock), so late results from released or
  superseded claims are discarded and audited
  (`admin_alert_result_discarded`) without modifying the successor's
  claim. Telegram delivery itself stays at-least-once: a stale worker
  may already have sent a duplicate operational message — ownership
  prevents stale database-result overwrites, not duplicate sends
- Health monitor with consecutive-failure thresholds and recovery alerts;
  optional daily report (Asia/Tehran default, restart-safe dedup);
  worker heartbeats recorded in the database
- Persian message formatting (Jalali timestamps) with HTML escaping of
  every dynamic value

**Phase 5 — Release-candidate hardening (0.5.0-rc1)**:

- One-time callback tokens bound into the HMAC signature (hash-only
  storage, durable consumption, no hard expiration — legitimate late
  returns still resolve)
- Strict CentralPay response parsing: explicit success allowlist and
  typed field parsing with explicit reason codes (success is never
  guessed from truthy values)
- Reference-ID uniqueness; collisions route to manual review with a
  critical `reference_id_collision` alert and never overwrite
- Application-level rate limiting (invalid API keys, invalid callback
  signatures, create bursts)
- `centralpay review` host CLI (acknowledge/resolve with non-financial
  resolutions only; gated resend), `centralpay update --check` with
  release-checksum verification, application-only `centralpay rollback`
- `GET /health/details` (internal), first-payment guard
  (`FIRST_PAYMENT_GUARD_ENABLED`), fault-injection and backup/restore
  integration tests, gated release workflow producing draft-only
  releases with SBOM and SHA256SUMS
- Release docs: [CHANGELOG.md](CHANGELOG.md),
  [RELEASE_NOTES_0.5.0_RC1.md](RELEASE_NOTES_0.5.0_RC1.md),
  [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md),
  [RELEASE_RISK_REGISTER.md](RELEASE_RISK_REGISTER.md),
  [PRODUCTION_CHECKLIST_FA.md](PRODUCTION_CHECKLIST_FA.md)

**Phase 6 — Dynamic percentage fee (0.6.0-rc1)** (this code):

- Percentage service fee paid by the payer through CentralPay, invisible
  to the selling bot: original invoice preserved, immutable per-payment
  fee snapshot, integer round-half-up arithmetic, getLink charges the
  payable amount, verify enforces it, and the bot notification payload
  is unchanged (exact JSON object and field set) — see
  [Dynamic service fee](#dynamic-service-fee-percentage)
- Append-only audited `fee_policies` with deterministic selection and
  restart-free scheduled changes; `centralpay fee` host CLI (root-only
  mutations) and read-only admin-bot `/fee`; installer fee question with
  `--ensure-initial`
- Migration `0006` (zero-fee backfill + CHECK constraints binding
  `payable = amount + fee`); fee-aware db-check, backups, and reporting
- Real-host fix: deployment scripts committed executable (100755) and
  installed with explicit modes (backup.sh 0750 root:root)
- Release docs: [RELEASE_NOTES_0.6.0_RC1.md](RELEASE_NOTES_0.6.0_RC1.md)

Persian documentation: [README_FA.md](README_FA.md),
[INSTALL_FA.md](INSTALL_FA.md), [OPERATIONS_FA.md](OPERATIONS_FA.md),
[BACKUP_RESTORE_FA.md](BACKUP_RESTORE_FA.md),
[ADMIN_BOT_FA.md](ADMIN_BOT_FA.md),
[PRODUCTION_CHECKLIST_FA.md](PRODUCTION_CHECKLIST_FA.md). Security
policy: [SECURITY.md](SECURITY.md).

## Production installation

On a fresh Ubuntu 22.04/24.04/26.04 server (amd64 or arm64) with DNS for
your payment domain pointed at it:

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

The installer asks for domains, CentralPay keys, the bot token, TLS email,
amount bounds, and the retry mode; generates the inbound API key, callback
HMAC secret, and database password; deploys the Docker Compose stack; and
prints the URLs and generated API token at the end. Configuration and
secrets live in `/etc/centralpay-bridge/` (mode 0700/0600), never in the
Git checkout. Backups go to `/var/backups/centralpay-bridge/` daily at
03:15 with 14-day retention.

Architecture:

```text
Internet ──► Caddy :80/:443 (automatic TLS) ──► API :8000 ──► PostgreSQL :5432
                                                     ▲
             Worker ── PostgreSQL ── Bot API ────────┘   (internal network only;
                                                          only Caddy publishes ports)
```

Manage the installation with `centralpay`:

```text
centralpay status | logs [api|worker|db|caddy] | logs-errors | diagnose
centralpay restart | stop | start | update | migrate | ssl | version
centralpay backup | backups | restore FILE
centralpay payment ORDER_ID | recent | retry-queue | manual-review
centralpay credentials | uninstall
centralpay admin-bot status | logs | restart | enable | disable | test-alert
```

## Requirements

- Python 3.12
- PostgreSQL (production and integration tests; SQLite is used only for
  isolated unit tests)

## Development setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Create a local PostgreSQL database, e.g.:

```bash
sudo -u postgres psql \
  -c "CREATE USER centralpay WITH PASSWORD 'devpassword' CREATEDB;" \
  -c "CREATE DATABASE centralpay OWNER centralpay;"
```

Configure the environment:

```bash
cp .env.example .env
# edit .env — every CHANGE_ME value must be replaced
```

`.env` is git-ignored. Never commit real credentials.

## Database migrations

Migrations read the database URL from `DATABASE_URL`:

```bash
export DATABASE_URL='postgresql+psycopg://centralpay:devpassword@localhost:5432/centralpay'
alembic upgrade head
```

## Running the API

```bash
uvicorn app.asgi:app --host 127.0.0.1 --port 8000 --no-access-log
```

`--no-access-log` is required in every environment that handles real
callbacks: uvicorn's access log prints full request lines including query
strings, which would leak callback signatures. The application logs each
request itself (method, path, status, request id — never the query string).

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /api/custom-payment` | Create a payment; body `{"api_key", "amount", "order_id"}`; returns `{"url": "..."}` |
| `GET /api/centralpay/callback?orderId=...&ct=...&sig=...` | Signed CentralPay return URL; triggers verification and returns a payer-facing status page |
| `GET /health/live` | Liveness probe |
| `GET /health/ready` | Readiness probe with a real database connectivity check |

### Payment-creation request contract (strict)

The request schema rejects anything outside this contract with a generic
`422 validation_error` (field contents are never echoed back):

- `api_key` — string. Compared in constant time; never logged, never
  included in errors.
- `amount` — JSON **integer**, TOMAN. Booleans, floats, and numeric
  strings are rejected, never coerced. Policy bounds are
  `MIN_PAYMENT_AMOUNT_TOMAN` / `MAX_PAYMENT_AMOUNT_TOMAN` (checked after
  authentication, error `amount_out_of_range`); the schema additionally
  enforces an absolute backstop of 10¹² TOMAN.
- `order_id` — opaque non-empty string, at most 128 characters, no
  control characters, no NUL. Passed through unchanged: never trimmed,
  case-folded, or Unicode-normalized.

### Idempotency contract

Creation is idempotent by `order_id`, serialized with a database row
lock (the lock is held across the CentralPay getLink call — model A —
so two concurrent requests can never both call getLink for one order):

- Same `order_id` + same `amount` with a live link → the **same URL** is
  returned (`payment_duplicate_returned` log event); no new gateway
  call, no new callback token.
- Same `order_id` + **different amount** → `409
  duplicate_order_amount_mismatch` (audited); the stored payment is
  never modified.
- Already gateway-verified order → `409 order_already_verified`; a new
  link is **never** issued for a verified payment (this also covers
  verified payments later moved to manual review).
- Never-verified order under manual review → `409 order_under_review`;
  state is never silently reset.
- After a getLink failure (including ambiguous timeouts) → the retry
  issues a fresh gateway order id and a fresh one-time callback token;
  the possibly-half-registered previous id is abandoned, and tokens from
  superseded attempts are rejected at callback time. **Link refresh is
  driven by the bot re-requesting the same `order_id`** — an unpaid
  link's token stays valid until a retry durably commits its
  replacement (token and redirect URL always commit atomically).
- Gateway order ids are random 12-digit integers under a database
  unique index — guess-resistant, no sequence to drift or reset after a
  backup restore.

A crash between a successful getLink and our commit loses the invoice
reference atomically (neither token nor URL is stored); the orphaned
CentralPay invoice is unreachable (its URL was never returned to
anyone), and the bot's retry recovers with a fresh link.

## Bot notification (Phase 2)

### gateway_verified vs bot_notify_accepted

These are different facts and must never be conflated:

- **Gateway verified** (`gateway_verified_at` set): CentralPay confirmed the
  money movement, and amount / userId / referenceId matched our records.
- **`bot_notify_accepted`**: the bot API answered HTTP 2xx to our
  notification. The bot API defines no response schema and no idempotency
  guarantee, so **HTTP 2xx only means the request was accepted — it is not
  proof the user balance was credited.** The bridge never records a
  "balance_credited" state, because it cannot know that.

### Payment states

`created` → `link_created` (→ `getlink_failed`) → `bot_notify_pending`
→ `bot_notify_accepted`, with `manual_review` reachable from verification
mismatches and delivery failures. `gateway_verified` exists as a transient /
legacy state: since Phase 2, verification commits straight to
`bot_notify_pending` (the durable verification fact is
`gateway_verified_at`).

Every non-success state carries an explicit machine-readable reason code in
`bot_notify_reason`, stored separately from human-readable `last_error` text.

### Reason codes

| Code | Meaning | Effect (safe mode) |
| --- | --- | --- |
| `bot_notify_accepted` | Bot API returned 2xx | terminal success |
| `bot_dns_failed` | DNS resolution failed before connecting | retry with backoff |
| `bot_connection_refused` | Connection refused | retry with backoff |
| `bot_connection_failed` | Connection could not be established | retry with backoff |
| `bot_http_500` / `502` / `503` / `504` | Bot server error | retry with backoff |
| `bot_http_429` | Rate limited | retry (honors integer `Retry-After`) |
| `bot_timeout_ambiguous` | Timeout after the request may have been transmitted | `manual_review` |
| `bot_http_400/401/403/404/409/422` | Client-side rejection | `manual_review` |
| `bot_http_other` | Unexpected status (3xx, 418, 501, …) | `manual_review` |
| `bot_invalid_configuration` | Notification URL/token missing | `manual_review` |
| `retry_limit_reached` | `BOT_NOTIFY_MAX_ATTEMPTS` exhausted | `manual_review` |
| `manual_review_required` | Generic review marker on audit events | — |

### Retry modes

- **`BOT_NOTIFY_RETRY_MODE=safe` (default):** only failures that clearly
  happened before the bot could have processed the request are retried
  (DNS, connection refused/failed, 5xx listed above, 429). An ambiguous
  read/write timeout — where the bot may already have credited the user —
  is **never** retried automatically; the payment moves to `manual_review`
  with reason `bot_timeout_ambiguous` and a critical audit event.
- **`BOT_NOTIFY_RETRY_MODE=idempotent`:** ambiguous timeouts are retried
  too. Enable this **only** when the bot developer has explicitly confirmed
  that duplicate `order_id` deliveries are idempotent.

Backoff schedule: 1, 2, 5, 10, 30, 60 minutes (±15% jitter), then
`manual_review` with `retry_limit_reached` after `BOT_NOTIFY_MAX_ATTEMPTS`
attempts (default 6). Retries and recovery survive process restarts.

### What manual_review means

The bridge could not safely decide the outcome on its own (verification
mismatch, ambiguous delivery, non-retryable bot rejection, retry limit).
The payment is frozen — never auto-retried, never overwritten — and its
full attempt history is preserved in `payment_events`. An administrator
must inspect it (`python -m app.cli manual-review`) and resolve it
manually. Administrator tooling for resolution arrives in later phases.

### Worker lifecycle and recovery guarantees (audit)

The delivery pipeline is a strict four-step sequence designed around
crash windows (documented model; every step below is regression-tested):

1. The **callback transaction** commits the verified fact together with
   `bot_notify_pending` and the queue timestamp — atomically.
2. A worker **claims** one due payment (`FOR UPDATE SKIP LOCKED`,
   attempt counter incremented, `bot_notification_started` audit event)
   and **commits the claim**.
3. The HTTP request to the bot runs with **no database transaction
   open** — locks are never held across external I/O in the worker.
4. The classified result is recorded in a **new transaction**, and only
   if the row still carries **this worker's claim at this attempt
   number** — a straggler whose attempt outlived its claim can never
   record a result against a successor's claim (the discard itself is
   audited as `bot_notification_result_discarded`).

Recovery guarantees:

- **Nothing lives in process memory.** Queue state, retry schedule, and
  attempt history are all in PostgreSQL; any worker restart (SIGTERM,
  SIGINT, container kill, crash) resumes from the database.
- **Stale claims** (a worker died mid-attempt) are recovered on every
  pass, in bounded batches: the interrupted attempt's outcome is
  unknown, so safe mode routes it to manual review as an ambiguous
  delivery; idempotent mode requeues it with backoff.
- **Retries are bounded in every path**: failed attempts AND interrupted
  attempts count against `BOT_NOTIFY_MAX_ATTEMPTS`; when the limit is
  reached — including via repeated stale-claim recovery — the payment
  moves to manual review with `retry_limit_reached`. Nothing retries
  forever, and nothing is silently dropped.
- **Manual review is terminal for the worker**: passes never select
  `manual_review` payments, callbacks never reset them, and duplicate
  create requests never reset them.
- Multiple workers are safe: `SKIP LOCKED` guarantees a payment is
  claimed by at most one worker; ordering is deterministic
  (`next_retry_at` ascending — oldest due first, so retries and new
  payments share one fair queue and neither starves); batches are
  bounded (20 per pass).

### Running the worker locally

```bash
python -m app.worker
```

The worker validates `BOT_PAYMENT_NOTIFY_URL` and `BOT_NOTIFY_TOKEN` at
startup (refusing to run without them, without ever logging their values),
polls every `BOT_NOTIFY_WORKER_INTERVAL_SECONDS`, claims due payments with
`FOR UPDATE SKIP LOCKED` (safe with multiple workers), sends each
notification with **no database transaction open**, and records the
classified result in a new transaction. Claims older than
`BOT_NOTIFY_CLAIM_TIMEOUT_SECONDS` (a worker crashed mid-attempt) are
recovered on every pass: manual review in safe mode, requeue in idempotent
mode.

### Inspecting payments and the retry queue

```bash
python -m app.cli recent --limit 20   # newest payments
python -m app.cli payment ORDER_ID    # one payment + full audit history
python -m app.cli retry-queue         # pending deliveries with next_retry_at
python -m app.cli manual-review       # payments waiting for an administrator
```

All commands are read-only and print one JSON object per line with order
IDs, verification status, delivery status, reason code, attempt count, last
HTTP status, next retry time, reference ID, and timestamps.

## Dynamic service fee (percentage)

The bridge can add a percentage service fee on top of the bot's invoice.
The fee is **paid by the payer through the gateway** and is invisible to
the selling bot: the bot's request, the bot's credited amount, and the bot
notification payload are all unchanged.

### Money model

| Field | Meaning |
| --- | --- |
| `payments.amount` | The ORIGINAL bot invoice — exactly what the bot sent and what the bot credits. Never includes the fee. |
| `payments.fee_rate_bps` | Fee percentage snapshot in basis points (10% = 1000 bps). |
| `payments.fee_amount` | Fee in TOMAN: `(amount * fee_rate_bps + 5000) // 10000` — pure integer arithmetic, round half up. Floats are never used for money. |
| `payments.payable_amount` | `amount + fee_amount` — what CentralPay is asked to charge (`getLink` amount) and what `verify` must report back. |
| `payments.fee_policy_id` | The `fee_policies` row the snapshot came from (`NULL` for pre-fee/zero-fee payments). |

Example: the bot requests 500 000 TOMAN with a 10% fee active → fee 50 000,
CentralPay charges 550 000, verify must report 550 000, and the bot is told
only `order_id` — it credits its own original 500 000 invoice.

Database `CHECK` constraints enforce the arithmetic at the storage layer
(`payable_amount = amount + fee_amount`, rate within 0..10000, fee ≥ 0),
and `centralpay db-check` additionally reports snapshot corruption (it
never alters financial fields).

### Snapshot immutability

The fee is **snapshotted once, at payment creation**, in the same
transaction that inserts the row (audit event `payment_fee_snapshotted`).
After that it never changes:

- Fee policy changes affect **new orders only**; existing payments keep
  their snapshot forever.
- Idempotent duplicate requests return the existing link with the original
  snapshot — even if the fee changed in between.
- A retry after `getlink_failed` keeps the original snapshot and re-sends
  the stored `payable_amount`.

### Verification and bounds

- `verify` must report exactly `payable_amount`; anything else (including
  the original amount, i.e. a fee that was not charged) routes the payment
  to `manual_review` with the `verify_payable_amount_mismatch` event. The
  bot is never notified for such a payment.
- `MIN_PAYMENT_AMOUNT_TOMAN` bounds the **original** amount;
  `MAX_PAYMENT_AMOUNT_TOMAN` bounds the **final payable** amount. If
  `amount + fee` would exceed the maximum, creation is rejected with
  `payable_amount_out_of_range` (HTTP 400) before any row, snapshot, or
  gateway call — the fee is never silently clamped or reduced.

### Fee policies (append-only, no restarts)

Policies live in the `fee_policies` table — never in an environment
variable — so every API/worker replica observes changes through
PostgreSQL, and backups capture the full history. The table is
append-only: policies are added or cancelled, never edited or deleted.
The active policy is selected deterministically: highest `effective_at`
not in the future, ties broken by highest `id`, cancelled rows excluded.
A scheduled policy activates at exactly its `effective_at` with no
restart. All changes are recorded as permanent audit events
(`fee_policy_created` / `fee_policy_scheduled` / `fee_policy_cancelled`).

### Operating the fee

```bash
centralpay fee status                                # current + next scheduled
centralpay fee set 10 --note "launch fee"            # root only
centralpay fee schedule 2.5 --at 2026-08-01T00:00:00+03:30 --note "summer"
centralpay fee history                               # full append-only history
centralpay fee cancel 3 --note "wrong rate"          # cancel a SCHEDULED policy only
```

Rates are 0–100 with at most two decimals; signs, scientific notation,
separators, and anything else are rejected. Mutations require root and
delegate to the typed Python ops command (`python -m app.ops fee ...`) —
never shell-generated SQL. The admin Telegram bot's `/fee` command is
strictly read-only. `fee cancel` refuses the currently effective policy
(and superseded history): cancelling it would silently fall back to an
older rate — change the current fee only with `fee set` (`fee set 0` to
remove it). The installer asks for the initial fee percentage (default
0) and applies it with `--ensure-initial`, which creates a policy only
when no policy row has ever existed (scheduled and cancelled history
count) — an installer re-run can never reset or inject a fee, and
concurrent reruns are serialized by a database advisory lock.

**Operator obligation:** the fee is charged to the payer, so the payer
must be told the final payable amount before paying. Disclose the fee in
the bot's purchase flow before issuing the payment link.

## Tests

Unit tests (SQLite in-memory, CentralPay mocked at the HTTP transport layer):

```bash
pytest
```

PostgreSQL integration tests (migration on an empty database, full payment
flow, concurrent callback locking) require `TEST_DATABASE_URL` pointing at a
disposable database:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://centralpay:devpassword@localhost:5432/centralpay_test'
pytest -m postgres
```

Without `TEST_DATABASE_URL`, the `postgres`-marked tests are skipped.

## Lint and type checking

```bash
ruff check .
mypy app tests
```

## Configuration reference

See [.env.example](.env.example) for the full list. Notable values:

| Variable | Meaning |
| --- | --- |
| `PUBLIC_BASE_URL` | Public HTTPS **origin** of the bridge (`https://host[:port]` — no path/query/fragment/userinfo; cleartext HTTP rejected at startup); used to build the signed CentralPay return URL |
| `INBOUND_API_KEY` | Key the bot must send in `POST /api/custom-payment` (min 16 chars) |
| `CALLBACK_HMAC_SECRET` | Secret for HMAC-SHA256 callback signatures (min 16 chars) |
| `CENTRALPAY_GETLINK_API_KEY` / `CENTRALPAY_VERIFY_API_KEY` | CentralPay web service key. The gateway issues a **single** API key used for both getLink and verify — the installer asks for it once and sets the same value in both variables (kept separate so a future split key needs no contract change) |
| `CENTRALPAY_USER_ID` | Numeric userId sent to getLink and validated on verify |
| `BOT_PAYMENT_NOTIFY_URL` | Complete bot payment endpoint (e.g. `https://bot.example.com/api/payment`). HTTPS required by default; validated strictly (no userinfo/query/fragment; path stored exactly) |
| `ALLOW_INSECURE_BOT_NOTIFY_URL` | Default `false`. When `true`, cleartext `http://` is allowed ONLY for private/internal hosts (localhost, private IP literals, single-label service names, `*.internal`/`*.local`) — for isolated mock-bot networks; the `Token` header then crosses without TLS. Public hosts stay rejected. No DNS is consulted |
| `CENTRALPAY_BASE_URL` | CentralPay service base (default `https://centralapi.org/webservice/basic`). Always HTTPS — no insecure exception; the API key travels in request bodies |
| `BOT_NOTIFY_TOKEN` | Bot `Token` header value; never logged |
| `BOT_NOTIFY_RETRY_MODE` | `safe` (default) or `idempotent` — see retry modes above |
| `BOT_NOTIFY_MAX_ATTEMPTS` | Attempts before `manual_review` with `retry_limit_reached` (default 6) |
| `BOT_NOTIFY_CONNECT_TIMEOUT_SECONDS` / `BOT_NOTIFY_READ_TIMEOUT_SECONDS` | Bot HTTP timeouts (5 / 15) |
| `BOT_NOTIFY_WORKER_INTERVAL_SECONDS` | Worker poll interval (default 10) |
| `BOT_NOTIFY_CLAIM_TIMEOUT_SECONDS` | Stale-claim threshold; must exceed connect+read timeouts (default 120) |
| `MIN_PAYMENT_AMOUNT_TOMAN` / `MAX_PAYMENT_AMOUNT_TOMAN` | Enforced amount bounds (defaults 1 000 / 100 000 000) |
| `TELEGRAM_BOT_USERNAME` | Optional; adds a "return to bot" link to payer pages |
| `LOG_FORMAT` | `json` (default) or `text`; both redact secrets |
| `CALLBACK_SECRET` | Accepted alias for `CALLBACK_HMAC_SECRET` |
| `ADMIN_BOT_ENABLED` | Admin Telegram bot (default `false`); see [ADMIN_BOT_FA.md](ADMIN_BOT_FA.md) |
| `ADMIN_BOT_TOKEN` / `ADMIN_TELEGRAM_IDS` | BotFather token + comma-separated numeric admin IDs |
| `ADMIN_BOT_*_ALERTS` | Per-category alert toggles (payment-success off by default) |
| `ADMIN_BOT_DAILY_REPORT_*` / `ADMIN_BOT_TIMEZONE` | Daily report time and timezone (Asia/Tehran) |

## Security notes

- Secrets live only in environment variables / `.env` (git-ignored).
- All configured secret values are redacted from log output as a backstop;
  code paths additionally never log keys, signatures, full card numbers, full
  redirect URLs, or callback query strings.
- Only the final four card digits are ever stored.
- Financial state transitions run inside database transactions with row
  locking (`SELECT ... FOR UPDATE`) and are each recorded in the permanent
  `payment_events` audit table.
