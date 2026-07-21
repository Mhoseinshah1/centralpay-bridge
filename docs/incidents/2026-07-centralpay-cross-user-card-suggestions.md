# Incident: cross-customer CentralPay card-suggestion leak

**Severity:** HIGH (privacy / data isolation)
**Status:** remediation implemented (code); gateway-side confirmation + staging validation pending
**Data involved:** masked (last-digits) card suggestions shown on the CentralPay
payment page. No full card numbers, secrets, or bridge data were exposed by the
bridge. Whether CentralPay retained/exposed more is an open question for their
support team (see below).

> No real customer identifiers or card data appear in this document.

## Impact

On the CentralPay payment page, the "source card" (کارت مبدا) suggestion list
showed masked cards belonging to *different* customers: a payer could see
card-history suggestions seeded by other payers. This is a cross-user privacy
leak of (masked) financial data.

## Detection

Reported from production: the card-suggestion dropdown on the CentralPay page
mixed multiple customers' masked cards. Confirmed **not** Chrome autofill.

## Root cause

The bridge sent **one global `CENTRALPAY_USER_ID` as the gateway `userId` for
every payment** (`app/services/payments.py`, `_ensure_payment_row`, previously
`gateway_user_id=settings.centralpay_user_id`). CentralPay evidently scopes
saved-card history/suggestions by `userId`, so all payers were presented to the
gateway as a single payer identity and therefore shared one saved-card history.

- **Code-level proof (certain):** every `getLink` call carried the same
  `userId`; unit/integration tests now assert distinct per-customer values.
- **Gateway-level cause (inferred, to confirm):** that `userId` scopes saved-card
  suggestions. This is consistent with the symptom and with `userId` being a
  payer/customer reference, but CentralPay's `userId` semantics are **not**
  documented (see `CENTRALPAY_CONTRACT_ASSUMPTIONS.md`) and were never validated
  against the real gateway. Must be confirmed via CentralPay support + a staging
  black-box test before payments reopen.

## Affected versions

All versions up to and including the deployed `0.6.0-rc1` (commit
`64717001…`). Every payment created before this fix used the shared payer id.

## Immediate containment

1. **Emergency stop (no code deploy needed if already on this build):** set
   `PAYMENT_CREATION_ENABLED=false` and restart the API. New payment links stop
   immediately with a fixed `503 payment_creation_disabled`. **Callback
   verification of in-flight payments is unaffected.**
2. On this build, payment creation also **fails closed** when
   `CENTRALPAY_PAYER_ID_SECRET` is unset (`503 payment_creation_unavailable`), so
   it can never silently fall back to the shared identity.

## Permanent remediation

Isolation must not break the upstream sales bot, whose contract keeps the three
original required fields (`api_key`, `amount`, `order_id`) and forwards the
end user's Telegram id only *optionally*. The fix derives an isolated gateway
payer id from whatever identity is available, and **never** falls back to the
shared id.

- **Original required fields only.** `POST /api/custom-payment` still requires
  exactly `api_key`, `amount`, `order_id` (JSON, JSON-string, urlencoded, and
  text/plain bodies). No new required field, so existing callers keep working.
- **Optional end-user identity via aliases.** The bot may send a Telegram
  numeric id under any of `user_id`, `userId`, `uid`, `chat_id`, `telegram_id`,
  in the body (all supported formats) **or** the query string. A value is used
  only if it is a valid positive integer within int64 (booleans and non-ASCII
  digits are never coerced); an absent/invalid alias is silently ignored, never
  rejected.
- **Two identity scopes (never the shared id), explicit schemes (revision 2 —
  matching the reference mirza-cpanel behavior by product requirement):**
  - `telegram_user` / scheme `telegram_raw_v1` — a valid Telegram id was
    supplied (positive, ≤ 2^52−1 per the Bot API). The gateway `userId` **is
    the exact Telegram id** — no hashing, remapping, truncation, modulo, or
    alternate allocation — so the same user trivially gets the same id across
    every order and two different users can never share one.
  - `order_fallback` / scheme `order_hmac_v1` — no usable identity. A keyed-HMAC
    id (dedicated `CENTRALPAY_PAYER_ID_SECRET`) is derived per `bot_order_id`
    inside the **reserved range `[6.0e15, 6.001e15)`**, which starts strictly
    above every valid Telegram id — asserted in code, so the two numeric
    namespaces cannot collide by construction. Stable across retries of one
    order, isolated from every other order.
  - scheme `historical_hmac_v1` — every mapping row created by the retired
    keyed-HMAC derivations (customer-era and v1 tg/order). Immutable.
  The scheme is stored per mapping row (`identity_scheme`) and never inferred
  from the numeric value.
