# Migration guide

CentralPay Bridge migrations are designed for a financial system: **forward-only by default**, non-destructive where practical, and gated by backup + integrity checks.

Current application version: **0.6.0-rc4**.  
Current Alembic head in this branch: **0013**.

## General rules

- `centralpay update` creates a validated pre-update backup before deployment work.
- The Compose `migrate` service runs `alembic upgrade head` before API/worker start.
- API/worker startup is health-gated behind migration success.
- `centralpay rollback` rolls back application files only; it does **not** automatically downgrade the database schema.
- If an application rollback is incompatible with the already-applied schema, roll forward or restore the pre-update backup as an explicit disaster-recovery decision.
- Never edit an already-deployed migration revision to add production schema changes. Add a new revision.
- PostgreSQL behavior is authoritative for financial migrations/tests.

## Current migration chain

| Revision | Purpose |
| --- | --- |
| `0001` | initial payment/audit schema |
| `0002` | bot-notification delivery state |
| `0003` | administrator alerts / worker heartbeats |
| `0004` | release hardening: callback token + manual-review fields + reference uniqueness |
| `0005` | financial CHECK constraints |
| `0006` | dynamic fee policies and immutable per-payment fee/payable snapshot |
| `0007` | per-customer/payer CentralPay identity mapping + payment identity snapshot |
| `0008` | explicit `payments.payer_identity_type` (`telegram_user` / `order_fallback`) with historical NULL support |
| `0009` | explicit payer mapping `identity_scheme` (`telegram_raw_v1`, `order_hmac_v1`, `historical_hmac_v1`) |
| `0010` | server-side reconciliation bookkeeping/index for recovering paid `link_created` payments when browser callback is missed |
| `0011` | durable `monitor_incidents` table for the optional monitoring subsystem's cross-restart incident lifecycle |
| `0012` | `monitor_incidents.last_alert_id` so a permanently failed alert delivery can be detected and re-queued instead of looking "already alerted" forever |
| `0013` | `payments.attention_resolved_at/_resolution/_resolved_by/_resolution_note` so a stale, non-financial failure (e.g. an old `getlink_failed` row) can be operationally closed with a durable, audited record instead of being deleted |

Run:

```bash
centralpay migrate current
centralpay migrate history
```

or, for the canonical production integrity view:

```bash
centralpay db-check --details --json
```

## 0006 — dynamic fee

Migration `0006` creates the append-only `fee_policies` table and adds the immutable payment snapshot fields:

- `fee_policy_id`
- `fee_rate_bps`
- `fee_amount`
- `payable_amount`

Existing payments are backfilled as fee-less so their original financial meaning does not change:

```text
fee_policy_id = NULL
fee_rate_bps = 0
fee_amount = 0
payable_amount = amount
```

Database constraints enforce rate bounds, non-negative fees, positive payable amount, and `payable_amount = amount + fee_amount`.

After `0006`, older application code that does not populate the new NOT NULL fields may no longer be able to create payments. Treat rollback across this boundary as a compatibility decision, not a routine schema downgrade.

## 0007 — payer identity isolation

Migration `0007` responds to the 2026-07 payer/card-suggestion incident by introducing `centralpay_payer_identities` and payment snapshot links to the chosen gateway payer mapping.

It is non-destructive for existing rows:

- existing `gateway_user_id` values remain unchanged
- existing active payment links continue verifying against their stored snapshot
- old rows have `payer_identity_id = NULL` as the historical marker

New code can therefore isolate future payer identities without rewriting old financial history.

## 0008 — hybrid identity scope

Migration `0008` adds `payments.payer_identity_type`.

Allowed new scopes:

- `telegram_user`
- `order_fallback`

Historical rows remain NULL because their original raw identity scope cannot be reconstructed safely. The migration deliberately does **not** guess a scope.

The downgrade path is non-destructive by default; explicit schema drop requires `CENTRALPAY_DROP_PAYER_IDENTITY=1`.

## 0009 — explicit identity derivation scheme

Migration `0009` adds `centralpay_payer_identities.identity_scheme` so the origin of every mapping is explicit rather than inferred from the numeric value.

Supported schemes:

- `telegram_raw_v1` — gateway user ID is the exact Telegram user ID
- `order_hmac_v1` — order fallback derived in the reserved fallback range
- `historical_hmac_v1` — pre-0009 mapping created by the retired derivation schemes

Existing mapping IDs and payment snapshots are preserved exactly.

Downgrade is non-destructive by default; explicit removal again requires `CENTRALPAY_DROP_PAYER_IDENTITY=1`.

## 0010 — reconciliation

Migration `0010` adds operational fields used by the worker to recover a payment that is still `link_created` because the payer's browser callback was not delivered.

Fields include:

- `reconciliation_attempts`
- `reconciliation_next_at`
- `reconciliation_last_at`
- `reconciliation_last_error_code`
- `reconciliation_claimed_at`
- `reconciliation_claimed_by`

and index:

- `ix_payments_reconciliation_due (status, reconciliation_next_at)`

There is no financial-data rewrite. Existing eligible `link_created` rows naturally enter reconciliation once they meet the configured age rules.

The worker uses the same canonical verification/settlement service as callback handling; the migration adds bookkeeping, not a second settlement model.

Downgrade is non-destructive by default. Explicit removal requires `CENTRALPAY_DROP_RECONCILIATION=1`.

## 0011 — monitor incidents

Migration `0011` adds `monitor_incidents`: durable, cross-restart incident state for the optional monitoring subsystem (`app.monitor`). A check transitioning from healthy to warning/critical opens a row; the same condition staying unhealthy across polling cycles never opens a second row; recovery resolves it. At most one open row can exist per `check_key` at a time, enforced by a partial unique index rather than application logic alone, so two racing monitor instances can never both open the same incident.

