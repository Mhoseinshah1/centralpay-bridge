# Changelog

All notable changes to centralpay-bridge. Dates are UTC.

## [0.6.0-rc3] — 2026-08-29 (release candidate — NOT production-ready)

**Supersedes the `v0.6.0-rc2` tag, whose first real `release.yml` run
(the first ever real execution of that workflow) failed on two
pre-existing, unrelated defects: 15 table-of-contents links in the
legacy Persian handbook pointed at headings containing zero-width
non-joiners that the link checker's slug algorithm drops rather than
preserves, and the Trivy scan step's image reference used the wrong
Docker Hub namespace (`aquasecurity/trivy` instead of `aquasec/trivy`).
Both are fixed here. The `v0.6.0-rc2` tag itself was not deleted, moved,
or reused — it remains as historical evidence of that failed attempt;
`RELEASE_NOTES_0.6.0_RC2.md` is unchanged.** Same known release
blockers remain open in `RELEASE_RISK_REGISTER.md`, including B2 (the
real CentralPay contract has still not been observed end-to-end
against the real/sandbox gateway) and B5 (a green tag-triggered
release-workflow run for this exact commit — rc2's run found the two
defects above; rc3 needs its own fresh green run). This version must
not be used for real payments until they are closed. Release notes:
`RELEASE_NOTES_0.6.0_RC3.md`. Creating this release does not deploy it
to production or upgrade any running instance.

### Fixed (release pipeline only — no application/payment behavior change from rc2)
- Regenerated the 15 broken table-of-contents anchors in the legacy
  Persian handbook from their headings' actual slugs (ZWNJ deleted,
  Unicode combining marks kept) instead of copying the ZWNJ character
  verbatim; verified against the exact `lychee` binary the release
  workflow downloads.
- Corrected the release workflow's Trivy scan image reference from the
  nonexistent `aquasecurity/trivy` to the real Docker Hub namespace
  `aquasec/trivy`; scan flags (`--exit-code 1`, `--severity
  CRITICAL,HIGH`, `--ignore-unfixed`) are unchanged.
- Pinned that same Trivy image by a verified `@sha256` digest in
  addition to its tag: the scan step mounts the host Docker socket, so
  a tag alone was the wrong trust boundary for a release-integrity-
  critical job (a silent tag republish could otherwise run different,
  unreviewed code with that access).

## [0.6.0-rc2] — 2026-08-29 (release candidate — NOT production-ready)

**Known release blockers remain open in `RELEASE_RISK_REGISTER.md`,
including B2 (the real CentralPay contract has still not been observed
end-to-end against the real/sandbox gateway); this version must not be
used for real payments until they are closed.** Release notes:
`RELEASE_NOTES_0.6.0_RC2.md`. Creating this release does not deploy it
to production or upgrade any running instance.

### Added
- Optional monitoring and alerting subsystem (`app.monitor`,
  `MONITOR_ENABLED=false` by default; see `MONITORING.md`).
- Application-level rate limiting / abuse protection across the inbound
  payment API (trusted-proxy client-IP resolution, per-endpoint sliding
  windows, structured log events on limit hits).
- Admin-bot operational visibility and payment-operations commands
  (system overview, stuck/reconciliation visibility, payment lookup +
  events, manual-review visibility, notification visibility).

### Fixed
- Monitoring hardening: gateway/bot-notification failure-burst
  detection no longer produces false positives on unrelated errors,
  backup manifests are validated before being trusted, and monitor
  checks degrade gracefully instead of crashing during a brief
  PostgreSQL outage.
- Production update/Caddy hardening: decoupled Caddy managed-config
  activation from overall app-deploy success (closing a first-upgrade
  bootstrap gap), added drift detection surfaced through
  `centralpay status`/`diagnose`, corrected admin-bot manual-review
  counts, removed a `centralpay update --help` side effect, and
  preserved opaque `order_id` values that collide with `-h`/`--help`
  through the host CLI's argument handling.
- Closed a notification-worker stale identity-map read after a fresh
  row-lock reload, and a create-payment safe-replay exemption that
  treated "a row already exists" as "safe to replay" without an
  identity-shape match.
- Caddy access logs no longer leak the callback `ct`/`sig` query
  parameters via the `Referer` header.
