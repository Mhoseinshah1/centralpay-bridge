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

- **Fee integrity (dynamic fee):** the service fee is snapshotted
  immutably at payment creation (`fee_amount = (amount * rate_bps +
  5000) // 10000`, integer-only); the gateway is asked to charge
  `payable_amount = amount + fee` and verification requires the gateway to
  report exactly that payable amount — a fee that was not actually
  collected freezes the payment for manual review
  (`verify_payable_amount_mismatch`), it is never silently accepted.
  Fee policies can be changed only by root on the host via
  `centralpay fee` (typed Python delegation, strict 0–100 two-decimal
  rate grammar, no shell-generated SQL); the admin Telegram bot is
  read-only; history is append-only and permanently audited. The bot
  notification payload carries no amounts, so a compromised transport
  could not be used to alter what the bot credits.
- **Verification before trust:** payments are only marked verified after
  CentralPay `verify` succeeds AND amount / userId / referenceId match our
  records; anomalies freeze the payment for manual review.
- **Conservative delivery:** HTTP 2xx from the bot API is recorded as
  "accepted", never as proof of balance credit; ambiguous deliveries are
  never retried automatically in the default `safe` mode.
- **Authentication:** constant-time comparison for the inbound API key and
  for HMAC-SHA256 callback signatures; signatures are validated before any
  database or gateway work.
- **Strict creation schema (audit):** `POST /api/custom-payment` accepts
  only a string `api_key`, a JSON-integer `amount` (booleans, floats, and
  numeric strings are rejected, never coerced; absolute schema backstop
  10¹² TOMAN below BIGINT), and an opaque `order_id` (≤128 chars, no
  control characters or NUL — NUL previously reached PostgreSQL and caused
  a 500). Malformed requests are rejected with a generic
  `validation_error` that never echoes field contents, and create no
  payment rows, no audit rows, and no gateway traffic. `order_id` is never
  trimmed, case-folded, or Unicode-normalized. Authentication runs before
  any order lookup, so unauthenticated callers cannot enumerate orders,
  and the `payment_create_requested` log event is emitted only after
  authentication.
- **Callback replay protection (0.5.0-rc1):** every payment link embeds a
  one-time token covered by the HMAC signature; only the token's SHA-256
  hash is stored, superseded tokens are rejected under the row lock before
  the gateway is contacted, and verified payments short-circuit to their
  final result without re-verification.
- **Strict gateway parsing (0.5.0-rc1):** CentralPay responses are accepted
  only on an explicit success marker; financial fields are parsed with
  typed coercion and malformed values route to manual review with explicit
  reason codes — success is never inferred from truthy values.
- **Gateway-controlled data policy (audit):** every byte of a gateway
  response body (message text, HTML, JSON values) is treated as
  attacker-influenceable content. It is classified inside
  `app/centralpay.py` into a fixed internal reason-code vocabulary
  (`gateway_rejected`, `gateway_response_invalid`, `gateway_missing_data`,
  `gateway_invalid_redirect_url`, `gateway_invalid_reference_id`,
  `gateway_invalid_amount`, `gateway_invalid_user_id`) and then discarded —
  raw gateway text never reaches logs, exceptions, audit events, stored
  errors, or API responses. Gateway logs carry only the endpoint name, the
  order id, the HTTP status, the internal reason code, and a fixed-value
  marker naming which failure signal was present.
- **Reference-ID storage boundary (fix/centralpay-reference-id-validation):**
  gateway-reported reference IDs are validated against the exact database
  storage contract (max 128 characters, no NUL/control characters) before
  any query, assignment, audit event, or log use. Invalid values are never
  truncated and route the payment to manual review without bot
  notification; the raw invalid value never leaves the CentralPay client
  module. (No claim is made that real CentralPay has returned such a
  value — this is defense at the trust boundary.)
- **Redirect URL validation policy (audit):** the `redirectUrl` returned by
  getLink is parsed with `urllib.parse.urlsplit` (never substring checks)
  and accepted only when it is HTTPS with a non-empty hostname, carries no
  userinfo credentials, contains no whitespace or control characters, has
  a well-formed port, and is at most 2048 characters. HTTPS-only is a
  deliberate decision: CentralPay serves its payment pages over HTTPS, and
  an `http://` redirect would downgrade the payer to cleartext.
