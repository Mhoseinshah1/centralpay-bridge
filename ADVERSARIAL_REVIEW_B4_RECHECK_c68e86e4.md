# Independent B4 recheck — main @ `c68e86e4` (2026-08-17)

This document records a fresh, independent re-audit of release blocker
**B4** (multi-agent adversarial review) against the CURRENT `main`, run
after the CANON-1/2/3/5 remediation (`fix/b4-confirmed-release-blockers`,
`fix/release-manifest-exactness`) and after new post-B4 features
(`recover-aged-out`, `db-check --details`) landed. It supersedes nothing —
`ADVERSARIAL_REVIEW_0.6.0_RC1.md` (the original 2026-07-19 review, audited
SHA `4e62a552a381f1853d0f04efce26c7142dfbf6d5`, verdict
`B4_FAILED_CONFIRMED_CODE_BLOCKERS`) stands unchanged as an honest,
permanent record that B4 failed on that commit. This document is the
independent recheck that original review's own "Remediation status" note
said was required before B4 could close.

## Audited commit

- **SHA:** `c68e86e45b718b1da34439246572dfe5d8ac947a`
- **Verified:** `git fetch origin main` then `git rev-parse origin/main`
  from a clean session, matched against the SHA supplied for this task,
  and independently re-confirmed via `git log -1` in an isolated
  `git worktree` checked out at that exact commit
  (`/scratchpad/b4-audit`, detached HEAD, `git status` clean throughout
  the audit — no tracked file was ever modified during review).
- This is `origin/main` HEAD, a merge of PR #62 ("Add read-only anomaly
  drill-down to centralpay db-check"), itself descended from PR #61 ("Do
  not auto-retry HTTP 5xx bot notifications in safe mode") and, before
  that, PR #60 ("Add explicit single-payment recovery for aged-out
  link_created payments" — the `recover-aged-out` command this document
  reviews), and, further back, the CANON-1/2/3/5 remediation PRs #31/#32
  — i.e. current `main`, not an old or unmerged branch.

## Independent reviewers/agents used

Seven independent adversarial subagents, each with no visibility into the
others' findings, each instructed to treat every prior "FIXED" label as
unproven and re-derive conclusions from current code/tests, each with a
dedicated PostgreSQL 16 database (`centralpay_audit_a` through `_g`) so
concurrency work could never collide across reviewers:

| Reviewer | Scope |
|---|---|
| A | Financial invariants (static/code-level) |
| B | PostgreSQL concurrency / race conditions |
| C | Notification delivery + reconciliation |
| D | recover-aged-out + db-check --details + admin bot/stuck categorization (the newest code, never previously adversarially reviewed) |
| E | Payment creation / getLink |
| F | Release/artifact/source-binding — the CANON-1/2/3/5 recheck |
| G | Security/privacy sweep |

Plus the orchestrating session's own independent, objective verification
run (full lint/type/test/shell-check suite against a freshly-recreated
PostgreSQL database, see below).

## Previously known B4 findings — re-tested, each verdict

