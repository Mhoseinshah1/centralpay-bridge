# centralpay-bridge 0.6.0-rc4 — release notes

Release candidate. **Not production-ready**: the release blockers in
`RELEASE_RISK_REGISTER.md` (real-host install B1, real-CentralPay
staging evidence B2, green tag-triggered release workflow B5) remain
open, as does live Telegram admin-bot validation B3 if the optional
admin bot is to be enabled (the payment path itself does not depend on
it). This tag must not be deployed and no real payment may be
processed until the blockers that apply to your deployment are closed
and a human approval is recorded. Creating this GitHub release does
**not** upgrade any running production instance.

## Why rc4 exists: rc3's tag reached the real container vulnerability gate — and it correctly blocked publication

`v0.6.0-rc3` was tagged and its release workflow (`release.yml`) got
further than any tag-triggered run before it: every job passed —
documentation checks, quality gates, shell/installer checks, secret
scan, dependency scan — except one. The `Docker build, scan, and SBOM`
job's `Trivy vulnerability scan (image)` step correctly failed on a
real finding in the built image itself:

```
libssl3t64 / openssl / openssl-provider-legacy
CVE-2026-14456   HIGH   status: fixed
Installed: 3.5.6-1~deb13u2   Fixed: 3.5.7-1~deb13u2
```

This was **not** a pipeline defect (unlike rc2's two failures) — it was
a genuine, fixable HIGH-severity vulnerability in the base OS layer,
and the fail-closed Trivy gate did exactly what it exists to do: it
stopped an image with an outdated, exploitable OpenSSL package from
ever being packaged into a draft release. Because the `package` job
`needs: [..., docker, ...]`, no draft release was created and nothing
was published for `v0.6.0-rc3`.

Root cause, verified directly against the Docker Hub v2 registry API
(not just the Trivy scan text): `Dockerfile` pinned `FROM
python:3.12-slim`, a floating tag that resolved, at build time, to a
Debian trixie snapshot whose `libssl3t64` package was one Debian
security point-release behind its own OpenSSL fix — a rebuild days
apart from the last one can silently pick up a different, less-patched
snapshot with no change on our side.

Fixed on `main`:

- `Dockerfile`: both build stages now pull the same
  `python:3.12-slim-trixie@sha256:...`-pinned base (an explicit,
  verified digest instead of a floating tag), and the runtime stage
  runs a general `apt-get upgrade` (never `dist-upgrade`, never a
  single hardcoded package pin) before installing `curl`, so any
  package with a newer build in Debian's own security repo by build
  time is picked up — not just this one CVE, and without waiting on
  the next upstream base-image publish.
- The Trivy scan's image reference, digest pin, and severity policy
  were extracted into one shared script
  (`.github/scripts/trivy-scan.sh`) so `release.yml` and `ci.yml`
  cannot silently diverge on vulnerability policy the way they had:
  **`ci.yml`'s `docker` job now runs the same fail-closed Trivy scan
  on every pull request**, before a build with a vulnerability like
  this one can ever reach a release tag again.

The `v0.6.0-rc3` tag itself was **not** deleted, moved, or reused — it
remains a failed/unpublished release-candidate attempt, preserved as
evidence, exactly like `v0.6.0-rc2` before it. `v0.6.0-rc4` is the same
source content as rc3 plus this container-security fix and the new
PR-time vulnerability gate; it introduces **no** application or payment
behavior change over rc3.

A separate, pre-existing gap was also found and fixed while verifying
this release manually: `release.yml`'s full-history secret scan
(`gitleaks`) had never been re-run since the very first release attempt
months ago, and the test suite had since grown two dummy-credential
fixture shapes its allowlist did not yet cover. Fixed in
`.gitleaks.toml`; unrelated to the container CVE and not part of the
Docker/Trivy fix above.

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
rc2 — rc3 and rc4 add no new migration), none of which rewrite
existing financial history:

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
2. Set `CENTRALPAY_UPDATE_REF=v0.6.0-rc4` in
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

There is no separate "upgrading from rc2" or "upgrading from rc3"
path: neither rc2 nor rc3 was ever successfully released or deployed,
so rc4 is upgraded-to exactly like rc2/rc3 would have been.

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
exact commit) are recorded as still open in `RELEASE_RISK_REGISTER.md`,
`STAGING_VALIDATION.md`, `REAL_HOST_VALIDATION.md`, and
`ADMIN_BOT_VALIDATION.md` — none of those external tests are claimed to
have occurred for this line. B5 has made real, verified progress: rc3's
tag-triggered run passed every job except the Trivy vulnerability scan,
and that scan's own finding (CVE-2026-14456) has since been fixed and
independently confirmed via a manual `release.yml` dispatch (`amd64` and
`arm64` builds, `dpkg-query`-visible package upgrade, and the exact
pinned Trivy command reporting `HIGH: 0, CRITICAL: 0`) — but B5 still
requires an actual green run against `v0.6.0-rc4`'s own tag, which has
not yet happened for any tag.