- Legacy `application/x-www-form-urlencoded` payment-body
  compatibility: a raw (non-percent-encoded) JSON object used as the
  form key, containing an unescaped internal `=`, no longer produces
  the intermittent `HTTP 422` seen by the legacy sales-bot integration.
  Confirmed against production rejection-log diagnostics
  (`raw_pair_equals_count=2`, matching the diagnostics added for the
  sibling shape in the previous release) and recovered through the
  same normalize/validate/auth/rate-limit/idempotency pipeline as every
  other request representation — no weaker path.

### Documentation
- Reconciled living-doc/audit contradictions surfaced after the
  monitoring-hardening and update-hardening work landed.

### Migration
- `0007`–`0012`: payer identity isolation, hybrid identity scope,
  explicit identity derivation scheme, reconciliation bookkeeping,
  monitor incidents, and monitor incident alert-delivery tracking. No
  existing financial data is rewritten by any of them. See
  `MIGRATION_GUIDE.md`.

## [0.6.0-rc1] — 2026-07-18 (release candidate — NOT production-ready)

**Known release blockers remain open in `RELEASE_RISK_REGISTER.md`;
this version must not be used for real payments until they are
closed.** Release notes: `RELEASE_NOTES_0.6.0_RC1.md`.

### Added
- Dynamic percentage service fee (migration 0006). The bot's original
  invoice amount is stored unchanged; an immutable per-payment fee
  snapshot (`fee_policy_id`, `fee_rate_bps`, `fee_amount`,
  `payable_amount`) is written at creation with pure-integer
  round-half-up arithmetic (`(amount * rate_bps + 5000) // 10000`).
  getLink charges the payable amount; verify must report exactly the
  payable amount (mismatch → manual review with
  `verify_payable_amount_mismatch`). The bot notification payload is
  unchanged (exact JSON object and field set) and still carries no
  amounts. Fee policies are
  append-only, fully audited, selected deterministically, changeable
  only via the root host CLI (`centralpay fee
  status|set|schedule|history|cancel`), read-only in the admin bot
  (`/fee`), backfilled as zero-fee for existing payments, and included
  in backups. `MAX_PAYMENT_AMOUNT_TOMAN` now explicitly bounds the final
  payable amount (`payable_amount_out_of_range`). The installer asks for
  the initial fee percentage (default 0) and never resets an existing
  policy on rerun.

### Added (cont'd)
- Optional monitoring subsystem (`app.monitor`, `MONITOR_ENABLED=false` by
  default): a dedicated process/container checks public readiness,
  database, worker heartbeats, notification/manual-review backlog,
  reconciliation health, backup freshness, disk space, DB integrity
  (reuses `centralpay db-check`'s SQL), and gateway/bot-notification
  failure bursts (counted as affected payments, never raw retry
  attempts). Incident state (migration `0011`, `monitor_incidents`)
  persists across restarts and is deduplicated with a PostgreSQL partial
  unique index (`WHERE status = 'open'`), so two racing monitor instances
  can never open a duplicate incident or send a duplicate alert — proven
  under real concurrent threads on PostgreSQL. Alerts are delivered
  through the existing admin-bot Telegram outbox exactly once per
  open/escalate/recover transition, never per polling cycle. New
  `centralpay monitor check [--json]` / `centralpay monitor incidents
  [--all]` host commands and a read-only `/monitor` admin-bot command.
  See `MONITORING.md`.

### Fixed
- Deployment scripts (`install.sh`, `scripts/backup.sh`,
  `scripts/centralpay`) are committed with the executable bit (git mode
  100755) and the installer sets explicit modes (backup.sh 0750
  root:root, centralpay 0755) — a plain clone previously produced a
  non-executable backup.sh, breaking the systemd backup timer with
  "Permission denied" on real hosts.
- `centralpay update` now rejects a non-release-tag `CENTRALPAY_UPDATE_REF`
  (e.g. `main`/`master`) by default instead of silently deploying it
  unverified with a warning. Production updates must pin a release tag
  (`vX.Y.Z` / `vX.Y.Z-rcN`), which is already checksum- and
  `SOURCE_COMMIT`-verified before deploy; the previous unverified-branch
  behavior is preserved only for local development, behind the new explicit
  opt-in `CENTRALPAY_UPDATE_ALLOW_DEV_REF=true`.

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
