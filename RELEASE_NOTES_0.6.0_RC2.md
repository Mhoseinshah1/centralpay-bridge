# centralpay-bridge 0.6.0-rc2 — release notes

Release candidate. **Not production-ready**: the release blockers in
`RELEASE_RISK_REGISTER.md` (real-host install B1, real-CentralPay
staging evidence B2, live Telegram admin-bot run B3, green
tag-triggered release workflow B5) remain open. This tag must not be
deployed and no real payment may be processed until they are closed
and a human approval is recorded. Creating this GitHub release does
**not** upgrade any running production instance.

## Headline: operational hardening, not a payment-model change

Unlike 0.6.0-rc1 (which introduced the fee model), this release
candidate makes no change to how a payment is priced, settled, or
reconciled. It consolidates the operational, safety, and legacy-
compatibility work merged since rc1: a durable monitoring/alerting
subsystem, application-level rate limiting, admin-bot operational
visibility, safer production updates, and one confirmed legacy
request-body compatibility fix.

### Monitoring and alerting subsystem

An optional, dedicated monitoring process (`app.monitor`,
`MONITOR_ENABLED=false` by default) checks public readiness, database
health, worker heartbeats, notification/manual-review backlog,
reconciliation health, backup freshness, disk space, DB integrity, and
gateway/bot-notification failure bursts (counted as affected payments,
never raw retry attempts). Incident state (migrations `0011`/`0012`)
persists across restarts and is deduplicated with a PostgreSQL partial
unique index, so two racing monitor instances can never open a
duplicate incident or send a duplicate alert. New `centralpay monitor
check [--json]` / `centralpay monitor incidents [--all]` host commands
and a read-only `/monitor` admin-bot command. See `MONITORING.md`.

Hardening after the initial monitor landed: gateway/bot-notification
failure-burst detection no longer produces false positives on
unrelated errors, backup manifests are validated before being trusted,
and monitor checks degrade gracefully (report `database_unavailable`)
instead of crashing during a brief PostgreSQL outage.

### Rate limiting / abuse protection

Application-level, per-process sliding-window rate limiting across the
inbound payment API, with trusted-proxy client-IP resolution
(`X-Forwarded-For` never trusted directly) and structured log events on
limit hits, classified per endpoint sensitivity.

### Admin-bot operational visibility

Read-only system overview, stuck/reconciliation visibility, payment
lookup with events, manual-review visibility, and notification
visibility commands, built on shared read-only query services with the
same authz and secret-redaction discipline as the existing admin bot.

### Safer production updates and Caddy config safety

- `centralpay update` rejects a non-release-tag `CENTRALPAY_UPDATE_REF`
  in production by default (a prior control-flow path could still
  silently accept one); the local-development-only escape hatch
  remains `CENTRALPAY_UPDATE_ALLOW_DEV_REF=true`, never set by the
  installer or the shipped template.
- Caddy's managed-config activation is no longer coupled to overall
  app-deploy success, closing a first-upgrade bootstrap gap where a
  failed post-Caddy deploy step could leave the reverse proxy on a
  stale config; `centralpay status`/`diagnose` now detect and warn
  about Caddy config drift, including when run as a non-root operator.
- Caddy access logs no longer leak the callback `ct`/`sig` query
  parameters via the `Referer` header.
- `centralpay update --help` no longer performs an update as a side
  effect, and an `order_id` value that happens to equal `-h`/`--help`
  is preserved (passed through `--`) instead of being swallowed as a
  CLI flag by the host tooling.
- Admin-bot manual-review counts corrected to avoid over/under-counting
  payments actually awaiting review.

### Legacy request-body compatibility