No existing table or financial data is touched. The monitoring subsystem itself is disabled by default (`MONITOR_ENABLED=false`).

Downgrade is non-destructive by default; explicit removal requires `CENTRALPAY_DROP_MONITOR_INCIDENTS=1`.

## 0012 — monitor incident alert-delivery tracking

Migration `0012` adds `monitor_incidents.last_alert_id`, a foreign key to `admin_alerts.id` recording which outbox row an incident's `last_alerted_at` refers to. Without it, an incident whose opening/escalation alert permanently failed delivery (every Telegram retry exhausted) would look "already alerted" forever; this column lets the catch-up path check the referenced alert's actual delivery status and re-queue a fresh one if it failed.

Downgrade is non-destructive by default; explicit removal requires `CENTRALPAY_DROP_MONITOR_INCIDENT_LAST_ALERT=1`.

## 0013 — operational attention resolution

Migration `0013` adds four nullable columns to `payments`
(`attention_resolved_at`, `attention_resolution`, `attention_resolved_by`,
`attention_resolution_note`), one CHECK constraint
(`ck_payments_attention_resolution_consistent`: all four set together or not
at all), and one partial index (`ix_payments_attention_unresolved` on
`(status, created_at) WHERE attention_resolved_at IS NULL`).

Why: a payment whose `getLink` call failed never obtains a payment link, is
never gateway verified, and is never revisited by any automatic path, so
`centralpay stuck` classified it `needs_attention` permanently. The only way
to clear it was to delete the row — destroying permanent financial/audit
history. These columns record the operator's decision instead. See
`app/services/attention.py`.

**Forward-safe, no data migration, no invented fact.** Every existing row gets
`NULL` in all four columns, which means exactly "not resolved" — the same
operational meaning those rows already had. No financial column, status, event,
or admin alert is touched, and no row is deleted.

**Deliberately absent constraint.** There is NO constraint tying
`attention_resolved_at` to `gateway_verified_at`.
`app.services.verification.process_callback` does not gate on
`status == link_created`, so a payment whose `getLink` call timed out (the
request WAS delivered; only the response was lost) can still be settled by a
valid late browser callback. A constraint there would turn that legitimate
settlement into an `IntegrityError` and fail a real customer payment. The
service layer refuses to resolve an already-verified payment; nothing more is
enforced in the schema.

**Downgrade limitation (honest statement).** `downgrade` is non-destructive by
default: it moves the Alembic pointer back to `0012` and PRESERVES the columns,
because the pre-`0013` application simply ignores them and dropping them would
permanently destroy operator resolution history (actor, time, reason, note)
that is not reconstructable from anywhere else. The `payment_events` trail
keeps a `payment_attention_resolved` event per resolution, so the *fact*
survives a destructive drop, but the structured columns the worklist filters on
do not. Dropping them requires the explicit
`CENTRALPAY_DROP_ATTENTION_RESOLUTION=1` opt-in, and doing so makes every
previously resolved row reappear in `needs_attention` — the correct
fail-visible direction.

**Application rollback with `0013` already applied** (the normal rollback
shape, since migrations are forward-only): the older application simply does
not read these columns, so every previously resolved item reappears in
`needs_attention`. That is inconvenient but correct and fail-visible — the
older code has no way to know the item was closed, so it shows it. No
financial behavior differs, because nothing outside the operator-attention
views ever reads these columns. Rolling forward again restores the filtering
with no data loss.

## Production update procedure

Production updates should use a release tag configured in `/etc/centralpay-bridge/centralpay.env`:

```env
CENTRALPAY_UPDATE_REF=v0.6.0-rc4
```

Normal flow:

```bash
centralpay update --check
centralpay update
centralpay status
centralpay db-check --details --json
```

For a release tag, the updater verifies the release artifact, `SOURCE_COMMIT`, and `SHA256SUMS`, and binds the fetched tag commit to the verified `SOURCE_COMMIT` before deployment work proceeds.

A branch ref such as `main` is rejected by default. Development-only branch updates require:

```env
CENTRALPAY_UPDATE_ALLOW_DEV_REF=true
```

Do not set that on a normal production installation.

`CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` is a separate emergency escape hatch for release-tag asset verification problems and should not be part of routine operations.

## Before any upgrade

1. Read the target release notes and changelog.
2. Confirm current DB integrity:

   ```bash
   centralpay db-check --details --json
   ```

3. Create or confirm a recent validated backup.
4. Confirm disk space.
5. Confirm the target update ref is the intended release tag.
6. Run `centralpay update --check`.
7. Avoid manual schema edits.

## After any upgrade

Verify:

```bash
centralpay status
centralpay db-check --details --json
centralpay migrate current
```

Confirm:

- API/worker/db/caddy are healthy
- public `/health/ready` is HTTP 200
- migration revision is the expected head
- sequence checks are not behind
- no financial-integrity check fails
- pending/manual-review queues are understood

If the optional admin bot is enabled, verify its service and `/version`/health views as applicable.

## Restore instead of downgrade

If the new schema is incompatible with the old application and a roll-forward fix is not possible, the safe rollback mechanism is an explicit restore of the pre-update backup, understanding that this discards transactions created after that backup.

Use [BACKUP_RESTORE_FA.md](BACKUP_RESTORE_FA.md) and never improvise destructive SQL against the production database.

## Historical version notes

Older release notes and audit documents in this repository are snapshots. Their migration-head/status text may intentionally reflect their original commit. Use this guide plus the actual `alembic/versions/` chain for the current schema.
