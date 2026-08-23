# CentralPay External Contract Assumptions

CentralPay is an external system outside this repository's control. This document records the behaviors the bridge relies on, how deviations are handled, and which behaviors must not be guessed.

This is a **living external-contract/risk catalog**, not proof that every undocumented CentralPay behavior has been formally guaranteed by the provider.

Real/sandbox CentralPay contract validation against the live gateway has not been formally recorded in this repository: [STAGING_VALIDATION.md](STAGING_VALIDATION.md) tracks it as release blocker **B2**, currently open, with no results recorded there. The strict parsing and state-machine behavior described below are hardened against every response shape this codebase could enumerate, but are based on the supported contract implemented in `app/centralpay.py`, exercised through local stubs and unit/integration tests — not an observed real response. Unknown or unrecognized response shapes fail closed rather than being guessed as success, and confirming real response shapes, verify-after-verify idempotency, and TOMAN-unit handling against the live/sandbox gateway remains an open release gate — see `STAGING_VALIDATION.md`'s required procedure to close B2. The bridge therefore remains fail-closed wherever a wrong assumption could affect money.

## Design rule

For each external assumption, ask:

> If this assumption is wrong, can the bridge silently credit, double-credit, or corrupt payment state?

The required answer is **no**. Wrong/unknown external behavior must result in an explicit error, retryable state, bounded reconciliation, or manual review — never guessed success.

## Transport

### CentralPay endpoints

The bridge calls the configured CentralPay getLink and verify endpoints over HTTPS with TLS verification enabled.

Assumptions:

- endpoints accept JSON POST bodies
- the configured API key authorizes the operations
- responses are parseable JSON objects for successful integrations

If transport fails, TLS fails, HTTP is non-success, or a body is not parseable under the supported contract, the operation fails with a fixed internal reason code. Raw gateway response text is not trusted or propagated.

Cleartext CentralPay HTTP is not supported.

## Success markers

The client accepts success only through the explicit success vocabulary implemented in `app/centralpay.py`.

It does **not** infer success merely because:

- `data` exists
- an object is truthy
- a message sounds successful
- HTTP status alone is 200

Unknown success/failure shapes fail closed.

If CentralPay changes its success vocabulary, the correct response is to inspect a sanitized real response, update the parser deliberately, add regression tests, and release the change — not to broaden parsing heuristically.

## getLink request

The bridge sends the gateway-required payment-link fields, including:

- API key
- payment type (`deposit`)
- amount in TOMAN
- gateway `userId`
- numeric gateway `orderId`
- signed HTTPS return URL

### Amount unit

The bridge's financial unit is **TOMAN**.

If a provider/environment unexpectedly interprets the amount in another unit, verification must catch the mismatch because the gateway-reported amount must equal the payment's immutable `payable_amount`.

A unit disagreement therefore must not silently credit the payment.

### `userId`

The current payer-identity design may send:

- the exact Telegram numeric user ID (`telegram_raw_v1`), or
- an order-derived fallback ID in the reserved fallback namespace (`order_hmac_v1`)

Historical mappings retain `historical_hmac_v1` values exactly as stored.

The bridge never truncates/modulos a payer identity to fit an undocumented smaller integer range. If CentralPay rejects a valid bridge identity value, getLink fails closed and the compatibility problem must be resolved explicitly.

### `orderId`

CentralPay receives a separate numeric gateway order ID, not the original arbitrary bot order string.

The bridge assumes the numeric order ID can identify a gateway payment. Failed/retryable link creation may allocate a fresh gateway ID according to the payment service's state machine rather than repeatedly registering a potentially poisoned old ID.

### Redirect URL

The bridge expects successful getLink data to contain a payment redirect URL.

Before accepting it, the bridge validates its storage/transport contract: HTTPS, valid host/port, no userinfo, bounded length, no whitespace/control-character abuse.

A malformed or unsafe redirect fails closed.

## Return URL / callback behavior

The generated return URL contains the bridge-controlled parameters:

- `orderId`
- `ct`
- `sig`

Assumptions:

- CentralPay/browser eventually requests the supplied return URL after a normal payer flow, or the server-side reconciliation worker can recover a paid order if that callback is missed
- query parameters are not intentionally rewritten into an incompatible shape by the gateway

The callback handler rejects duplicated/mangled security parameters and invalid HMAC/token combinations.

A missed browser callback is **not** treated as payment failure. The payment remains recoverable through server-side reconciliation.

## verify request

The bridge sends the configured verify API key and gateway order ID.

On success, the bridge expects typed/safely-coercible financial data including:

