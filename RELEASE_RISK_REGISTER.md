# Release risk register — 0.6.0-rc1

Triage of every unresolved topic from `DEFERRED_REVIEW.md` for the
0.6.0-rc1 release candidate. Each item records a classification
(**fixed** / **accepted risk** / **release blocker** / **post-release
backlog**), severity, financial impact, exploitability, likelihood,
mitigation, test coverage, and the release decision.

**Bottom line: 0.6.0-rc1 may NOT be tagged and may NOT be used for real
payments while any release blocker below is open.** The blockers are
summarized at the end.

Severity scale: critical / high / medium / low. "Financial impact"
means the possibility of money being lost, double-credited, or
unaccounted for — the top priority of AGENTS.md.

---

## Topics 1–12 (Phase 1/2 review)

### 1. Callback replay protection — **FIXED (0.5.0-rc1)**
- Severity: high · Financial impact: none direct (replays triggered
  outbound re-verify, never a double credit) · Exploitability: medium ·
  Likelihood: medium
- Fix: every payment link embeds a one-time token (`ct`) covered by the
  HMAC signature (`orderId={id}&ct={token}`). Only the SHA-256 hash is
  stored; regenerating a link supersedes the old token durably, so stale
  callbacks are rejected under the row lock **before** CentralPay verify
  is contacted. Verified payments short-circuit to their final page, so
  legitimate late returns are never rejected.
- Tests: `test_stale_callback_token_rejected_before_verify`,
  duplicate-callback fault-injection tests, PostgreSQL concurrency tests.
- Release decision: closed.

### 2. Gateway-controlled error text — **FIXED (audit/gateway-response-hardening)**
- Severity: low · Financial impact: none · Exploitability: low (requires
  a hostile/compromised gateway) · Likelihood: low
- Fix: raw gateway text no longer leaves `app/centralpay.py`. Responses
  are classified into a fixed internal reason-code vocabulary
  (`gateway_rejected`, `gateway_response_invalid`, `gateway_missing_data`,
  `gateway_invalid_redirect_url`, `gateway_invalid_*` field codes); logs,
  exceptions, `last_error`, audit event data, and API responses carry
  codes only. Redirect URLs are parsed with `urlsplit` and accepted only
  as HTTPS with a valid hostname, no credentials, no control characters,
  and bounded length.
- Tests: sentinel-text redaction suites (client-level and end-to-end),
  redirect-URL rejection matrix.
- Release decision: closed.

### 3. Untrusted `X-Request-ID` — **ACCEPTED RISK**
- Severity: low · Financial impact: none · Exploitability: low ·
  Likelihood: low
- Mitigation: sanitized to `[A-Za-z0-9._-]{1,64}`; used only for log
  correlation, never for authorization or financial logic.
- Release decision: accepted for RC; stripping/overwriting the header at
  Caddy is post-release backlog.

### 4. Verify success detection — **HARDENED; confirmation is a RELEASE BLOCKER**
- Severity: critical · Financial impact: a misclassified verify response
  could mark an unpaid order verified · Exploitability: low ·
  Likelihood: low after hardening, unknown against the real gateway
- Fix (0.5.0-rc1): success is recognised only from an explicit allowlist
  of positive markers — never inferred from truthy values or the
  presence of `data`. Every financial field is parsed with typed
  coercion; malformed fields produce explicit reason codes
  (`gateway_invalid_reference_id`, `gateway_invalid_amount`,
  `gateway_invalid_user_id`) and route to manual review.
- Residual: the real CentralPay response contract has never been
  observed from this codebase. → **Release blocker B2 (staging
  validation, `STAGING_VALIDATION.md`)**.
- Tests: `tests/test_centralpay_client.py` (explicit-success, rejection,
  field-error suites).

### 5. Crash window after gateway verification — **FIXED in code; gateway idempotency confirmation under B2**
- Severity: high · Financial impact: none (fails safe: nothing persisted
  → later retry re-verifies) · Likelihood: low
- Proof: fault-injection test crashes inside the verification
  transaction after verify succeeds; the transaction rolls back
  atomically (no verified fact, no queue state, no partial audit
  events) and a later callback recovers by verifying again.
- Residual: confirmation that CentralPay `verify.php` tolerates
  verify-after-verify for the same order → part of **blocker B2**.
- Tests: `test_crash_during_verification_commit_is_recoverable`.

### 6. Malformed integer conversion — **FIXED**
- Severity: medium · Financial impact: none (conservative fallback:
  malformed → `None` → manual review, never a guessed amount)
- Fix: typed coercion with bool exclusion; field-level reason codes
  flow into manual-review audit data (see topic 4).
- Tests: client field-error tests; mismatch → manual-review tests.
- Release decision: closed; conservative fallback is the intended
  behavior.

### 7. Payment amount bounds — **FIXED (Phase 3)**
- `MIN_PAYMENT_AMOUNT_TOMAN` / `MAX_PAYMENT_AMOUNT_TOMAN` enforced at
  the API with explicit error codes. Tests in `test_phase3_app.py`.

### 8. Race conditions — **ACCEPTED RISK for RC; adversarial review is blocker B4**
- Severity: high · Financial impact: potential if a locking flaw exists ·
  Likelihood: low for covered paths
- Mitigation/coverage: `SELECT … FOR UPDATE` on create and callback,
  `SKIP LOCKED` worker claims, PostgreSQL concurrency tests (concurrent
  callbacks verify exactly once; concurrent creates return one link;
  racing workers claim once), fault-injection at transaction boundaries.
- Narrowed by the callback and payment-creation audits: 10-way identical
  create races (one row, one getLink, one event), conflicting-amount
  races, unique gateway-id allocation under concurrency, stale-token
  callback races, and post-verification replay storms are now
  deterministically tested on real PostgreSQL.
- Residual: the full adversarial concurrency review (lock pile-ups under
  callback floods, gateway latency at the lock boundary) was never
  completed → part of **blocker B4**.

### 9. Duplicate callbacks in other states — **FIXED for known paths**
- Old `gateway_order_id` after regeneration → 404; stale token on the
  current id → 403 before verify (topic 1); `manual_review` and verified
  duplicates never re-verify. Remaining exotic-state probing folds into
  blocker B4.

### 10. Recovery after process crash — **ACCEPTED RISK**
- Worker crash recovery is implemented and tested (stale-claim release,
  safe-mode manual review). Rows stuck in `created`/`getlink_failed`
  have no automated sweep; recovery is the bot re-requesting the same
  `order_id` (which regenerates the link and supersedes the old token).
  Operator inspection via `centralpay review list` / `python -m app.cli`.
- Financial impact: none (no money moves in those states). Post-release
  backlog: automated sweep + runbook automation.

### 11. Bot notification ambiguity — **ACCEPTED RISK with checklist gate**
- Safe mode is the default: ambiguous deliveries go to manual review and
  are never auto-retried. Idempotent mode requires the bot developer's
  written confirmation — this is a go-live checklist item
  (`PRODUCTION_CHECKLIST_FA.md`), not a code change.
- Tests: classification suite, worker mode tests, resend gating tests.

