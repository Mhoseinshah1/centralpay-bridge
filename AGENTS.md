# CentralPay Bridge — Master Project Contract

## Mission

Build a production-grade payment bridge between a Telegram bot custom gateway API and CentralPay.

Priorities, in order:

1. Financial correctness
2. Security
3. Reliability
4. Recoverability
5. Observability

Availability is less important than financial correctness.

## Supported deployment targets

- Ubuntu Server 22.04 LTS, 24.04 LTS, and 26.04 LTS
- amd64 and arm64
- Docker Engine and Docker Compose plugin
- One-line interactive installer

Required installation experience:

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

## Required public endpoints

### Create payment

`POST /api/custom-payment`

Request from the bot:

```json
{
  "api_key": "API",
  "amount": 10000,
  "order_id": "string-order-id"
}
```

Response:

```json
{
  "url": "https://payment-url"
}
```

### CentralPay callback

`GET /api/centralpay/callback?orderId=...&sig=...`

### Health checks

- `GET /health/live`
- `GET /health/ready`

## CentralPay integration

### getLink

POST JSON to:

`https://centralapi.org/webservice/basic/getLink.php`

Fields:

- `api_key`: string
- `type`: always `deposit`
- `amount`: integer, TOMAN
- `userId`: integer
- `orderId`: integer
- `returnUrl`: string containing `orderId`

Successful response contains `data.redirectUrl`.

### verify

POST JSON to:

`https://centralapi.org/webservice/basic/verify.php`

Fields:

- `api_key`: string
- `orderId`: integer

On success validate:

- `referenceId`
- `amount`
- `userId`

Never notify the bot before CentralPay verification succeeds.
Never send verify again after a transaction is already marked successfully verified.

## Order IDs

- Bot `order_id` may be a string.
- CentralPay `orderId` must be an integer.
- Store both.
- Generate a unique numeric gateway order ID.
- Preserve the original bot order ID for notification.

## Required payment states

At minimum:

- `created`
- `link_created`
- `getlink_failed`
- `gateway_verified`
- `bot_notify_pending`
- `bot_notify_accepted`
- `manual_review`

## Financial safety rules

Financial correctness is more important than availability.

Never:

- mark a payment successful before CentralPay verification
- notify the bot before CentralPay verification
- silently ignore financial errors
- delete audit events
- overwrite successful payment records
- perform automatic balance changes outside the documented bot API
- retry ambiguous bot deliveries automatically in safe mode

Every financial state transition MUST be stored in `payment_events`.

Successful payment records are immutable except for appending audit information.

If a payment reaches `manual_review`, it must remain recoverable by an administrator.

Use database transactions and row locking for financial state transitions.

## Bot notification

After successful verification, send:

`POST https://BOT_DOMAIN/api/payment`

Headers:

```text
Token: BOT_TOKEN
Content-Type: application/json
```

Body:

```json
{
  "order_id": "original-bot-order-id",
  "actions": "custom_payment_verify"
}
```

The bot documentation does not define a response schema or guarantee idempotency.

Therefore use conservative delivery semantics:

- HTTP 2xx means `bot_notify_accepted`, not guaranteed balance credit.
- Do not automatically retry after an ambiguous timeout unless retry mode is explicitly configured as idempotent.
- Retry safe pre-send connection failures and selected 5xx responses.
- Move ambiguous cases to `manual_review`.

## Retry policy

Default mode:

```env
BOT_NOTIFY_RETRY_MODE=safe
```

Safe mode behavior:

- DNS resolution failure before delivery: retry
- connection refused before delivery: retry
- HTTP 500, 502, 503, 504: retry with bounded backoff
- HTTP 4xx: do not retry automatically; record exact reason and move to review when appropriate
- ambiguous timeout after request transmission may have begun: `manual_review`
- HTTP 2xx: `bot_notify_accepted`; never automatically resend

Optional mode:

```env
BOT_NOTIFY_RETRY_MODE=idempotent
```

Only enable this when the bot developer has explicitly confirmed duplicate `order_id` delivery is idempotent.

## Audit trail

Create a permanent table named `payment_events`.

Store:

- `payment_id`
- `event_type`
- `level`
- `request_id`
- `data`
- `created_at`

Every financial state transition must be recorded.
Audit data must not be silently deleted.

## Logging and observability

Use structured JSON logs and shared request IDs across reverse proxy, API, worker, and audit events.

Required event names include:

- `payment_created`
- `payment_link_created`
- `centralpay_getlink_failed`
- `callback_received`
- `centralpay_verify_failed`
- `verify_amount_mismatch`
- `verify_user_id_mismatch`
- `gateway_payment_verified`
- `bot_connection_failed`
- `bot_timeout_ambiguous`
- `bot_http_4xx`
- `bot_http_5xx`
- `bot_notify_accepted`
- `manual_review_required`
- `backup_failed`
- `service_unhealthy`

Never log:

- API keys
- bot token
- database passwords
- callback secret or callback signature
- full card number
- request bodies containing secrets
- full payment redirect URL
- full callback query string

Only the final four card digits may be stored, if necessary.

Implement:

- `/health/live`
- `/health/ready`
- request IDs
- permanent audit history
- diagnostics command
- explicit failure reasons

## Security requirements

Use:

- HMAC callback signing
- TLS
- SQLAlchemy parameterized queries
- database transactions
- row locking where required
- secrets outside the repository
- constant-time secret comparison
- least-privilege containers and service users
- secure file permissions

Forbidden:

- disabling SSL verification
- committing `.env` files
- committing production credentials
- logging secrets
- force-pushing to `main`
- bypassing tests
- using SQLite in production

## Database

Required production database:

- PostgreSQL

