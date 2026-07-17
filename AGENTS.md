# CentralPay Bridge — Coding Agent Instructions

## Mission
Build a production-grade payment bridge between a Telegram bot custom gateway API and CentralPay.

## Deployment target
- Ubuntu Server 22.04, 24.04, and 26.04
- amd64 and arm64
- Docker Engine + Docker Compose plugin
- One-line interactive installer

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

Successful response contains:
`data.redirectUrl`

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

## Order IDs
- Bot `order_id` may be a string.
- CentralPay `orderId` must be an integer.
- Store both.
- Generate a unique numeric gateway order ID.
- Preserve the original bot order ID for notification.

## Payment states
At minimum:
- `created`
- `link_created`
- `getlink_failed`
- `gateway_verified`
- `bot_notify_pending`
- `bot_notify_accepted`
- `manual_review`

## Reliability
Implement:
- idempotent payment creation by original bot order ID
- callback HMAC signature
- database row locking
- amount and user ID validation
- permanent `payment_events` audit table
- structured JSON logs
- request IDs shared across proxy, API, and audit events
- retry worker with conservative and idempotent modes
- PostgreSQL backups
- health checks
- management CLI
- safe update, backup, restore, diagnose, and uninstall commands

## Logging rules
Never log:
- API keys
- bot token
- callback secret or callback signature
- full card number
- request bodies containing secrets
- full payment redirect URL

Log explicit failure reasons, including:
- `centralpay_getlink_failed`
- `centralpay_verify_failed`
- `verify_amount_mismatch`
- `verify_user_id_mismatch`
- `bot_connection_failed`
- `bot_timeout_ambiguous`
- `bot_http_4xx`
- `bot_http_5xx`
- `bot_notify_accepted`
- `manual_review_required`

## Installer
Required one-line installation experience:

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

The installer must ask interactively for:
- payment domain
- bot API domain
- CentralPay getLink API key
- CentralPay verify API key
- bot `/token2`
- Telegram bot username
- SSL email

The installer must generate:
- inbound API key
- callback HMAC secret
- PostgreSQL password

At completion print:
- custom payment API URL
- generated inbound API key
- callback URL
- health URL
- status command
- log command
- diagnose command

Store credentials with mode `0600` outside the repository.

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

## Technology
Preferred stack:
- FastAPI
- SQLAlchemy 2
- Alembic
- PostgreSQL
- httpx
- Pydantic Settings
- Docker Compose
- Caddy or Nginx with automatic TLS

## Quality gates
Before considering work complete:
- unit tests pass
- integration tests with PostgreSQL pass
- lint passes
- type checking passes
- ShellCheck passes
- Docker image builds
- no secrets are committed
- documentation is updated

## Development workflow
- First produce a detailed implementation plan.
- Implement in small reviewable changes.
- Prefer pull requests over direct changes to main.
- Do not claim completion while tests are failing.
