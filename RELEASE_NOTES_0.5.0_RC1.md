# centralpay-bridge 0.5.0-rc1

First release candidate of the CentralPay ⇄ Telegram-bot payment bridge.

> **⚠️ This is a release candidate, not a production release.**
> Open release blockers are tracked in `RELEASE_RISK_REGISTER.md`:
> the installer has never run on a real host (B1), the CentralPay
> contract has never been observed against the real gateway (B2), the
> admin bot has never talked to real Telegram (B3), the multi-agent
> adversarial review was never completed (B4), and the release workflow
> must run green (B5). **Do not use this version for real payments, and
> do not publish this release, until those blockers are closed and human
> approval is recorded.**

## Highlights

- **Callback replay hardening** — every payment link now carries a
  one-time token bound into the HMAC signature. Only the token's SHA-256
  hash is stored; superseded links are rejected before the gateway is
  ever contacted, while legitimate late returns still resolve to their
  final result (no hard expiration).
- **Strict gateway parsing** — CentralPay responses are accepted only on
  an explicit success marker; every financial field is parsed with typed
  coercion, and anything malformed routes to manual review with an
  explicit reason code. Success is never guessed from truthy values.
- **Reference-ID integrity** — reference ids are unique; a collision
  reported by the gateway goes to manual review with a critical alert
  and never overwrites an existing record.
- **Operational manual review** — `centralpay review
  show/list/acknowledge/resolve` with strictly non-financial
  resolutions; resend requires explicit idempotent-bot confirmation.
- **Verified updates** — `centralpay update` now pins a release tag by
  default and verifies published SHA256SUMS before deploying;
  `centralpay rollback` never downgrades the database schema.
- **Rate limiting** — application-level sliding windows for invalid API
  keys, invalid callback signatures, and create bursts.
- **Crash-safety proofs** — fault-injection tests at the transaction
  boundaries and a real pg_dump/pg_restore backup round-trip test.
- **Release engineering** — gated release workflow producing a source
  tarball, SPDX SBOM, and SHA256SUMS attached to a *draft* release;
  Trivy, gitleaks, and pip-audit scans.

## Upgrading

See `MIGRATION_GUIDE.md`. Key facts: migration `0004` applies
automatically; **unpaid payment links issued by 0.4.0 become invalid**
(the bot re-requesting the same order id regenerates them); rate
limiting is on by default; set `FIRST_PAYMENT_GUARD_ENABLED=true`
before go-live.

```bash
centralpay backup
centralpay update --check
centralpay update
```

## Install (new host)

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

## Artifacts

Produced by the release workflow (draft release only):

- `centralpay-bridge-0.5.0-rc1.tar.gz` — source archive (tracked files
  only; contains no `.env`, secrets, or database dumps)
- `sbom-centralpay-bridge.spdx.json` — SPDX SBOM (Syft)
- `SHA256SUMS` — checksums for both, verified by `centralpay update`

## Documents

- `RELEASE_RISK_REGISTER.md` — full triage of all deferred-review topics
- `MIGRATION_GUIDE.md`, `CHANGELOG.md`
- `REAL_HOST_VALIDATION.md`, `STAGING_VALIDATION.md`,
  `ADMIN_BOT_VALIDATION.md` — validation matrices (currently blocked)
- `PRODUCTION_CHECKLIST_FA.md` — go-live checklist (Persian)
