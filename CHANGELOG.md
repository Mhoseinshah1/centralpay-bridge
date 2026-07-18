# Changelog

All notable changes to centralpay-bridge. Dates are UTC.

## [0.5.0-rc1] — 2026-07-18 (release candidate — NOT production-ready)

Release-candidate hardening. **Known release blockers are tracked in
`RELEASE_RISK_REGISTER.md`; this version must not be used for real
payments until they are closed.**

### Security
- One-time callback tokens: every payment link embeds a per-link token
  covered by the HMAC signature; only its SHA-256 hash is stored, stale
  tokens are rejected before CentralPay verify, and legitimate late
  returns still resolve (no hard expiration).
- Strict CentralPay response parsing: explicit success allowlist (never
  truthy guessing), typed per-field parsing with explicit reason codes
  routed to manual review.
- Application-level rate limiting: invalid API keys, callback signature
  failures, and create bursts (per-process sliding windows;
  `X-Forwarded-For` never trusted).
- Reference-ID integrity: unique constraint; a colliding reference id
  from the gateway routes to manual review with a
  `reference_id_collision` critical alert — existing records are never
  overwritten.
- Update integrity: `CENTRALPAY_UPDATE_REF` defaults to a pinned release
  tag; `centralpay update` verifies published SHA256SUMS before
  deploying; `centralpay rollback` is application-only (schema is never
  downgraded); version history recorded.
- Admin-bot container no longer receives payment/API secrets it does not
  need (masked env overrides).
- OCI image labels; Trivy scan, Syft SBOM, gitleaks, and pip-audit wired
  into the release workflow.

### Added
- `centralpay review show/list/acknowledge/resolve` host CLI with an
  allowlist of non-financial resolutions; `review resend` requires
  `--confirm-idempotent-bot --yes` AND idempotent bot mode AND a
  gateway-verified payment.
- `centralpay update --check` and `centralpay rollback`.
- `GET /health/details`: machine-readable internal health (version,
  migration revision, worker heartbeat age, queue depths, last backup) —
  not routed through Caddy.
- `FIRST_PAYMENT_GUARD_ENABLED` (default off): one-time critical alert +
  audit event on the first gateway-verified payment.
- Fault-injection tests at transaction boundaries; backup/restore
  round-trip integration test (corrupted archives rejected).
- Release workflow (`.github/workflows/release.yml`): full gate set,
  artifact packaging with SHA256SUMS, draft-only GitHub releases.
- Release documentation: risk register, migration guide, validation
  matrices (real-host / staging / admin-bot), Persian production
  checklist.

### Migration
- `0004`: callback-token and review columns; unique `reference_id`.
  See `MIGRATION_GUIDE.md` — pre-upgrade unpaid links become invalid.

## [0.4.0-dev] — 2026-07-17

- Optional read-only administrator Telegram bot (numeric-ID auth,
  private chats only), durable alert outbox (Telegram outage never
  blocks payments), health monitor, restart-safe daily report
  (Asia/Tehran), worker DB heartbeats, hardened profile-gated compose
  service. Migration `0003`.
- CI fix: signature-storm reporting on freshly booted machines.

## [0.3.0-dev] — 2026-07-16

- Dockerized deployment: multi-stage non-root image, Docker Compose
  (api/worker/db/caddy, migration-gated startup), Caddy TLS with
  redacted access logs, one-line installer, `centralpay` management
  command, validated backups with systemd timer and retention, CI
  workflows.

## [0.2.0] — 2026-07-15

- Safe bot notification pipeline: explicit reason codes, safe (default)
  vs idempotent retry modes, ambiguous-timeout → manual review, worker
  with `FOR UPDATE SKIP LOCKED`, stale-claim recovery, payer-facing
  pages, read-only inspection CLI. Migration `0002`.

## [0.1.0] — 2026-07-14

- Core payment API: `POST /api/custom-payment`, CentralPay
  getLink/verify integration, HMAC-signed callback, append-only audit
  events, health endpoints. Migration `0001`.