- **Rate limiting (0.5.0-rc1):** application-level sliding windows for
  invalid API keys, invalid callback signatures, and payment-create bursts;
  `X-Forwarded-For` is never trusted for limiter identity. Limiters are
  per-process and in-memory (documented limitation).
- **Update integrity (0.5.0-rc1):** `centralpay update` pins a release tag
  by default and verifies the published `SHA256SUMS` before deploying;
  rollback is application-only — the database schema is never downgraded.
- **Backup integrity (audit):** backups are created atomically
  (`.partial` → validate → rename), validated before the `.ok` marker
  exists (non-empty, `PGDMP` magic, `pg_restore --list`), and carry an
  atomically-written SHA-256 manifest sidecar (no secrets, no payment
  data). Restores refuse symlinks, verify the checksum before any
  destructive action (legacy files need an explicit `RESTORE-LEGACY`
  confirmation that `--yes` cannot bypass), hold an exclusive lock shared
  with the backup job, stop every writer including the admin bot, run
  `pg_restore --exit-on-error`, and gate service startup behind a
  post-restore integrity check (`centralpay db-check`) with sequence
  repair. A mid-restore failure leaves services stopped with exact
  recovery instructions — never running against a half-restored database.
  Backup files and manifests are 0600 in a 0700 directory; the backup
  script never reads or logs database credentials.
- **Secret handling:** secrets live in `/etc/centralpay-bridge/` (0700
  directory, 0600 files), outside the Git checkout; `.env` files are
  git-ignored; a log-redaction backstop strips every configured secret from
  all log output; access logs redact callback signatures; only the final
  four card digits are ever stored.
- **Network exposure:** only Caddy publishes ports 80/443; the API and
  PostgreSQL are reachable solely on internal Docker networks; TLS is
  automatic; the Caddy admin API is disabled; only the four public routes
  are proxied. Since the deployment audit the networks are split: Caddy
  sits on an **edge** network that reaches only the API, and PostgreSQL
  sits on the **internal** network that Caddy cannot reach at all.
- **Runtime:** non-root containers (fixed UID/GID 10001), pinned-bounded
  dependencies, log rotation, health-gated deployments (API/worker never
  start on a failed migration). The api, worker, migrate, and admin-bot
  services all run with a read-only root filesystem, tmpfs `/tmp`,
  `cap_drop: ALL`, and `no-new-privileges`; db and caddy keep the vendor
  capabilities they require but also run with `no-new-privileges`.
- **Container trust boundary (audit):** no Docker socket, no privileged
  containers, no host network/PID/IPC, no broad host mounts anywhere.
  Per-service secrets are minimized: Caddy receives no application env
  file or secrets; the worker has CentralPay keys, the inbound API key,
  and the callback HMAC secret masked (it needs only the database URL and
  bot-notify settings); the admin bot masks everything payment-related.
  **Impact of a compromised container:** Caddy → can reach only the API's
  public routes (no DB route, no secrets); worker → can read/write the
  database and the bot token but cannot forge callbacks or talk to
  CentralPay; API → the widest (DB + gateway keys + HMAC), which is why
  the callback/creation paths carry the strictest validation; none of
  them can touch Docker, the host filesystem, deployment configuration,
  or the backups directory. Access logs redact both the callback
  signature (`sig`) and the one-time token (`ct`).
- **Update trust model:** production updates pin a release tag; the
  published artifact checksum (SHA256SUMS) is verified before deployment,
  which then happens via `git checkout` of the fetched ref over HTTPS —
  no archive is ever extracted, so archive path-traversal/symlink attacks
  have no surface. Signed tags remain pre-1.0 backlog. Rollback reuses
  the previously recorded local version, never touches configuration or
  secrets, and never downgrades the schema.
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

Every deferred review topic has been triaged in
[RELEASE_RISK_REGISTER.md](RELEASE_RISK_REGISTER.md) (fixed / accepted
risk / release blocker / backlog), with the original list preserved in
[DEFERRED_REVIEW.md](DEFERRED_REVIEW.md). The multi-agent adversarial
review has not been completed; production deployment is blocked until it
is. Other open blockers: real-host installer validation, staging
validation of the real CentralPay response schema, and live Telegram
validation for the optional admin bot. Notable accepted risks: proxy-level
rate limiting absent (app-level limits added in 0.5.0-rc1), base images
tag-pinned rather than digest-pinned.

## Supported versions

Pre-release (`0.x`) versions receive fixes on `main` only. There is no
stable release yet.