- **Mapping table & collision policy.** `centralpay_payer_identities` keeps
  `UNIQUE(customer_key_hash)` (the keyed, non-reversible lookup hash of the
  scoped identity key) and `UNIQUE(gateway_user_id)`. An `order_hmac_v1`
  collision re-derives deterministically. A **raw Telegram id is never
  re-derived**: if its numeric value is already owned by a historical mapping,
  or equals the legacy shared id, creation **fails closed**
  (`503 payer_identity_conflict` + an actionable `payer_identity_collision`
  audit event naming the occupying row id — never the raw id). One user is
  never handed another identity's mapping and an id is never silently altered.
- **Privacy (explicit product tradeoff).** `gateway_user_id` now intentionally
  CONTAINS the raw Telegram id for `telegram_raw_v1` rows and is sent to
  CentralPay — an explicit product requirement. Everywhere else the id stays
  restricted: it is never logged, audit events no longer carry
  `gateway_user_id` values at all (identity-created, adoption, and
  verify-mismatch events carry scheme/type/fingerprint only), and admin/ops
  output reports counts and scheme labels. (`bot_order_id` remains
  stored/logged as the documented idempotency key.)
- **Determinism & safety:** stored ids are immutable, so restarts, redeploys,
  backup/restore, and changing *other* secrets never move them; raw Telegram
  ids are secret-independent by definition, and fallback ids depend only on
  the dedicated payer secret.
- **Snapshot & verification unchanged in spirit:** each payment snapshots
  `gateway_user_id` (+ `payer_identity_id`, `payer_identity_type`,
  `payer_derivation_version`). Callback verification still compares CentralPay's
  reported `userId` to that snapshot (mismatch → manual review). **Historical
  payments keep their old shared-id snapshot and keep verifying correctly.**
- **Duplicate-order & reconciliation safety (never cross payer identities):**
  - same order + same identity → idempotent (existing link returned);
  - a retry that merely *dropped* the optional Telegram id keeps the established
    Telegram identity (never downgraded to per-order);
  - an order first seen without an identity, retried with a Telegram id **before**
    a link exists → deterministically **adopts** the exact Telegram id;
  - the same order once a link exists is **never** re-pointed: the (already
    isolated) order-scoped link is returned unchanged;
  - a *different* Telegram user on an existing order is rejected
    (`409 duplicate_order_customer_mismatch`) and never handed the first user's
    link;
  - **historical HMAC rows: retries reuse stored snapshots.** A retry whose
    identity matches the stored mapping under the retired v1 hash is recognized
    (keyed comparison, no guessing) and reuses the stored HMAC snapshot —
    pre-link or linked — so history is never re-derived to the raw scheme; a
    different user on such an order is still rejected.
- **Legacy in-flight rows are healed, not exempt.** A pre-fix row that never
  produced a link (status `created`/`getlink_failed`, `payer_identity_id NULL`)
  would otherwise mint a *new* link under the shared id on the next retry; it now
  **adopts** the resolved isolated identity before `getLink`
  (`payment_payer_identity_adopted` audit event). Already-`link_created`/
  verified rows are left untouched (their link already exists; forward-only).
- **Legacy shared id excluded from the derived range.** Because historical
  payments used `CENTRALPAY_USER_ID` with no mapping row, `UNIQUE(gateway_user_id)`
  cannot stop a brand-new identity from HMAC-landing on it; the resolver treats
  that value as reserved and re-derives, so new identities never share the
  historical pool's id either.
- **Fail-closed guards unchanged.** `PAYMENT_CREATION_ENABLED=false` stops new
  links (`503 payment_creation_disabled`); an unset `CENTRALPAY_PAYER_ID_SECRET`
  (or one shorter than 16 chars, rejected at config load) fails closed
  (`503 payment_creation_unavailable`) rather than falling back to a shared id.
- **Legacy marker & audit:** `payer_identity_id IS NULL` marks payments created
  under the legacy shared id. `python -m app.ops privacy-audit` reports counts
  only (legacy vs isolated payments, mapping count, duplicate gateway ids — expected
  zero, newest legacy payment time, guard state). `/health/details` reports the
  `payment_creation` guard state.

## Database migrations