### 12. Manual review workflow — **FIXED (0.5.0-rc1)**
- `centralpay review show/list/acknowledge/resolve` on the host, with an
  allowlist of strictly non-financial resolutions
  (`confirmed_by_bot_operator`, `duplicate_notification_confirmed_safe`,
  `bot_not_credited`, `refund_required`, `false_positive`,
  `configuration_fixed`). Resolution never mutates financial fields.
  `review resend` requires `--confirm-idempotent-bot --yes` AND
  idempotent bot mode AND a gateway-verified payment.
- Tests: `tests/test_phase5_hardening.py` review suite.

## Topics 13–16 (Phase 2)

### 13. Stale-claim conservatism in safe mode — **ACCEPTED RISK (deliberate)**
- Availability is sacrificed for financial correctness by design.
  Pre-send-marker optimization is post-release backlog. Severity: low.
- Worker-audit update: stale-claim recovery is now bounded per pass, and
  interrupted attempts count against the retry limit in idempotent mode
  (previously unbounded requeue — fixed). Claim ownership is verified
  before any result is recorded (straggler writes discarded + audited).

### 14. `Retry-After` integer-seconds only — **ACCEPTED RISK**
- HTTP-date values fall back to the backoff schedule. Severity: low;
  financial impact: none.

### 15. Worker scaling / load testing — **ACCEPTED RISK; backlog**
- Load testing remains explicitly out of scope. Multiple workers are
  safe (`SKIP LOCKED`, tested). Severity: low (throughput, not
  correctness).

### 16. Payer-facing failure pages — **POST-RELEASE BACKLOG**
- Non-verified outcomes return JSON errors. Cosmetic; severity: low.

## Topics 17–21 (Phase 3)

### 17. Rate limiting — **FIXED at application level (0.5.0-rc1)**
- Sliding-window limiters for invalid API keys, callback signature
  failures, and create bursts. Limiters are per-process and in-memory
  (documented); `X-Forwarded-For` is never trusted for limiter identity.
  Proxy-level limiting remains absent (stock Caddy has no module) —
  residual accepted risk, mitigated by request-size limits, signature
  validation, and the new app-level limits.
- Tests: `tests/test_phase5_hardening.py` rate-limit suite.

### 17b. Deployment-audit note (audit/deployment-installer-security)
- The compose stack now isolates Caddy on an edge network (no route to
  PostgreSQL), hardens api/worker/migrate like the admin bot (read-only
  root fs, cap_drop ALL, no-new-privileges, tmpfs), masks unneeded
  secrets per service (worker included), redacts the `ct` token in Caddy
  access logs, and enforces all of it with policy tests. Runtime behavior
  of the hardened profile is validated by pattern (the admin bot has run
  it since Phase 4) — full runtime confirmation lands with real-host
  validation (B1).

### 18. Base images not digest-pinned — **ACCEPTED RISK for RC; backlog**
- Images remain tag-pinned (`python:3.12-slim`, `postgres:16`,
  `caddy:2`). This sandbox cannot reach Docker Hub to resolve digests;
  pinning must be done from CI or an operator host (process: `docker
  buildx imagetools inspect <image> --format '{{json .Manifest}}'`, then
  pin `image@sha256:…` in Dockerfile/compose). Mitigation: Trivy image
  scan in the release workflow. Severity: medium (supply chain).

### 19. Update channel integrity — **FIXED (0.5.0-rc1; strengthened by CANON-3, fix/b4-confirmed-release-blockers)**
- `CENTRALPAY_UPDATE_REF` defaults to a pinned release tag. For release
  tags, `centralpay update` now downloads the artifact, a `SOURCE_COMMIT`
  release asset, and `SHA256SUMS`, verifies the checksums of BOTH the
  artifact and `SOURCE_COMMIT`, validates `SOURCE_COMMIT`'s grammar (one
  40-character lowercase hex commit), resolves the fetched tag to its
  commit (`FETCH_HEAD^{commit}`, annotated-tag aware), and **requires the
  deployed commit to equal the verified `SOURCE_COMMIT`** before any
  checkout, build, migration, restart, or version-history mutation. This
  closes the earlier gap (topic 35): the checksum previously covered only
  a discarded tarball while the deploy was an independent
  `git checkout FETCH_HEAD`, so a moved tag could not be detected. The
  release-tag grammar is now strict (`vX.Y.Z` / `vX.Y.Z-rcN`); other refs
  stay explicit development mode. `CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true`
  remains a root-only escape hatch that warns unmistakably and never
  falsely claims verification. The exact guarantee: **the deployed Git
  commit must match a separately checksummed `SOURCE_COMMIT` release
  asset.** Signed-tag (GPG) verification remains a separate pre-1.0
  backlog item — this is commit binding, not signature verification.
  Version history is recorded; `centralpay rollback` is application-only
  and never downgrades the DB schema.

### 20. Installer never executed on a real host — **RELEASE BLOCKER B1**
- This environment has no VM/root target available; a real
  `curl | sudo bash` install on Ubuntu 22.04/24.04 has never been
  executed. Per the release instructions this is explicitly a release
  blocker — see `REAL_HOST_VALIDATION.md`. The RC must not be tagged
  until this is done and recorded.

### 21. Off-site backup replication — **ACCEPTED RISK; backlog**
- Backups are local; replication is a documented manual recommendation
  (`BACKUP_RESTORE_FA.md`). **A local backup on the same VPS is not
  disaster recovery** — this is stated explicitly in the operator docs.
- Backup-audit update: backups now carry SHA-256 manifests verified
  before restore (legacy files require RESTORE-LEGACY), backup/restore
  hold a shared exclusive lock, restores run --exit-on-error with all
  writers stopped (admin bot included), and service startup is gated on
  a post-restore integrity check with sequence repair
  (`centralpay db-check`). Full-state restore fidelity (every payment
  state + audit history + alert outbox + sequence safety) is proven by
  integration tests on real PostgreSQL.

## Topics 22–25 (Phase 4)

### 22. Live Telegram integration untested — **RELEASE BLOCKER B3 (for admin-bot use)**
- All Telegram traffic is mocked. A supervised run against the real Bot
  API is required before relying on alerts operationally — see
  `ADMIN_BOT_VALIDATION.md`. The admin bot is optional and disabled by
  default; the payment path does not depend on it. It remains a blocker
  for enabling the admin bot in production and for the overall RC
  validation matrix.

### 23. Duplicate alert delivery on stale-claim recovery — **ACCEPTED RISK (deliberate)**
- Alerts are operational, never financial; at-least-once is preferred
  over lost alerts. Severity: low.

### 24. In-memory health monitor counters — **ACCEPTED RISK**
- Restart can delay (never fabricate) an unhealthy/recovery alert by one
  cycle. Severity: low.

### 25. Admin bot resolution tooling — **FIXED via host CLI (topic 12); Telegram-side mutations deliberately absent**
- The bot stays read-only per AGENTS.md. Resolution now happens through
  the audited host CLI instead of direct database work.

## Deferred checks from DEFERRED_REVIEW.md

