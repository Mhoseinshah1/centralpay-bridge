# Post-tag release evidence — `v0.6.0-rc4`

**Scope of this document.** `RELEASE_NOTES_0.6.0_RC4.md` was written BEFORE
the `v0.6.0-rc4` tag existed, so it necessarily describes **B5** (a green
tag-triggered `release.yml` run) as still open — which was accurate at the
moment it was written. That run has since happened. This file records the
evidence that closes B5, on current `main`, without touching the immutable
release artifacts.

**What this document deliberately does NOT do:**

- it does not modify, move, delete, or recreate the `v0.6.0-rc4` tag, nor the
  `v0.6.0-rc2` / `v0.6.0-rc3` tags — all remain immutable historical evidence;
- it does not rewrite `RELEASE_NOTES_0.6.0_RC4.md` or the published release
  artifact. A release-notes snapshot describes a release line at tag time; it
  is not retroactively edited to look newer (see `DOCUMENTATION.md`'s
  documentation-maintenance rules);
- it does not mutate any GitHub Release metadata (see §4);
- it does not close **B1**, **B2**, or **B3**, and it states exactly what each
  still needs (see §3);
- it does not authorize a deployment, a new tag, or a publish.

---

## 1. Evidence that closes B5

| Item | Value |
| --- | --- |
| Tag | `v0.6.0-rc4` |
| Commit | `ee3f2e694a7d45cf5d378a42d760381588c0071c` |
| Application version | `0.6.0-rc4` |
| Workflow | `.github/workflows/release.yml`, tag-triggered (`push: tags`) |
| Run ID | `33276671809` |
| Result | Completed successfully |
| Release | Published as a **prerelease** (`draft=false`, `prerelease=true`, `latest=false`) |

Jobs reported successful in that run:

- documentation checks
- ShellCheck / installer checks
- gitleaks secret scan
- dependency vulnerability scan
- Python quality gates on Ubuntu 22.04 and 24.04
- migrations on PostgreSQL 16
- full PostgreSQL test suite
- security suites
- Docker `amd64` build
- `arm64` build validation
- smoke test
- Caddy validation
- strict Trivy `HIGH`/`CRITICAL` scan
- Syft SPDX SBOM generation
- release artifact packaging

Release artifacts were created and the GitHub Release was published as a
prerelease.

### B5 verdict: **CLOSED**

B5's exact scope was: *"Release workflow (`.github/workflows/release.yml`) has
not yet run green: Docker builds, Trivy scan, SBOM, and artifact packaging are
CI-delegated and unverified locally."*

Every element of that scope is now covered by a real tag-triggered run against
this exact commit, not by a manual dispatch and not by inference from an
earlier RC. The rc3 history remains instructive and unaltered: rc3's own
tag-triggered run passed every job except Trivy, which correctly fail-closed on
a genuine HIGH finding in the built image; rc4 carries that fix and its Trivy
gate passed.

**What B5 closing does and does not mean.** It means the release *pipeline* is
proven end-to-end for this commit. It does **not** mean the release is approved
for real payments — that remains gated on B1, B2, and (for admin-bot
enablement) B3, plus a recorded human approval.

---

## 2. Production rollout observation (context only — not a blocker closure)

Production is running `0.6.0-rc4` at commit `ee3f2e6`, reporting: API, worker,
PostgreSQL, and Caddy healthy; public `/health/ready` HTTP 200; TLS active;
Caddy config in sync with activation confirmed; 0 pending notifications; 0
unresolved manual reviews after operator resolution; Alembic revision `0012`;
`db-check` status `ok` with no failures.

A first upgrade from the older updater initially reported the Caddy config OUT
OF SYNC because of the already-documented first-transition bootstrap
limitation. Re-running `centralpay update` from rc4 backed up the Caddy config,
rendered and validated the new one, restarted Caddy, and confirmed activation.
**This is a known, documented limitation of the first transition, not an open
production incident.**

This section is recorded as operational context. **It closes nothing.** A
successful in-place update of an existing host is not evidence for B1, and
production traffic is not evidence for B2 — see §3.

---

## 3. Blockers that remain OPEN, and exactly what each still needs

### B1 — installer never executed from zero on a real host: **OPEN**

Evidence document: `REAL_HOST_VALIDATION.md`.

Updating an existing, already-provisioned production host exercises
`perform_update`. It does **not** exercise the code paths B1 is about. Still
missing:

- an end-to-end `curl -fsSL .../install.sh | sudo bash` run on a **separate,
  clean** Ubuntu 22.04 **and** 24.04 VM, from zero;
- real Caddy TLS certificate issuance on a real domain;
- systemd unit / backup-timer behavior on a real host;
- UFW rules applied on a real host;
- `centralpay update` **and** `centralpay rollback` against a real GitHub
  release, from a fresh install;
- the results recorded in `REAL_HOST_VALIDATION.md` (dates, OS versions, arch,
  sanitized output).

The operator has reported using an Ubuntu 26.04 VPS; no sanitized evidence for
any of the three supported versions has been recorded.

### B2 — real CentralPay contract never observed: **OPEN**

Evidence document: `STAGING_VALIDATION.md`.

Production has processed real payments, which proves the happy path works well
enough to move money. That is **not** the same as the controlled validation
procedure B2 defines, and it must not be recorded as if it were. Still missing,
per `STAGING_VALIDATION.md`'s own required procedure:

- the recorded **verify-after-verify** behavior for the same order — the
  specific unknown gating `CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED`, which stays
  `false` by default precisely because this is unconfirmed;
- recorded real `getLink` / `verify` response schemas, including the exact
  "payment is paid" and "payment type invalid" message texts our handling
  currently assumes from stubs;
