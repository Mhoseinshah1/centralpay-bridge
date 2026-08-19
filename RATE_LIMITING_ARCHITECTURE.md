# Rate Limiting & Abuse Protection — architecture note

Audit performed before implementation, read directly from source
(`app/main.py`, `app/ratelimit.py`, `app/api/payments.py`,
`app/api/callback.py`, `app/api/health.py`, `app/middleware.py`,
`app/config.py`, `app/exceptions.py`, `app/security.py`,
`app/services/payments.py`, `deploy/caddy/Caddyfile.template`, existing
tests) plus Graphify for route/dependency confirmation.

**Headline finding, same shape as the last two roadmap items on this
repo: rate limiting already exists.** `app/ratelimit.py` (`SlidingWindowLimiter`,
`RateLimiters`), `RateLimitedError` (429), and three wired-in checks in
`app/api/payments.py` / `app/api/callback.py` predate this task. This
document audits that existing implementation against the full brief and
implements the gaps found — it does not start from zero.

## 1. Attack surface

Caddy (`deploy/caddy/Caddyfile.template`) proxies **exactly five** paths to
the API container; everything else 404s at the edge:

| Path | Method | Class |
|---|---|---|
| `/api/custom-payment` | POST | Payment creation (B) |
| `/api/centralpay/callback` | GET | Gateway callback (C/E) |
| `/health/live` | GET | Health/liveness (A) |
| `/health/ready` | GET | Health/readiness (A) |
| `/static/*` | GET | Static font assets (StaticFiles, no app logic) |

`/health/details` (machine-readable operational detail) is registered in
the FastAPI app but **deliberately not in Caddy's `@public` matcher** —
reachable only on the internal Docker network. An existing test
(`test_health_details_not_publicly_routed`) already pins this; unaffected
by this PR.

There is **no public payment-status/lookup endpoint** (task category D).
The only payer-facing status surface is the HTML page the signed callback
itself returns; there is nothing else to classify or protect here — noted
explicitly rather than inventing a limiter for an endpoint that doesn't
exist.

