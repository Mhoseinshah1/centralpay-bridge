# Changelog

All notable changes to centralpay-bridge. Dates are UTC.

## [Unreleased] — post-`v0.6.0-rc4` `main`

Operator-tooling and reporting work driven by real production observations
after the successful rc4 rollout. **No payment, callback, verification,
notification, or reconciliation behavior changes.** Not tagged, not
released, not deployed.

### Added
- **Operational attention resolution** (`app/services/attention.py`,
  migration `0013`, `centralpay attention list|show|resolve`). Production
  held a 2026-08-01 payment in `getlink_failed` after a `getLink.php`
  ReadTimeout: no payment link, no gateway verification, no reference id,
  no downstream delivery — yet `centralpay stuck` classified it
  `needs_attention / unexpected_status:getlink_failed` permanently, so the
  only way to clear the worklist was to delete it and destroy audit
  history. Resolution instead records time, actor, an allowlisted reason,
  and a mandatory note in four new `payments` columns, appends a
  `payment_attention_resolved` audit event, and changes nothing else. The
  payment row, every payment event, and every admin alert are preserved;
  `status` is never rewritten; a strict resolution-to-status allowlist
  (`stale_getlink_failure` for `getlink_failed`, `stale_incomplete_creation`
  for `created`) plus a financial-inertness guard is re-checked under
  `SELECT ... FOR UPDATE` with refreshed ORM state; a duplicate operator
  action is refused rather than overwriting the first record. No gateway or
  downstream-bot HTTP.
- **`centralpay review resolve-many ORDER_ID [ORDER_ID ...]`**
  (`app/services/review_resolution.py`). Production needed to resolve 15
  gateway-verified `retry_limit_reached` reviews the bot operator had
  independently confirmed were already credited, which required a shell
  loop over the single-item command. Explicit order-id list only (no
  "resolve all", no filter-based selection), preview-only without `--yes`,
  every row independently passing the same checks, all-or-nothing across the
  batch, and rejection of a set that mixes gateway-verified and
  never-verified payments. One audit event per row plus one batch event. No
  gateway HTTP, no downstream-bot HTTP, no financial mutation. The
  single-payment workflow is unchanged.

  Bulk eligibility is restricted to allowlisted downstream-DELIVERY failures,
  reusing `app.services.bulk_resend.ELIGIBLE_RESEND_REASONS` (the same object,
  imported — not a second copy that could drift). A financial/verification
  manual review (`bot_notify_reason IS NULL` — the amount, user-id,
  reference-id, callback, and configuration mismatches
  `app.services.verification` raises, which never reach notification and so
  never set a reason) fails CLOSED with
  `financial_review_requires_individual_resolution`, and a non-allowlisted
  delivery reason with `delivery_reason_not_bulk_eligible`. Either refusal
  rejects the ENTIRE batch. Those reviews are exactly the ones where a wrong
  blanket judgement has financial consequences; they remain resolvable
  individually with `centralpay review resolve`, which is unchanged.

### Changed
- **Attention resolution is scoped to the incident, not the payment.**
  `create_payment` deliberately retries `getLink` for an existing
  `created`/`getlink_failed` row. If that retry also fails, the row returns to
  `getlink_failed` while the earlier `attention_resolved_at` still stands, so
  a plain "resolved" filter hid the new, never-reviewed failure permanently
  and the operator could not record another resolution. The canonical
  predicate now also reopens an item whose most recent
  `centralpay_getlink_failed` event is newer than its most recent
  `payment_attention_resolved` event. The comparison is on monotonic
  `payment_events.id`, not timestamps: PostgreSQL's `now()` is
  transaction-start time, so a slow `getLink` timeout records a failure event
  stamped before the call began, and SQLite resolves `CURRENT_TIMESTAMP` to
  whole seconds. Both writers serialize on the payment row lock, so id order
  is exact.