- a deliberate **amount/user-id mismatch** forced under controlled conditions,
  confirming it routes to manual review and never credits;
- the fee-bearing flow recorded end to end (payer charged the PAYABLE amount,
  verify reports the payable amount, the bot notification carries only
  `order_id`/`actions`, the bot credits the ORIGINAL amount);
- `FIRST_PAYMENT_GUARD_ENABLED=true` producing its one-time critical alert;
- redacted request/response shapes recorded in `STAGING_VALIDATION.md`.

A controlled procedure produces evidence for the failure and edge cases.
Observing successful production traffic produces evidence only for the cases
that happened to occur.

### B3 — live Telegram admin-bot validation: **OPEN**

Evidence document: `ADMIN_BOT_VALIDATION.md`.

**Audit of existing evidence performed for this document.**
`ADMIN_BOT_VALIDATION.md` defines seven numbered acceptance steps and its
`## Results` section currently reads *"None recorded. Blocker open."* No
recorded evidence satisfies any of the seven. The production observation that
"unresolved manual reviews became 0 after operator resolution" was achieved
through the host CLI and says nothing about Telegram.

Still missing, per that document's own acceptance criteria:

1. enablement via the installer or `centralpay admin-bot enable`, confirming
   the container starts only under the `admin-bot` profile and that masked env
   vars hide payment secrets;
2. every read-only command run from the admin account, with rendering confirmed
   (Persian text, HTML escaping, message-length splitting);
3. a non-admin account and a group chat both receiving the generic denial, with
   `admin_bot_unauthorized_access` audit events recorded;
4. the bot container stopped while alerts are generated, confirming payments
   continue unaffected and queued alerts deliver after restart;
5. a real Telegram 429 triggered and backoff confirmed;
6. the bot left running across a daily-report boundary, confirming exactly one
   report;
7. the dynamic-fee additions listed at the end of that document (`/fee`
   read-only from Telegram, `/payment` separating invoice/fee/gateway amounts,
   the daily report separating the three totals).

**B3 therefore stays OPEN.** It blocks admin-bot enablement only; the payment
path does not depend on the admin bot, which is optional and disabled by
default.

---

## 4. GitHub Release metadata hygiene — recommendation only, NOT executed

### Observation

An older GitHub Release named/tagged **`v0.6.1-rc1`** was historically
published with `prerelease=false`. GitHub computes `/releases/latest` as the
most recent non-draft, non-prerelease release, so that old RC-named release is
currently what `/releases/latest` resolves to. `v0.6.0-rc4` was deliberately
published with `draft=false`, `prerelease=true`, `latest=false`, so it is
correctly excluded from that pointer.

### Impact assessment

**No impact on the updater's normal production path.** `centralpay update`
resolves an explicit release tag matching `vX.Y.Z` / `vX.Y.Z-rcN` and verifies
the artifact and `SOURCE_COMMIT` through `SHA256SUMS`, requiring the fetched
tag commit to equal the verified `SOURCE_COMMIT` before any deployment work
begins. It does not consult `/releases/latest`.

The impact is **confusing release metadata for humans and for any third-party
tooling that does read `/releases/latest`**: the API currently advertises an
RC-named `0.6.1` line as this project's latest stable release, when the current
release candidate is `0.6.0-rc4` and no `0.6.1` line is current.

### Recommended remediation (requires separate human authorization)

Per this task's constraints, **no GitHub Release metadata was mutated.** The
recommendation, for a human to authorize and execute separately:

1. Edit the existing **`v0.6.1-rc1`** release and set `prerelease=true`.
   Change **only** that flag. Do not delete the release, do not delete or move
   the tag, and do not alter its notes or assets — it is historical evidence
   like every other RC.
2. Confirm afterwards that `GET /repos/Mhoseinshah1/centralpay-bridge/releases/latest`
   returns **404** (the correct result when a repository has no non-prerelease
   release yet), rather than silently resolving to some other RC.
3. Do **not** set `v0.6.0-rc4` — or any RC — as `latest`. Every `-rcN` release
   must remain `prerelease=true`. `/releases/latest` should start resolving
   only when a genuine stable `vX.Y.Z` is published.
4. Re-verify that `centralpay update --check` and `centralpay update` still
   behave identically before and after, since neither should depend on the
   pointer. If either changes behavior, that is itself a defect to fix in the
   updater.

### Preventive note

Any future release published from `release.yml` for a tag matching `-rcN` must
carry `prerelease=true`. The rc4 publish already does this correctly; the
`v0.6.1-rc1` release predates that discipline.

---

## 5. Blocker status summary as of this document

| # | Blocker | Status | Evidence |
| --- | --- | --- | --- |
| B1 | Installer never executed from zero on a real Ubuntu host | **OPEN** | `REAL_HOST_VALIDATION.md` |
| B2 | Real CentralPay contract / verify-after-verify never observed under a controlled procedure | **OPEN** | `STAGING_VALIDATION.md` |
| B3 | Live Telegram admin-bot validation (blocks admin-bot enablement only) | **OPEN** — existing evidence audited; none of the seven acceptance steps recorded | `ADMIN_BOT_VALIDATION.md` |
| B4 | Multi-agent adversarial review | CLOSED (2026-08-17) | `ADVERSARIAL_REVIEW_B4_RECHECK_c68e86e4.md` |
| B5 | Green tag-triggered release-workflow run | **CLOSED** — run `33276671809` on `v0.6.0-rc4` / `ee3f2e6` | this document, §1 |

**Release decision is unchanged by this document.** `0.6.0-rc4` must not be
used for real payments until B1 and B2 are closed (and B3 if the optional admin
bot is to be enabled), with a recorded human approval. Nothing here authorizes
a deployment, a tag, or a publish.
