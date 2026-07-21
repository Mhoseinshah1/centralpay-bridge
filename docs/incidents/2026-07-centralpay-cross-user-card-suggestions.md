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

- **New required request field `customer_id`** (opaque, stable upstream customer
  identity) on `POST /api/custom-payment` — for JSON, JSON-string, and legacy
  form bodies. Requests without it are rejected (versioned validation error); the
  bridge never falls back to the shared payer id. Strictly validated: ≤128 chars,
  no control/format/bidi/zero-width characters, no whitespace-only/padded value.
- **Per-customer gateway payer id.** `customer_id` is mapped to a stable numeric
  gateway `userId` via keyed HMAC over a **dedicated** `CENTRALPAY_PAYER_ID_SECRET`
  (never reused from any other secret). The mapping lives in
  `centralpay_payer_identities` (`UNIQUE(customer_key_hash)`,
  `UNIQUE(gateway_user_id)`, `gateway_user_id > 0`). The raw `customer_id` is
  never stored — only a keyed, non-reversible `customer_key_hash` — and never
  logged (only a 12-char fingerprint appears in logs/events).
- **Determinism & safety:** the same `customer_id` always resolves to the same
  gateway id; two different customers never share one (DB uniqueness + a
  deterministic re-derivation counter on the astronomically rare HMAC collision);
  stored ids are immutable, so restarts, redeploys, backup/restore, and changing
  *other* secrets never move them.
- **Payment snapshot & verification unchanged in spirit:** each payment snapshots
  `gateway_user_id` (+ `payer_identity_id`, `payer_derivation_version`). Callback
  verification still compares CentralPay's reported `userId` to that snapshot
  (mismatch → manual review). **Historical payments keep their old shared-id
  snapshot and keep verifying correctly** — history is never rewritten.
- **Duplicate-order safety:** re-creating an order for the same customer + amount
  stays idempotent; a *different* customer reusing an order id is rejected
  (`409 duplicate_order_customer_mismatch`) and never handed the first customer's
  link.
- **Legacy marker & audit:** `payer_identity_id IS NULL` marks payments created
  under the legacy shared id. `python -m app.ops privacy-audit` reports counts
  only (legacy vs isolated payments, mapping count, duplicate gateway ids — expected
  zero, newest legacy payment time, guard state). `/health/details` reports the
  `payment_creation` guard state.

## Database migration

`alembic/versions/0007_payer_identity.py` (reversible):
- creates `centralpay_payer_identities` with the two unique constraints and the
  positive-id CHECK;
- adds nullable `payments.payer_identity_id` (FK, `ON DELETE RESTRICT`, indexed)
  and `payments.payer_derivation_version`.

Non-destructive: existing payment rows are untouched (their shared-id snapshot is
preserved, `payer_identity_id` stays NULL, active links stay valid). Tested on
PostgreSQL 16 upgrade → downgrade → upgrade and against a DB containing
historical (legacy shared-id) payments. Forward-only in production as usual.

## Backward compatibility & required upstream (sales bot) change

`customer_id` is **required** — the sales bot must be updated to send a stable,
opaque per-customer identifier (its internal customer/account id is ideal). It
must NOT be a raw phone number, email, username, IP, session id, order id, or a
per-order random value. Rollout:
1. Deploy this build with `PAYMENT_CREATION_ENABLED=false` (containment).
2. Update the sales bot to send `customer_id`.
3. Validate on staging (below), then set `PAYMENT_CREATION_ENABLED=true`.

Compatibility mode that reuses the shared id is intentionally **not** offered:
it cannot be made safe. (Option A — reject — was chosen over Option B.)

## Validation evidence

- `app/services/payer_identity.py` derivation: deterministic, in-range, stable,
  collision-safe (`tests/test_payer_identity.py`).
- Request contract: `customer_id` required + rejects null/int/bool/empty/
  whitespace/over-length/NUL/control/bidi/zero-width, no side effects on
  rejection (`tests/test_payer_identity.py`).
- Isolation end-to-end: two customers → two distinct gateway `userId`s, never the
  legacy id; same customer → one id; duplicate-order/customer rules
  (`tests/test_payer_identity.py`).
- Concurrency on real PostgreSQL: concurrent same-customer → one mapping;
  concurrent different customers → all distinct; stable across reconnect
  (`tests/integration/test_payer_identity_pg.py`).
- Historical payment still verifies against its own snapshot; no raw
  `customer_id` in logs/events/errors (`tests/test_payer_identity.py`).
- Migration up/down/up + against historical rows on PostgreSQL 16.
- Full suite, ruff, strict mypy, shellcheck, `docker compose config`, secret &
  dependency scans — see the PR.

## Staging black-box validation (before reopening payments)

Separate **code-level** proof (distinct `userId` sent — covered by tests) from
**gateway-level** proof (no cross-user suggestions in the real UI):

1. Customer A: create a link (customer_id = A), pay/seed a card on the CentralPay
   page.
2. Customer B (different customer_id), clean browser profile/device: create a
   link and open the CentralPay page.
3. Expected: CentralPay receives different `userId` values (verify in the bridge
   `centralpay_getlink_ok` events / request capture) **and** B sees none of A's
   card suggestions. A and B may each retain their own independent history if the
   gateway supports it.

Do not mark the incident resolved until both the code-level and gateway-level
checks pass.

## Rotation strategy

`CENTRALPAY_PAYER_ID_SECRET` is **not** a routine-rotation secret. Because the
raw `customer_id` is never stored, an existing mapping cannot be re-keyed, so
rotating the secret would give returning customers new ids (new histories).
Stored mappings and their gateway ids are immutable across a rotation; a
deliberate scheme change is expressed by bumping `DERIVATION_VERSION` (and the
domain strings) in `app/services/payer_identity.py`, which affects only
customers first seen afterward. Treat any rotation as a planned migration.

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
- `YYYY-MM-DD HH:MM` — sales bot updated to send `customer_id`.
- `YYYY-MM-DD HH:MM` — staging validation passed; payments reopened.
- `YYYY-MM-DD HH:MM` — CentralPay confirmation / purge complete.