**0007 (`alembic/versions/0007_payer_identity.py`) — already deployed; kept
byte-exact.** Production executed the original 0007 (mapping table +
`payments.payer_identity_id`/`payer_derivation_version`) and `alembic_version`
is `0007`. Alembic never re-runs an applied revision, so 0007 is **never edited**
to deliver new schema — it stays exactly as merged in PR #44.

**0008 (`alembic/versions/0008_hybrid_payer_identity.py`) — new.** Adds the
identity-scope column on top of the deployed state:
- `payments.payer_identity_type VARCHAR(16) NULL` + CHECK
  `ck_payments_payer_identity_type_valid` (NULL, `telegram_user`, or
  `order_fallback`);
- **no backfill, by design**: 0007-era rows (payer_identity_id set under the
  retired `customer_id` scheme) have no determinable scope — the raw identity is
  intentionally not stored — so they keep `NULL` as the explicit
  historical/untyped marker (same as pre-0007 legacy rows) and are never guessed
  to be Telegram identities. The application handles both historical shapes
  explicitly (`_reconcile_identity`); `privacy-audit` reports them as
  `untyped_isolated_payments`.

**Rollback-safe / recovery-safe.**
- 0008 `upgrade()` no-ops for objects that already exist (a DB that briefly
  carried the column still upgrades cleanly), so a rollback that leaves the DB
  ahead of the pointer never blocks rolling the code forward again.
- 0008 `downgrade()` is **non-destructive by default** — it only moves the
  Alembic pointer back to 0007 and preserves the column + CHECK; dropping is an
  explicit opt-in (`CENTRALPAY_DROP_PAYER_IDENTITY=1`).
- The current production state (DB at 0007, app possibly rolled back to the
  pre-0007 code at `b897e69` — a code rollback never moves schema) recovers by
  deploying this build and running `alembic upgrade head`: exactly 0008 runs, no
  schema downgrade, no data loss.

Proven on PostgreSQL 16 by `tests/integration/test_migration_0008_pg.py`, which
starts from the EXACT deployed original-0007 schema (and asserts 0007 in this
tree still is that original), seeds legacy + 0007-era rows via raw SQL, runs
alembic in a subprocess, and verifies: 0008 applies, historical rows keep
`NULL` scope with zero data loss, the CHECK enforces the value set, re-upgrade
after `stamp 0007` is a no-op, the downgrade preserves the schema, and the new
application serves existing links and verifies existing callbacks against
their stored snapshots.

**0009 (`alembic/versions/0009_identity_scheme.py`) — new (raw-id revision).**
Production is at 0008; 0009 is the forward-only follow-up (0007/0008 are
applied and never edited):
- adds `centralpay_payer_identities.identity_scheme VARCHAR(32) NOT NULL`
  with server default `historical_hmac_v1` + CHECK
  `ck_payer_identities_identity_scheme_valid` (`telegram_raw_v1`,
  `order_hmac_v1`, `historical_hmac_v1`);
- the server default is the **non-destructive backfill**: every row that exists
  when 0009 runs was created by a retired keyed-HMAC derivation, so the
  collective `historical_hmac_v1` label is accurate — and it stays accurate for
  any row a not-yet-updated 0008-era application inserts during a rollback
  window;
- existing mappings, payment snapshots, and live links are untouched;
- `upgrade()` no-ops for already-present objects; `downgrade()` is
  non-destructive by default (pointer-only; dropping requires
  `CENTRALPAY_DROP_PAYER_IDENTITY=1`).

Proven on PostgreSQL 16 by `tests/integration/test_migration_0009_pg.py`: from
the exact production-0008 schema with a seeded historical v1 payment,
`alembic upgrade head` runs exactly 0009; the historical mapping keeps its
derived id byte-for-byte and is labeled `historical_hmac_v1`; the CHECK rejects
unknown schemes; `stamp 0008` + re-upgrade no-ops; the downgrade preserves the
schema; and the new application sends the exact raw id for a fresh payment
while the historical payment's retry reuses its stored snapshot and its
callback still verifies.

## Backward compatibility (no upstream change required)

The three original required fields are unchanged, so the existing sales bot keeps
working **without modification**. The end-user Telegram id is accepted
*optionally* under any of `user_id`/`userId`/`uid`/`chat_id`/`telegram_id` (body
or query); when the bot forwards it, payers are isolated per Telegram user, and
when it does not, they are isolated per order. Either way the shared id is never
used for a new link.

For the strongest isolation the bot *should* forward the Telegram id (ideally as
`user_id`), but this is an enhancement, not a hard requirement. A compatibility
mode that reuses the shared id is intentionally **not** offered — it cannot be
made safe.

## Validation evidence

