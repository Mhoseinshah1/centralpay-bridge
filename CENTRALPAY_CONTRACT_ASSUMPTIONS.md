# CentralPay contract assumptions (final financial audit)

Every assumption this bridge makes about `getLink.php` and `verify.php`,
with its failure classification. **None of these has been observed
against the real gateway** (release blocker B2 / `STAGING_VALIDATION.md`);
the design goal is that no unverified assumption can produce false
success — the worst case of every wrong assumption below is a safe
failure or a manual-review outcome, never a silent credit.

Classifications: **SAFE-FAIL** (wrong assumption → explicit error, no
money risk) · **REVIEW** (wrong assumption → manual review) ·
**STAGING** (must be confirmed on the real gateway before go-live).

## Transport (both endpoints)

| Assumption | If wrong | Class |
|---|---|---|
| Endpoints are `POST {BASE}/getLink.php` and `POST {BASE}/verify.php` with JSON bodies | connection/HTTP error → `centralpay_connection_error`/`centralpay_rejected` | SAFE-FAIL + STAGING |
| Success responses use HTTP 200 | non-200 → rejected, payment stays recoverable | SAFE-FAIL |
| Responses are JSON objects | non-JSON / non-object → `centralpay_invalid_response` | SAFE-FAIL |
| 15s timeout suffices | timeout → transport error; creation retries with a fresh gateway id; verification retries on a later callback | SAFE-FAIL |

## Success detection (both endpoints)

- A response is successful **only** with an explicit positive marker:
  `success: true` (bool or `"true"`), or `status` ∈
  {1, "1", "success", "ok", "completed", "done", "true"} (case/whitespace
  tolerant, booleans excluded from numeric coercion).
- Explicit failure markers: `success: false`, `status` ∈
  {"error","failed","fail","0","-1"}, or a truthy `error` field.
- Anything else → `gateway_response_invalid` (never guessed).

If the real gateway uses a different success vocabulary, every payment
fails closed (`gateway_response_invalid`) — loud, recoverable, no money
risk, but the bridge is unusable until the allowlist is corrected →
**STAGING (critical to confirm before go-live)**.

## getLink.php request/response

| Assumption | If wrong | Class |
|---|---|---|
| Payload fields: `api_key`, `type:"deposit"`, `amount` (integer TOMAN), `userId`, `orderId` (numeric), `returnUrl` | rejection → `getlink` failure, retry with fresh id | SAFE-FAIL + STAGING |
| `data.redirectUrl` holds the payment URL; HTTPS, valid hostname, ≤2048 chars, no credentials/control chars | anything else → `gateway_invalid_redirect_url` | SAFE-FAIL |
| CentralPay redirects the payer to `returnUrl` **exactly as given** (our `orderId`/`ct`/`sig` params intact, each exactly once) | extra params are ignored by us; duplicated/mangled security params → 403, payment recoverable via re-request | SAFE-FAIL + STAGING |
| Amount unit is TOMAN (not Rial) | **unit mismatch would make every verify amount comparison fail → 100% manual review** — visible immediately, no silent credit | REVIEW + STAGING (critical) |
| A given `orderId` can be registered once; re-registering may be rejected | handled: failed/crashed attempts abandon the old id and retry with a fresh one | SAFE-FAIL |

## verify.php request/response

| Assumption | If wrong | Class |
|---|---|---|
| Payload: `api_key`, `orderId` | rejection → `verification_failed` (409), payer can retry | SAFE-FAIL |
| Success `data` carries `referenceId` (non-empty str/int), `amount` (int or digit-string, TOMAN), `userId` (int or digit-string), optional `cardNumber` | missing/mistyped → field reason codes → manual review | REVIEW |
| Verify is **idempotent**: re-verifying an already-paid order returns success again (needed only for the crash-before-commit window) | if not idempotent, a crash-window retry gets a failure → payment stays `link_created`, payer holds a paid-but-unverified order → callback keeps failing → visible, manual review via operator; **no double credit possible** | REVIEW + STAGING (critical) |
| Verify success means money moved | if verify can return success for unpaid orders, the gateway itself is broken; mitigations: amount/userId/reference matching, unique reference ids | STAGING |
| `cardNumber` may be full PAN | only last 4 digits stored, never logged | SAFE (privacy) |

## Callback (redirect back to the bridge)

| Assumption | If wrong | Class |
|---|---|---|
| Payer's browser is redirected to our signed `returnUrl` after payment | if the redirect never happens, no callback → payment stays `link_created`, payer complains, operator investigates; money moved at CentralPay but nothing credited — visible, recoverable via manual verify | REVIEW + STAGING |
| Callbacks may arrive late, repeatedly, or never | handled: no token expiry, replay-safe, at-most-once verify | SAFE |

## Undocumented behaviors (explicitly unassumed)

The bridge assumes **nothing** about: response HTTP headers, additional
JSON fields (ignored), error message text (never trusted, never parsed,
never logged raw), rate limits (transport errors are retried by the
payer/bot, not automatically), or TLS certificate details beyond system
trust (verification is never disabled).