- Multi-agent adversarial review — **RELEASE BLOCKER B4** (never
  completed; explicitly required before any production claim).
- Dependency vulnerability scan / secret scan — **FIXED**: pip-audit,
  gitleaks, and Trivy run in CI and in the release workflow.
- Docker build + end-to-end installer test — build/scan delegated to CI
  (**B5** until the release workflow has run green); installer is B1.
- Load testing — out of scope; backlog (topic 15).

---

## Open release blockers

| # | Blocker | Evidence document |
|---|---------|-------------------|
| B1 | Installer never executed on a real Ubuntu host (no VM access from this environment) | `REAL_HOST_VALIDATION.md` |
| B2 | CentralPay contract never observed for real: staging run against the real/sandbox gateway (verify schema, verify-after-verify idempotency, real Caddy TLS) | `STAGING_VALIDATION.md` |
| B3 | Live Telegram validation of the admin bot (blocker for enabling the admin bot; the payment path does not depend on it) | `ADMIN_BOT_VALIDATION.md` |
| B5 | Release workflow (`.github/workflows/release.yml`) has not yet run green: Docker builds, Trivy scan, SBOM, and artifact packaging are CI-delegated and unverified locally | GitHub Actions |

## Closed release blockers

| # | Blocker | Resolution | Evidence document |
|---|---------|------------|--------------------|
| B4 | Multi-agent adversarial review | **CLOSED (2026-08-17), independently revalidated on `c68e86e45b718b1da34439246572dfe5d8ac947a`.** First run 2026-07-19 (six agents, real PostgreSQL 16) on SHA `4e62a552…` → `B4_FAILED_CONFIRMED_CODE_BLOCKERS` (topics 33–35, 38 = CANON-1/2/3/5). Remediated in `fix/b4-confirmed-release-blockers` + `fix/release-manifest-exactness`. Independently rechecked by seven adversarial agents plus a full local verification run (ruff/mypy/pytest incl. real PostgreSQL 16/shellcheck) on current `main`: **zero confirmed B4 blockers**, CANON-1/2/3/5 all FIXED-CONFIRMED, financial and concurrency invariants proven live on PostgreSQL. Six new non-blocking-defect topics recorded (42–47), plus one incremental detail folded into existing topic 39 and one already-known B2-scoped risk note recorded at topic 48 (not a new B4 finding). The historical FAILED verdict on `4e62a552…` is preserved unchanged. | `ADVERSARIAL_REVIEW_0.6.0_RC1.md` (original, FAILED) + `ADVERSARIAL_REVIEW_B4_RECHECK_c68e86e4.md` (recheck, CLOSED) |

**Release decision:** 0.6.0-rc1 is a code-complete release candidate.
It must not be tagged, published, or used for real payments until B1,
B2, and B5 are closed (and B3 if the admin bot is to be enabled), and a
human approval is recorded. **B4 was run on 2026-07-19 and FAILED with
confirmed code blockers** (topics 33–35); those were fixed and B4 was
**independently rechecked and CLOSED on 2026-08-17** against
`c68e86e45b718b1da34439246572dfe5d8ac947a` — see
`ADVERSARIAL_REVIEW_B4_RECHECK_c68e86e4.md`.

**Final-audit classification (audit/final-financial-correctness):**
after six focused audits plus the final end-to-end audit, **no code
blocker remains** — see `FINAL_FINANCIAL_AUDIT.md`
(CODE_FINANCIALLY_SOUND / PRODUCTION_VALIDATION_STATUS: INCOMPLETE).
Every open register item is one of: real-host blocker (B1),
real-CentralPay blocker (B2, incl. TOMAN-unit and verify-idempotency
confirmation), live-Telegram blocker (B3), process blocker (B5
release-workflow run — B4 adversarial review CLOSED 2026-08-17), real-bot blocker
(2xx/duplicate semantics confirmation), accepted risk (items 2*, 3, 8
residual, 10, 11, 13, 14, 15, 16, 17 residual, 18, 21, 23, 24), or
post-release backlog. Migration 0005 added the financial CHECK
constraints; no schema work remains open.

## Topic 30 (feat/dynamic-payment-fee)

### 30. Dynamic percentage fee — **NEW FEATURE; staging evidence folded into B2**

The fee is snapshotted immutably at creation (integer round-half-up
arithmetic, DB CHECK constraints binding `payable = amount + fee`),
charged via getLink's amount, and enforced at verify (payable mismatch →
manual review). The bot payload and credited amount are unchanged.
Residual risks:

- **Real-gateway fee behavior is unobserved** (B2): the assumption that
  CentralPay charges exactly the requested payable amount and reports it
  back in verify — including the TOMAN unit — needs staging evidence
  with a fee-bearing payment.
- **Payer-disclosure obligation:** the payer sees the payable amount on
  the gateway page, but disclosing the fee BEFORE the link is issued is
  a bot-flow/operator obligation the bridge cannot enforce (go-live
  checklist item in PRODUCTION_CHECKLIST_FA.md).
- **Operator error** (wrong rate): mitigated by strict rate grammar,
  root-only mutation, append-only audited history, scheduling with
  cancellation, and `/fee` visibility — not eliminated.

## Topic 31 (release/0.6.0-rc1)

### 31. pip-audit finding in a dev-only dependency — **ACCEPTED for RC; post-release backlog**

`pip-audit` over the full development environment reports
PYSEC-2026-1845 in pytest 8.4.2 (fixed in 9.0.3). pytest is a
`dev`-extra dependency only: it is never installed in the production
image (the Dockerfile runs `pip install .` — runtime dependencies
only) and never ships in the release artifact, and CI's dependency
scan of the runtime dependency set is clean. Migrating the test suite
to pytest 9 is deliberately NOT done inside a release-candidate branch
(major-version test-framework bump ≠ release hardening); it is recorded
here as post-release backlog.

## Topic 32 (fix/public-base-url-security-validation)

### 32. Adjacent URL configuration can still be cleartext HTTP — **FIXED (fix/outbound-url-transport-security)**

Resolution: `CENTRALPAY_BASE_URL` now rejects cleartext HTTP
unconditionally (validated HTTPS base with strict authority/path
grammar). `BOT_PAYMENT_NOTIFY_URL` requires HTTPS by default; the
explicit `ALLOW_INSECURE_BOT_NOTIFY_URL=true` opt-in permits `http://`
only for syntactically private/internal destinations (mock bots on
isolated networks) and public hosts remain rejected even with the flag.
No DNS is consulted. The original finding below is kept for history.

While enforcing the PUBLIC_BASE_URL HTTPS-origin contract, the adjacent
outbound URLs were audited:

- `CENTRALPAY_BASE_URL` (default `https://centralapi.org/webservice/basic`)
  has **no application-side validation**: an operator could configure a
  cleartext `http://` value, sending the CentralPay API key in POST
  bodies over plaintext.
- `BOT_PAYMENT_NOTIFY_URL` is validated against `^https?://` — cleartext
  `http://` is **explicitly permitted**, sending the bot `Token` header
  over plaintext. The installer defaults to `https://` but passes a
  user-typed `http://` prefix through unchanged.