A production customer (via a legacy sales bot) intermittently received
`HTTP 422` on `POST /api/custom-payment`. Diagnosed from safe,
non-secret structural diagnostics already shipped in the prior line: a
raw (non-percent-encoded) JSON object sent as a single
`application/x-www-form-urlencoded` form key, containing a literal `=`
inside the JSON content that was left unescaped, so `parse_qsl` split
the key apart at that internal `=` instead of the intended trailing
separator. A narrowly-gated recovery (six explicit structural
conditions; exactly one JSON decode; the candidate reconstructed only
by trimming the one known trailing `=` from the complete original
body) now recovers this exact shape and feeds it through the
**unchanged** normalize/validate/auth/rate-limit/idempotency/creation
pipeline — no separate, weaker path. Every adjacent ambiguous shape
(JSON in a form value, double-percent-encoding, an arbitrary prefix
before the JSON, a JSON string containing JSON, malformed JSON, a real
form missing required fields, etc.) still correctly rejects.

## Migrations 0007–0012

Six migrations landed since rc1's migration `0006`, none of which
rewrite existing financial history:

- **`0007` — payer identity isolation**: adds
  `centralpay_payer_identities` and payment snapshot links, responding
  to the 2026-07 payer/card-suggestion incident (see
  `docs/incidents/2026-07-centralpay-cross-user-card-suggestions.md`).
  Existing `gateway_user_id` values and active links are unchanged;
  old rows get `payer_identity_id = NULL` as a historical marker.
- **`0008` — hybrid identity scope**: adds
  `payments.payer_identity_type` (`telegram_user` / `order_fallback`);
  historical rows stay `NULL` rather than guessing.
- **`0009` — explicit identity derivation scheme**: adds
  `identity_scheme` so every payer-identity mapping's origin
  (`telegram_raw_v1` / `order_hmac_v1` / `historical_hmac_v1`) is
  explicit instead of inferred.
- **`0010` — reconciliation**: adds the worker bookkeeping fields
  (`reconciliation_attempts`, `reconciliation_next_at`, etc.) used to
  recover a payment still `link_created` because the payer's browser
  callback never arrived, via the same canonical verification/
  settlement service callbacks use — not a second settlement model.
- **`0011` — monitor incidents**: adds `monitor_incidents` (durable,
  deduplicated incident state for the optional monitor).
- **`0012` — monitor incident alert-delivery tracking**: adds
  `monitor_incidents.last_alert_id` so a permanently-failed incident
  alert is correctly re-queued instead of looking "already alerted"
  forever.

Every migration's downgrade is non-destructive by default; explicit
schema removal requires the documented per-migration
`CENTRALPAY_DROP_*` opt-in. See `MIGRATION_GUIDE.md` for full detail.

## Upgrading from 0.6.0-rc1

1. Take a backup: `centralpay backup` (also created automatically by
   `centralpay update`).
2. Set `CENTRALPAY_UPDATE_REF=v0.6.0-rc2` in
   `/etc/centralpay-bridge/centralpay.env`.
3. Run `centralpay update`. Migrations `0007`–`0012` apply
   automatically before api/worker start; no payment-model behavior
   changes as part of this upgrade.
4. The optional monitoring subsystem and rate limiting are both
   opt-in/already-safe by default (`MONITOR_ENABLED=false`; rate
   limiting has no production-affecting default-off toggle — review
   `MONITORING.md` and `RATE_LIMITING_ARCHITECTURE.md` before enabling
   or tuning either for your traffic).

## Rollback limitations

- The database schema is **never downgraded** by `centralpay
  rollback`; it rolls back the application only.
- Payer-identity mappings, reconciliation bookkeeping, and monitor
  incident state are operational/financial-adjacent history and are
  never rewritten by any rollback or repair tooling.

## Real-provider validation status

**PRODUCTION_VALIDATION_STATUS: INCOMPLETE.** No new real-CentralPay,
real-Telegram, or real-host validation evidence was produced to
prepare this release. Release blockers B1 (real-host install), B2
(real CentralPay contract validation), B3 (live Telegram admin-bot
validation), and B5 (green tag-triggered release workflow run for this
exact commit) are recorded as still open in `RELEASE_RISK_REGISTER.md`,
`STAGING_VALIDATION.md`, `REAL_HOST_VALIDATION.md`, and
`ADMIN_BOT_VALIDATION.md` — none of those external tests are claimed to
have occurred for this line.
