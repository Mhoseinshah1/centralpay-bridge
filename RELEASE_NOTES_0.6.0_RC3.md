# centralpay-bridge 0.6.0-rc3 — release notes

Release candidate. **Not production-ready**: the release blockers in
`RELEASE_RISK_REGISTER.md` (real-host install B1, real-CentralPay
staging evidence B2, green tag-triggered release workflow B5) remain
open, as does live Telegram admin-bot validation B3 if the optional
admin bot is to be enabled (the payment path itself does not depend on
it). This tag must not be deployed and no real payment may be
processed until the blockers that apply to your deployment are closed
and a human approval is recorded. Creating this GitHub release does
**not** upgrade any running production instance.

## Why rc3 exists: rc2's tag attempt failed its own release pipeline

`v0.6.0-rc2` was tagged and its release workflow (`release.yml`) ran
for the first time as a tag-triggered run — B5, "green tag-triggered
release workflow," had been an open blocker until then. (An earlier
`workflow_dispatch` run on `main`, before `v0.6.0-rc1` was even
tagged, had already caught and fixed two different pipeline defects
in commit `88090fc`; that run was not tag-triggered.) This
tag-triggered run found two genuine, pre-existing defects, unrelated
to anything rc2's version-prep changed:

- **Documentation checks**: 15 table-of-contents links in the legacy
  Persian handbook pointed at headings containing zero-width
  non-joiners (ZWNJ), which the link checker's heading-slug algorithm
  drops rather than preserves — so those 15 anchors never resolved.
- **Trivy vulnerability scan**: the scan step's Docker image reference,
  `aquasecurity/trivy:0.58.0`, is not a real Docker Hub repository
  (Aqua Security's Docker Hub namespace is `aquasec`, not
  `aquasecurity`) — the scan failed to even start.

Both were root-caused, reproduced locally, and fixed on `main`.
A follow-up automated code review then flagged that the Trivy scan
step, which mounts the host Docker socket, was trusting a mutable
image tag rather than a content digest; the image reference now also
carries a verified `@sha256` digest pin, so a future republish of that
tag cannot silently swap in different, unreviewed code with that
access. The `v0.6.0-rc2` tag itself was **not** deleted, moved, or
reused — it remains a failed/unpublished release-candidate attempt,
preserved as evidence. `v0.6.0-rc3` is the same source content as rc2
plus these three release-pipeline fixes; it introduces **no**
application or payment behavior change over rc2.

## Headline: operational hardening, not a payment-model change

Unlike 0.6.0-rc1 (which introduced the fee model), this release line
makes no change to how a payment is priced or settled. It does add
automatic reconciliation of payments stuck in `link_created` when the
browser callback never arrives (enabled by default; see "Upgrading
from 0.6.0-rc1" below) — a new recovery capability rc1 did not have,
using the exact same verification/settlement path as a callback, not
a second settlement model. Otherwise it consolidates the operational,
safety, and legacy-compatibility work merged since rc1: a durable
monitoring/alerting subsystem,
application-level rate limiting, admin-bot operational visibility,
safer production updates, and one confirmed legacy request-body
compatibility fix.

### Monitoring and alerting subsystem

An optional, dedicated monitoring process (`app.monitor`,
`MONITOR_ENABLED=false` by default) checks public readiness, database
health, worker heartbeats, notification/manual-review backlog,
reconciliation health, backup freshness, disk space, DB integrity, and
gateway/bot-notification failure bursts (counted as affected payments,
never raw retry attempts). Incident state (migrations `0011`/`0012`)
persists across restarts and is deduplicated with a PostgreSQL partial
unique index, so two racing monitor instances can never open a
duplicate incident row or enqueue a duplicate alert for one. Delivery
to Telegram itself is at-least-once by design, like the rest of this
codebase's notification paths: if a send succeeds but its HTTP response
is lost, a retry can still produce a second operator-visible message on
the wire — the guarantee is no duplicate incident bookkeeping, not an
exactly-once delivery promise. New `centralpay monitor
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

Six migrations landed since rc1's migration `0006` (unchanged since
rc2 — rc3 adds no new migration), none of which rewrite existing
financial history:

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

Every migration's downgrade **except `0007`** is non-destructive by
default, gated behind the documented per-migration `CENTRALPAY_DROP_*`
opt-in. `0007` has no such guard: its downgrade unconditionally drops
`centralpay_payer_identities` and both payment snapshot columns —
never run `alembic downgrade` past `0007` against a database holding
real payer-identity history. See `MIGRATION_GUIDE.md` for full detail.

## Upgrading from 0.6.0-rc1

1. Take a backup: `centralpay backup` (also created automatically by
   `centralpay update`).
2. Set `CENTRALPAY_UPDATE_REF=v0.6.0-rc3` in
   `/etc/centralpay-bridge/centralpay.env`.
3. Run `centralpay update`. Migrations `0007`–`0012` apply
   automatically before api/worker start; pricing/fee behavior is
   unchanged from rc1. **Gateway-call behavior is not fully
   unchanged**: the worker's reconciliation thread
   (`RECONCILIATION_ENABLED=true` by default, both in `app/config.py`
   and the shipped `deploy/centralpay.env.template`) starts
   automatically and issues its own CentralPay `verify` calls for
   payments still `link_created` after their browser callback never
   arrived — a capability rc1 did not have. It uses the same
   verification/settlement path as a callback, touches only payments
   already eligible under the documented age/attempt rules, and never
   mutates a payment outside that path; disabling it
   (`RECONCILIATION_ENABLED=false`) stops only the polling, callbacks
   are unaffected. Review `MIGRATION_GUIDE.md`'s `0010` section before
   upgrading if this automatic recovery is not wanted immediately.
4. The optional monitoring subsystem and rate limiting are both
   opt-in/already-safe by default (`MONITOR_ENABLED=false`; rate
   limiting has no production-affecting default-off toggle — review
   `MONITORING.md` and `RATE_LIMITING_ARCHITECTURE.md` before enabling
   or tuning either for your traffic).

There is no separate "upgrading from rc2" path: rc2 was never
successfully released or deployed, so rc3 is upgraded-to exactly like
rc2 would have been.

## Rollback limitations

- The database schema is **never downgraded** by `centralpay
  rollback`; it rolls back the application only, so this limitation
  does not apply to a normal `centralpay rollback` — only to an
  operator running raw `alembic downgrade` directly.
- Reconciliation bookkeeping and monitor incident state are
  operational/financial-adjacent history and are never rewritten by
  any rollback or repair tooling. Payer-identity mappings share that
  guarantee from migration `0008` onward (guarded behind
  `CENTRALPAY_DROP_PAYER_IDENTITY=1`), but **not** for `0007` itself:
  its downgrade unconditionally drops the mapping table and both
  payment snapshot columns, with no opt-in guard — see the migrations
  section above.

## Real-provider validation status

**PRODUCTION_VALIDATION_STATUS: INCOMPLETE.** No new real-CentralPay,
real-Telegram, or real-host validation evidence was produced to
prepare this release. Release blockers B1 (real-host install), B2
(real CentralPay contract validation), B3 (live Telegram admin-bot
validation), and B5 (green tag-triggered release workflow run for this
exact commit — the two defects rc2's real run found are fixed on
`main`, but B5 requires a fresh green run against rc3's own tag) are
recorded as still open in `RELEASE_RISK_REGISTER.md`,
`STAGING_VALIDATION.md`, `REAL_HOST_VALIDATION.md`, and
`ADMIN_BOT_VALIDATION.md` — none of those external tests are claimed to
have occurred for this line.