Deployment implications: both are outbound URLs under operator control;
the installer's defaults are HTTPS, so exposure requires an explicit
misconfiguration, and some bot deployments legitimately use plain HTTP
inside a private network. Tightening either is a behavioral change for
existing configurations and is deliberately NOT bundled into the
PUBLIC_BASE_URL fix — recorded here for an explicit follow-up decision
(options: require https, or add an explicit
`ALLOW_INSECURE_*_URL=true` escape hatch for private-network bots).

## Topics 33–41 (audit/adversarial-review-0.6.0-rc1 — B4)

The B4 independent adversarial review (2026-07-19, six agents, real
PostgreSQL 16) verdict is **`B4_FAILED_CONFIRMED_CODE_BLOCKERS`**. All
eighteen financial invariants HOLD and every runtime failure mode fails
closed/safe (no path moves money incorrectly), but three confirmed
defects (33–35) must be fixed in a separate focused PR before B4 closes.
Full evidence, per-invariant verdicts, false-positive appendix, and the
recommended remediation scope are in `ADVERSARIAL_REVIEW_0.6.0_RC1.md`.

**Remediation status (fix/b4-confirmed-release-blockers):** CANON-1
(topic 33), CANON-2 (topic 34), and CANON-5 (topic 38) were **FIXED IN
CODE**. CANON-3 (topic 35)'s core commit-binding landed in
fix/b4-confirmed-release-blockers, and the residual `SHA256SUMS`/
`SOURCE_COMMIT` parsing-exactness defects were tightened in
fix/release-manifest-exactness.

**Independent B4 recheck (2026-08-17, seven agents, real PostgreSQL 16,
SHA `c68e86e4…`):** all four — CANON-1, CANON-2, CANON-3 (including the
manifest-exactness follow-up), and CANON-5 — are **FIXED-CONFIRMED**. Full
evidence in `ADVERSARIAL_REVIEW_B4_RECHECK_c68e86e4.md`. The historical
`ADVERSARIAL_REVIEW_0.6.0_RC1.md` verdict for its audited SHA
(`4e62a552…`) stands unchanged as an honest record that B4 failed on that
commit. **B4 is now CLOSED** (see the "Closed release blockers" table
above). B1, B2, B3, and B5 remain open on their existing, unchanged
scope. `PRODUCTION_VALIDATION_STATUS: INCOMPLETE` (B1/B2/B3/B5 still
open).

### 33. Installer rerun silently applies a 0% fee — **FIXED-CONFIRMED (independent B4 recheck, 2026-08-17, `c68e86e4…`)** (was: CONFIRMED DEFECT; MEDIUM; financial correctness)
- **Resolution (CANON-1):** the operator's initial fee is persisted as
  `INSTALLER_INITIAL_FEE_PERCENT` recovery metadata (never the live fee)
  and re-read on the keep-existing path. A new typed operation
  `app.ops fee ensure-initial` replaces `fee set --ensure-initial`: under
  a transaction-level advisory lock it no-ops when any policy history
  exists, and when the table is empty it REQUIRES an explicit validated
  rate — a missing value can never mean 0%. A legacy install with history
  is preserved; with zero rows it fails closed. Proven on real
  PostgreSQL (7 scenarios). The original finding is kept below for history.
- `PAYMENT_FEE_PERCENT` is never persisted (`install.sh:330/331/593`
  only; absent from `deploy/centralpay.env.template` and
  `write_configuration`). If the first-run `fee set … --ensure-initial`
  step fails transiently (no policy row committed) and the operator
  reruns and accepts the default "Keep existing configuration?" → `Y`,
  `gather_input` is skipped, `${PAYMENT_FEE_PERCENT:-0}` = 0, and
  `fee set 0 --ensure-initial` creates a **0% policy** while reporting
  success. The intended non-zero rate is lost (revenue-correctness
  error). Operator-only, narrow precondition, but the default rerun path
  is the buggy one.
- Fix direction: persist the chosen fee and re-read it on the
  keep-existing path, or refuse to create a policy when the rate was
  never supplied on a rerun. Add a rerun regression test.

### 34. `isdigit()`-gated `int()` crashes on gateway/bot digit-like strings — **FIXED-CONFIRMED (independent B4 recheck, 2026-08-17, `c68e86e4…`)** (was: CONFIRMED CODE DEFECT; LOW; fails closed/safe)
- **Resolution (CANON-2):** both parsers now use an explicit ASCII grammar
  (`-?[0-9]+` for gateway numbers, `[0-9]+` for `Retry-After`) and catch
  `ValueError`/`OverflowError` defensively, returning `None` so malformed
  values flow through the existing field-error / normal-backoff paths
  instead of raising. Malformed gateway numbers now route the payment to
  manual review (no 500); a malformed `Retry-After` falls back to the
  normal backoff without a worker crash. Raw malformed values never reach
  logs, events, alerts, or responses. The original finding is kept below.
- Two sites, one root cause (`str.isdigit()` ⊋ `int()`-parseable):
  `_to_int` (`app/centralpay.py:85-95`, gate `lstrip("-").isdigit()`)
  crashes on `"²"`/`"⁵"`/`"--5"` on the verify path → uncaught
  `ValueError` (not a `CentralPayError`) → HTTP 500 → the payment is
  **not** routed to manual review as designed (stays `link_created`,
  re-500s). `_parse_retry_after` (`app/bot.py:71-81`, gate
  `stripped.isdigit()`) crashes on a `Retry-After: \xb2` 429 → the
  worker pass fails and the row self-heals to manual review via
  stale-claim recovery. Reproduced with the real modules; gateway/bot
  trust boundary, never the public payer; no money moves incorrectly.
- Fix direction: `try/except ValueError` (or `isdecimal()` under
  `re.ASCII`) at both sites, routing to the existing safe paths; add
  tests for `"²"`, `"--5"`, and a `\xb2` Retry-After.

### 35. Update integrity control decoupled from the deployed bytes — **FIXED-CONFIRMED, incl. manifest exactness (independent B4 recheck, 2026-08-17, `c68e86e4…`)** (was: CONFIRMED DEFECT; MEDIUM; weakened control + doc mismatch)
- **Resolution (CANON-3):** the release workflow emits a checksummed
  `SOURCE_COMMIT` asset, and `centralpay update` requires the fetched
  tag's commit to equal the verified `SOURCE_COMMIT` before any
  checkout/build/migration/restart (see topic 19 for the full new
  guarantee). A tag moved after the release was built is rejected.
