# Release risk register — 0.5.0-rc1

Triage of every unresolved topic from `DEFERRED_REVIEW.md` for the
0.5.0-rc1 release candidate. Each item records a classification
(**fixed** / **accepted risk** / **release blocker** / **post-release
backlog**), severity, financial impact, exploitability, likelihood,
mitigation, test coverage, and the release decision.

**Bottom line: 0.5.0-rc1 may NOT be tagged and may NOT be used for real
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

### 2. Gateway-controlled error text — **ACCEPTED RISK**
- Severity: low · Financial impact: none · Exploitability: low (requires
  a hostile/compromised gateway) · Likelihood: low
- Mitigation: gateway text is truncated to 200 characters, passes
  through the secret-redaction pipeline, and is stored in audit data.
  Client-visible error responses use fixed error codes; free text is not
  interpolated into HTML (payer pages are templated with escaping).
- Tests: redaction suite; page-rendering tests.
- Release decision: accepted for RC; sanitization review remains on the
  post-release backlog.

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
  (`verify_empty_reference_id`, `verify_invalid_amount`,
  `verify_invalid_user_id`) and route to manual review.
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

### 18. Base images not digest-pinned — **ACCEPTED RISK for RC; backlog**
- Images remain tag-pinned (`python:3.12-slim`, `postgres:16`,
  `caddy:2`). This sandbox cannot reach Docker Hub to resolve digests;
  pinning must be done from CI or an operator host (process: `docker
  buildx imagetools inspect <image> --format '{{json .Manifest}}'`, then
  pin `image@sha256:…` in Dockerfile/compose). Mitigation: Trivy image
  scan in the release workflow. Severity: medium (supply chain).

### 19. Update channel integrity — **FIXED (0.5.0-rc1)**
- `CENTRALPAY_UPDATE_REF` now defaults to a pinned release tag. For
  release tags, `centralpay update` downloads the published artifact's
  `SHA256SUMS` and verifies the checksum before deploying; unverifiable
  updates abort (development-mode escape hatch requires an explicit env
  var). Version history is recorded; `centralpay rollback` is
  application-only and never downgrades the DB schema. Signed
  tags/artifacts remain pre-1.0 backlog.

### 20. Installer never executed on a real host — **RELEASE BLOCKER B1**
- This environment has no VM/root target available; a real
  `curl | sudo bash` install on Ubuntu 22.04/24.04 has never been
  executed. Per the release instructions this is explicitly a release
  blocker — see `REAL_HOST_VALIDATION.md`. The RC must not be tagged
  until this is done and recorded.

### 21. Off-site backup replication — **ACCEPTED RISK; backlog**
- Backups are local; replication is a documented manual recommendation
  (`BACKUP_RESTORE_FA.md`). The restore path itself is now proven by an
  automated pg_dump/pg_restore round-trip integration test.

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
| B4 | Multi-agent adversarial review (started for Phase 1, stopped, never completed; not run for Phases 2–5) | `DEFERRED_REVIEW.md` |
| B5 | Release workflow (`.github/workflows/release.yml`) has not yet run green: Docker builds, Trivy scan, SBOM, and artifact packaging are CI-delegated and unverified locally | GitHub Actions |

**Release decision:** 0.5.0-rc1 is a code-complete release candidate.
It must not be tagged, published, or used for real payments until B1,
B2, B4, and B5 are closed (and B3 if the admin bot is to be enabled),
and a human approval is recorded.
