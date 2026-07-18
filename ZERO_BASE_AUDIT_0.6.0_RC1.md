# Zero-based clean-room audit — 0.6.0-rc1

Branch: `audit/zero-based-0.6.0-rc1`. A fully independent audit of the
entire repository, performed with no reliance on prior PR descriptions,
audit verdicts, documentation claims, or test summaries. Every claim
below was re-derived from the source at the audited SHA or from tests
executed in the clean-room environment described here.

## Audited revision and environment

| Item | Value |
|---|---|
| Audited main SHA | `7dfa083509ae017b1984fdf1a71194e9497fd849` (matches the expected merge of PR #18) |
| Checkout | fresh `git clone` into a new directory (no reused worktree, caches, or artifacts) |
| Python | 3.12.3 (fresh `uv`-managed virtual environment; no reused venv) |
| PostgreSQL | 16.13 (server and client; fresh `centralpay_zero` database, dropped and recreated between full runs) |
| Docker / Compose | 29.3.1 / v5.1.1 |
| OS / arch | Ubuntu 24.04.4 LTS, x86_64, kernel 6.18.5 |
| pip-audit | 2.10.1 |
| SQLite evidence policy | SQLite used ONLY for logic-level unit tests; every locking / migration / constraint / concurrency / backup claim is proven on real PostgreSQL 16. The CI-guard test (`tests/integration/test_ci_guard.py`) fails the build if the PostgreSQL suites would silently skip. Zero tests skipped in every clean-room run. |

## Baseline (before any fix)

Full suite on the pristine tree, twice, each on a freshly created
PostgreSQL database: **465 passed, 0 failed, 0 skipped** both times —
no order dependence or leaked global state observed. All baseline
failures reported below were found by inspection and adversarial
reproduction, not by existing tests (which is why regression tests were
added for each).

## Confirmed findings and fixes

### Finding 1 — installer accepted out-of-range fee percentages (MEDIUM)

`install.sh` validated the fee answer with `^[0-9]{1,3}(\.[0-9]{1,2})?$`
only: `101`, `999`, `100.01`, `100.99` passed the prompt. The typed
Python parser later rejected them, but `ensure_initial_fee_policy`
merely printed a WARNING and the installation completed "successfully"
without the fee the operator requested — a silent financial-configuration
failure. Prompt numbering was also inconsistent (`1/9`…`8/9`, `9/10`,
`10/10`).

**Fix:** new unit-testable `validate_fee_percent` bash function —
exactly the language of `parse_rate_percent` (0–100 inclusive, ≤2
decimals, ASCII digits only, no signs/whitespace/exponents/commas/
newlines, pure string+integer comparison, no float arithmetic; `100`,
`100.0`, `100.00` accepted; `100.01`/`101`/`999` rejected). The prompt
loop re-prompts until valid; `ensure_initial_fee_policy` re-validates
the value and now **fails the installation** (`fail`, not `warn`) if the
policy cannot be ensured. All prompts renumbered `1/10`…`10/10`.
Hostile-input/boundary tests added for both the bash validator and its
lockstep with the Python parser (`test_installer_fee_validator_*`,
`test_installer_prompt_numbering_is_consistent`,
`test_installer_fee_initialization_failure_is_fatal`).

### Finding 2 — `--ensure-initial` used the wrong "exists" predicate (HIGH)

`app/ops.py` treated "an initial policy is unnecessary" as
`select_effective_policy(db) is not None`. That predicate is false when
the table contains ONLY a future scheduled policy or ONLY cancelled
history — in both cases an installer rerun would inject a surprise
immediate policy, changing financial configuration the operator had set
deliberately. Two concurrent reruns could also both pass the check.

**Fix:** `--ensure-initial` now creates a policy **only when
`fee_policies` contains zero rows**; any row (active, scheduled, or
cancelled) makes it a no-op with an accurate message. Concurrent
executions are serialized with a PostgreSQL **transaction-level advisory
lock** (`pg_advisory_xact_lock`, fixed key `FEE_ENSURE_INITIAL_LOCK_KEY`)
— no process-local locking; the loser waits for the winner's commit,
re-counts, and no-ops. History is never deleted or rewritten. Tests:
no-rows → one policy; active/future-only/cancelled-only → no-op
(`test_ops_ensure_initial_*`); PostgreSQL barrier race → exactly one row
(`test_concurrent_ensure_initial_creates_exactly_one_policy`).

### Finding 3 — cancelling the ACTIVE policy silently reactivated an old rate (HIGH)

`cancel_policy` accepted any non-cancelled policy id. Reproduced in the
clean room: with history [90% (old), 10% (current)], cancelling the
current 10% policy made selection fall back to the stale **90%** policy
— a silent, unaudited-looking rate change (the cancel event does not say
"rate is now 90%"). The CLI help always described `fee cancel` as
cancelling a *scheduled* policy; the implementation did not enforce it.

**Fix:** `cancel_policy` now refuses any policy whose `effective_at` is
not in the future (active or superseded history), with an error that
directs the operator to `fee set` (`fee set 0` to remove the fee).
Cancelling a future scheduled policy remains allowed and never changes
the active rate. Tests: active-cancel refused with rate proven
unchanged, superseded-history cancel refused, scheduled cancel leaves
the rate untouched before and after its would-be activation.

### Finding 4 — documentation referenced nonexistent commands/routes (LOW)

`centralpay health` (REAL_HOST_VALIDATION.md, MIGRATION_GUIDE.md,
PRODUCTION_CHECKLIST_FA.md) and `https://<domain>/health`
(REAL_HOST_VALIDATION.md) do not exist. **Fix:** replaced with the real
`centralpay status` / `centralpay diagnose` and
`/health/live` + `/health/ready`. Added consistency tests that parse all
nine operator-facing documents and assert every `centralpay <cmd>`
exists in the CLI dispatch and no bare `/health` URL is documented
(`test_docs_reference_only_real_cli_commands`,
`test_docs_reference_only_real_public_health_routes`). Also added the
supported-vs-validated OS matrix to REAL_HOST_VALIDATION.md: the
installer accepts Ubuntu 22.04/24.04/26.04, CI exercises 22.04/24.04,
and **no real-host evidence is recorded for any version** — the
operator's reported 26.04 VPS use is noted without result because no
sanitized evidence was supplied; code support for 26.04 proves nothing
about production validation.

### Finding 5 — stale packaging version (LOW)

`pyproject.toml` still declared `version = "0.5.0rc1"` while
`app/version.py` is `0.6.0-rc1`: wheel/SBOM metadata would carry the
wrong version. **Fix:** bumped to `0.6.0rc1` (PEP 440 form) and added
`test_pyproject_version_matches_app_version` so the two can never drift
again.

### Finding 6 — overclaiming test language (DOC)

"Byte-for-byte" was claimed for the bot notification payload, but the
regression test asserts the **exact JSON object and exact field set
parsed from the raw request body** (plus the Token header) — it
deliberately does not pin the JSON encoder's byte serialization.
Similarly, backup tests compare every financial **field**, not archive
bytes. All such wording was corrected ("exact JSON object and field
set", "field-for-field"); no test was weakened — the wording now matches
what is actually proven. "Exactly once" claims were individually checked
and are each backed by direct count assertions under real PostgreSQL
races (`len(verify_requests) == 1`, `len(bot_stub.requests) == 1`, …)
and stand as written. "Production-ready" appears only in negations.

## Money-model audit (independent)

Writer sweep over `app/` for every financial field (full grep evidence
in the audit branch history):

| Field | Writers | Verdict |
|---|---|---|
| `Payment.amount` | 1 — `_ensure_payment_row` insert (`app/services/payments.py`) | never reassigned (A) |
| `fee_policy_id` / `fee_rate_bps` / `fee_amount` / `payable_amount` | 1 each — same insert, same transaction | snapshot immutable (E) |
| `Payment.bot_order_id` | 1 — same insert | never reassigned |
| `Payment.status` | 8 — creation, link/getlink-fail, verification manual-review, queue, worker accept/retry/manual-review, gated ops resend | matches the documented state machine exactly |
| `Payment.reference_id` | 1 — post-collision-check assignment in verification | single writer (F4) |
| `Payment.gateway_verified_at` | 1 — post-validation assignment in verification | single writer |

- (B/C/D) `fee_amount = (amount * fee_rate_bps + 5000) // 10000`,
  `payable = amount + fee` — pure integers; a grep for
  `float|round(|Decimal|* 0.` over all money paths returns nothing.
  DB CHECKs bind `payable = amount + fee` at the storage layer.
- (F/G/H) duplicate orders, getlink-failed retries, and policy changes
  proven (SQLite logic + PG races) to preserve the creation snapshot.
- (I/J) getLink is called with `amount=payment.payable_amount`; verify
  compares `result.amount != payment.payable_amount`.
- (K) a payable mismatch routes to `manual_review` before any verified
  fact; `claim_next_due` requires `status = bot_notify_pending AND
  gateway_verified_at IS NOT NULL`, and a worker pass after a mismatch
  delivers nothing (`test_payable_mismatch_never_notifies_bot`).
- (L/M) the outgoing bot request body parses to exactly
  `{"order_id": ..., "actions": "custom_payment_verify"}` — two keys,
  Token header, no amount/fee/payable/reference/gateway-order-id.
- (N) populated-0005 upgrade test proves the backfill
  (`NULL / 0 / 0 / amount`).
- (O/P) backup/restore preserves policies (active+scheduled+cancelled)
  and snapshots field-for-field with ids; db-check detects policy-less
  fee corruption, exits 1, and provably does not modify the corrupted
  row.

## Subsystem audits (evidence = clean-room test executions)

- **API & auth:** the full required case matrix exists and passes
  (strict schema tests in `test_creation_hardening.py`, bounds incl.
  payable-max boundary, duplicate matrix incl. post-verification and
  under-review, concurrency races on PG). Rejections assert status,
  stable public code, row/event/getLink counts, and no credential
  disclosure.
- **CentralPay client:** transport/timeout/non-200/non-JSON/missing-
  marker/false-marker/malformed-field matrix in
  `test_centralpay_client.py` + `test_fault_injection.py`; gateway text
  is reduced to a fixed reason-code vocabulary; redaction proven by
  sentinel extraction tests over logs, responses, `last_error`, events,
  and alerts.
- **Callback & replay:** HMAC, one-time token, parameter pollution,
  malformed hex, unknown payment, replay before/after verification and
  after acceptance, manual-review terminality, concurrent valid/stale
  races, signature-storm memory bound — all present and passing on PG.
- **Worker:** claim requires verification; one claim = one worker +
  attempt; straggler results refused; bounded retries in all paths incl.
  stale-claim recovery; safe-mode ambiguity → manual review; accepted
  never auto-resent; 2xx = accepted only. Injected clocks; no sleeps.
- **Migrations:** stepwise 0001→0006 + repeated head on fresh PG;
  populated-0005 upgrade with backfill verification; CHECK/UNIQUE
  constraints tested by direct violating SQL (rejected by PostgreSQL).
- **Backup/restore:** custom-format dump of full lifecycle states +
  policies; corrupt/truncated/zero-byte/plain-SQL rejection; restore →
  migrations → db-check → sequence safety; post-restore policy change
  touches no restored payment; restored accepted payments are not
  re-delivered (claim query excludes accepted).
- **Installer/CLI static:** tty prompts, silent secrets, rerun
  idempotence, 0700/0600 config modes, backup.sh root:root 0750 +
  git mode 100755, no secret echo, no env-file dumps, restore
  confirmation, uninstall data preservation.
- **Update/rollback:** pre-update backup, checksum-gated update,
  application-only rollback; the cross-0006 rollback limitation (old app
  cannot satisfy NOT NULL `payable_amount`) is documented in
  MIGRATION_GUIDE.md and RELEASE_NOTES_0.6.0_RC1.md.

## Results

- Tests before fixes: **465 passed / 0 failed / 0 skipped** (twice,
  fresh DBs). The five findings were latent — none was covered by an
  existing test, which is precisely why each fix ships with regression
  tests.
- Tests after fixes: **480 passed / 0 failed / 0 skipped** (repeated
  runs, each on a freshly created database; +15 regression tests). Ruff clean, mypy clean (71 files),
  ShellCheck + `bash -n` clean, Compose config valid for both profiles,
  offline doc-link check clean.
- pip-audit 2.10.1: one finding — pytest 8.4.2 (PYSEC-2026-1845, fix
  9.0.3), a `dev`-extra dependency never installed in the production
  image or shipped in the artifact (risk register topic 31; runtime
  dependency set clean).
- **Not executable in this sandbox** (container-registry egress
  blocked): local Docker image builds, the non-root smoke test, Caddy
  container validation, gitleaks/Trivy/Syft binaries. Docker build +
  smoke + Caddy + gitleaks + runtime pip-audit run in this PR's CI
  (verified green for this branch); Trivy and Syft run only in the
  tag-triggered release workflow, which has never run — that is blocker
  B5 and a reason NOT_SAFE_TO_TAG stands until CI on the tag proves it.
- Release artifact (built exactly as release.yml does, from the audit
  branch HEAD): `centralpay-bridge-0.6.0-rc1.tar.gz` — tracked files
  only, expected top-level prefix, contains install.sh/backup.sh/
  centralpay (executable modes preserved), migration 0006, release
  notes; no `.env`, secrets, dumps, private keys, or credentials.
  SHA256SUMS generated and verified from a separate, env-isolated
  process: `797824bf2e99027c51df3aa70e6044b165a3d934f9f066781019cf5b1e4ba3b0`.

## Accepted risks

- pytest dev-dependency CVE (topic 31, dev-only, post-release backlog).
- All previously recorded accepted risks in RELEASE_RISK_REGISTER.md
  (off-site DR, digest pinning, proxy-level rate limiting, signed
  releases, load testing, payer failure pages) — re-reviewed, unchanged.

## External blockers (unchanged, all open)

B1 real-host install evidence (22.04 AND 24.04; none recorded — see the
OS matrix), B2 real CentralPay staging evidence incl. a fee-bearing
payment, the payable amount reported in verify, TOMAN unit, and verify
idempotency, B3 live admin-Telegram run, B4 external adversarial
review, B5 green tag-triggered release workflow (Trivy/SBOM/artifacts),
plus real-bot credit-semantics confirmation.

## Verdicts

```
CODE_VERDICT: CODE_FINANCIALLY_SOUND

RELEASE_TAG_VERDICT: NOT_SAFE_TO_TAG

PRODUCTION_VERDICT: NOT_SAFE_FOR_REAL_PAYMENTS
```

- **CODE_FINANCIALLY_SOUND:** with the five findings fixed on this
  branch, no known code path can double-credit, lose a credit, mutate a
  fee snapshot, silently change the effective fee rate, convert an
  unknown delivery into success, bypass manual review, or erase
  financial history — per the writer sweep and 480 clean-room tests.
  Note: findings 2 and 3 were financial-configuration hazards present
  in the audited main; main is sound only once this branch merges.
- **NOT_SAFE_TO_TAG:** the tag gate requires this branch merged (it
  fixes confirmed defects in 0.6.0-rc1's fee-ops surface), and the
  release workflow itself (B5) has never run; tagging before both is
  releasing unaudited-in-anger automation.
- **NOT_SAFE_FOR_REAL_PAYMENTS:** verdicts from mocks and CI cannot
  substitute for B1–B5 and real-bot confirmation. No external test is
  claimed to have occurred.