- **Manifest-exactness follow-up (`fix/release-manifest-exactness`):** the
  first implementation selected `SOURCE_COMMIT` / `centralpay-bridge-.*.tar.gz`
  loosely (a wildcard artifact match, only "two lines present"), and used
  `tr -d '\n'` to normalize `SOURCE_COMMIT`. Both were tightened to a strict
  fail-closed helper: `SHA256SUMS` must contain EXACTLY one entry each for
  the exact runtime artifact filename and the exact filename `SOURCE_COMMIT`,
  each formatted precisely as `[0-9a-f]{64}  <name>` (rejecting duplicates,
  `./SOURCE_COMMIT`, `path/SOURCE_COMMIT`, `SOURCE_COMMIT.old`, similarly
  named artifacts, binary `*` markers, uppercase/malformed hashes, and
  leading/trailing junk); and the `SOURCE_COMMIT` file bytes must full-match
  `[0-9a-f]{40}` or `[0-9a-f]{40}\n` read as RAW BYTES (rejecting embedded
  newlines, a second trailing newline, CRLF, spaces, tabs, NUL/control bytes,
  uppercase hex, and any other length — no normalization into validity).
  This combined result awaits the independent B4 recheck before being marked
  fully fixed. The original finding is kept below for history.
- `verify_release_artifact` (`scripts/centralpay:239-263`) checksums the
  release tarball then `rm -rf`s it; `cmd_update` deploys the tag via an
  independent `git fetch --tags` + `git checkout FETCH_HEAD`
  (`:298-299`) with no `git verify-tag`/SHA pin. The checksum never
  gates the deployed tree. Fails closed on a missing checksum; under the
  honest threat model (GitHub+TLS trusted) the trees are identical, so
  no practical exploit today, but the control gives false assurance and
  topic 19 overstates it. High impact / low likelihood (needs tag/GitHub
  compromise).
- Fix direction: deploy from the verified tarball, or `git verify-tag` /
  pin `FETCH_HEAD` to the manifest commit; correct topic 19's wording.

### 36. GitHub Actions are not SHA-pinned — **SUPPLY-CHAIN GAP; MEDIUM; register blind spot (topic 18 covers base images only)**
- Every `uses:` in `ci.yml`/`release.yml` is a mutable tag
  (`actions/checkout@v4`, `docker/*@v3/v6`, `anchore/sbom-action@v0`,
  `gitleaks/gitleaks-action@v2`, `lycheeverse/lychee-action@v2`, …).
  Third-party actions run with repo access; the release `package` job
  holds `contents: write`. Fix: pin to full commit SHAs (Dependabot).

### 37. No dependency lockfile / hash pinning — **POST-RELEASE BACKLOG; LOW-MEDIUM; supply chain**
- Runtime deps are ranges only; no lock/hash file → non-reproducible
  builds and a non-deterministic pip-audit set. Fix: `pip-compile`/`uv
  lock` + `pip install --require-hashes`.

### 38. Dockerfile OCI version label stale (`0.5.0-rc1`) — **FIXED-CONFIRMED (independent B4 recheck, 2026-08-17, `c68e86e4…`; live `docker build` blocked by *local sandbox* egress policy — static analysis, a regression test, and GitHub Actions' Docker build/compose validation job on PR #63 all corroborate the fix)** (was: DOCUMENTATION MISMATCH; LOW)
- **Resolution (CANON-5):** the label is now `${APP_VERSION}`, supplied by
  a build ARG that CI and the release workflow set from
  `app.version.APP_VERSION` (a local build with no `--build-arg` gets an
  empty, never stale, label). Both image smoke tests inspect the label and
  assert it equals `APP_VERSION`, and a Dockerfile test forbids any version
  literal in the version label. `APP_VERSION` and the pyproject version are
  unchanged. Original finding kept below.
- `Dockerfile:26` (pre-fix) `org.opencontainers.image.version="0.5.0-rc1"`
  vs `APP_VERSION="0.6.0-rc1"`; syft could propagate it into the shipped
  SBOM. Unguarded by tests.

### 39. Concurrent `reference_id` collision → HTTP 500 — **CONFIRMED DEFECT; LOW; fails safe (not a B4 blocker)**
- The non-locking collision `SELECT` (`app/services/verification.py:150`)
  can be raced by two callbacks reporting the same `reference_id` for
  different payments; the loser's commit hits `uq_payments_reference_id`
  → `IntegrityError` → 500, then self-heals to manual review on retry.
  The UNIQUE constraint is the real backstop — no double credit (proven
  on real PostgreSQL). Optional fix: catch `IntegrityError` → manual
  review.
  **B4 recheck (2026-08-17):** independently re-proven live on real
  PostgreSQL (5 trials) — classification unchanged. Incremental detail:
  `app/services/aged_out_recovery.py`'s `execute_confirmed_recovery` only
  catches `CentralPayError`, not this `IntegrityError`, so hitting the
  same collision via `recover-aged-out --confirm` surfaces an unhandled
  traceback rather than a graceful 500 — same fails-safe outcome, rougher
  operator UX on a second entry point. See topic 47.

### 40. No reconciliation for a crash in the verify→commit window — **PARTIALLY SUPERSEDED — see B4 recheck note** (was: EXTERNAL VALIDATION GAP / POST-RELEASE; LOW-MEDIUM; fails closed)
- A crash after `client.verify()` succeeds but before `db.commit()`
  leaves the payment `link_created`. Ties to B2 (verify-after-verify
  idempotency). No money moves incorrectly.
  **B4 recheck (2026-08-17):** the "no background sweep... recovery
  relies on the payer re-hitting the callback URL" claim below is now
  stale. `app/services/reconciliation.py`'s always-on worker
  (`reconciliation_enabled: bool = True` by default) already re-verifies
  aged `link_created` payments through the exact same `verify_and_settle`
  path, and `app/services/aged_out_recovery.py` (`recover-aged-out`)
  adds an explicit operator escape hatch beyond even that. The residual
  risk this topic describes is smaller than previously documented, not
  larger — updated here for accuracy, not because a new defect was
  found. Original text preserved below for history.
- *(original text)* There is no background sweep to re-verify aged
  `link_created` payments, so recovery relies on the payer re-hitting the
  callback URL. Optional fix: a reconciliation job.

### 41. `_to_int` accepts non-ASCII decimal digits — **POST-RELEASE BACKLOG; LOW; no financial impact**
- Diverges from `services/fees.py` (`re.ASCII`); parses to the correct
  integer and must still match the stored ASCII value, so no wrong value
  and no crash. Consistency nit; align with `re.ASCII`.

## Topics 42–48 (independent B4 recheck, 2026-08-17 — `c68e86e4…`)