- The reused delivery-attention bucket's detail rows and its exact total now
  come from ONE windowed statement (`queries.open_attention_snapshot`). A
  capped list plus a separate `COUNT` is two READ COMMITTED snapshots, so a
  worker delivering a stale pending payment between them could leave the
  overview carrying a detail entry while reporting `needs_attention: 0` — the
  same hazard `queries.bot_delivery_snapshot` already documents and solves.
- `centralpay attention list --resolved` orders newest-resolution-first, so
  past `--limit` an operator sees recent decisions rather than only the oldest
  payments by creation date. The open worklist keeps oldest-first (most
  urgent) ordering.
- `attention resolve` now enforces the SAME grace period the worklist
  predicate applies, sharing one constant. `_ensure_payment_row` commits the
  `created` row and releases its lock before `create_payment` re-acquires it
  to attempt getLink, so a brand-new row is briefly visible and lock-free: the
  mutating path could close an item `attention list` and `stuck` both
  correctly hide as in-flight, and if creation then died without recording a
  failure event the supersession rule would never reopen it.
- A blank `--note` or actor is refused by the service before any lock (the CLI
  already refused it, but the service is what claims to own every safety
  decision), and `ck_payments_attention_resolution_fields_not_empty` is the
  database backstop — the consistency CHECK alone rejects only NULL, so an
  empty-string note satisfied it while recording no justification.
- Historical review listings (`centralpay manual-review --all`, `centralpay
  review list --all`) select on manual-review HISTORY via one shared
  `queries.manual_review_history_conditions`, not on the current status.
  `review resend` moves a review to `bot_notify_pending` while keeping its
  `review_resolved_at`/`review_resolution`, so a status filter dropped exactly
  the rows an operator most wants to look back at — a review that was resolved
  and then successfully redelivered — while the docs promised to print
  resolved rows.
- **One canonical unresolved-attention predicate.** The unexpected-status
  half of the needs-attention definition was written out twice — once for
  `centralpay stuck`'s detail rows and once for the admin bot's `/status`
  and `/stuck` summary counts — so a filter added to one and not the other
  would have made the CLI and the bot disagree about the same payment. Both
  now derive from a single
  `app.services.stuck_payments.unexpected_status_conditions` builder, and a
  load-time assertion proves every attention-resolvable status is contained
  in that predicate's population (so no other attention surface can hold a
  resolvable row).
- **`centralpay stuck --json` summary fields.** `total` now reports the TRUE
  sum of the three exact category counts. It previously reported the size of
  the internally capped result set, which made the output
  self-contradictory once any category exceeded that cap — a real
  production line read `needs_attention: 1, waiting_gateway: 25, expired:
  5788, shown: 20, total: 226` (= 1 + 25 + 200). The old value is still
  available under the explicit name `materialized_total`, and a new
  `truncated` boolean reports whether any matching payment is missing from
  the entry lines. The three category fields are unchanged. Human mode no
  longer claims "raise --limit to see more" when the internal cap, not
  `--limit`, is what is hiding rows.
- **`centralpay manual-review` lists only UNRESOLVED reviews by default**
  (`--all` for the historical view), and is documented as deprecated in
  favour of `centralpay review list`. It previously filtered on
  `status == manual_review` alone; because `review resolve` deliberately
  keeps that status as permanent history and records the outcome in
  `review_resolved_at`/`review_resolution`, every already-resolved review
  kept printing as though it were still active — contradicting `review
  list`, the admin bot's `/manual_review` and `/status`, and the
  `manual_review` monitor check, all of which already excluded them.
  `centralpay payment`/`recent`/`retry-queue` output additionally gained
  purely additive review/attention resolution keys, so a resolved row can no
  longer look identical to an active one.
- `app.ops review list` filters resolved rows in SQL via the shared
  predicate instead of selecting every `manual_review` row and discarding
  them in Python.

