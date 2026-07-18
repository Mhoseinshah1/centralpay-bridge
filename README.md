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

The Phase 2 test suite, lint, type checking, and migration validation pass;
the multi-agent adversarial review remains outstanding. Unresolved review
topics are tracked in [DEFERRED_REVIEW.md](DEFERRED_REVIEW.md).

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

**Phase 2 — Bot notification and recovery** (this code):

- Safe delivery of verified payments to the bot API with explicit reason
  codes for every non-success state (no generic "stuck")
- Notification worker (`python -m app.worker`) with `FOR UPDATE SKIP LOCKED`
  claims, bounded exponential backoff with jitter, and stale-claim recovery
- `safe` (default) and `idempotent` retry modes
- Payer-facing callback status pages (verified+accepted / verified+pending /
  under review)
- Read-only inspection CLI (`python -m app.cli`)

Not yet implemented (later phases): Docker deployment, installer,
`centralpay` management command, backups, admin Telegram bot, CI workflows.

Persian documentation: [README_FA.md](README_FA.md).

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
| `GET /api/centralpay/callback?orderId=...&sig=...` | Signed CentralPay return URL; triggers verification and returns a payer-facing status page |
| `GET /health/live` | Liveness probe |
| `GET /health/ready` | Readiness probe with a real database connectivity check |

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
| `PUBLIC_BASE_URL` | Public HTTPS base URL of the bridge; used to build the signed CentralPay return URL |
| `INBOUND_API_KEY` | Key the bot must send in `POST /api/custom-payment` (min 16 chars) |
| `CALLBACK_HMAC_SECRET` | Secret for HMAC-SHA256 callback signatures (min 16 chars) |
| `CENTRALPAY_GETLINK_API_KEY` / `CENTRALPAY_VERIFY_API_KEY` | CentralPay web service keys |
| `CENTRALPAY_USER_ID` | Numeric userId sent to getLink and validated on verify |
| `BOT_PAYMENT_NOTIFY_URL` | Complete bot payment endpoint (e.g. `https://bot.example.com/api/payment`) |
| `BOT_NOTIFY_TOKEN` | Bot `Token` header value; never logged |
| `BOT_NOTIFY_RETRY_MODE` | `safe` (default) or `idempotent` — see retry modes above |
| `BOT_NOTIFY_MAX_ATTEMPTS` | Attempts before `manual_review` with `retry_limit_reached` (default 6) |
| `BOT_NOTIFY_CONNECT_TIMEOUT_SECONDS` / `BOT_NOTIFY_READ_TIMEOUT_SECONDS` | Bot HTTP timeouts (5 / 15) |
| `BOT_NOTIFY_WORKER_INTERVAL_SECONDS` | Worker poll interval (default 10) |
| `BOT_NOTIFY_CLAIM_TIMEOUT_SECONDS` | Stale-claim threshold; must exceed connect+read timeouts (default 120) |

## Security notes

- Secrets live only in environment variables / `.env` (git-ignored).
- All configured secret values are redacted from log output as a backstop;
  code paths additionally never log keys, signatures, full card numbers, full
  redirect URLs, or callback query strings.
- Only the final four card digits are ever stored.
- Financial state transitions run inside database transactions with row
  locking (`SELECT ... FOR UPDATE`) and are each recorded in the permanent
  `payment_events` audit table.