New items surfaced by the seven-agent B4 recheck (see
`ADVERSARIAL_REVIEW_B4_RECHECK_c68e86e4.md` for full evidence), recorded
here per this register's own practice so nothing found during the recheck
is fixed-and-hidden. Topics 42–47 are six **new non-blocking defects**.
A seventh new finding — `recover-aged-out --confirm`'s `IntegrityError`
handling gap on a `reference_id` collision — is NOT a new topic; it is
recorded as an incremental detail under the existing topic 39 (see
topic 47's cross-reference). Topic 48 is **not a non-blocking defect**:
it is an already-known open release/environment risk scoped entirely to
B2, extended by this recheck to one more scenario — not a new B4 finding.
None of topics 42–48 is a B4 blocker.

### 42. `review resend` (app/ops.py) has weaker eligibility checks than `bulk_resend.py` — **CONFIRMED NON-BLOCKING DEFECT; LOW**
- Independently found by two reviewers: missing the `bot_notify_reason`
  allowlist filter, and ignoring `review_resolved_at` (a payment just
  marked "resolved, do not deliver" can still be resent). Mitigated by
  two mandatory confirmation flags, by requiring `gateway_verified_at`
  non-null (financial-mismatch reviews never set it), and by being a
  single-payment, human-reviewed, host-CLI-only action. Fix direction:
  reuse `bulk_resend.ELIGIBLE_RESEND_REASONS` and add a
  `review_resolved_at IS NULL` guard.

### 43. CANON-2 `isdigit()`-then-unguarded-`int()` pattern reappears at new sites — **CONFIRMED NON-BLOCKING DEFECT; LOW; fails safe/unreachable**
- `app/adminbot/queries.py:find_payment`, `app/ops.py:_load_review_payment`,
  two sites in `app/adminbot/commands.py`, and `app/api/payments.py`'s
  Content-Length header parsing. Admin/ops sites are gated behind admin
  auth and/or a broad top-level exception handler (generic error reply,
  never a crash). The `app/api/payments.py` site was proven empirically
  unreachable: a raw malformed `Content-Length` header is rejected by
  uvicorn's own HTTP parser with `400 Bad Request` before the ASGI app is
  ever entered (verified against the actual production stack). Fix
  direction: apply the `isdecimal()` + BIGINT-bound pattern already proven
  in `app/cli.py:_find_payment` to the admin/ops sites.

### 44. `_is_private_bot_host` misclassifies hex/octal/integer IPv4 literals as "private" — **CONFIRMED NON-BLOCKING DEFECT; LOW-MEDIUM; requires non-default opt-in**
- `app/config.py`'s numeric-IP-literal detection only recognizes
  strictly-decimal-digit dotted quads; `0x08080808` (resolves to the real
  public `8.8.8.8`) falls through to the "single-label = private
  container name" default and is wrongly accepted. Proven live (real DNS
  resolution). Falsifies the module's own docstring claim for this input
  class. Not exploitable without the non-default
  `ALLOW_INSECURE_BOT_NOTIFY_URL=true` opt-in. Fix direction: validate
  every authority label via `ipaddress.ip_address()`.

### 45. Secret-redaction floor can be undercut by short-configured API keys/tokens — **CONFIRMED NON-BLOCKING DEFECT; LOW; defense-in-depth only**
- `app/logging_setup.py`'s `_MIN_REDACTABLE_LENGTH = 6` combined with no
  `Field(min_length=...)` on `centralpay_getlink_api_key`/
  `centralpay_verify_api_key`/`bot_notify_token`/`admin_bot_token` (unlike
  `inbound_api_key`/`callback_hmac_secret`, which require ≥16). No
  currently-live code path logs these raw, so no active leak — proven by
  configuring a 5-char key and confirming it is not redacted. Fix
  direction: raise the length floor on these four settings to match the
  others.

### 46. `CENTRALPAY_UPDATE_REF` passed to `git fetch` without a `--` separator — **CONFIRMED NON-BLOCKING DEFECT; LOW; requires already-compromised config**
- Permits git-flag injection if the value begins with `-`. Requires the
  attacker to already control the root-owned, 0600 env file, at which
  point more direct compromise paths already exist. Fix direction:
  `git fetch --tags --force origin -- "$ref"`.

### 47. `recover-aged-out --confirm` lacks the `require_root` gate its sibling mutating commands have — **CONFIRMED NON-BLOCKING DEFECT; LOW**
- `scripts/centralpay`'s `cmd_recover_aged_out` has no `require_root`
  call, unlike `cmd_review`/`cmd_fee`'s mutating subcommands — the
  codebase's own established pattern for financial-mutation commands.
  Bounded in practice: `docker exec` access is already effectively
  root-equivalent in the standard deployment. Also see topic 39's
  incremental detail (this command's `IntegrityError` handling gap). Fix
  direction: add `require_root "recover-aged-out --confirm"`.

### 48. `recover-aged-out --confirm` extends the open B2 verify-after-verify risk to a new scenario — **ALREADY-KNOWN OPEN RELEASE/ENV RISK, scoped to B2, not a new B4 finding**
- Makes a real gateway `verify()` call and *applies* the result for a
  payment the reconciliation worker has typically already polled — the
  same "verify-after-verify against real CentralPay, never confirmed
  safe" question B2 already tracks, extended to payments outside the
  worker's normal polling window. Not a code defect this recheck's scope
  can fix — it is B2's existing whole-system risk. Recommended (not
  required for B4): extend `STAGING_VALIDATION.md`'s procedure to
  explicitly cover a `recover-aged-out --confirm` call on a payment with
  ≥1 prior worker verify attempt before this command is used against
  production CentralPay.

**Accepted risks confirmed by the B4 review (not defects):** the
intentional serialization of the gateway HTTP call across the row lock
(capacity concern only; makes invariant 10 hold), the bounded
unauthenticated signature-storm alert write (~1/600s window), the
`CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` root-only escape hatch, and the
interrupted-restore + manual-`start` operator override.

**Rejected candidate findings (false positives):** SSRF via config (the
gateway `redirectUrl` is validated and only returned to the payer, never
fetched server-side); IPv4-mapped IPv6 host misclassification (correct
classification); any double-credit / false-verification path (blocked by
the `FOR UPDATE` lock, verified-status short-circuit, the
`reference_id` UNIQUE constraint, and the financial CHECK constraints —
proven on real PostgreSQL); aborted-transaction continuation and
deadlock/lock-ordering cycles (none found).

## Topic 49 — production update-mode enforcement

### 49. `centralpay update` silently allowed a non-release-tag ref in production — **FIXED**
- Topic 19 closed the checksum/`SOURCE_COMMIT` binding for release-tag
  updates, but a non-tag `CENTRALPAY_UPDATE_REF` (e.g. `main`, left over from
  a misconfiguration or an operator typo) was still accepted unconditionally:
  `resolve_verified_update_commit` printed a `DEVELOPMENT MODE` warning and
  deployed the fetched branch commit anyway, with no checksum and no
  `SOURCE_COMMIT` binding, on a production host exactly as readily as on a
  development one.
- **Resolution:** a non-release-tag ref now fails closed by default — before
  any fetch result matters — unless `CENTRALPAY_UPDATE_ALLOW_DEV_REF=true` is
  explicitly set in `centralpay.env`. That flag is documented as
  local-development-only and is never set by the installer or the shipped
  `deploy/centralpay.env.template` default. Covered by
  `tests/test_update_integrity.py::test_non_release_ref_fails_closed_by_default`
  and `::test_non_release_ref_development_mode_requires_explicit_optin`.
- Orthogonal to, and does not change, the already-accepted
  `CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` escape hatch (topic 19/48), which
  only applies when the ref IS a release tag but its assets can't be
  downloaded.

## Topic 50 — 0.6.0-rc2 version preparation