The admin bot (PR #66) is Telegram long-polling, not HTTP-reachable —
entirely out of scope for HTTP rate limiting; already numeric-ID
authorization-gated.

Internal-only worker/reconciliation/notification loops never go through
Caddy or these routes at all — nothing to accidentally rate-limit.

## 2. Current protections (pre-existing, confirmed by reading source)

- `app/ratelimit.py`: three **global** (not per-client) in-process sliding
  windows — `create` (120/min default), `invalid_api_key` (20/10min),
  `invalid_signature` (100/10min). `rate_limit_enabled` kill switch.
- `POST /api/custom-payment`: bounded body read (64KB, matches Caddy's edge
  `request_body max_size`), bounded legacy-body decoding (≤32 urlencoded
  pairs, ≤1 extra JSON-string decode layer), strict Pydantic schema
  (`StrictInt`/`StrictStr`, no silent coercion), constant-time API-key
  comparison (`hmac.compare_digest`), invalid-key attempts hit
  `invalid_api_key` before an `InvalidApiKeyError`, valid-key attempts hit
  `create` before any DB/gateway work.
- `GET /api/centralpay/callback`: HTTP-parameter-pollution guard (any
  repeated `orderId`/`ct`/`sig` rejected before signature check), HMAC
  signature verified (`hmac.compare_digest`) **before any DB work**,
  invalid signatures hit `invalid_signature` plus a separate bounded
  (`maxlen=1000`) `SignatureFailureTracker` deque that fires one
  deduplicated admin alert per storm window. The one-time callback token
  (consumed inside the row lock in `app/services/verification.py`) is the
  actual replay protection — audited, found adequate, not touched.
- Payment creation is idempotent by `bot_order_id` (unique + indexed
  column): a retry of an already-`LINK_CREATED` order returns the cached
  `redirect_url` with **zero gateway calls and zero new writes**
  (`app/services/payments.py:420-430`).
- Existing tests already cover: burst-limited create, invalid-key
  rate-limiting, invalid-signature rate-limiting, and unbounded-memory
  regression tests for both the limiter and the signature tracker
  (`tests/test_phase5_hardening.py`, `tests/test_callback_hardening.py`).

## 3. Trust boundary / client-IP handling — explicit hardening, not a proven default vulnerability

Caddy's `handle @public` block sets `X-Forwarded-Proto` and `X-Request-ID`
explicitly (the latter **overwritten** — "the proxy overwrites any
client-supplied value" per its own comment) but had **no explicit
`X-Forwarded-For` directive**.

**Correction from an earlier draft of this document**: that draft claimed
Caddy's unconfigured `reverse_proxy` default *appends* the resolved peer
to whatever value a client already sent in `X-Forwarded-For`, letting a
caller prepend a spoofed first hop. That claim was verified against
official Caddy documentation and is **wrong**. The documented default is
the opposite: *"For these X-Forwarded-\* headers, by default, [the proxy]
will ignore their values from incoming requests, to prevent spoofing"* —
i.e. `reverse_proxy` already substitutes its own resolved value for the
immediate connecting peer unless `trusted_proxies` is explicitly
configured (which this Caddyfile does not do). So there was no proven
pre-existing `X-Forwarded-For` spoofing vulnerability caused by Caddy's
default behavior.

What the audit *did* find missing was code-level, not a Caddy default:
the app itself had no visible, testable statement of this trust boundary
anywhere in the repository. `app/ratelimit.py`'s own docstring said
"spoofable headers like X-Forwarded-For are never consulted" — the app
didn't read the header at all, so no per-client keying was possible, and
reasoning about the boundary would have required knowing Caddy's internal
default rather than reading anything in this repository.

This is exactly why the existing limiters are global-only: without a safe
IP, per-client keying isn't possible. **Change applied** (kept, reframed
from a "fix" to hardening): the Caddyfile gains one explicit line,
`header_up X-Forwarded-For {remote_host}` — an *explicit* overwrite using
Caddy's own resolved TCP-peer placeholder, mirroring the exact pattern
already used for `X-Request-ID`. This makes the single-hop trust boundary
a visible, auditable, test-pinned line in this repository
(`tests/test_deployment.py`) instead of an implicit dependency on Caddy's
internal default — which is correct today, but was not something a
reader of this repo could verify without consulting Caddy's own docs —
and keeps behavior deterministic even if `trusted_proxies` is added
carelessly later or a future Caddy default changes. After this change the
app can trust `X-Forwarded-For` as a single, non-spoofable value
**because it is the only network path that can reach the app**
(confirmed: `docker-compose.yml` has no other published port for the API
service, and it carries no `deploy.replicas`/scaling config — single
container, single trusted proxy hop). This boundary is documented in code
(`app/clientip.py`) and enforced defensively: the resolver still validates
the header parses as exactly one syntactically valid IPv4/IPv6 address via
Python's `ipaddress` module, and falls back to the ASGI socket peer for
anything else (missing header, multiple comma-separated values, malformed
value) — so a stale/reverted Caddy config degrades to today's *global*
behavior (via a shared fallback key) rather than becoming exploitable.

`inbound_api_key` is a single shared secret (`Field(min_length=16)`, one
value) — there is no notion of multiple distinct API credentials in this
integration, so "per API credential" and "per client IP" are the same
dimension in practice here; a separate credential-keyed limiter would be
redundant with per-IP keying and is not added.

## 4. Gaps found and closed

1. **No per-client dimension on any limiter** — pure global counters mean
   one source (accidental retry loop or actual abuse) can exhaust the
   shared budget for every other legitimate caller. **Fixed**: `create`
   and `invalid_signature` gain a per-IP sliding window (bounded-cardinality,
   LRU-evicted store — see §6), *layered on top of*, not replacing, the
   existing global ceilings (dimension 5, "global emergency ceiling",
   satisfied by keeping the pre-existing global limiters as the outer
   backstop). `invalid_api_key` is intentionally left **global-only** —
   see rationale below.
2. **No `Retry-After` header** — `_bridge_error_handler` only ever returns
   `{status_code, {"error": {...}}}` with no custom headers.
   `RateLimitedError` never carried a duration. **Fixed**: `RateLimitedError`
   gains an optional `retry_after_seconds`; the handler adds the header
   when present.
3. **Idempotent retries are not exempted from the create limiter** — a
   retry of an *already-linked* order goes through the exact same
   pre-work rate-limit check as a brand-new order, even though
   `create_payment()` itself would answer it for free (no gateway call,
   no write). Under sustained load from unrelated new-order traffic, a
   legitimate retry could be rejected with 429 instead of receiving its
   already-issued link. **Fixed**: one cheap, indexed, read-only lookup
   (`bot_order_id` is unique + indexed) runs before the `create` limiter,
   via `find_safe_replay_redirect_url()` (`app/services/payments.py`).
   This adds one indexed point lookup per request — not a table scan, not
   a write, not a lock.
   >
   > **Regression, found and fixed in a follow-up commit**: the first
   > version of this exemption used "a row exists for this `bot_order_id`"
   > as the entire test — which incorrectly exempted every existing row,
   > including a `GETLINK_FAILED` one. `create_payment()` retries link
   > creation (a real `get_link()` gateway call, a fresh
   > `gateway_order_id`, state mutation) for `GETLINK_FAILED`, so that
   > exemption let a caller replay one `GETLINK_FAILED` order id
   > indefinitely and call the real gateway on every attempt while the
   > `create` limiter was fully exhausted — defeating the limiter for
   > exactly the traffic shape (retries against one order id) it exists to
   > bound. **Corrected exemption rule**: the limiter is skipped ONLY when
   > `find_safe_replay_redirect_url()` confirms the specific request is a
   > safe, work-free replay — `bot_order_id` already has a row in
   > `LINK_CREATED` status with a non-null `redirect_url`, the requested
   > `amount` matches the stored one, and (when a Telegram id is supplied)
   > it matches the stored `telegram_raw_v1` identity. Every other existing
   > row — `CREATED`, `GETLINK_FAILED`, `MANUAL_REVIEW`, an already-verified
   > order, an amount mismatch, or a different Telegram user — consumes
   > limiter budget exactly like a genuinely new order, since
   > `create_payment()` still does real work (or records a conflict event)
   > for all of those. The helper never calls `resolve_payer_identity()`
   > (which can itself create a new identity-mapping row), so the
   > limiter-exemption check itself is provably zero-mutation.
   >
   > **Second regression, found and fixed in a further follow-up commit**:
   > the corrected rule above still checked "different Telegram user" only
   > when the STORED row's identity type was `telegram_user`. But
   > `create_payment()` calls `resolve_payer_identity()` for the REQUESTED
   > identity BEFORE the row lock and BEFORE `_reconcile_identity()` ever
   > runs — and that call can create and commit a brand new
   > `CentralPayPayerIdentity` row (plus a `centralpay_payer_identity_created`
   > audit event) even when reconciliation would go on to harmlessly
   > "reuse" the stored identity. A stored `order_fallback` row retried
   > with an arbitrary, never-before-seen Telegram id — or a stored
   > `telegram_user` row retried with NO Telegram id — both hit that write
   > path while still being wrongly exempted, letting an authenticated
   > caller vary the identity on one live `LINK_CREATED` order repeatedly
   > and create unbounded identity-mapping rows/audit events without ever
   > consuming create-limiter budget. **Corrected exemption rule**: the
   > requested identity must exactly match the *shape* the stored mapping
   > was already resolved under, not merely fail to conflict with it —
   > stored `telegram_user` + a Telegram id supplied that equals
   > `payment.gateway_user_id`, OR stored `order_fallback` + NO Telegram id
   > supplied. Both re-derive the identical `identity_key` the stored
   > mapping already satisfies, so `resolve_payer_identity()` (or the
   > retired-scheme shortcut) is provably a no-write lookup. Every other
   > combination — including a legacy/untyped row with no stored identity
   > type at all, for which the retired-scheme lookup can never match —
   > is conservatively NOT exempt, even in cases where reconciliation would
   > still end up "reuse".
4. **Malformed-body floods are bounded but not rate-limited before
   parsing work begins.** Reviewed and judged acceptable, not changed:
   the work done before any auth/rate-limit check is already tightly
   bounded (64KB body cap, ≤32 form pairs, ≤1 extra JSON-decode layer,
   no recursion) and stateless (no DB, no gateway). Adding a limiter
   ahead of this adds a new global bottleneck for a cost that is already
   capped and cheap; not worth the complexity for this PR. Documented as
   a residual, low-severity item.

## 5. Endpoint classification (final)

| Class | Endpoint(s) | Limiter | Fail-open/closed |
|---|---|---|---|
| A — health | `/health/live`, `/health/ready` | **None** — must always stay reachable for orchestration | n/a |
| B — payment creation | `POST /api/custom-payment` | `invalid_api_key` (global, unchanged) on bad key; `create` (per-IP + global) on a genuinely new order; **idempotent retries of an existing order bypass `create` entirely** | fail-open (see §7) |
| C/E — gateway callback | `GET /api/centralpay/callback` | **Valid** signatures: never limited (signature verification is the primary, sufficient control; the task explicitly warns against dropping legitimate gateway retries here). **Invalid** signatures: `invalid_signature` (global, unchanged) + new per-IP layer | fail-open (see §7) |
| D — public lookup | *(does not exist)* | n/a | n/a |
| F — internal/admin | `/health/details`, admin bot (Telegram) | **None** — not Caddy-routed / not HTTP | n/a |

## 5a. Burst vs. sustained window — kept as one window per limiter

The brief lists "short burst window" and "longer sustained window" as
separate possible dimensions. The pre-existing design (and this PR's
extension of it) uses **one sliding window per limiter**, not two. A
sliding window (as opposed to a fixed-window counter) already smooths
both concerns reasonably — it has no reset-boundary cliff a fixed window
would — and doubling every limiter into an independent burst+sustained
pair would double the configuration surface (`rate_limit_*` settings)
for a marginal gain here, conflicting with "keep operational settings
understandable; do not over-configure." Judged not worth it for this
system's actual traffic shape (a small number of server-to-server
integrations, not a large open user base). Noted explicitly as a
deliberate scope decision, not an oversight.

