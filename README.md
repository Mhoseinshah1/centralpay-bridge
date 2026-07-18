# CentralPay Bridge

Production-grade payment bridge between a Telegram bot custom gateway API and
[CentralPay]. Priorities, in order: financial correctness, security,
reliability, recoverability, observability.

The authoritative project contract is [AGENTS.md](AGENTS.md). The delivery
roadmap is GitHub issue #1.

## Verification status

> Automated adversarial verification and the full test suite were deferred
> and have not yet been completed. This implementation must not be
> considered production-ready until those checks are completed.

Unresolved review topics are tracked in [DEFERRED_REVIEW.md](DEFERRED_REVIEW.md).

## Status

**Phase 1 — Core payment API** (this code):

- `POST /api/custom-payment` — payment creation for the bot, idempotent by
  `order_id`, authenticated with a constant-time API key comparison
- CentralPay `getLink` integration
- `GET /api/centralpay/callback` — HMAC-signed return URL, row-locked
  processing, CentralPay `verify` with amount / userId / referenceId
  validation
- `GET /health/live` and `GET /health/ready`
- Permanent `payment_events` audit trail
- Structured JSON logs with request IDs and secret redaction

Not yet implemented (later phases): bot notification, retry worker, Docker
deployment, installer, management command, backups, admin Telegram bot.

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
| `GET /api/centralpay/callback?orderId=...&sig=...` | Signed CentralPay return URL; triggers verification |
| `GET /health/live` | Liveness probe |
| `GET /health/ready` | Readiness probe with a real database connectivity check |

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

## Security notes

- Secrets live only in environment variables / `.env` (git-ignored).
- All configured secret values are redacted from log output as a backstop;
  code paths additionally never log keys, signatures, full card numbers, full
  redirect URLs, or callback query strings.
- Only the final four card digits are ever stored.
- Financial state transitions run inside database transactions with row
  locking (`SELECT ... FOR UPDATE`) and are each recorded in the permanent
  `payment_events` audit table.