### 50. Package/source metadata bumped to `0.6.0-rc2` ahead of tagging — **NOT A DEFECT; PROCESS NOTE**
- `app/version.py` (`APP_VERSION`) and `pyproject.toml` (`version`) bumped
  from the `0.6.0-rc1` line to `0.6.0-rc2`, following this project's
  established practice of preparing source/metadata for the next release
  candidate ahead of its tag (the same pattern used for `v0.6.0-rc1` itself
  per `FINAL_FINANCIAL_AUDIT.md`'s verdict section). Living docs and the
  shipped `deploy/centralpay.env.template` / `CENTRALPAY_UPDATE_REF` example
  values were updated in lockstep; historical audit/validation snapshots
  (this register's own earlier topics included) were deliberately **not**
  rewritten, per `DOCUMENTATION.md`'s documentation-maintenance rules.
- **This does not close, reopen, or change the status of any blocker
  above.** B1 (real-host install), B2 (real CentralPay contract
  validation), B3 (live Telegram admin-bot validation), and B5 (release
  workflow run for this exact commit) remain open exactly as recorded.
  `v0.6.0-rc2` must not be tagged, published, or used for real payments
  until they are closed and a human approval is recorded — same as the
  `0.6.0-rc1` release decision above.
- No production access, no deployment, and no real CentralPay/Telegram
  call were performed to prepare this version bump.

## Topic 51 — v0.6.0-rc2's first real release.yml run: two pre-existing defects found, fixed on main, rc3 prepared

### 51. `v0.6.0-rc2` tagged; its first real tag-triggered release run failed on two unrelated, pre-existing defects — **FIXED on main; rc2 tag preserved as evidence; rc3 prepared**
- `v0.6.0-rc2` was tagged at `6e3f33636e7f059086444ee03ec46e38b78c1ded`.
  Its `release.yml` run (id `33252207603`) was the first *tag-triggered*
  execution of that workflow — B5 had been open precisely because no
  tag-triggered run had happened yet. (An earlier `workflow_dispatch`
  run on `main`, before `v0.6.0-rc1` was even tagged, had already
  caught and fixed two different pipeline defects — an unresolvable
  Trivy Action pin and a gitleaks full-history false positive — via
  commit `88090fc`; that run was not tag-triggered.) It found:
  1. **Documentation checks / Local links resolve (lychee)**: 15
     table-of-contents anchors in the legacy Persian handbook
     (`docs/راهنمای_جامع_کاربری_CentralPay_Bridge_FA.md`) linked to
     headings containing zero-width non-joiners (ZWNJ, U+200C) copied
     verbatim into the anchor; the actual heading-slug algorithm the
     link checker uses drops ZWNJ rather than preserving it, so none
     of those 15 anchors resolved. Root-caused by downloading the
     exact `lychee v0.24.2` binary the workflow pulls and reproducing
     the failure locally byte-for-byte, then empirically deriving the
     real slug algorithm (ZWNJ deleted; Unicode combining marks kept)
     and regenerating the 15 anchors from it. The other 24 links in
     the file were already correct and untouched.
  2. **Docker build, scan, and SBOM / Trivy vulnerability scan**: the
     scan's `docker run` image reference, `aquasecurity/trivy:0.58.0`,
     is not a real Docker Hub repository — Aqua Security's Docker Hub
     namespace is `aquasec`, not `aquasecurity` (that longer name is
     only the GHCR/GitHub org). Confirmed directly against the Docker
     Hub v2 registry API (manifest request for `aquasec/trivy:0.58.0`
     → 200; same request for `aquasecurity/trivy:0.58.0` → 401).
     `--exit-code 1` / `--severity CRITICAL,HIGH` / `--ignore-unfixed`
     are unchanged.
- Because `package` (which drafts the release) `needs: [docs, ...]`,
  no draft release was ever created for `v0.6.0-rc2` — `docs` failing
  meant `package` was skipped entirely.
- Both fixes landed on `main` via PR #82, validated (targeted +
  full-suite pytest against real PostgreSQL 16, ruff, mypy, shellcheck,
  both docker-compose profiles, and an independent full local `lychee`
  run against `**/*.md`: 0 errors) and merged.
- **`v0.6.0-rc2` was not deleted, moved, or reused.** It remains
  tagged at the same commit as a preserved historical record of a
  release-candidate attempt whose *pipeline* (not its application
  behavior) failed validation; `RELEASE_NOTES_0.6.0_RC2.md` and its
  `CHANGELOG.md` entry are unchanged. `app/version.py`/`pyproject.toml`
  were then bumped to `0.6.0-rc3` (same pattern as topic 50) and
  `RELEASE_NOTES_0.6.0_RC3.md` prepared, describing rc3 as the
  fixed-pipeline supersession of the rc2 attempt with no application
  or payment behavior change from rc2.
- **This does not close, reopen, or change B1/B2/B3.** B5 is
  **partially addressed but still open**: the two concrete defects
  rc2's real run found are fixed on `main`, but B5 requires an actual
  green tag-triggered run, which has not yet happened for any tag —
  `v0.6.0-rc3` has not been tagged as of this entry. Closing B5
  necessarily requires creating the `v0.6.0-rc3` tag first — the
  release workflow only runs on `push: tags` or manual dispatch (see
  B5's own definition above) — so the tag is the mechanism for
  producing that evidence, not something gated behind it. What
  remains gated on B1, B2, and a green B5 run (and B3 if the optional
  admin bot is to be enabled) — plus a recorded human approval — is
  **publishing** the GitHub release and using this version for any
  real payment; this register does not authorize either.
- No production access, no deployment, and no real CentralPay/Telegram
  call occurred while diagnosing or fixing either defect.

## Topic 52 — migration `0007`'s downgrade has no `CENTRALPAY_DROP_*` guard, unlike `0008`–`0012` — **KNOWN GAP; NOT FIXED; NON-BLOCKING FOR THIS RELEASE**

### 52. `alembic/versions/0007_payer_identity.py::downgrade` unconditionally drops payer-identity data — no opt-in required
- Migrations `0008`, `0009`, `0010`, `0011`, and `0012` all gate their
  destructive downgrade path behind an explicit `CENTRALPAY_DROP_*`
  environment opt-in (`CENTRALPAY_DROP_PAYER_IDENTITY`,
  `CENTRALPAY_DROP_RECONCILIATION`, `CENTRALPAY_DROP_MONITOR_INCIDENTS`,
  `CENTRALPAY_DROP_MONITOR_INCIDENT_LAST_ALERT`). `0007` — which
  originally created `centralpay_payer_identities` and the payment
  snapshot columns those later migrations extend — has no such guard:
  its `downgrade()` unconditionally drops the FK, both snapshot
  columns, and the entire mapping table the moment `alembic downgrade`
  is run past it.
- `centralpay rollback` never exercises this path (it is
  application-only and never calls `alembic downgrade`), so no
  supported CLI workflow can trigger it. The risk is limited to an
  operator manually running raw `alembic downgrade` below `0007`
  against a database with real payer-identity history.
- Found by an automated review of PR #84 (`chatgpt-codex-connector`),
  which correctly flagged that `RELEASE_NOTES_0.6.0_RC3.md` and
  `CHANGELOG.md` previously promised this was guarded for every
  migration without exception. Those documents are corrected in the
  same commit that adds this topic, to stop overpromising and instead
  name `0007` as the explicit exception with a directly-stated
  operational warning.
- **This is a pre-existing gap, not introduced by rc3**: `0007` shipped
  as part of the rc2 line and has read this way since it merged. It is
  recorded here for the first time because this is the first review to
  have caught it.
- **Not fixed in this branch.** `release/0.6.0-rc3`'s own stated scope
  is metadata/pipeline-adjacent only, with no `app/` or migration code
  touched beyond the version string, and this project's own
  `MIGRATION_GUIDE.md` rule is to never edit an already-deployed
  migration revision for schema changes — adding a guard to `0007`'s
  downgrade is exactly that kind of change and belongs in a dedicated,
  reviewed follow-up (e.g. a `CENTRALPAY_DROP_PAYER_IDENTITY_MAPPING`
  guard on `0007` itself, matching the naming and behavior of
  `0008`/`0009`'s own `CENTRALPAY_DROP_PAYER_IDENTITY`). Until then,
  operators must not run manual `alembic downgrade` past `0007` on any
  database holding real payer-identity history.
- Does not close, reopen, or change B1/B2/B3/B5, and does not block
  this release line — no supported production path exercises the
  unguarded downgrade.

## Topic 53 — v0.6.0-rc3's tag-triggered release.yml run passed every job but Trivy; CVE-2026-14456 fixed; rc4 prepared

### 53. `v0.6.0-rc3` tagged; its release.yml run correctly failed the Trivy image scan (CVE-2026-14456, HIGH) — **FIXED on main; rc3 tag preserved as evidence; rc4 prepared**
- `v0.6.0-rc3` was tagged at `a963f295e3e1b28733a8e77105bdc780e27f399d`.
  Its `release.yml` run (id `33256093589`) was the furthest any
  tag-triggered run of this workflow has gotten: every job passed —
  `docs`, `quality` (both OS legs), `shell`, `secret-scan`,
  `dependency-scan` — except `docker`. That job's `Trivy vulnerability
  scan (image)` step correctly failed on a real, fixable finding in the
  built image:
  ```
  libssl3t64 / openssl / openssl-provider-legacy
  CVE-2026-14456   HIGH   status: fixed
  Installed: 3.5.6-1~deb13u2   Fixed: 3.5.7-1~deb13u2
  ```
  This is categorically different from rc2's two failures: it was not a
  pipeline defect (a broken action pin, a wrong image namespace) but the
  fail-closed vulnerability gate doing exactly its job — stopping a real,
  actionable, fixable HIGH-severity finding from ever being packaged. No
  release-notes/gate weakening of any kind occurred or was considered.
- Because `package` (which drafts the release) `needs: [..., docker,
  ...]`, no draft release was ever created for `v0.6.0-rc3` — `docker`
  failing meant `package` was skipped entirely, same mechanism as rc2's
  `docs` failure before it.
- Root cause, verified directly against the Docker Hub v2 registry API
  (not just the Trivy scan text): `Dockerfile` pinned `FROM
  python:3.12-slim`, a floating tag. At build time it resolved to
  `python:3.12.14-slim-trixie` (built 2026-08-25), one Debian security
  point-release behind its own OpenSSL fix. Confirmed identical content
  under the explicit `python:3.12-slim-trixie` tag, and confirmed both
  `amd64` (`sha256:a249c9f4…`) and `arm64` (`sha256:dcac2e6d…`) exist
  under the same multi-arch index (`sha256:09f7da3b…`).
- Fixed on `main` via PR #85:
  - `Dockerfile`: both build stages now pull the same pinned
    `python:3.12-slim-trixie@sha256:...` base via a shared `ARG
    BASE_IMAGE`, instead of the floating tag.
  - Runtime stage now runs a general `apt-get upgrade` (never
    `dist-upgrade`, never a single hardcoded package pin) before
    installing `curl`, closing not just this CVE but the general class
    of "pinned base trails Debian's own security repo by build time."
  - The Trivy scan's image reference, digest pin, and severity policy
    were extracted into `.github/scripts/trivy-scan.sh`, and `ci.yml`'s
    `docker` job now runs the identical scan on every pull request —
    this exact class of issue reached a release tag undetected
    specifically because pull-request CI had no equivalent gate; it now
    does, for every future PR before a release is ever tagged.
  - Validated via a manual `workflow_dispatch` of `release.yml` against
    the fix branch (run `33270153365`, not a tag push — the draft-release
    step is `if: startsWith(github.ref, 'refs/tags/v')` and correctly
    showed `skipped`): `amd64` build, `arm64` build, image smoke test,
    Syft SBOM, and the exact pinned Trivy command all passed, reporting
    `HIGH: 0, CRITICAL: 0`. The build log independently confirms the
    package upgrade: `Unpacking libssl3t64:amd64 (3.5.7-1~deb13u2) over
    (3.5.6-1~deb13u2)`.
- **`v0.6.0-rc3` was not deleted, moved, or reused.** It remains tagged
  at the same commit as a preserved historical record of a
  release-candidate attempt whose *container image*, not its release
  pipeline configuration or application behavior, failed validation;
  `RELEASE_NOTES_0.6.0_RC3.md` and its `CHANGELOG.md` entry are
  unchanged. `app/version.py`/`pyproject.toml` were then bumped to
  `0.6.0-rc4` (same pattern as topics 50/51) and
  `RELEASE_NOTES_0.6.0_RC4.md` prepared, describing rc4 as the
  fixed-image supersession of the rc3 attempt with no application or
  payment behavior change from rc3.
- A separate, pre-existing gap was found and fixed while manually
  verifying the container fix: `release.yml`'s full-history `gitleaks`
  secret scan (job `secret-scan`) had not been re-run since the
  original topic-19-era fix months ago, and the test suite had since
  grown two dummy-credential fixture shapes (`tests/test_phase3_app.py`'s
  `alias-secret-...`, `tests/test_logging_redaction.py`'s
  `attacker-guessed-key-...`) that `.gitleaks.toml`'s allowlist did not
  yet cover. Fixed via a separate, dedicated PR (#86) — unrelated to the
  container CVE, no shared root cause, not bundled into PR #85.
- **This does not close, reopen, or change B1/B2/B3.** B5 made real,
  verified progress but is **not yet closed**: rc3's tag-triggered run
  passed every job except Trivy, and that finding is now fixed and
  independently confirmed (see above), but B5 still requires an actual
  green run against `v0.6.0-rc4`'s own tag — closing B5 necessarily
  requires creating that tag first (the release workflow only runs on
  `push: tags` or manual dispatch), so the tag is the mechanism for
  producing that evidence, not something gated behind it. What remains
  gated on B1, B2, and a green B5 run (and B3 if the optional admin bot
  is to be enabled) — plus a recorded human approval — is **publishing**
  the GitHub release and using this version for any real payment; this
  register does not authorize either.
- No production access, no deployment, and no real CentralPay/Telegram
  call occurred while diagnosing or fixing any of this.