## 6. Storage / backend decision

**Kept in-process, in-memory** (`threading.Lock`-guarded sliding windows) —
extended, not replaced.

Considered and rejected:
- **Redis**: not present anywhere in this project (`pyproject.toml`,
  `docker-compose.yml` — confirmed absent). Introducing a new stateful
  service, a new Compose dependency, a new failure mode, and new
  operational surface purely for this feature, when the existing
  in-process design already documents and accepts its single-replica
  scope, fails the task's own bar ("do not introduce it casually without
  a clear architecture reason").
- **PostgreSQL-backed counters**: every request would need a DB
  round-trip (and a write, to update a counter) purely to decide whether
  to *reject* — directly conflicts with "the limiter itself must not
  become an expensive database bottleneck" and "avoid transaction-heavy
  hot paths." The existing idempotency/locking machinery already uses
  Postgres row locks for the *payment* correctness guarantees that
  actually need durable, cross-replica atomicity; counting requests is
  not in that category.

Deployment topology confirms this is the right call for *this* repository
today: `docker-compose.yml` runs exactly one `api` service, no
`deploy.replicas`, no external load balancer in front of Caddy. This is
the same scope `app/ratelimit.py`'s own docstring already accepts
("with one API container this is a global limit; running multiple API
replicas multiplies the effective limit by the replica count"). This PR
keeps that limitation, states it explicitly again here, and does not
silently worsen it: per-IP buckets are *additional* sliding windows next
to the existing global ones, not a replacement, so horizontal scaling
degrades exactly as it does today (each replica's global ceiling and
per-IP ceiling both multiply by replica count) — a known, accepted,
unchanged tradeoff, not a new one.

**Memory bound**: a naive per-IP `dict[str, SlidingWindowLimiter]` grows
without bound under a distributed/spoofed-source flood (task: "avoid
high-cardinality unbounded storage"). The store is a bounded LRU
(`OrderedDict`, capacity from config, oldest-evicted-first) — see
`app/ratelimit.py`'s `_BoundedLimiterStore`.

## 7. Fail-open / fail-closed decision

The backend is in-process memory guarded by a `threading.Lock` — there is
no external dependency that can be independently "down." The only failure
mode is a bug inside the limiter code itself throwing an unexpected
exception. Per endpoint class:

- **Payment creation and callback signature paths: fail OPEN.** A limiter
  bug must never block a legitimate payment or drop a legitimate gateway
  callback — that would be a self-inflicted denial of service directly
  against the thing this task exists to protect. `RateLimiters.check()`
  is wrapped so any unexpected exception is logged
  (`rate_limiter_check_failed`, exception class only — no request data)
  and treated as **allowed**, exactly the same fail-open discipline
  `app/adminbot/alerts.py::on_audit_event` already uses for its own
  best-effort side channel.
- There is no fail-closed class here: nothing security-critical (auth,
  signature verification, idempotency) depends on the limiter — it is a
  strictly additive layer. Its failure can only ever make the system
  *more* permissive, never *less* correct.

## 8. Configuration added

All under the existing `rate_limit_*` prefix, following existing
`Field(..., gt=0)`-style validation conventions in `app/config.py`:

- `rate_limit_create_per_ip_per_minute` (new, per-IP burst counterpart to
  the existing global `rate_limit_create_per_minute`)
- `rate_limit_invalid_signature_per_ip_per_10min` (new, per-IP counterpart
  to the existing global `rate_limit_invalid_signature_per_10min`)
- `rate_limit_ip_bucket_capacity` (new — bounds the LRU per-IP store size)
- `rate_limit_trust_proxy_headers` (new, default `True` — the documented
  single-hop-Caddy trust boundary; set `False` to always fall back to the
  raw socket peer, e.g. for a topology this app does not currently have)

`rate_limit_enabled` (existing) continues to gate everything, including
the new per-IP layer.

## 9. Observability

`rate_limited` log event (existing, extended) now also carries `scope`
("global" | "per_ip"), and the new fail-open path logs
`rate_limiter_check_failed`. Neither ever logs the API key, the HMAC
signature, the raw request body, or (per existing convention) the query
string. The per-IP key is the resolved client IP itself — not a secret,
already the standard field logged by every HTTP server/proxy — so it is
logged as-is for operator triage, never hashed unnecessarily.

## 10. Admin bot visibility

Reviewed PR #66's `/status` and `/health` commands. Neither is a natural
fit for exposing in-process, per-container rate-limiter counters: they
are not persisted (unlike everything else `/status` shows, which is
DB-backed and thus meaningful across the admin bot's own separate
process/container), and would only reflect the *admin bot's own* process
memory, not the API container's — actively misleading if shown. No new
admin-bot command or field added; the structured `rate_limited` log event
is the documented visibility channel, consistent with the rest of this
audit's "leave operational visibility to structured logs" fallback.

## 11. Security self-review (post-implementation)

Full diff reviewed against every item in the task's self-review checklist.
Findings below; everything else checked out with no action needed
(confirmed by the tests referenced):

- **IP spoofing / proxy trust**: Caddy's own default already ignores a
  client-supplied `X-Forwarded-For` value (verified against official
  Caddy docs — see §3's correction); the explicit Caddyfile overwrite
  (§3) makes that boundary auditable in this repo rather than fixing a
  proven default vulnerability. `resolve_client_ip`'s strict single-IP
  validation is defense in depth regardless
  (`tests/test_clientip.py`, `test_spoofed_forwarding_header_cannot_bypass_the_global_ceiling`).
- **Bypass via alternate route / method**: confirmed exactly five routes
  exist (`grep -rn "@router\." app/api/*.py`), all classified in §5.
  `/api/custom-payment` is POST-only; GET/HEAD/OPTIONS/PUT 404/405 at
  Starlette's routing layer before any handler code (including the
  limiter) runs — no bypass surface there.
- **High-cardinality / unbounded key growth**: `BoundedLimiterStore`'s
  LRU eviction, tested under a 50,000-distinct-key flood.
- **Race conditions**: real-thread concurrency tests for both the raw
  limiter and the store (§ tests).
- **Limiter state after restart**: intentionally NOT persisted — a
  restart resets every counter to empty (a fresh `RateLimiters` is built
  in `create_app()`). This is a clean, harmless reset consistent with
  fail-open, never "corruption": the system is briefly more permissive
  after a restart, never less correct or stuck rejecting.
- **Idempotency / financial side effects before rejection**: exhaustively
  tested (`test_idempotent_retry_bypasses_the_create_limiter_entirely`,
  `test_no_db_mutation_on_rejected_new_order_create`,
  `test_no_gateway_call_on_rejected_new_order_create`).
- **Secret leakage**: tested — the rejected-request log path never
  contains the API key, HMAC secret, callback token, or signature.

**Residual, documented, not fixed in this PR — follow-up candidate**: the
`rate_limited` warning log fires once per rejected request with no
sampling or deduplication. This is **pre-existing behavior**, unchanged
from before this PR (the three original global limiters already logged
this way); this PR's new per-IP checks follow the same convention for
consistency rather than inventing a one-off dedup scheme. Under a
sustained flood this produces one log line per rejected request — each
line is small and Docker's own log rotation is already configured
(`Caddyfile.template`'s access-log comment), so this is a log-volume
nuisance, not a resource-exhaustion vector, but a proper fix (e.g. a
storm-dedup tracker mirroring `SignatureFailureTracker`'s design) is a
separable improvement with its own tradeoffs, not bundled into an
already-large PR per the task's own "document as a separate follow-up"
guidance for exactly this kind of low-risk, pre-existing item.

## 12. No migration

No new database tables, columns, or persisted state. Confirmed: this
section can be read as the explicit "no migration needed" report the task
requires.