### Documentation
- `OPERATIONS_FA.md`: attention-resolution runbook, bulk review resolution,
  and a reconciliation polling-cadence diagnosis section.
- `MIGRATION_GUIDE.md`: migration `0013`, including an honest downgrade
  limitation and why no constraint ties `attention_resolved_at` to
  `gateway_verified_at`.
- `RELEASE_EVIDENCE_0.6.0_RC4_POST_TAG.md` (new): the post-tag
  release-workflow evidence that closes **B5**, an explicit statement of
  what is still missing for **B1**, **B2**, and **B3**, and the
  recommended (unexecuted) remediation for the stale `v0.6.1-rc1`
  `/releases/latest` pointer.

### Investigated — no change (reconciliation polling load)
Production showed unverified `link_created` payments with ~100–180
reconciliation attempts, ages of 1–2 hours, and a next retry roughly 60
seconds after the last check, against a documented 300-second slow
interval. Audit result: **the shipped defaults and the scheduler are
correct.** `app/config.py`, `.env.example`, and
`deploy/centralpay.env.template` all agree on the documented two-stage
schedule, and no commit in the repository's history ever shipped a
60-second value. The attempt arithmetic attributes the observation to a
deployment-level override: the shipped 300-second interval tops out near
111 attempts within the 2-hour lifetime and cannot produce 180, while a
60-second interval spans exactly the observed range. No financial or
reconciliation behavior was changed. Added
`tests/test_reconciliation_schedule_defaults.py`, which pins the schedule
math and asserts the three config surfaces agree — the realistic way this
could later become a genuine shipped-default defect — plus an
`OPERATIONS_FA.md` procedure for reading the effective values from
`centralpay reconciliation status --json` and restoring the documented
cadence.

### Tests
- `tests/test_attention_review_findings.py` — regressions for five defects
  found in review: incident-scoped reopening (and that an unsuperseded
  duplicate resolve is still refused), the open `attention list` composing the
  canonical predicate rather than a fourth copy, the historical listing
  keeping a payment that settled after resolution, bulk rejection of two
  aliases naming one payment, and `needs_attention` staying exact past the
  materialization cap.
- `tests/test_attention.py`, `tests/test_attention_canonical.py`,
  `tests/integration/test_attention_pg.py` (real PostgreSQL: an 8-way
  resolution race, an identity-map staleness race, concurrent overlapping
  bulk batches, the CHECK constraint, and migration `0013` upgrade,
  idempotency, and non-destructive downgrade).
- `tests/test_stuck_json_contract.py` — including cases where real category
  counts exceed the internal query cap, and a replay of both observed
  production summary lines.
- `tests/test_operator_cli_resolution.py`,
  `tests/test_reconciliation_schedule_defaults.py`,
  `tests/test_migration_chain.py`.
- The three PostgreSQL migration test files no longer hardcode the head
  revision; they read it from the real Alembic script directory
  (`tests/alembic_head.py`), with the exact value pinned in one deliberate
  place (`tests/test_migration_chain.py`).

## [0.6.0-rc4] — 2026-08-29 (release candidate — NOT production-ready)

**Supersedes the `v0.6.0-rc3` tag: its tag-triggered `release.yml` run
passed every job except one — the `Docker build, scan, and SBOM` job's
Trivy vulnerability scan correctly failed on a real, fixable HIGH-severity
finding in the built image itself (`libssl3t64`/`openssl`,
CVE-2026-14456, installed `3.5.6-1~deb13u2`, fixed `3.5.7-1~deb13u2`).
This was not a pipeline defect like rc2's two failures — it was the
fail-closed gate correctly stopping a vulnerable image from ever being
packaged into a draft release; no draft release was created and nothing
was published for `v0.6.0-rc3`. The `v0.6.0-rc3` tag itself was not
deleted, moved, or reused — it remains as historical evidence of that
failed attempt; `RELEASE_NOTES_0.6.0_RC3.md` is unchanged.** Same known
release blockers remain open in `RELEASE_RISK_REGISTER.md`, including B2
(the real CentralPay contract has still not been observed end-to-end
against the real/sandbox gateway) and B5 (a green tag-triggered
release-workflow run for this exact commit — rc3's run passed every job
but Trivy; rc4 needs its own fresh green run, now independently confirmed
via a manual `release.yml` dispatch reporting `HIGH: 0, CRITICAL: 0`).
This version must not be used for real payments until they are closed.
Release notes: `RELEASE_NOTES_0.6.0_RC4.md`. Creating this release does
not deploy it to production or upgrade any running instance.

