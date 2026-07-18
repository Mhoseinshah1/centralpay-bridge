# Migration guide

How to move an existing installation between versions. General rules:

> **Migration `0005` (final financial audit):** adds three CHECK
> constraints to `payments` (`amount > 0`, `bot_notify_attempts >= 0`,
> and delivery-states-require-`gateway_verified_at`). All constraints are
> valid for every state the application can have produced, so the
> migration is safe on existing data; adding a CHECK takes a brief
> table-scan lock (negligible at this system's row counts). Applied
> automatically by the `migrate` service.

- **Migrations are forward-only.** Audit and financial data are never
  rewritten; the database schema is **never downgraded**.
  `centralpay rollback` rolls back the *application* only.
- **Always back up first.** `centralpay update` creates a pre-update
  backup automatically; `centralpay backup` does it on demand.
- Migrations run automatically: the `migrate` service applies
  `alembic upgrade head` before api/worker start (compose gating).

## 0.5.0-rc1 → 0.6.0-rc1

### Schema (migration `0006`)

Applied automatically by the `migrate` service. Creates the append-only
`fee_policies` table and adds four snapshot columns to `payments`:
`fee_policy_id` (FK RESTRICT, nullable), `fee_rate_bps`, `fee_amount`,
`payable_amount`. Every existing payment is backfilled as fee-less:

```
fee_policy_id  = NULL
fee_rate_bps   = 0
fee_amount     = 0
payable_amount = amount
```

so its financial meaning is unchanged. CHECK constraints then bind
`payable_amount = amount + fee_amount`, the rate range (0..10000 bps),
non-negative fees, and fee-policy row consistency at the storage layer.
The backfill is a single UPDATE plus CHECK validation — a brief
table-scan lock, negligible at this system's row counts, and valid for
every state 0.5.0-rc1 can have produced (proven by the
populated-database migration test).

### Behavior

- With no fee policy configured (the default after upgrade), behavior is
  identical to 0.5.0-rc1: getLink is asked for the original amount and
  verify is compared against it (payable == amount when the fee is 0).
- Enabling a fee is an explicit operator action:
  `centralpay fee set RATE --note "..."` (root). Fee changes affect NEW
  orders only. The installer's fee question (default 0) only applies on
  fresh installs; `--ensure-initial` never overwrites an existing
  policy on a rerun.
- `MAX_PAYMENT_AMOUNT_TOMAN` now bounds the FINAL payable amount; with
  a non-zero fee, orders whose `amount + fee` exceeds it are rejected
  with `payable_amount_out_of_range`.

### Rollback limitation (important)

The schema is never downgraded. **After 0006 is applied, the 0.5.0-rc1
application can no longer create payments** — it does not populate the
NOT NULL `payable_amount` column. `centralpay rollback` across this
boundary is therefore NOT a routine path: roll forward, or restore the
pre-update backup (losing post-upgrade payments) as disaster recovery.

## 0.4.0-dev → 0.5.0-rc1

### Schema (migration `0004`)

Applied automatically. Adds to `payments`:

- `callback_token_hash`, `callback_token_issued_at` — one-time callback
  token (hash only; plaintext exists only inside the signed link URL)
- `review_acknowledged_at`, `review_resolved_at`, `review_resolution`
  — manual-review bookkeeping for the new `centralpay review` commands
- unique constraint `uq_payments_reference_id` on `reference_id`
  (PostgreSQL permits multiple NULLs; existing NULL rows are unaffected)

**Pre-check for the unique constraint:** duplicate non-NULL
`reference_id` values would fail the migration. Verify before upgrading:

```sql
SELECT reference_id, count(*) FROM payments
WHERE reference_id IS NOT NULL
GROUP BY reference_id HAVING count(*) > 1;
```

Any duplicates indicate a serious pre-existing anomaly: stop, resolve
via manual review (with audit trail), then upgrade.

### Behavior changes

1. **Callback links now carry a one-time token (`ct`).** Links issued
   by 0.4.0 (signed over `orderId` only) are **no longer valid** after
   the upgrade. Impact: a payer holding an unpaid pre-upgrade link gets
   a signature error; the bot re-requesting the same `order_id`
   regenerates a fresh valid link. Upgrade during a quiet window; treat
   in-flight unpaid links as expired.
2. **Stricter CentralPay response parsing.** Responses without an
   explicit success marker are now rejected (previously: heuristic).
   Malformed financial fields route to manual review with explicit
   reason codes. Watch manual-review volume right after upgrading — a
   sudden spike would mean the real gateway schema disagrees with the
   allowlist (see `STAGING_VALIDATION.md`).
3. **Application rate limiting is on by default** (`RATE_LIMIT_*`
   vars). Defaults are generous; tune in
   `/etc/centralpay-bridge/centralpay.env` if the bot legitimately
   bursts above 120 creates/minute per API process.
4. **`centralpay update` now verifies release checksums** when
   `CENTRALPAY_UPDATE_REF` is a release tag (the new default —
   `v0.5.0-rc1`). Branch refs remain development mode with no
   verification.
5. New optional env vars (all with safe defaults):
   `RATE_LIMIT_ENABLED`, `RATE_LIMIT_CREATE_PER_MINUTE`,
   `RATE_LIMIT_INVALID_KEY_PER_10MIN`,
   `RATE_LIMIT_INVALID_SIGNATURE_PER_10MIN`,
   `FIRST_PAYMENT_GUARD_ENABLED` (recommended `true` for go-live).

### Procedure

```bash
centralpay backup
centralpay update --check   # shows current vs target, checksum status
centralpay update           # pre-update backup, checksum verify, deploy, migrate
centralpay status && centralpay diagnose
```

Rollback (application only — schema stays at 0004):

```bash
centralpay rollback         # typed ROLLBACK confirmation, pre-rollback backup
```

0.4.0 code runs against the 0004 schema (new columns are nullable and
unused by it), but new-format callback links stop being issued —
payments created *after* the upgrade keep working because their tokens
were already stored. Links created by 0.5.0 remain verifiable only by
0.5.0; roll forward again as soon as possible.

## Earlier versions

- **0.3.0-dev → 0.4.0-dev**: migration `0003` (admin_alerts,
  worker_heartbeats). Admin bot optional/off by default.
- **0.2.x → 0.3.0-dev**: first dockerized deployment; use the installer
  on a fresh host and restore a backup rather than migrating in place.
- **0.1.x → 0.2.x**: migration `0002` (notification delivery tracking).