- `app/services/payer_identity.py` derivation: deterministic, in-range, stable,
  collision-safe; `tg:`/`order:` keys cannot collide (`tests/test_payer_identity.py`).
- Alias contract: `_coerce_telegram_id` accepts a positive int64 / ASCII-decimal
  string only (rejects bool, 0, negative, non-ASCII digits, over-range);
  `_extract_telegram_user_id` precedence (body aliases in order, then query); a
  3-field request and every alias name are accepted; an invalid alias falls back
  to per-order isolation instead of a 4xx (`tests/test_payer_identity.py`, the
  legacy-body / urlencoded parser suites).
- Alias parsing across every body format (JSON object, JSON-string, urlencoded,
  text/plain) and the query string (parser suites).
- Isolation end-to-end: two Telegram users → two distinct gateway `userId`s,
  never the legacy id; same user across orders → one id; no identity → per-order
  isolation; reconciliation (adopt-before-link, keep-on-drop, no-switch-after-
  link, reject-different-user) (`tests/test_payer_identity.py`).
- Concurrency on real PostgreSQL: concurrent same identity → one mapping;
  concurrent different identities → all distinct; stable across reconnect
  (`tests/integration/test_payer_identity_pg.py`).
- Historical payment still verifies against its own snapshot; **no raw Telegram
  id** in logs/events/errors, only a presence flag + fingerprint
  (`tests/test_payer_identity.py`, parser suites).
- Fail-closed: missing/short payer secret and `PAYMENT_CREATION_ENABLED=false`
  both refuse without creating a row or mapping (`tests/test_payer_identity.py`).
- Production upgrade/recovery path on PostgreSQL 16
  (`tests/integration/test_migration_0008_pg.py`): exact deployed original-0007
  schema (+ seeded legacy and 0007-era rows) → `alembic upgrade head` runs 0008
  → no data loss, NULL scopes preserved, CHECK enforced, re-upgrade after
  `stamp 0007` no-ops, downgrade preserves schema, and the new app serves the
  existing links and callbacks.
- 0007-era untyped rows handled explicitly, never guessed or rejected
  (`tests/test_payer_identity.py`).
- Full suite (1100+ tests, unit + PostgreSQL), ruff, strict mypy — see the PR.

## Staging black-box validation (before reopening payments)

Separate **code-level** proof (distinct `userId` sent — covered by tests) from
**gateway-level** proof (no cross-user suggestions in the real UI):

1. User A: create a link (forward Telegram id A, e.g. `user_id=A`), pay/seed a
   card on the CentralPay page.
2. User B (different Telegram id), clean browser profile/device: create a link
   and open the CentralPay page.
3. Expected: CentralPay receives different `userId` values (verify in the bridge
   `centralpay_getlink_ok` events / request capture) **and** B sees none of A's
   card suggestions. A and B may each retain their own independent history if the
   gateway supports it. (Also spot-check the no-identity path: two orders with no
   alias must still send two different `userId`s.)

Do not mark the incident resolved until both the code-level and gateway-level
checks pass.

## Rotation strategy

`CENTRALPAY_PAYER_ID_SECRET` is **not** a routine-rotation secret. Because the
raw identity (Telegram id / order id) is never stored, an existing mapping cannot
be re-keyed, so rotating the secret would give returning identities new ids (new
histories). Stored mappings and their gateway ids are immutable across a
rotation; a deliberate scheme change is expressed by bumping `DERIVATION_VERSION`
(and the domain strings) in `app/services/payer_identity.py`, which affects only
identities first seen afterward. Treat any rotation as a planned migration.

## Known residuals (low)

- An authenticated caller can create a `centralpay_payer_identities` mapping row
  (and one `centralpay_payer_identity_created` event) with a request that is
  ultimately rejected for amount/status reasons on an existing order — the
  identity reconciliation legitimately needs the resolved identity first. Bounded:
  authenticated (valid inbound API key), rate-limited, tiny rows, no financial
  effect, no card/identity data. Not the leak; noted for completeness.
- A historical row that *already* has a live link — legacy
  (`payer_identity_id NULL`, shared id) or 0007-era untyped
  (`payer_identity_id` set, `payer_identity_type NULL`, customer-scoped id) — is
  returned as-is on retry regardless of who retries the same `bot_order_id`.
  This is the documented forward-only behavior for historical links (bot order
  ids are unique per order upstream, so this is not a new cross-user path); new
  rows never behave this way.