| Finding | Original verdict (2026-07-19) | Recheck verdict (this document) |
|---|---|---|
| **CANON-1** / topic 33 — installer rerun silently applies a 0% fee | CONFIRMED DEFECT | **FIXED-CONFIRMED.** `INSTALLER_INITIAL_FEE_PERCENT` recovery metadata + `app.ops fee ensure-initial` (advisory-lock-serialized, fails closed on an empty policy table with no supplied rate) verified by reading `install.sh`/`app/ops.py` end-to-end and running `tests/integration/test_installer_fee_recovery.py` against a real PostgreSQL 16 database: 10/10 passed, including the concurrent-rerun race. No bypass found across every rerun/interruption sequence tried. |
| **CANON-2** / topic 34 — `isdigit()`-gated `int()` crashes | CONFIRMED CODE DEFECT | **FIXED-CONFIRMED** at both originally-cited sites (`app/centralpay.py:_to_int`, `app/bot.py:_parse_retry_after`) — strict ASCII grammar + `try/except`, zero exceptions escape across an adversarial battery (superscripts, `--5`, raw `\xb2`, 10,000-digit strings, fullwidth/Arabic-Indic digits, `+5`, embedded NUL, non-string types). `tests/test_canon2_safe_int_parsing.py`: 54/54 passed. Reviewer F additionally grepped the whole tree for the same anti-pattern and found it re-appears at several NEW sites not covered by the original fix — see New Finding 2 below; none is a B4 blocker. |
| **CANON-3** / topic 35 — update integrity decoupled from deployed bytes | CONFIRMED DEFECT | **FIXED-CONFIRMED.** `cmd_update` deploys via `git checkout -q "$target_commit"` where `target_commit` is the value `resolve_verified_update_commit` already fetched and checksum-verified in the same call — not an independent `git fetch`+`checkout FETCH_HEAD` as originally found. `verify_manifest_and_extract_commit`'s strict per-line regex grammar (`^([0-9a-f]{64})  <exact-name>$`, exactly-one-match required) plus exact `SOURCE_COMMIT` byte-grammar (`[0-9a-f]{40}\n?` full match) were adversarially fixture-tested via `tests/test_update_integrity.py`: 58/58 passed, covering every hostile fixture named in the recheck brief (trailing/second newline, CRLF, whitespace, `SOURCE_COMMIT.old`, path-qualified names, uppercase hex, duplicate entries, `*` binary markers, fuzzy artifact names, wrong-length hex, moved-tag-after-release) plus more. |
| **CANON-5** / topic 38 — Dockerfile OCI version label stale | DOCUMENTATION MISMATCH | **FIXED-CONFIRMED** (static analysis + regression test; the one live empirical step — a `docker build` — could not be completed in this sandbox, see Environment note below). `Dockerfile` uses `ARG APP_VERSION=""` / `LABEL ...="${APP_VERSION}"` with zero literal anywhere; `tests/test_deployment.py::test_dockerfile_oci_version_label_is_not_a_stale_literal` passed, and both CI and the release workflow build with `--build-arg APP_VERSION=$(...)` and assert the label via `docker inspect`. |
| Release manifest / SOURCE_COMMIT / artifact-source binding (domain 9 core) | Part of CANON-3 | **FIXED-CONFIRMED**, see CANON-3 row. Additionally verified: the very first install (`install.sh`'s `git clone`) has no checksum/commit binding — this is outside CANON-3's stated scope (which was specifically the *update* path) and is an accepted characteristic of curl-pipe-bash installers, not a new finding. Current production's `main`-tracking DEVELOPMENT MODE is explicit, warned-about, fail-safe behavior tied to the still-open **B1** (no real host has ever run this installer) — not a new B4 finding. |

**All four re-tested CANON findings are FIXED-CONFIRMED on `c68e86e4`.**
Nothing was accepted on the strength of a documentation label; every
verdict above rests on independently reading the current implementation
and running/constructing a real test against it (existing regression
suites, hand-built adversarial fixtures, or — for CANON-2's new sites — a
real `uvicorn`+`httptools` process driven over a raw TCP socket).

## Financial-invariant verdict

**HOLDS — all 10 invariants confirmed**, each with file:line evidence and,
for the highest-stakes ones, direct proof against a live, freshly-migrated
PostgreSQL 16 database (CHECK/UNIQUE constraints proven by attempting to
violate them with raw SQL, not just read in `models.py`):
gateway-verification-is-the-only-success-source; bot-notification
acceptance kept structurally separate from gateway verification; no bot
HTTP response can fabricate a financial fact; manual-review operations
never touch amount/fee/payable/reference_id/gateway_verified_at; no
retry/recovery/reconciliation path can double-settle (proven live via a
real two-thread PostgreSQL race); amount/fee/payable snapshots are
immutable after creation (grep-confirmed single write site + four live
DB CHECK constraints); reference_id collision handling is fail-safe
(UNIQUE constraint backstop, proven by a live collision-losing
transaction rollback); gateway-identity isolation holds (fail-closed
collision handling, DB-unique `gateway_user_id`); callback/token
validation cannot be bypassed (HMAC + one-time token, both checked before
any gateway call); duplicate/replayed callbacks are safe (VERIFIED_STATUSES
short-circuit, zero re-verify calls).

## Notification-delivery verdict

**HOLDS — all 10 items confirmed.** Safe vs idempotent retry modes are
schema-enforced (`Literal["safe","idempotent"]`); 5xx never becomes
success; ambiguous transport failures (verified live against the actual
httpx exception types for a post-connect reset/hang) never auto-retry in
safe mode; stale-claim recovery is ownership-fenced; retry-limit routes to
manual_review, never silently drops a payment; `BOT_NOTIFY_ACCEPTED` has
exactly one write site, gated on a genuine 2xx; no response body is ever
parsed/trusted (proven by injecting a malicious 2xx body and confirming
classification is unaffected); the bridge's own logic cannot double-send
(single-claim-per-payment enforced by `FOR UPDATE SKIP LOCKED`). One
resend-path hardening gap found — see New Finding 1.

## Reconciliation verdict

**HOLDS — all 8 items confirmed.** One shared age-boundary predicate
source (no duplicated math anywhere downstream); attempt ceilings
correctly stop scheduling without ever auto-failing/auto-paying a row;
`verify_and_settle` is the sole settlement path (grep-confirmed: exactly
one `gateway_verified_at =` write site in the entire codebase); the
diagnostic `reconcile --verify` path was independently re-derived (not
trusted from prior session work) and confirmed to make zero HTTP/lock
calls when `CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED` is unset, with
`--confirm-aged-out` structurally incapable of bypassing that gate;
aged-out payments are excluded from scheduling, never deleted/auto-failed;
`attempts_exhausted` is correctly computed from the raw
attempts-vs-cap fact, not the mutually-exclusive dashboard-bucket helper
(directly regression-tested with a row that is simultaneously exhausted
and aged-out).

## recover-aged-out verdict

**HOLDS — all 10 items confirmed**, on code that had NEVER been
adversarially reviewed before this recheck (it landed after the original
2026-07-19 B4 run). Preview mode is proven zero-write/zero-network/zero-lock;
`--confirm` takes the row lock, captures a fresh timestamp, and reloads
eligibility under that lock before doing anything; the only settlement
call is the single canonical `verify_and_settle`; two-simultaneous-confirm
and confirm-vs-callback/reconciliation races were proven safe on real
PostgreSQL with genuine thread contention (up to 5-way races, beyond the
existing test suite's coverage); it is structurally a single-payment,
operator-invoked command with no sweep and no bespoke bot call; it reuses
the shared, already-hardened `_find_payment` order-id resolution rather
than re-implementing it. Two non-blocking findings recorded (New Findings
6 and 7 below); one B2-scope observation recorded (see "B2 scope note").

## db-check verdict

**HOLDS — all 6 items confirmed**, also newly-landed, never previously
reviewed code. Plain `db-check` still correctly rejects `bot_notified` as
an invalid status (confirmed against the live `PaymentStatus` enum — it is
NOT a valid status and was not added as one). `--details` is proven
strictly `SELECT`-only (no `FOR UPDATE`, no writes, no network — verified
with the network layer poisoned). The event-data allowlist was
adversarially tested by planting an API key, a callback token, raw
gateway HTML, and Telegram identifiers into a single audit event and
confirming none of it survives into `--details` output. No financial
interpretation or mutation anywhere in the module.

## admin/stuck verdict

**HOLDS — all 7 items confirmed.** `/stuck`/`/waiting`/`/expired`
categorization is correct and built from the same shared predicates as
the CLI equivalents (one non-blocking documentation/implementation
mismatch noted — see below); resolved manual reviews are correctly
excluded everywhere; delivery-failure vs. financial-mismatch manual
reviews are structurally partitioned, never conflated; the historical
`getlink_failed` row visible under `needs_attention` causes no unsafe
action (grep-confirmed: zero `db.commit()` in the admin bot ever touches
a financial field); the one mutating admin-bot command
(`/resend_failed`) is hard-gated to already-gateway-verified,
delivery-only-failure rows.

## payment/getLink verdict

**HOLDS — all 12 items confirmed.** Timeout/connection-reset on getLink
is unambiguous (routes to `GETLINK_FAILED`, never conflated with the
distinct bot-notification "ambiguous" concept); retries reuse the SAME
row (never orphan/duplicate), draw a genuinely fresh `gateway_order_id`,
and regenerate the one-time callback token; payer-identity isolation
holds under real-PostgreSQL concurrent-creation races (the incident
2026-07 fix); no duplicate-order or cross-user link leakage; a failed
getLink can never satisfy any "verified" check anywhere in the codebase;
fee/payable snapshots are frozen before any identity-resolution
sub-transaction runs; amount bounds and the `PAYMENT_CREATION_ENABLED`
kill switch are both correctly server-side-only and structurally absent
from the callback/verify path. One non-blocking finding (New Finding 2,
below) — the CANON-2 anti-pattern reappearing in sibling admin/ops
lookup helpers.

## PostgreSQL concurrency results

**HOLDS across every race tested — no B4-blocker-class concurrency defect
found.** Reviewer B ran the full existing `postgres`-marked suite (110
tests, confirmed genuinely hitting PostgreSQL 16.13 via
`TEST_DATABASE_URL=postgresql+psycopg://...`, not a silent SQLite
fallback) and then went materially beyond it with hand-built
`threading`-based probes exercising 11 named race scenarios — several
pushed past the existing suite's coverage (3-5 concurrent
`recover-aged-out --confirm` calls instead of 2; 3 simultaneous
reconciliation workers vs. one due row instead of 1-vs-1; 8-10 concurrent
workers/claimants vs. fewer available rows; a sustained 12-thread,
5.8-second mixed-workload stress test combining payment creation, real
signed HTTP callbacks, reconciliation, recovery, notification delivery,
and bulk resend simultaneously). Every invariant held: exactly-once
settlement via the shared row lock in every combination tested, no
double credit even in the one scenario where the guarantee depends on a
database UNIQUE constraint rather than application logic
(`reference_id` collision — independently re-proven live, still fails
safe exactly as the register already documents), zero deadlocks, zero
duplicate/lost ids across 136 payments created under sustained mixed
load. Reviewer A separately proved all four financial DB CHECK
constraints live on a real, freshly-migrated database by directly
attempting (and having PostgreSQL reject) violating inserts.

## Verification commands and results (this session, independent of the 7 subagents)

Run against the SAME audited worktree, with a freshly-recreated
PostgreSQL 16 test database (`centralpay_test`, dropped and recreated
under `centralpay` ownership to eliminate a stale local-environment
ownership artifact unrelated to any application code — confirmed by a
clean rerun):

```
ruff check .                    → All checks passed!
mypy app tests                  → Success: no issues found in 119 source files
pytest -q                       → 1667 passed, 37 warnings, 0 failed, 0 errors  (160.08s)
                                   TEST_DATABASE_URL=postgresql+psycopg://centralpay:ci_test_password@localhost:5432/centralpay_test
shellcheck install.sh scripts/centralpay scripts/backup.sh  → clean (no output)
bash -n install.sh scripts/centralpay scripts/backup.sh     → OK (all three)
```

**Environment note (Docker, sandbox):** the release/artifact reviewer (F)
attempted a live `docker build` in this local sandbox to empirically
confirm CANON-5's OCI label behavior; the base-image (`python:3.12-slim`)
blob pull was blocked by the sandbox's egress proxy policy
(`production.cloudfront.docker.com` → 403 Forbidden, consistent on retry
— a policy denial, not a transient failure) and could not complete
locally. This is a local-sandbox limitation, not a code finding, and is
already covered by the existing, still-open **B5** ("release workflow has
not yet run green: Docker builds ... CI-delegated and unverified
locally"). CANON-5's conclusion does not rest on this local step alone —
it is independently corroborated by static code reading, a passing
dedicated regression test
(`test_dockerfile_oci_version_label_is_not_a_stale_literal`), and by
GitHub Actions CI on PR #63 (which is NOT sandbox-egress-restricted),
below.

**GitHub Actions CI on PR #63:** as of this update, ShellCheck, the
dependency vulnerability scan, the secret scan (gitleaks), and Docker
build and compose validation (including image smoke/version/OCI-label
validation) have all completed successfully — corroborating CANON-5's
Docker-build conclusion directly in CI where the sandbox's egress
restriction does not apply. Both Python test matrix jobs (Tests
ubuntu-22.04, Tests ubuntu-24.04) have also completed successfully;
all 6 of PR #63's checks are green as of this update. This is CI status
on the docs-only PR itself, not a re-audit of application code — see the
verification commands above for this session's own local run.

## New findings from this recheck

None of the following is a **CONFIRMED B4 BLOCKER**. Recorded here (per
this register's own practice — see topics 36/37/41) so they are not lost;
full evidence for each lives in the individual subagent transcripts this
document synthesizes.

1. **`app/ops.py`'s single-payment `review resend` has weaker eligibility
   checks than `app/services/bulk_resend.py`** — independently found by
   two reviewers (C: missing the `bot_notify_reason` allowlist filter; B:
   also ignores `review_resolved_at`, so a payment an operator just marked
   "resolved, do not deliver" can still be resent). **CONFIRMED
   NON-BLOCKING DEFECT.** Mitigated by two mandatory confirmation flags
   (`--confirm-idempotent-bot --yes`), by requiring `gateway_verified_at`
   non-null (financial-mismatch reviews are structurally excluded — those
   never set that column), and by being a single-payment, human-reviewed,
   host-CLI-only action. Recommend aligning it with
   `bulk_resend.ELIGIBLE_RESEND_REASONS` and adding a
   `review_resolved_at IS NULL` guard.
2. **The CANON-2 `isdigit()`-then-unguarded-`int()` anti-pattern reappears
   at sites the original fix didn't cover**, found independently by two
   reviewers (E, F): `app/adminbot/queries.py:find_payment`,
   `app/ops.py:_load_review_payment`, two sites in
   `app/adminbot/commands.py`, and `app/api/payments.py`'s Content-Length
   header parsing. **CONFIRMED NON-BLOCKING DEFECT** at every site: the
   admin-bot/ops sites are gated behind admin authentication and/or a
   broad top-level exception handler (generic error reply / local
   traceback, never a crash or financial effect); the `app/api/payments.py`
   site was proven empirically unreachable by driving a raw malformed
   `Content-Length: --5` / `\xb2` request through a real
   `uvicorn`+`httptools` process (the actual production stack,
   `Dockerfile:66`) over a TCP socket — uvicorn's own HTTP parser rejects
   both with `400 Bad Request` before the ASGI app is ever entered.
   Recommend applying the already-proven `isdecimal()` + BIGINT-bound
   pattern from `app/cli.py:_find_payment` to the admin/ops sites for
   consistency.
3. **`_is_private_bot_host` (`app/config.py`) misclassifies hex/octal/
   bare-integer IPv4 literals as "private."** Its numeric-IP-literal check
   only recognizes strictly-decimal-digit dotted quads; a value like
   `0x08080808` (resolves to the real public `8.8.8.8`) falls through to
   the "single-label = private container name" default and is wrongly
   accepted. **CONFIRMED NON-BLOCKING DEFECT**, proven live (real
   `socket.getaddrinfo` resolution + `normalize_bot_notify_url` call): the
   module's own docstring makes an empirically-false claim ("public IP
   literals are rejected even with the flag") for this specific
   under-tested input class. Not remotely exploitable without the
   non-default `ALLOW_INSECURE_BOT_NOTIFY_URL=true` opt-in. Recommend
   validating every authority label against `ipaddress.ip_address()`
   rather than `str.isdigit()`.
4. **Secret-redaction backstop's `_MIN_REDACTABLE_LENGTH=6` floor can be
   undercut** by `centralpay_getlink_api_key`/`centralpay_verify_api_key`/
   `bot_notify_token`/`admin_bot_token`, none of which enforce a minimum
   length (unlike `inbound_api_key`/`callback_hmac_secret`, which require
   ≥16). **CONFIRMED NON-BLOCKING DEFECT** — proven by configuring a
   5-character API key and confirming it is not redacted; no currently-live
   code path actually logs any of these raw, so this is a defense-in-depth
   gap only, not an active leak. Recommend raising the `Field(min_length=...)`
   floor on these four settings to match the others.
5. **`CENTRALPAY_UPDATE_REF` is passed to `git fetch`/related calls
   without a `--` separator**, permitting git-flag injection if the value
   begins with `-`. **CONFIRMED NON-BLOCKING DEFECT** — requires the
   attacker to already control the root-owned, 0600 env file, at which
   point far more direct compromise paths already exist. Recommend
   `git fetch --tags --force origin -- "$ref"`.
6. **`recover-aged-out --confirm` lacks the `require_root` gate its
   sibling mutating host-CLI commands (`review`, `fee`) have** in
   `scripts/centralpay`. **CONFIRMED NON-BLOCKING DEFECT** — a real
   consistency gap versus the codebase's own established pattern for
   financial-mutation commands, but bounded in practice because
   `docker exec` access is already effectively root-equivalent in the
   standard deployment. Recommend adding
   `require_root "recover-aged-out --confirm"`.
7. **`execute_confirmed_recovery` only catches `CentralPayError`, not the
   `IntegrityError` from a `reference_id` collision race** (the existing,
   already-known topic 39/CANON-6 scenario) — an operator hitting this
   exact collision via `recover-aged-out --confirm` gets an unhandled
   traceback instead of the callback path's graceful 500-then-self-heal.
   **CONFIRMED NON-BLOCKING DEFECT** — same failure class as topic 39
   (fails safe, no double credit, self-heals on retry), just a rougher
   operator experience on a second entry point. No classification change
   to topic 39 itself.

**B2-scope note (not a new B4 finding):** `recover-aged-out --confirm`
makes a real gateway `verify()` call against a payment that, by
construction, the reconciliation worker has typically already polled
one or more times — the same "verify-after-verify against real
CentralPay, never confirmed safe" question tracked under the already-open
**B2**. Unlike `reconcile --verify` (which is diagnostic-only and gated
behind `CENTRALPAY_DIAGNOSTIC_VERIFY_ENABLED`), `recover-aged-out --confirm`
*applies* its result. This does not reopen B4 — the reconciliation
worker's own routine polling already makes repeated, ungated `verify()`
calls in production as its core function, so this is B2's existing
whole-system risk extended to one more scenario, not a new code defect.
Recommended (not required for B4): extend STAGING_VALIDATION.md's
procedure to explicitly cover a `recover-aged-out --confirm` call on a
payment with ≥1 prior worker verify attempt before this command is used
against production CentralPay.

**Documentation-accuracy note (favorable direction, not a defect):**
topic 40's existing text ("no background sweep... recovery relies on the
payer re-hitting the callback URL") is now stale — `app/services/
reconciliation.py`'s always-on worker (`reconciliation_enabled: bool =
True` by default) already provides exactly such a sweep, closing that gap
by default; `app/services/aged_out_recovery.py` adds an operator escape
hatch beyond even that. Topic 40's wording is corrected in this PR to
reflect current behavior — the residual risk it describes is smaller than
previously documented, not larger.

## B4 disposition

**B4 = CLOSED**, revalidated on `c68e86e45b718b1da34439246572dfe5d8ac947a`.

- No confirmed B4 blocker remains — zero `CONFIRMED B4 BLOCKER`
  classifications across all seven independent reviewers' full scopes.
- Every previously reported B4 finding (CANON-1/2/3/5, the four items
  this task explicitly named) was independently re-tested against current
  code and is FIXED-CONFIRMED, not merely re-labeled.
- Financial and concurrency invariants were proven, not assumed, against
  a real PostgreSQL 16 database — including constraint-violation attempts,
  live multi-thread races, and a sustained mixed-workload stress test.
- No old finding is hidden behind documentation or tests: topic 39
  (reference_id collision) and the B2/B1/B5-scoped items were
  independently re-derived and reconfirmed at their existing
  classification, not silently dropped; the seven new items surfaced by
  this recheck (six new non-blocking-defect topics 42–47, one incremental
  detail folded into existing topic 39, and one already-known B2-scoped
  risk note at topic 48 — none a new B4 finding in its own right) are
  recorded above and in the register below, not fixed-and-hidden.
- Current `main` (not an old or unmerged branch) was audited, with the
  SHA independently verified via `git fetch`+`git rev-parse` before any
  review work began.

B1 (real-host install), B2 (real-CentralPay staging validation), B3
(live-Telegram admin-bot validation), and B5 (release workflow run) are
**unaffected by this document** and remain open on their existing,
unchanged scope.
