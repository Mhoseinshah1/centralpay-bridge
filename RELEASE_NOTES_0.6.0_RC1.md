# centralpay-bridge 0.6.0-rc1 — release notes

Release candidate. **Not production-ready**: the release blockers in
`RELEASE_RISK_REGISTER.md` (real-host install, real-gateway staging
evidence, live Telegram run, adversarial review, green tag-triggered
release workflow) remain open. This tag must not be published and no
real payment may be processed until they are closed.

## Headline: dynamic percentage service fee

The bridge can now add a percentage service fee on top of the bot's
invoice. The fee is paid by the payer through CentralPay and is
invisible to the selling bot.

### Money model and immutable snapshots

- `payments.amount` always remains the ORIGINAL bot invoice — exactly
  what the bot sent and what the bot credits.
- At payment creation, an immutable snapshot is written in the same
  transaction as the row insert: `fee_policy_id`, `fee_rate_bps`,
  `fee_amount`, `payable_amount`.
- Fee arithmetic is pure integer, round half up, never floats:
  `fee_amount = (amount * fee_rate_bps + 5000) // 10000`.
- Example: original 500 000 TOMAN at 10% → fee 50 000 → CentralPay
  charges 550 000; the bot credits its original 500 000.
- The snapshot never changes afterwards: duplicate orders, getLink
  retries after failure, and later fee changes all preserve it. Fee
  changes affect NEW orders only.

### getLink and verify behavior

- getLink is asked to charge `payable_amount` (original + fee).
- verify must report exactly `payable_amount`; any other value —
  including the original amount, i.e. a fee that was never collected —
  freezes the payment in `manual_review` with the
  `verify_payable_amount_mismatch` audit event. The bot is never
  notified for such a payment.
- `MIN_PAYMENT_AMOUNT_TOMAN` bounds the original amount;
  `MAX_PAYMENT_AMOUNT_TOMAN` now explicitly bounds the FINAL payable
  amount (`payable_amount_out_of_range`, rejected before any row,
  snapshot, or gateway call — never clamped).

### Unchanged bot notification payload

The bot notification remains exactly this JSON object and field set:

```
Token: BOT_NOTIFY_TOKEN

{"order_id": "<bot_order_id>", "actions": "custom_payment_verify"}
```

No amount, fee, payable, or reference field is ever sent to the bot.
A regression test parses the raw request body and asserts the exact
JSON object, the exact field set, and the Token header.

### Fee CLI and scheduled changes

- `centralpay fee status | set | schedule | history | cancel` —
  mutations are root-only and delegate to the typed Python operations
  command (argv arrays, no shell-generated SQL). Rates are 0–100 with
  at most two decimals; signs, exponents, separators, non-ASCII digits,
  and injection-shaped input are rejected.
- Policies live in the append-only `fee_policies` table (never an
  environment variable); selection is deterministic (`effective_at`
  DESC, then id DESC, cancelled excluded); a scheduled policy activates
  at exactly its `effective_at` with no restart; history is permanent
  and fully audited (`fee_policy_created/scheduled/cancelled`).
- `fee cancel` accepts only future scheduled policies; cancelling the
  effective policy is refused (it would silently fall back to an older
  rate) — the current rate changes only via explicit `fee set`.
- The admin Telegram bot gains a strictly read-only `/fee` command.
- The installer asks for the initial fee percentage (default 0) and
  applies it with `--ensure-initial`: it creates a policy only when no
  policy row has ever existed (scheduled/cancelled history counts), so
  a rerun never resets or injects a fee; concurrent reruns are
  serialized by a database advisory lock.

### Migration 0006

Creates `fee_policies`, adds the four snapshot columns to `payments`,
backfills every existing payment as fee-less (`fee_rate_bps = 0`,
`fee_amount = 0`, `payable_amount = amount` — financial meaning
unchanged), and adds CHECK constraints enforcing
`payable_amount = amount + fee_amount`, the rate range, and policy-row
consistency at the storage layer. Applied automatically by the
`migrate` service. `centralpay db-check` validates fee integrity
(including corruption the CHECKs cannot express) and never rewrites
financial history. Backups include the full fee-policy history.

## Real-host fix: script permissions

`install.sh`, `scripts/backup.sh`, and `scripts/centralpay` are now
committed with the executable bit (git mode 100755), and the installer
sets explicit modes: `backup.sh` 0750 `root:root`, `centralpay` 0755.
This fixes a confirmed real-host incident where a clone without the
executable bit broke the systemd backup timer with "Permission denied".

## Upgrading from 0.5.0-rc1

1. Take a backup: `centralpay backup` (also created automatically by
   `centralpay update`).
2. Set `CENTRALPAY_UPDATE_REF=v0.6.0-rc1` in
   `/etc/centralpay-bridge/centralpay.env` (release tags get artifact
   checksum verification; branch refs are development mode).
3. Run `centralpay update`. Migration 0006 applies automatically before
   api/worker start; existing payments are backfilled as zero-fee and
   behave exactly as before.
4. Optionally enable a fee:
   `centralpay fee set RATE --note "..."` (root). Until then the
   default is 0% and behavior is identical to 0.5.0-rc1.
5. Disclose the fee to payers in the bot's purchase flow BEFORE issuing
   payment links (operator obligation; see
   `PRODUCTION_CHECKLIST_FA.md`).

## Rollback limitations

- The database schema is **never downgraded**; `centralpay rollback`
  rolls back the application only.
- **After migration 0006 has been applied, the 0.5.0-rc1 application
  can no longer create payments** (it does not populate the NOT NULL
  `payable_amount` column). Rolling back to 0.5.0-rc1 therefore
  requires restoring the pre-update backup (which loses payments made
  after the upgrade) — or rolling forward instead. Prefer roll-forward;
  treat application rollback across 0006 as a disaster-recovery path,
  not a routine one.
- Fee policies and payment fee snapshots are financial history: they
  are never rewritten by any rollback or repair tooling.

## Real-provider validation status

**PRODUCTION_VALIDATION_STATUS: INCOMPLETE.** There is no evidence yet
of: a real CentralPay payment carrying a fee, CentralPay reporting the
final payable amount in verify (or the TOMAN unit against real
invoices), mock-bot notification delivery outside the test stubs, or
real bot integration. These are recorded as open blockers in
`RELEASE_RISK_REGISTER.md`, `STAGING_VALIDATION.md`,
`REAL_HOST_VALIDATION.md`, and `ADMIN_BOT_VALIDATION.md` — none of
those external tests are claimed to have occurred. The full audit
verdict (`CODE_FINANCIALLY_SOUND`, validation incomplete) is in
`FINAL_FINANCIAL_AUDIT.md`.