- When a Telegram-scoped order is retried without the id (kept, per the rules
  above), the order-scoped mapping resolved for that retry is left unused in
  `centralpay_payer_identities`. Harmless: isolated, never attached to a payment,
  no financial or privacy effect.
- **Raw Telegram ids are disclosed to CentralPay** (explicit product
  requirement — the gateway `userId` is the user's exact Telegram id). The
  gateway can therefore correlate payments to Telegram accounts; this is
  accepted by design and confined to the gateway payload — logs, audit events,
  and admin output still never carry the raw id.
- A user whose exact Telegram id is already occupied by a historical HMAC
  mapping (the old derived range `[1, 2e9]` overlaps real Telegram ids) — or
  equals the legacy shared id — cannot create payments (fail-closed
  `503 payer_identity_conflict`) until an operator resolves the
  `payer_identity_collision` event (e.g. by verifying the historical mapping is
  orphaned and retiring it manually). Probability per user ≈ number of
  historical mappings / 2e9; never silent, never cross-user.

## Remaining unknowns

- CentralPay's exact `userId` semantics and accepted numeric range. The bridge
  now requires 64-bit acceptance: raw Telegram ids (documented < 2^52; today up
  to ~1e10 — already beyond int32) and the reserved order-fallback range
  `[6.0e15, 6.001e15)` (see `CENTRALPAY_CONTRACT_ASSUMPTIONS.md`). If a value
  is rejected, `getLink` fails closed — ids are never truncated or remapped to
  fit. Confirm with support (question 2 below) before go-live.
- Whether/what CentralPay retained or exposed for the previously shared id.
- Whether any other gateway/merchant/session key also influences suggestions.

## CentralPay support escalation

Send the following (no card numbers, no secrets):

### فارسی

با سلام،
ما یک نشتِ حریمِ خصوصیِ میان‌کاربری در صفحهٔ پرداخت مشاهده کرده‌ایم: در بخش
«کارت مبدأ»، کارت‌های ماسک‌شدهٔ مشتریانِ مختلف به یکدیگر پیشنهاد می‌شوند. تا این
لحظه، سرویس ما برای همهٔ تراکنش‌ها یک `userId` ثابت به `getLink` ارسال می‌کرده
است. لطفاً موارد زیر را روشن کنید:
1. آیا `userId` در `getLink` دامنهٔ پیشنهادِ کارت‌های ذخیره‌شده را تعیین می‌کند؟
2. محدوده و نوعِ مجازِ `userId` (بازهٔ عددی) چیست؟
3. آیا سابقهٔ کارت مربوط به `userId`ِ مشترکِ قبلی قابلِ پاک‌سازی است؟
4. آیا می‌توان پیشنهادِ کارت را موقتاً برای این پذیرنده غیرفعال کرد؟
5. آیا کلید دیگری (پذیرنده/مشتری/نشست) بر پیشنهادها اثر می‌گذارد؟
6. آیا کارت‌های ماسک‌شده به همهٔ لینک‌هایی که آن `userId` مشترک را داشتند نمایش
   داده شده‌اند؟
7. مدت نگه‌داری داده و مراحل پیشنهادیِ شما برای رفع و پاک‌سازیِ این رخداد چیست؟
با تشکر.

### English

Hello,
We have observed a cross-customer privacy leak on the payment page: in the
"source card" section, masked cards from different customers are suggested to
one another. Until now our service sent a single fixed `userId` to `getLink` for
every transaction. Please clarify:
1. Does the `getLink` `userId` scope saved-card suggestions?
2. What is the accepted `userId` range and type?
3. Can the card history for the previously shared `userId` be purged?
4. Can card suggestions be temporarily disabled for our merchant?
5. Does any other merchant/customer/session key influence suggestions?
6. Were masked card suggestions exposed to all links sharing that `userId`?
7. What is the data-retention period and your recommended incident remediation
   (including purge) steps?
Thank you.

## Customer notification

Deferred to the owner/legal team; not decided here.

## Timeline (placeholders)

- `YYYY-MM-DD HH:MM` — reported.
- `YYYY-MM-DD HH:MM` — root cause identified (shared `userId`).
- `YYYY-MM-DD HH:MM` — containment (`PAYMENT_CREATION_ENABLED=false`).
- `YYYY-MM-DD HH:MM` — fix merged / deployed.
- `YYYY-MM-DD HH:MM` — (optional) sales bot updated to forward the Telegram id.
- `YYYY-MM-DD HH:MM` — staging validation passed; payments reopened.
- `YYYY-MM-DD HH:MM` — CentralPay confirmation / purge complete.