Technology:

- SQLAlchemy 2
- Alembic

SQLite may only be used in isolated unit tests when the test does not depend on PostgreSQL behavior. Financial integration tests must use PostgreSQL.

## Deployment architecture

Preferred Docker Compose services:

- `api`
- `worker`
- `db`
- `admin-bot`
- `caddy`

Preferred TLS reverse proxy:

- Caddy with automatic certificate issuance and renewal

The API and database must not expose unnecessary public ports.

## Interactive installer

The installer must ask for:

- payment domain
- bot API domain or complete bot payment endpoint
- CentralPay getLink API key
- CentralPay verify API key
- bot `/token2`
- Telegram bot username
- SSL email
- whether the optional admin Telegram bot should be enabled
- admin bot token and allowed Telegram IDs when enabled

The installer must generate:

- inbound API key
- callback HMAC secret
- PostgreSQL password

Store credentials outside the repository under:

```text
/etc/centralpay-bridge/
```

Credential files must have mode `0600`.

At completion print:

- custom payment API URL
- generated inbound API key
- callback URL
- health URL
- status command
- logs command
- diagnose command
- credentials file location

The installer must validate:

- root privileges
- supported Ubuntu version
- amd64 or arm64 architecture
- available disk space
- required ports
- domain format
- DNS readiness when possible
- Docker installation
- service health after startup

If DNS is not ready, installation may finish without TLS but must clearly instruct the administrator to run `centralpay ssl` later.

## Management command

Install a `centralpay` command supporting:

- `status`
- `logs`
- `logs-errors`
- `restart`
- `update`
- `backup`
- `restore`
- `diagnose`
- `payment ORDER_ID`
- `recent`
- `credentials`
- `ssl`
- `uninstall`

Destructive commands must ask for confirmation unless explicitly passed a documented non-interactive flag.

## Backups

Implement automatic PostgreSQL backups with configurable retention.

Required capabilities:

- create backup
- list backups
- verify backup readability
- restore to a new database first
- documented production restore procedure

Never overwrite the production database without an explicit confirmation step.

## Optional administrator Telegram bot

The project must include an optional administrator-only Telegram bot as a separate service.

Purpose:

- payment alerts
- stuck transaction alerts
- health notifications
- backup notifications
- operational reports

Authentication rules:

- allow only configured numeric Telegram user IDs
- never trust Telegram usernames for authorization
- support multiple administrators

Environment variables:

```env
ADMIN_BOT_ENABLED=false
ADMIN_BOT_TOKEN=
ADMIN_TELEGRAM_IDS=
```

Initial read-only commands:

- `/start`
- `/status`
- `/health`
- `/recent`
- `/stuck`
- `/errors`
- `/payment ORDER_ID`
- `/backup_status`
- `/retry_queue`
- `/manual_review`

Initial alerts:

- payment verified
- manual review required
- ambiguous bot timeout
- repeated bot delivery failure
- callback signature failures above a threshold
- backup failure
- service unhealthy

The first production version of the admin bot must be read-only.

Do not implement a Telegram `/retry` command until retry safety and authorization are separately reviewed.

Never send through Telegram:

- API keys
- database passwords
- callback secrets
- callback signatures
- bot tokens
- full card numbers
- full redirect URLs

## Testing requirements

Tests must cover at minimum:

- invalid inbound API key
- duplicate order ID
- duplicate order with different amount
- getLink success and rejection
- network and timeout failures
- invalid callback signature
- verify success
- amount mismatch
- user ID mismatch
- missing reference ID
- repeated callback after successful verification
- concurrent callback handling
- bot HTTP 2xx semantics
- bot HTTP 4xx handling
- bot HTTP 5xx handling
- ambiguous timeout handling in safe mode
- idempotent retry mode
- audit event creation
- secret redaction in logs
- readiness health check

## CI/CD quality gates

GitHub Actions must run:

- pytest unit tests
- PostgreSQL integration tests
- linting with Ruff
- type checking with mypy or pyright
- ShellCheck
- Docker image build
- dependency vulnerability scan
- secret scan

Nothing may merge while required checks fail.

No production secret may be committed.

## Development policy

DO NOT IMPLEMENT EVERYTHING AT ONCE.

Implementation order:

### Phase 1 — Core payment API

- FastAPI structure
- configuration
- PostgreSQL models
- Alembic migrations
- payment creation
- CentralPay getLink
- signed callback
- CentralPay verify
- amount and user ID validation
- health endpoints
- tests

### Phase 2 — Bot notification and reliability

- bot notification
- conservative delivery states
- retry policy
- worker
- permanent audit events
- manual review flow
- tests

### Phase 3 — Deployment and operations

- Docker images
- Docker Compose
- Caddy
- interactive installer
- management command
- backups
- update, restore, diagnose, and uninstall
- tests

### Phase 4 — Admin Telegram bot

- read-only admin bot
- alerts
- reporting
- stuck and manual-review inspection
- tests

### Phase 5 — CI/CD, hardening, and documentation

- complete workflows
- security review
- English and Persian documentation
- architecture diagram
- troubleshooting guide
- end-to-end installation test

Do not start the next phase until the previous phase passes its tests and review.

Prefer small reviewable pull requests over large direct changes.

## Completion criteria

The project is not complete until:

- unit tests pass
- PostgreSQL integration tests pass
- lint passes
- type checking passes
- ShellCheck passes
- Docker images build
- documentation is current
- no secrets are committed
- installer has been tested on supported Ubuntu targets
- payment flow has been tested end-to-end
- ambiguous bot delivery cannot silently be marked as credited
- every financial state transition is present in `payment_events`

Do not claim completion while any required check is failing.
