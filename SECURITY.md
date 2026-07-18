# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security
Advisories on this repository (Security → Report a vulnerability). Do not
open public issues for security reports. Include reproduction steps and
affected versions; you should receive an initial response within 7 days.

## Scope and priorities

This project moves money. Financial correctness outranks availability:
a report that shows a payment can be credited twice, marked verified without
CentralPay confirmation, or lost silently is always critical.

## Security posture (current)

- **Verification before trust:** payments are only marked verified after
  CentralPay `verify` succeeds AND amount / userId / referenceId match our
  records; anomalies freeze the payment for manual review.
- **Conservative delivery:** HTTP 2xx from the bot API is recorded as
  "accepted", never as proof of balance credit; ambiguous deliveries are
  never retried automatically in the default `safe` mode.
- **Authentication:** constant-time comparison for the inbound API key and
  for HMAC-SHA256 callback signatures; signatures are validated before any
  database or gateway work.
- **Secret handling:** secrets live in `/etc/centralpay-bridge/` (0700
  directory, 0600 files), outside the Git checkout; `.env` files are
  git-ignored; a log-redaction backstop strips every configured secret from
  all log output; access logs redact callback signatures; only the final
  four card digits are ever stored.
- **Network exposure:** only Caddy publishes ports 80/443; the API and
  PostgreSQL are reachable solely on the internal Docker network; TLS is
  automatic; the Caddy admin API is disabled; only the four public routes
  are proxied.
- **Runtime:** non-root containers, pinned-bounded dependencies, log
  rotation, health-gated deployments (API/worker never start on a failed
  migration).
- **Audit:** every financial state transition is recorded in the permanent
  append-only `payment_events` table; migrations refuse to drop it.
- **Admin Telegram bot (optional, off by default):** read-only; authorizes
  by numeric Telegram ID only (usernames never trusted), private chats
  only; unauthorized attempts get a generic denial and are audited. Alerts
  flow through a database outbox so Telegram can never block payments. The
  container is hardened (no ports, read-only filesystem, all capabilities
  dropped, no privilege escalation, no Docker socket). API keys, tokens,
  secrets, signatures, full card numbers, redirect URLs, raw external
  error text, and backup paths are never sent to Telegram.

## Known gaps

Unresolved review topics are tracked openly in
[DEFERRED_REVIEW.md](DEFERRED_REVIEW.md). The multi-agent adversarial
review has not been completed; production deployment is blocked until it
is. Notable open items include callback replay protection, verify response
schema confirmation against real CentralPay documentation, and rate
limiting at the proxy.

## Supported versions

Pre-release (`0.x`) versions receive fixes on `main` only. There is no
stable release yet.
