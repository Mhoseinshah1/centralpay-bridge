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
- **Two identity scopes (never the shared id):**
  - `telegram_user` — a valid Telegram id was supplied. Identity key
    `tg:<id>`: the same user maps to the same gateway id across orders, and two
    different users never share one.
  - `order_fallback` — no usable identity. Identity key `order:<bot_order_id>`:
    stable across retries of that one order, isolated from every other order, so
    at worst an order shares nothing with any other. The two key prefixes cannot
    collide (`tg:` is digits; `order:` carries the opaque order id).
- **Derivation.** The identity key is mapped to a stable numeric gateway
  `userId` via keyed HMAC over a **dedicated** `CENTRALPAY_PAYER_ID_SECRET`
  (never reused from any other secret). The mapping lives in
  `centralpay_payer_identities` (`UNIQUE(customer_key_hash)` — the column keeps
  its historical name but now stores the keyed hash of the *scoped identity
  key*, `UNIQUE(gateway_user_id)`, `gateway_user_id > 0`). The raw Telegram id is
  never stored (only the keyed, non-reversible hash) and never logged — only a
  12-char fingerprint and the identity scope reach logs/events. (`bot_order_id`
  remains stored/logged as the documented idempotency key.)
- **Determinism & safety:** the same identity key always resolves to the same
  gateway id; two different identities never share one (DB uniqueness + a
  deterministic re-derivation counter on the astronomically rare HMAC collision);
  stored ids are immutable, so restarts, redeploys, backup/restore, and changing
  *other* secrets never move them.
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
    a link exists → deterministically **adopts** the Telegram identity;
  - the same order once a link exists is **never** re-pointed: the (already
    isolated) order-scoped link is returned unchanged;
  - a *different* Telegram user on an existing order is rejected
    (`409 duplicate_order_customer_mismatch`) and never handed the first user's
    link.
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

## Database migration

`alembic/versions/0007_payer_identity.py`:
- creates `centralpay_payer_identities` with the two unique constraints and the
  positive-id CHECK;
- adds nullable `payments.payer_identity_id` (FK, `ON DELETE RESTRICT`, indexed),
  `payments.payer_identity_type` (`telegram_user`/`order_fallback`), and
  `payments.payer_derivation_version`.

Non-destructive: existing payment rows are untouched (their shared-id snapshot is
preserved, `payer_identity_id` stays NULL, active links stay valid).

**Rollback-safe / re-entrant.** An application rollback can leave the database at
revision 0007 while the code expects 0006. Rolling the code forward again must
not fail, so:
- `upgrade()` is **idempotent** — every object is created behind an
  `IF NOT EXISTS` inspection, so re-running it against a DB that already has the
  table/columns is a no-op, never a "already exists" error;
- `downgrade()` is **non-destructive by default** — it only moves the Alembic
  pointer back to 0006 and *preserves* the mapping table and columns, so no payer
  identity is lost and no active link is invalidated. To actually drop the schema
  an operator opts in explicitly with `CENTRALPAY_DROP_PAYER_IDENTITY=1`.

Tested on PostgreSQL 16: upgrade → (stamp 0006) idempotent re-upgrade →
non-destructive downgrade (schema retained) → upgrade, and against a DB
containing historical (legacy shared-id) payments. Forward-only in production as
usual; the recovery path above exists only for the rollback edge case.

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
- Migration up → idempotent re-upgrade → non-destructive downgrade → up, plus
  historical rows, on PostgreSQL 16.
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
- A legacy pre-fix row (`payer_identity_id NULL`) that *already* has a live link
  is returned as-is on retry regardless of who retries the same `bot_order_id`;
  its link uses the old shared id. This is the documented forward-only behavior
  for historical links (bot order ids are unique per order upstream, so this is
  not a new cross-user path); new rows never behave this way.
- When a Telegram-scoped order is retried without the id (kept, per the rules
  above), the order-scoped mapping resolved for that retry is left unused in
  `centralpay_payer_identities`. Harmless: isolated, never attached to a payment,
  no financial or privacy effect.

## Remaining unknowns

- CentralPay's exact `userId` semantics and accepted range (the derived range is
  a documented assumption; if wrong, `getLink` fails closed, never leaks).
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