### Fixed (release pipeline only — no application/payment behavior change from rc3)
- Pinned the Docker base image by verified digest
  (`python:3.12-slim-trixie@sha256:...`) instead of the floating
  `python:3.12-slim` tag, for both the builder and runtime stages, so a
  rebuild is reproducible instead of silently drifting to whatever
  Debian snapshot Docker Hub last published.
- Added a general `apt-get upgrade` security refresh (never
  `dist-upgrade`, never a single hardcoded package pin) to a shared base
  stage both the builder and runtime stages derive from, so any package
  with a newer build in Debian's own security repo by build time is
  picked up — closing the specific CVE-2026-14456 gap and the general
  class of "pinned base image trails upstream security fixes" without
  waiting on the next base-image publish. `ci.yml`'s build-layer cache
  is busted daily (the actual `apt-get` cost paid once a day rather than
  on every PR push); `release.yml`'s is busted per `github.run_id`/
  `run_attempt` instead, since a release run is rare enough that a
  same-day retry deserves its own fresh refresh rather than reusing that
  day's cache.
- Extracted the Trivy scan's image reference, digest pin, and severity
  policy into one shared script (`.github/scripts/trivy-scan.sh`), and
  added the same fail-closed scan to `ci.yml`'s `docker` job — this
  exact class of issue reached a release tag undetected specifically
  because pull-request CI had no equivalent scan; it now does, for
  every future PR before a release is ever tagged. (Follow-up review
  findings landed as a separate PR, **#88, merged**.)
- Propagated the apt-refresh cache-bust to production builds: it
  originally only reached CI images (`docker-compose.yml` declared no
  `args:` for it), so `centralpay update`/`rollback` and the installer
  always built with an empty value and could serve a stale apt-refresh
  layer indefinitely on an already-built host. `scripts/centralpay` now
  has a shared `build_with_apt_refresh_cachebust` helper (used by both
  `perform_update` and `perform_rollback`) and `install.sh`'s
  `deploy_stack` runs matching logic: each detects whether the checked-
  out Dockerfile actually references the cache-bust ARG — a rollback
  target, an explicit `CENTRALPAY_UPDATE_REF`/
  `CENTRALPAY_UPDATE_ALLOW_DEV_REF` downgrade, or an installer rerun
  against a different `CENTRALPAY_REF` can all target a commit old
  enough to predate it — using the fast `--build-arg` path when present
  and falling back to a full `--no-cache` rebuild only when absent.
- Found a separate, pre-existing gap in `.gitleaks.toml`'s full-history
  secret-scan allowlist (two dummy test-fixture value shapes added to
  the test suite since the original rc1 fix were never covered, because
  nothing had re-run the full-history scan since); unrelated to the
  container CVE. Fixed in a separate PR, **#86, merged**.

## [0.6.0-rc3] — 2026-08-29 (release candidate — NOT production-ready)

**Supersedes the `v0.6.0-rc2` tag, whose first real `release.yml` run
(the first *tag-triggered* run of that workflow — an earlier
`workflow_dispatch` run on `main` had already caught and fixed two
different pipeline defects, commit `88090fc`, before `v0.6.0-rc1` was
even tagged) failed on two pre-existing, unrelated defects: 15 table-of-contents links in the
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
