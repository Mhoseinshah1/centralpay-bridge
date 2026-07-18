# Staging validation — 0.6.0-rc1

**Status: NOT PERFORMED — part of RELEASE BLOCKER B2.**

No CentralPay credentials (real or sandbox) and no bot-API endpoint are
available in the development environment, and none were requested — this
document must never contain production credentials. All gateway and bot
behavior in the test suite is exercised against local stubs.

**Consequence:** the strict response parsing added in 0.5.0-rc1
(explicit success allowlist, typed field parsing) is hardened against
every response shape we could enumerate, but it has **never seen a real
CentralPay response**. The same applies to `verify.php` idempotency
(verify-after-verify for the same order), real redirect flow timing, and
real bot `/api/payment` semantics.

## Validation matrix

| Area | Real | Mocked | Not tested |
|---|---|---|---|
| `getLink` request/response handling | | ✔ (stub, incl. malformed shapes) | real schema |
| `verify` success/failure/field parsing | | ✔ (stub) | real schema |
| verify-after-verify idempotency | | ✔ (assumed tolerated) | real behavior |
| Callback redirect flow (payer browser) | | ✔ (TestClient) | real redirect |
| One-time token + HMAC on real URLs | | ✔ | real gateway URL handling |
| Bot notification `POST /api/payment` | | ✔ (stub bot) | real bot |
| Fee: getLink charges payable, verify reports payable | | ✔ (stub) | real gateway with fee |
| Real gateway messages ("payment is paid", "payment type invalid") | | | real schema/text |
| Caddy TLS + header behavior | config validated | | real TLS traffic |
| PostgreSQL behavior | ✔ (real PG 16 in tests/CI) | | production sizing |
| Backup/restore round-trip | ✔ (real pg_dump/pg_restore) | | real host cron/timer |

## Required staging procedure (to close B2)

On a staging install (after `REAL_HOST_VALIDATION.md` steps 1–4), with
**staging/sandbox** CentralPay credentials only:

1. Create a small real payment via `POST /api/custom-payment`; complete
   it in the CentralPay flow; confirm: callback signature+token accepted,
   verify succeeds, amounts/user-id match, payment reaches
   `bot_notify_pending`, worker delivers to a staging bot endpoint.
2. Replay the callback URL: must return the final page without
   re-contacting verify.
3. Attempt a second verify for the same order (e.g. by replaying before
   the first commit in a controlled test) — record how the real gateway
   responds; confirm our conservative handling matches.
4. Force a mismatch (wrong amount expectation in a test record) — must
   route to manual review, never credit.
5. Confirm `FIRST_PAYMENT_GUARD_ENABLED=true` produces the one-time
   critical alert on the first verified payment.
6. **Fee flow (dynamic fee):** set a staging fee (`centralpay fee set 10
   --note "staging"`), create a payment for a small original amount, and
   confirm: the CentralPay page asks the payer for the PAYABLE amount
   (original + fee, TOMAN unit), verify reports the payable amount and
   the payment verifies, and the bot notification carries only
   `order_id`/`actions` and the bot credits the ORIGINAL amount. Record
   the real gateway response shapes for a fee-bearing payment, including
   the exact "payment is paid" / "payment type invalid" message texts —
   our handling of those strings currently rests on stub assumptions.
7. Confirm the fee is disclosed to the payer in the bot's purchase flow
   BEFORE the payment link is issued (operator/bot-side obligation).
8. Record dates, versions, redacted request/response shapes (no keys,
   no card data beyond last4) in this file.

## Results

_None recorded. Blocker open._