- `referenceId`
- `amount`
- `userId`
- optional card data from which, at most, the final four digits may be retained

Missing or invalid financial fields do not become success; they fail closed or move the payment to manual review with fixed reason codes.

## verify amount

Gateway `amount` must equal the payment's immutable `payable_amount` exactly.

For a fee-bearing payment:

```text
payable_amount = original amount + fee_amount
```

If CentralPay reports only the original invoice amount, or any other amount, the payment is not accepted as verified settlement by the bridge.

## verify `userId`

Gateway `userId` must equal the `gateway_user_id` snapshotted on that payment.

A mismatch moves the payment into the conservative review path and never notifies the selling bot as a verified success.

## `referenceId`

The bridge expects a successful verify response to provide a non-empty reference ID that fits the database storage contract.

The value is validated before assignment/query/log/audit use. It must not contain forbidden control characters and must not exceed the shared storage/parser length bound.

A non-null reference ID must be unique across payments. A collision does not overwrite an existing payment; it is treated as a serious anomaly/manual-review condition.

## Verify-after-verify / idempotency caution

A special external-contract risk exists around calling CentralPay verify repeatedly after the provider has already considered an order paid/verified.

The bridge minimizes this surface:

- once verification is committed locally, duplicate callback/reconciliation paths short-circuit and do not call verify again
- concurrency is serialized so simultaneous successful paths do not intentionally verify the same already-committed payment twice
- diagnostic `centralpay reconcile ORDER_ID --verify` is gated/disabled by default because it is a real gateway call and should not be used casually as a read-only local inspection

A crash can still occur after the gateway responded successfully but before the local transaction committed. In that narrow crash window, recovery may need to call verify again because the bridge has no committed verification fact.

The safety design assumes a repeated verify in this crash-recovery situation will either:

- return a usable success again, or
- fail conservatively, leaving the payment visible/recoverable rather than falsely credited

Do not broaden repeated diagnostic verify usage without provider/staging evidence.

## Gateway “not paid”

A normal verify result indicating the order is not yet paid is not an infrastructure incident by itself.

Reconciliation may schedule another bounded attempt according to the configured age/tier/max-age policy.

Monitoring/operations must distinguish ordinary unpaid orders from:

- transport/server error bursts
- stale worker heartbeat
- reconciliation exhaustion/aged-out work
- abnormal backlog

## Callback timing

Callbacks may be:

- delayed
- duplicated
- absent

The system is designed for all three:

- duplicate callback is replay-safe
- late callback can resolve an existing live payment without re-settling already verified state
- absent callback is recoverable by reconciliation while the payment remains eligible

No fixed browser-callback arrival time is a financial correctness requirement.

## Raw gateway text is untrusted

The bridge deliberately does not depend on free-form gateway error messages.

Do not:

- parse monetary meaning from prose
- log raw gateway bodies
- store raw external error text in payment rows/events
- forward raw error text to Telegram
- expose it in public API responses

Map external behavior into the fixed internal reason-code vocabulary.

## TLS and certificate assumptions

The bridge relies on normal system trust for HTTPS certificate validation. SSL verification must never be disabled as an operational workaround.

DNS/provider outages become explicit transport failures; they are not converted into successful payment state.

## What this bridge intentionally does not assume

No correctness decision depends on:

- undocumented response headers
- ordering of unrelated JSON fields
- free-form error wording
- browser `Referer` privacy behavior
- a callback being delivered exactly once
- selling-bot HTTP 2xx proving customer balance credit
- infinite availability of CentralPay

## Selling-bot contract boundary

CentralPay verification and downstream selling-bot credit are separate trust boundaries.

The bridge tells the selling bot only:

```json
{
  "order_id": "original-bot-order-id",
  "actions": "custom_payment_verify"
}
```

with the configured Token header.

A downstream HTTP 2xx is recorded as `bot_notify_accepted`; it is not a provider-independent proof of customer balance state.

Ambiguous downstream delivery is therefore handled according to `safe` vs `idempotent` notification mode rather than treated as CentralPay verification state.

## How to update this document

When a real provider behavior is learned or changes:

1. capture only sanitized evidence; never commit keys, full callback secrets, full PAN, or raw sensitive payloads
2. identify whether current code fails safe
3. add/adjust parser or state-machine tests
4. update this catalog
5. update staging/validation evidence when appropriate
6. do not rewrite an older historical audit to make it appear it knew the new contract

Historical provider-validation notes remain in [STAGING_VALIDATION.md](STAGING_VALIDATION.md). Current engineering behavior is governed by source, tests, [AGENTS.md](AGENTS.md), and this living catalog.
