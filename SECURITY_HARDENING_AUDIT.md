# Security Hardening Audit — Roadmap Item 6

Audit performed against `main` @ `3c200832a7f8644110d0b9c296ec59da82c06fcc` (which
already contains PR #67 rate limiting/abuse protection, #68 CI hardening, and
#69/#70's create-limiter safe-replay fixes). Read directly from source across
every listed domain; Graphify (`graphify-out/graph.json`) was used for
navigation only — every claim below is verified against the actual file/line,
not the graph summary.

**Headline finding: no confirmed vulnerabilities.** Every one of the eight
audited domains checked out clean. This document exists to make that
conclusion auditable — each section states what was checked, what evidence
supports "no issue," and, where a defense-in-depth idea was considered,
explicitly why it was or wasn't implemented. No application code changed as a
result of this audit; only this document was added.

## 1. Attack surface

Two public HTTP endpoints (unchanged from PR #67's audit): `POST
/api/custom-payment`, `GET /api/centralpay/callback`, plus `/health/live`,
`/health/ready`, `/static/*`. Caddy's `@public` matcher (`deploy/caddy/
Caddyfile.template:51`) is the only route to the API container — confirmed via
`docker-compose.yml`: only the `caddy` service publishes host ports (80/443);
`api`, `worker`, `migrate`, `admin-bot`, and `db` have no `ports:` mapping and
are reachable only on the internal Docker bridge networks.

Internal-only: `/health/details` (deliberately absent from Caddy's matcher,
pinned by `test_health_details_not_publicly_routed`), the admin bot (Telegram
long-polling, not a webhook — no inbound HTTP surface at all), and the host
CLI (`scripts/centralpay`, operator-only, requires host/SSH access).

## 2. Authentication / authorization

**Confirmed sound, no bypass found.**

- **Inbound API key** (`app/api/payments.py:526-537`): `constant_time_equals`
  (`hmac.compare_digest`, `app/security.py:9-10`); an unconfigured
  (`""`) key can never match (`not settings.inbound_api_key` short-circuits
  first); invalid attempts rate-limited before any DB/gateway work; the key
  is never logged or echoed; `api_key: StrictStr` is a required Pydantic
  field with no type coercion. Auth is the first statement in the route body
  — no DB/gateway work precedes it.
- **Callback HMAC + token** (`app/api/callback.py`, `app/security.py:24-45`):
  signature verified with `hmac.compare_digest`; parameter-pollution guard
  rejects any repeated `orderId`/`ct`/`sig` before signature validation;
  the one-time callback token is checked under a row lock
  (`app/services/verification.py:251-270`) before any gateway `verify` call;
  `VERIFIED_STATUSES`/`gateway_verified_at` gating prevents re-verification.
  No DB/gateway work occurs on an invalid signature beyond an in-memory
  failure counter.
- **X-Forwarded-For trust boundary** (PR #67): Caddy still unconditionally
  overwrites the header (`Caddyfile.template:68`); `app/clientip.py` still
  falls back to the raw ASGI peer for anything that doesn't parse as exactly
  one IP. No regression.
- **Admin bot authorization** (`app/adminbot/auth.py`,
  `app/adminbot/commands.py:122-139`): a single dispatch function,
  `CommandHandlers.handle()`, is the *only* way any Telegram update reaches
  a command handler (confirmed: `app/adminbot/runner.py:78` is the sole
  caller of `.handle`), and it checks `is_authorized()` — numeric Telegram
  user ID against `admin_telegram_ids`, private chat only, username never
  consulted — *before* looking up a handler. No per-command bypass is
  architecturally possible; every command shares one enforcement point.
- **Host CLI confirmation gating** (`app/cli.py`): `recover-aged-out
  ORDER_ID` defaults to a read-only preview; `--confirm` is required to
  acquire the row lock and mutate (module docstring, `cli.py:28`).
  `--confirm-aged-out` similarly requires `--verify` and is documented as
  "fully read-only" itself (`cli.py:74`). No `shell=True` subprocess call
  anywhere in `app/` or `scripts/`.
- **Ambiguity-safe order lookup**: `app/services/payment_lookup.py`'s
  `AmbiguousOrderIdError`/lookup helper is reused (not re-implemented) by
  every CLI command and admin-bot command that resolves an order from a
  user-supplied identifier — confirmed via `app/cli.py` and
  `app/adminbot/commands.py` imports.

**Considered, not implemented**: two identity-hash lookups
(`app/services/payer_identity.py:179`, `app/services/payments.py:447`) use
SQLAlchemy `.where(Column == value)` rather than `hmac.compare_digest`. These
are Postgres-side indexed `WHERE` comparisons, not Python-level secret
comparisons — there is no way to make a database index lookup "constant
time" without abandoning indexed lookups entirely (fetch-and-compare, an
architecturally invasive change). The values compared are keyed-HMAC identity
dedup keys, not authentication credentials: a timing side-channel here would
at most reveal whether a *derived* identity hash collides with an existing
row, which grants no access and bypasses no check (every payment operation
is still gated by the API key or, on the admin side, Telegram user ID). Not
worth the architectural cost for a channel that isn't an auth bypass.

## 3. Input validation / parsing

Unchanged from the extensive audit already performed for PR #67 (64KB body
cap, ≤32 urlencoded pairs, ≤1 extra JSON-decode layer, `StrictInt`/`StrictStr`
schema, control-character/NUL rejection on `order_id`, ASCII-only decimal
coercion). Re-verified: rejection in `parse_create_payment_request`
(`app/api/payments.py`) happens entirely before authentication and DB work,
and diagnostic logging on rejection carries only fixed field names and
counts, never submitted values (`_reject`, `payments.py:457-475`).

`bot_order_id` (used to render `__ORDER_ID__` in the payment-status HTML
page, `app/api/pages.py:452-454`) is `html.escape()`-d at render time before
interpolation — the one place attacker-influenceable input reaches an HTML
response. No XSS path found (see §4).

## 4. HTTP security / Caddy

- `@public` matcher (`Caddyfile.template:50-52`) is exact-string path
  matching for the five allowed routes; everything else falls through to the
  `handle { respond "Not found" 404 }` catch-all (`Caddyfile.template:72-74`),
  pinned by `test_caddyfile_template_contents`.
- Security headers already present globally (`Caddyfile.template:32-39`):
  `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options:
  DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, and `-Server`
  (removes the Server header). TLS is fully automatic (Caddy ACME); `admin
  off` disables Caddy's own admin API.
- `/docs`, `/redoc`, `/openapi.json` are disabled unconditionally
  (`app/main.py`: `docs_url=None, redoc_url=None, openapi_url=None`), not
  environment-gated — no debug/reload flag anywhere (`Dockerfile`'s `CMD`
  has no `--reload`).
- Exception handling never leaks stack traces, DB details, or internal URLs:
  `_bridge_error_handler` returns only fixed `code`/`message` strings;
  `_validation_error_handler` strips submitted values; `_unhandled_error_handler`
  logs the traceback server-side only and returns a generic error
  (`app/main.py:29-62`).
- `/static/*` (`StaticFiles`, `app/main.py`) uses Starlette defaults: no
  directory listing, no traversal outside the fixed base directory; contents
  are only the bundled font files.

**Considered, not implemented — CSP**: the payment-status page
(`app/api/pages.py`) has inline `<style>`/`<script>` blocks. A CSP header
would need `'unsafe-inline'` to accommodate them, which provides no real
protection over the existing `html.escape()`-based mitigation — a CSP here
would be decorative, not defensive, unless the inline blocks were first
extracted to `/static/*.css`/`.js` files. That's a larger, unrelated
refactor of the success-page implementation for a page that already has no
demonstrated XSS path (the only dynamic value is escaped); not undertaken in
this audit. Flagged as a future roadmap item if the page ever gains more
dynamic content.

## 5. SSRF / outbound request hardening

**No confirmed SSRF vulnerabilities.** Every outbound HTTP destination
traces to fixed configuration, never to request-time or Telegram-message
input:

- **CentralPay client** (`app/centralpay.py`): `base_url` fixed at
  construction from `settings.centralpay_base_url`, validated HTTPS-only by
  `normalize_centralpay_base_url` (`app/config.py:176-198`, rejects an
  endpoint filename in the path). `get_link()`/`verify()` never take a
  caller-supplied URL — only `amount`/`user_id`/`order_id`/`return_url`,
  and `return_url` is `build_callback_url()` (`app/security.py:54-70`):
  fixed path + validated `public_base_url` + internally generated
  gateway_order_id/token/signature. Timeout (`centralpay_timeout_seconds`)
  is a single `httpx.Client(timeout=...)` value, applied by httpx to all
  four phases (connect/read/write/pool) uniformly. The redirect URL
  CentralPay *returns* is itself validated (`_validate_redirect_url`,
  `centralpay.py:194-215`: HTTPS-only, no userinfo, length-bounded) before
  being handed to the payer's browser.
- **Bot notification client** (`app/services/notification.py`, `app/bot.py`):
  URL fixed from `settings.bot_payment_notify_url`, validated by
  `normalize_bot_notify_url` (`app/config.py:222-242`) — HTTPS required by
  default; plaintext `http://` accepted only with an explicit
  `allow_insecure_bot_notify_url=True` flag *and* a syntactically
  private/internal host (`_is_private_bot_host`) — a public host is
  rejected even with the flag set. Timeouts explicitly bounded per-phase.
  `follow_redirects` is never set anywhere in the codebase (httpx's default
  is `False`), so no redirect-following risk exists for any outbound client.
- **Telegram Bot API client** (`app/adminbot/telegram.py`): official
  library, default `api.telegram.org` endpoint; `chat_id` values are only
  `settings.admin_telegram_ids` (config), never derived from an inbound
  message or payment data.
- **Admin-bot health probe** (`app/adminbot/commands.py:895-909`): fixed
  config URL (`admin_bot_api_url`, default `http://api:8000`, an internal
  container hostname), fixed literal paths (`/health/live`,
  `/health/ready`), bounded 5s timeout. No arbitrary-URL-fetch feature
  exists anywhere in the admin bot or CLI.

**Considered, not implemented — `admin_bot_api_url` validation**: unlike
`centralpay_base_url`/`bot_notify_url`, this field has no `@field_validator`.
Investigated adding one (reusing `_parse_outbound_url`) but its *correct*
default behavior is plaintext `http://` to an internal single-label
container hostname (`api:8000`) — the same policy `normalize_bot_notify_url`
only allows behind an explicit insecure-opt-in flag. Reusing that pattern
verbatim would incorrectly reject the working default configuration; a
bespoke validator would need new logic for a setting that is (a) 100%
operator-controlled, never touched by attacker input at request time, and
(b) bounded to a 5-second internal health probe whose worst-case
misconfiguration outcome is a wrong boolean shown to an already-authorized
admin who has full DB read access via other commands regardless. Not
implemented — logged as an accepted low-value gap, not a vulnerability.

## 6. Secret management

- **Log redaction**: `SecretRedactor` (`app/logging_setup.py:35-49`)
  collects every configured secret (`collect_secret_values`, lines 52-69:
  inbound API key, callback HMAC secret, both CentralPay keys, payer-id
  secret, bot notify token, admin bot token, and the DB password parsed out
  of the connection URL) and runs a redaction pass over the **final
  serialized log line** — both JSON and text formatters — so a secret can
  never reach output regardless of which code path tried to log it. This is
  a global safety net on top of (not instead of) per-call-site discipline.
  Covered by `tests/test_logging_redaction.py`.
- **Host file permissions** (`install.sh`): `umask 077` is set before
  writing `ENV_FILE`, `DB_PASSWORD_FILE`, `CADDYFILE`, and `CREDENTIALS_FILE`
  (line 569), each is additionally `chmod 600`'d explicitly as defense in
  depth (lines 571/574/577/594), and `umask` is restored to `022` immediately
  after (line 595). `scripts/backup.sh` is installed `chmod 0750` (line
  615); the `centralpay` management CLI is `chmod 0755` (line 616).
- **DB credential handling**: `docker-compose.yml` uses
  `POSTGRES_PASSWORD_FILE: /run/secrets/db_password` (a Docker secret, file
  path only — never a plain env var or CLI argument). `DATABASE_URL` for
  every app service arrives via `env_file:`, never a `command:` array, so
  it never appears in `ps` output. `scripts/backup.sh`/`scripts/centralpay`
  invoke `pg_dump`/`pg_restore`/`psql` via `docker compose exec -T db ...`
  with no password flag at all — auth relies on the official `postgres:16`
  image's local Unix-socket trust auth inside the container, itself
  reachable only via an already-privileged `docker exec`.
- **Per-service least privilege** (`docker-compose.yml`): each of
  `migrate`/`api`/`worker`/`admin-bot` masks every credential it doesn't
  need with a fixed non-secret placeholder via `environment:` overrides on
  top of the shared `env_file:` — e.g. `worker` never receives
  `CENTRALPAY_GETLINK_API_KEY`, `INBOUND_API_KEY`, `CALLBACK_HMAC_SECRET`,
  or `ADMIN_BOT_TOKEN`. A compromise of any one service does not hand over
  every secret in the deployment.
- **CI secrets**: confirmed no `pull_request_target` trigger anywhere (only
  `pull_request`, which never has secrets/write access on a fork PR by
  default); no `${{ github.event.* }}` interpolation inside any `run:`
  shell block (the one usage, in `ci.yml`'s `concurrency:` group key, is a
  numeric PR number consumed by workflow-level YAML, not shell-interpolated
  attacker-controlled text — zero injection surface).

No secret-leakage vulnerability found in any of the above.

## 7. Container / host hardening

**Already extensively hardened** — largely nothing new to add:

- Dockerfile: multi-stage build (builder stage discarded), non-root
  (`USER centralpay`, uid/gid 10001, `--shell /usr/sbin/nologin`), pinned
  version tag (`python:3.12-slim` — meets "at minimum a specific version
  tag," this audit's own stated bar), healthcheck defined.
- `docker-compose.yml`'s shared `x-app-hardening` anchor applies
  `read_only: true` + `tmpfs: /tmp:size=16m` + `cap_drop: [ALL]` +
  `security_opt: [no-new-privileges:true]` to `migrate`/`api`/`worker`; the
  `admin-bot` service repeats the identical profile explicitly. `db` and
  `caddy` deliberately keep vendor defaults (documented in-file: PostgreSQL
  needs setuid/chown capabilities; Caddy binds privileged ports and writes
  its ACME certificate volumes) but still carry `no-new-privileges:true`.
  This is a documented, justified exception, not an oversight.
  - No writable-filesystem regression risk found: the only runtime writes
    (worker/admin-bot heartbeat files) already go to the `tmpfs`-mounted
    `/tmp`.
- Network segmentation: `edge` (Caddy↔API only) and `internal`
  (API/worker/migrate/admin-bot↔PostgreSQL) are separate bridge networks;
  `db` is on `internal` only — a compromised Caddy container has no network
  path to PostgreSQL. `caddy` is on `edge` only — it receives no
  application or database secrets.
- No Docker socket mount anywhere in `docker-compose.yml`. No unnecessary
  published ports (only Caddy's 80/443/443-udp).
- Logging is bounded everywhere (`x-logging`: 20MB × 5 files per service).

No gaps identified worth changing.

## 8. Database security

**No SQL-injection vulnerabilities found.** Every `text()` call in `app/`
and `scripts/` was enumerated:

- The overwhelming majority are fixed literals or use bound parameters
  (`:param` with a params dict) — safe by construction.
- `app/ops.py:700/705/717` build sequence-repair SQL via f-string
  (`f"SELECT ... FROM {table}"`, `f"... {seq_name} ..."`,
  `f"SELECT setval('{seq_name}', {max_id})"`). Verified: `table` iterates
  over `_SEQUENCE_TABLES`, a fixed 5-tuple module constant (`ops.py:355-361`)
  — never CLI/user input; `seq_name` is PostgreSQL's own
  `pg_get_serial_sequence()` return value, not attacker-influenced;
  `max_id` is `int()`-coerced before interpolation. This code path is only
  reachable via the operator-only `db-check --repair-sequences` CLI
  command — no HTTP endpoint or bot command reaches it.
- No dynamic table/column name construction exists anywhere else in `app/`.
- No raw `cursor()`/string-concatenated/`%`-formatted SQL passed to
  `.execute()` anywhere — every other call site goes through SQLAlchemy
  ORM `select()`/`func`/`case` constructs, parameterized automatically.
- Row locks (`with_for_update()`) throughout the payment/notification/
  reconciliation/alert services sit on ORM `.where(Column == value)`
  comparisons — always parameterized regardless of value origin.
- The two `pg_advisory_xact_lock` calls (`app/ops.py:234,310`) use only a
  fixed module constant (`FEE_ENSURE_INITIAL_LOCK_KEY`) as the lock key —
  never a user-derived value.

**Considered, not implemented**: hardening `app/ops.py`'s f-string sequence
SQL with an explicit `assert table in _SEQUENCE_TABLES` guard at the
interpolation site, purely as a regression tripwire against a future edit
accidentally sourcing `table` from configurable input. Currently safe by
construction (verified above); adding a redundant runtime assertion for a
value that's already only ever a hardcoded loop variable was judged
unnecessary defensive clutter rather than a real hardening gain — not
implemented.

## 9. Security-sensitive error handling

Verified via §4/§5: all externally visible errors are fixed strings with no
stack traces, DB details, internal URLs, or secrets. Machine-readable error
codes (`InvalidApiKeyError`, `RateLimitedError`, `DuplicateOrderAmountMismatchError`,
etc. in `app/exceptions.py`) are unchanged — existing API contract preserved.
The rate limiter's fail-open policy (deliberate, documented in
`RATE_LIMITING_ARCHITECTURE.md`) was left unchanged per this task's explicit
instruction not to revisit it without a concrete issue — none was found.

## 10. Dependency / supply-chain hardening

`.github/workflows/ci.yml` and `.github/workflows/release.yml` read in full:

- Top-level `permissions: contents: read` on both workflows (minimal
  default). The only overrides are narrowly scoped to what each job
  genuinely needs: `ci.yml`'s secret-scan job adds `pull-requests: read`
  (gitleaks-action needs it to list PR commits on a private repo);
  `release.yml`'s release-publish job (tag-push/`workflow_dispatch`
  triggered only — never PR-triggered) needs `contents: write` to create
  the GitHub release.
- No `pull_request_target` anywhere — no fork-PR-with-secrets vulnerability
  pattern present.
- No `continue-on-error` on the dependency-scan (pip-audit) or secret-scan
  (gitleaks) jobs — findings actually fail the build.
- No shell/script injection: the only `${{ github.event.* }}` usage in
  either workflow is `ci.yml`'s `concurrency:` group key
  (`github.event.pull_request.number`), which is workflow-level YAML
  configuration, not a shell command — zero injection surface. No `run:`
  step anywhere interpolates PR-attacker-controlled text (title, body,
  branch name) directly into a shell command.
- `.github/scripts/pg-client-wrapper.sh` (PR #68) forwards `"$@"` into
  `docker exec` array-form (not `eval`/string-built) — no injection risk;
  re-confirmed with `shellcheck`/`bash -n` in §14.

**Considered, not implemented — SHA-pinning GitHub Actions**: every `uses:`
line is pinned to a version tag (`actions/checkout@v4`,
`actions/setup-python@v5`, `docker/build-push-action@v6`,
`gitleaks/gitleaks-action@v2`, etc.) — none to a mutable branch name.
Converting these to commit-SHA pins is a recognized supply-chain hardening
practice (protects against a compromised action publisher moving a tag),
but it was not done here: it would touch every workflow file for a benefit
that's already substantially mitigated (these are all official/first-party
actions from GitHub and Docker, or well-known widely-used actions), and it
introduces ongoing maintenance cost (SHA pins need periodic, deliberate
updates that tag pins don't). Documented as an accepted residual risk /
future roadmap item rather than implemented speculatively in this PR.

## 11. Regression safety confirmation (task §12)

No application code changed in this audit, so every previously-fixed
property is unchanged by construction. Re-confirmed via the full test suite
(§ below, `pytest -q` and `pytest -q -m postgres`, both green with unchanged
counts) that nothing regressed: payment idempotency, one-live-link-under-
concurrency, payer isolation, the GETLINK_FAILED create-limiter fix (#69),
the safe-replay identity-shape rules (#70) and their zero-gateway-call/
zero-mutation guarantee, callback-before-financial-mutation ordering,
notification stale-result/manual-accept semantics, reconciliation behavior,
fee-snapshot immutability, audit-event durability, backup/restore fidelity,
ambiguity-safe order lookup, and admin-bot authorization.

## Summary

| Category | Confirmed vulnerabilities | Defense-in-depth implemented | Accepted residual risk |
|---|---|---|---|
| HTTP/Caddy | 0 | — | CSP (needs static-asset extraction first; not undertaken) |
| Auth | 0 | — | Hash-lookup `==` vs `compare_digest` (not applicable to DB WHERE clauses) |
| Admin bot/CLI | 0 | — | — |
| Database/SQL | 0 | — | Redundant assert in `ops.py` sequence repair (unnecessary) |
| SSRF/outbound | 0 | — | `admin_bot_api_url` unvalidated (operator-only, low impact) |
| Secrets/logging | 0 | — | — |
| Container/Docker | 0 | — | — |
| CI/supply-chain | 0 | — | Actions tag-pinned not SHA-pinned |

No production access, no deployment, no configuration changes, and no
application behavior changes resulted from this audit.
