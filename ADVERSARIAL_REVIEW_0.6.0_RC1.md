# Independent adversarial review — 0.6.0-rc1 (release blocker B4)

- **Audited main SHA:** `4e62a552a381f1853d0f04efce26c7142dfbf6d5` (verified: `git rev-parse HEAD`
  on branch `audit/adversarial-review-0.6.0-rc1`, created from `origin/main` at that SHA).
- **Review date:** 2026-07-19.
- **Reviewers:** six independent agents (five parallel domain reviews + one reconciliation),
  each working from the current source without seeing the others' conclusions before
  submitting. Nothing was trusted from prior audits, PR descriptions, code comments, test
  names, documentation, or earlier completion reports — every claim was re-verified against
  the current code, schema, workflows, and tests.
- **Exact B4 verdict:** **`B4_FAILED_CONFIRMED_CODE_BLOCKERS`** (see the disposition at the end).
- **`PRODUCTION_VALIDATION_STATUS: INCOMPLETE`** — B1, B2, B3, and B5 remain open external gates
  regardless of this review's outcome.

This branch changes only this file and `RELEASE_RISK_REGISTER.md`. **No runtime code, test,
migration, workflow, script, or deployment file was modified.** Confirmed defects are documented,
not fixed here; a separate focused remediation PR is recommended below.

---

## Agent scopes

| Agent | Scope | Method |
|-------|-------|--------|
| A | Financial state machine: creation, immutable amount, fee snapshot, payable, getLink, callback/verify classification, manual review, bot payload, terminal states | Source trace + pure-Python parser repros (dialect-independent) |
| B | Transactions & concurrency: tx boundaries, aborted-tx behaviour, row locks, `SKIP LOCKED`, create/callback races, crash windows, worker & admin-alert ownership, stale-claim recovery, deadlocks | **Real PostgreSQL 16** (own DB `centralpay_auditb`), two-session probe scripts |
| C | Trust boundaries: inbound auth, callback HMAC + one-time token, gateway JSON, referenceId/redirect handling, URL parsing/SSRF, secret-bearing outbound, log/exception/page leakage, malformed Unicode/control/NUL/oversize | Source review + decode/parse probes (no DNS, no external probes) |
| D | Deployment & operations: Dockerfile, compose, credentials, network isolation, Caddy, installer, rerun, migrations, update/rollback, backup/restore, db integrity, permissions, health, admin-bot optionality, partial deployment | Source review + `shellcheck`, `bash -n`, `docker compose config` (both profiles) |
| E | Release & supply chain: CI/release workflows, tag/version checks, artifacts, checksums, SBOM, Trivy, gitleaks, pip-audit, dependency scopes, draft behaviour, update compatibility, B1/B2/B3/B5 assumptions | Source review + `pip-audit`, workflow ref inventory |
| F | Reconciliation: dedupe, challenge false positives, verify every surviving finding directly, reject speculation, rank by release impact | Independent re-verification against current code + repros |

---

## Files and workflows examined

- **Runtime:** `app/main.py`, `app/api/{callback,payments,pages,health,deps}.py`, `app/services/{payments,verification,notification,fees,heartbeat}.py`, `app/{centralpay,bot,security,config,models,db,audit,logging_setup,middleware,ratelimit,reasons,exceptions,ops,cli,worker}.py`, `app/adminbot/*`.
- **Schema:** `alembic/versions/0001…0006`, `alembic/env.py`.
- **Deployment:** `Dockerfile`, `docker-compose.yml`, `deploy/caddy/Caddyfile.template`, `deploy/centralpay.env.template`, `deploy/systemd/*`, `install.sh`, `scripts/centralpay`, `scripts/backup.sh`, `.dockerignore`, `.gitleaks.toml`.
- **Workflows:** `.github/workflows/ci.yml`, `.github/workflows/release.yml`.
- **Tests (as hypotheses, re-verified):** the full `tests/` and `tests/integration/` trees.

---

## Commands and tests executed

Baseline and static gates (orchestrator, on real PostgreSQL 16):

| Command | Result |
|---------|--------|
| Full `pytest -q` with `TEST_DATABASE_URL` → PostgreSQL 16 | **726 passed**, 7 warnings |
| `ruff check .` / `ruff format --check .` | clean |
| `mypy app tests` | clean (80 source files) |
| `shellcheck install.sh scripts/backup.sh scripts/centralpay` | clean |
| `bash -n` on the three scripts | clean |
| `docker compose config` (default and `--profile admin-bot`) | valid (Agent D) |
| `pip-audit` (repo venv) | 1 finding: `pytest 8.4.2` PYSEC-2026-1845 — **dev-only, never shipped** |

Concurrency / fault-injection (Agent B, real PostgreSQL, DB `centralpay_auditb`, schema via
`alembic upgrade head`): two concurrent callbacks (verify called **exactly once**); stale-worker
straggler after successor claim (idempotent / safe / same-worker-id-reused — all discarded);
admin-alert failure injected via a genuine `BEFORE INSERT … RAISE` trigger (financial tx still
commits, no full-session rollback); AMBIGUOUS bot outcome → manual review, never re-claimable;
stale admin-alert straggler discarded, successor `delivered` preserved.

Direct reproductions (orchestrator): `_to_int("--5")`, `_to_int("²")`, `_to_int("⁵")`, and
`_parse_retry_after("²")` each raise `ValueError` while `_to_int("½")` (isdigit False) safely
returns `None`; installer `PAYMENT_FEE_PERCENT` non-persistence confirmed by grep across
`install.sh`, `deploy/centralpay.env.template`, and `write_configuration`; `Dockerfile:26`
version label read directly; `scripts/centralpay` update path (verify-then-discard vs
independent `git checkout FETCH_HEAD`) read directly.

**Tests that could NOT be executed here (documented, not skipped):**

- **gitleaks** — binary not installed in this environment. Config (`.gitleaks.toml`) was
  inspected and is test-guarded (`useDefault=true`, value-shape allowlist, no path allowlisting).
  The scan itself runs in CI and in `release.yml`.
- **Trivy image scan** — the container registry / Docker Hub is unreachable from this sandbox.
  The workflow pins `aquasecurity/trivy:0.58.0` with `--exit-code 1 --severity CRITICAL,HIGH`
  and is a `needs:` of the release `package` job. Runs in `release.yml` only. → part of **B5**.
- **Docker image build + smoke test** — CI-delegated; the release pipeline has never run green
  end-to-end. → **B5**.
- **Caddy runtime validation** — the Caddyfile template was validated statically (routes,
  redaction, headers, callback-path equality with `app.security.CALLBACK_PATH`); a live
  `caddy validate` against a rendered file with real TLS is part of **B1/B2** (real host).

---

## Financial-invariant verdict table

All eighteen required invariants **HOLD** against the current code. Every confirmed failure
mode identified below **fails closed or fails safe** — no path moves money incorrectly.

| # | Invariant | Verdict | Primary evidence |
|---|-----------|---------|------------------|
| 1 | Original amount never changes after creation | **HOLDS** | `amount` set only in `Payment()` ctor `app/services/payments.py:118`; no reassignment in `app/`; conflicting duplicate → `DuplicateOrderAmountMismatchError` without mutation `payments.py:180`; `ck_payments_amount_positive` |
| 2 | Fee snapshotted at creation; later policy changes can't alter an existing payment | **HOLDS** | snapshot taken once `payments.py:90-124`; no reassignment of `fee_*`/`payable_amount`/`fee_policy_id`; `test_fee_flow.py::test_fee_change_affects_only_new_orders` |
| 3 | `payable_amount == amount + fee_amount` | **HOLDS** | `calculate_fee` `services/fees.py:54`; `ck_payments_payable_equals_amount_plus_fee` `models.py:185`, migration 0006 |
| 4 | getLink receives `payable_amount` | **HOLDS** | single caller `payments.py:236` → `get_link(amount=payment.payable_amount)` → `centralpay.py:344` |
| 5 | Verify requires returned amount == `payable_amount` exactly | **HOLDS** | `verification.py:118` exact int `!=`, mismatch → manual review |
| 6 | Bot receives only `{"order_id","actions":"custom_payment_verify"}` | **HOLDS** | `bot.py:194-198`; only poster; body never logged |
| 7 | Bot never receives fee or payable amount | **HOLDS** | only `bot_order_id` passed `notification.py:391`; `test_fee_flow.py::test_bot_notification_payload_contains_no_fee_fields` |
| 8 | Malformed/ambiguous gateway success can never mark verified | **HOLDS** (routing caveat: CANON-2) | conservative `gateway_reason_code`/`_explicit_success`/typed coercion `centralpay.py`; anomalies → manual review `verification.py:98-146` |
| 9 | Verified fact + queue state committed atomically | **HOLDS** | single tx `verification.py:172-193`; **real-PG probe** (1 verified, 1 queued) |
| 10 | Duplicate/concurrent callbacks ≤1 verification side effect | **HOLDS** | `FOR UPDATE` `verification.py:240` + verified-status short-circuit `:276`; **real-PG probe**: verify called exactly once |
| 11 | One-time tokens; superseded links rejected; post-verify replay does not re-contact CentralPay | **HOLDS** | token checked under lock before verify `verification.py:258`; verified short-circuit `:276`; token rotates per link-creation `payments.py:223` |
| 12 | Invalid reference IDs never reach a query/assignment/audit/log | **HOLDS** | `_parse_reference_id` returns `(None, invalid)` without the raw value `centralpay.py:98-131`; only fixed codes emitted |
| 13 | Bot timeout in safe mode not auto-retried into duplicate credit | **HOLDS** | AMBIGUOUS → manual review, `next_retry_at=None` `notification.py:326-348`; **real-PG probe** |
| 14 | Stale worker cannot overwrite a successor's notification result | **HOLDS** | ownership predicate under `FOR UPDATE` `notification.py:216-220`; **real-PG probe** (closes the SQLite-only test gap) |
| 15 | Stale admin-alert worker cannot overwrite a successor's alert result | **HOLDS** | `ClaimedAlert` ownership predicate; **real-PG probe** |
| 16 | Admin-alert failures can never abort a financial transaction | **HOLDS** | `begin_nested` SAVEPOINT isolation; **real-PG probe** with a genuine trigger `RAISE` |
| 17 | Manual-review tools cannot mutate financial facts | **HOLDS** | `cli.py` read-only; `ops.py` review/resend touch only status/claim/review metadata, gated on `gateway_verified_at IS NOT NULL` + idempotent mode; fee ops append-only; `db-check` touches only sequences |
| 18 | Backup/restore cannot silently restore a partial/corrupt DB and restart writers | **HOLDS** | layered gates in `cmd_restore` `scripts/centralpay:426-516`; writers stay stopped on any gate failure; `--exit-on-error`, sha256 manifest, post-restore `db-check` |

---

## Confirmed findings (ranked by actual release impact)

### CANON-1 — Installer rerun silently applies a 0% fee and reports success — **CONFIRMED DEFECT · MEDIUM · financial correctness**

- **Files:** `install.sh:593` (`percent="${PAYMENT_FEE_PERCENT:-0}"`), keep-existing path
  `install.sh:732-737`, `ensure_initial_fee_policy` `install.sh:586-608` (called `:757`);
  consumer `app/ops.py:232-273`.
- **Financial impact:** the platform charges a **0% fee** instead of the operator's intended
  non-zero rate — a silent revenue-correctness error that reports "installed successfully". Not
  a money-safety issue (no payer is overcharged; no double credit), but an incorrect financial
  configuration shipped silently.
- **Exploitability / preconditions:** operator-only (root installer). Requires the initial
  `fee set … --ensure-initial` step to fail on the **first** run (e.g. a transient DB/infra
  error — the `fail` message itself tells the operator to re-run the installer), then the
  operator accepting the default `Y` on the "Keep existing configuration?" prompt on rerun.
  The default rerun path is the buggy one, which raises likelihood.
- **Failure sequence:** first run gathers `PAYMENT_FEE_PERCENT=5` → `write_configuration` writes
  the env file (which does **not** contain the fee) → `deploy_stack` runs migrations →
  `ensure_initial_fee_policy` runs `fee set 5 --ensure-initial`, which **fails transiently** →
  installer `fail`s, **no policy row committed**. Rerun → `ENV_FILE` exists → prompt defaults to
  `Y` → `KEEP_EXISTING=true` → `gather_input` skipped → `PAYMENT_FEE_PERCENT` unset →
  `${PAYMENT_FEE_PERCENT:-0}` = `0` → `fee set 0 --ensure-initial` sees zero policy rows →
  creates a **0% "Initial installation fee"** policy → prints success.
- **Proof:** `PAYMENT_FEE_PERCENT` occurs only at `install.sh:330` (prompt), `:331` (validate),
  `:593` (use); it is absent from `deploy/centralpay.env.template` and from `write_configuration`
  (grep confirmed). The keep-existing branch (`:734-737`) re-derives `PAYMENT_DOMAIN`,
  `BOT_PAYMENT_NOTIFY_URL`, `BOT_NOTIFY_RETRY_MODE`, `BACKUP_RETENTION_DAYS` only. `app/ops.py`
  `--ensure-initial` no-ops only when a policy already exists; with zero rows it creates the 0%
  policy and commits.
- **Why existing tests miss it:** `test_fee_flow.py` proves `--ensure-initial` no-ops with an
  existing policy and creates from zero rows — both correct in isolation. No test drives the
  installer rerun where `gather_input` is skipped **and** zero policies exist.
- **Smallest remediation direction:** persist the chosen fee (write `PAYMENT_FEE_PERCENT` into
  the env file, re-read it on the keep-existing path), or make `ensure_initial_fee_policy` refuse
  to create a policy when the rate was never supplied on a rerun.

### CANON-2 — `isdigit()`-gated `int()` crashes on gateway/bot-controlled digit-like strings — **CONFIRMED CODE DEFECT · LOW · fails closed/safe**

- **Two sites, one root cause:**
  - **Callback path:** `app/centralpay.py:85-95` `_to_int` gates on
    `stripped.lstrip("-").isdigit()` then `int(stripped)`; called from `verify()` at
    `centralpay.py:326` (amount) and `:329` (userId), outside any `try`.
  - **Worker path:** `app/bot.py:71-81` `_parse_retry_after` gates on `stripped.isdigit()`
    (`:76`) then `int(stripped)` (`:78`); called from `classify_response` at `bot.py:111`.
- **Root cause:** Python's `str.isdigit()` is a **superset** of `int()`-parseable — it accepts
  Unicode superscripts (`"²"` U+00B2, `"⁵"`) which `int()` rejects; and `_to_int`'s
  `lstrip("-")` also lets a multi-sign string (`"--5"`) through the gate. Both raise `ValueError`.
- **Financial impact:** none — **both paths fail before any state change**. Site 1 leaves the
  payment 500-looping instead of routed to manual review (a robustness/design-intent gap on the
  money-critical callback); site 2 self-heals to manual review via stale-claim recovery.
- **Exploitability / preconditions:** **gateway** trust boundary (site 1) and **bot** trust
  boundary (site 2) — a compromised or buggy CentralPay / customer bot, never the public payer.
  Site 1: `verify.php` returns success with `amount`/`userId` as `"²"` or `^-{2,}\d+$`. Site 2:
  the bot returns HTTP 429 with `Retry-After: \xb2` (httpx decodes the latin-1 byte to `"²"`).
- **Failure sequence (site 1):** valid signed callback → `process_callback` → `client.verify()`
  → `_to_int` raises `ValueError` → **not** a `CentralPayError`, so it escapes the
  `except CentralPayError` guard (`verification.py:299`) and the route (`callback.py:142`) →
  `_unhandled_error_handler` (`main.py:49`) → **HTTP 500**; `get_db` closes the session with no
  commit → the uncommitted `callback_received` event rolls back → payment stays `link_created`
  and re-500s on every retry while the gateway returns that value (design intent — "malformed
  fields → manual review" — is violated).
- **Failure sequence (site 2):** worker claims a due payment (attempt committed) → bot returns
  429 with the crafted `Retry-After` → `classify_response` raises `ValueError` in the `else`
  branch of `execute_claimed_attempt` (`notification.py:395`), **outside** `except httpx.HTTPError`
  → propagates to the worker loop's broad `except Exception` (`worker.py:91`) →
  `record_attempt_result` never runs, the row stays claimed → after
  `bot_notify_claim_timeout_seconds` (default 120s) `release_stale_claims` moves it to manual
  review (safe mode).
- **Proof (reproduced with the real modules):** `_to_int("--5")`, `_to_int("²")`, `_to_int("⁵")`
  and `_parse_retry_after("²")` each raise `ValueError`; `"½"` (isdigit False) → `None`;
  `"²".isdigit()` is `True` while `int("²")` raises. Confirmed independently by Agents A and C.
- **Why existing tests miss it:** the field-error suites feed ASCII-digit or plainly non-numeric
  strings; the `isdigit()` ⊋ `isdecimal()` ⊋ `int()`-accepts gap is a Python-specific gotcha not
  enumerated. No test uses `"²"`, `"--5"`, or a `\xb2` Retry-After.
- **Smallest remediation direction:** at both sites, wrap the coercion in
  `try/except ValueError: return None` (or gate with `str.isdecimal()` under `re.ASCII` semantics)
  so failures route to the existing manual-review / `None` paths; add regression tests for `"²"`,
  `"--5"`, and a `\xb2` Retry-After. **These are the confirmed runtime-code defects.**

### CANON-3 — Update integrity control is decoupled from the deployed bytes — **CONFIRMED DEFECT · MEDIUM · weakened control + documentation mismatch**

- **Files:** `scripts/centralpay:239-263` (`verify_release_artifact`) vs `:298-299` (deploy);
  register claim `RELEASE_RISK_REGISTER.md` topic 19 ("verifies the checksum before deploying").
- **Impact:** `verify_release_artifact` downloads and checksums
  `centralpay-bridge-<tag>.tar.gz` + `SHA256SUMS`, then `rm -rf "$tmp"` **discards the verified
  bytes** (`:258`, `:261`). The actual deploy is an **independent** `git fetch --tags origin "$ref"`
  + `git checkout -q FETCH_HEAD` (`:298-299`) with **no** `git verify-tag` and **no** SHA pin
  binding the checkout to the verified manifest. The checksum proves only the release asset's
  internal consistency, not the deployed tree. The register overstates the guarantee.
- **Exploitability / preconditions:** root operator runs `centralpay update`; requires a
  moved/tampered tag on origin (GitHub or maintainer compromise) with the old valid asset +
  `SHA256SUMS` left in place. High impact / low likelihood. Under the honest threat model
  (GitHub + TLS trusted) `git archive HEAD` (the asset) and `git checkout FETCH_HEAD` yield
  identical trees, so there is no practical exploit today. The control **fails closed** on a
  missing checksum (`fail` at `:254`) unless `CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true`.
- **Why existing tests miss it:** deployment tests assert installer/CLI text and ordering; none
  asserts the verified artifact equals the deployed content.
- **Smallest remediation direction:** deploy **from** the verified tarball (extract into
  `INSTALL_DIR`), or add `git verify-tag` / pin `FETCH_HEAD` to the manifest's commit; and correct
  the register topic 19 wording. (Signed tags/artifacts are already pre-1.0 backlog.)

### CANON-4 — GitHub Actions are not SHA-pinned — **EXTERNAL VALIDATION GAP / POST-RELEASE · MEDIUM · supply chain**

- Every `uses:` in `.github/workflows/ci.yml` and `release.yml` references a **mutable tag**:
  `actions/checkout@v4`, `actions/setup-python@v5`, `docker/setup-buildx-action@v3`,
  `docker/build-push-action@v6`, `docker/setup-qemu-action@v3`, `anchore/sbom-action@v0`,
  `gitleaks/gitleaks-action@v2`, `lycheeverse/lychee-action@v2`,
  `actions/upload|download-artifact@v4`. Third-party actions run with repo access and the
  release `package` job holds `contents: write`.
- `RELEASE_RISK_REGISTER.md` topic 18 accepts unpinned base **images** only — Action refs are a
  register blind spot.
- **Remediation direction:** pin all (especially third-party) actions to full commit SHAs
  (Dependabot keeps them fresh); add a register entry. Docker images `aquasecurity/trivy:0.58.0`
  (immutable) plus `postgres:16`/`caddy:2`/`python:3.12-slim` (tag-pinned, digest pinning is
  accepted-risk topic 18).

### CANON-5 — Dockerfile OCI version label is stale (`0.5.0-rc1`) — **DOCUMENTATION MISMATCH · LOW**

- `Dockerfile:26` `org.opencontainers.image.version="0.5.0-rc1"` while `app/version.py:3`
  `APP_VERSION="0.6.0-rc1"` and `pyproject.toml` `version="0.6.0rc1"`. The label is a static
  string (not an ARG), and syft reads image labels, so the shipped SBOM can misreport the
  version. Unguarded by tests (the zero-based audit caught the identical drift in pyproject but
  missed the Dockerfile).
- **Remediation direction:** source the label from a build ARG fed by `APP_VERSION`, or remove
  the hardcoded label; extend `test_dockerfile_properties` to assert the label tracks
  `APP_VERSION`.

### CANON-6 — Concurrent `reference_id` collision degrades to HTTP 500 — **CONFIRMED DEFECT · LOW · fails safe**

- The collision check at `verification.py:150-154` is a **non-locking** `SELECT` against other
  rows; each callback holds `FOR UPDATE` only on its own payment (`:240`). Two simultaneous
  callbacks reporting the **same** `reference_id` for **different** payments can both pass the
  pre-commit check; the second `db.commit()` (`:193`) hits `uq_payments_reference_id` UNIQUE
  (`models.py:125-127`, migration 0004) → `IntegrityError`, uncaught → **HTTP 500**, and the
  loser's tx rolls back (stays `link_created`).
- **Financial impact:** none — the UNIQUE constraint is the real backstop (**real-PG probe**:
  1 verified, 1 row carrying the reference, 1 queued notification; no double credit). On the
  payer's retry the committed sibling is visible so the app-level check routes the loser to
  manual review — self-heals.
- **Why existing tests miss it:** the PG suite tests the collision only **sequentially**.
- **Remediation direction (optional):** catch `IntegrityError` on the verified-state commit and
  route to `_move_to_manual_review("reference_id_collision")` so the first colliding callback
  also gets a graceful `under_review` page.

### CANON-7 — No reconciliation for a crash in the verify→commit window — **EXTERNAL VALIDATION GAP / POST-RELEASE · LOW-MEDIUM · fails closed**

- A crash after `client.verify()` succeeds (`verification.py:298`) but before `db.commit()`
  (`:193`) persists nothing → the payment stays `link_created`. `client.verify()` is the only
  verify caller; **no background job re-verifies aged `link_created` payments**. Recovery relies
  on the payer re-hitting the signed callback URL (verify is idempotent, so re-verification is
  safe). Crash-**after**-commit is benign (verified fact + queue state durable; the worker
  delivers regardless).
- **Residual:** a *stuck-but-uncredited* payment if the payer never returns — no money moves
  incorrectly. Confirmation of CentralPay verify-after-verify idempotency is part of **B2**.
- **Remediation direction (optional):** a periodic reconciliation sweep that re-verifies aged
  `link_created` payments against the gateway.

### CANON-8 — No dependency lockfile / hash pinning — **POST-RELEASE · LOW-MEDIUM · supply chain**

- `pyproject.toml` pins runtime deps as **ranges** only; no `requirements*.txt`/lock/hash file,
  so `pip install .` in the image build and in the dependency scan resolves whatever PyPI serves
  at build time (non-reproducible; the pip-audit result is not deterministic).
- **Remediation direction:** add a hash-pinned lock (`pip-compile`/`uv lock`) and
  `pip install --require-hashes` for the image.

### CANON-9 — `_to_int` accepts non-ASCII decimal digits — **POST-RELEASE · LOW · no financial impact**

- `_to_int("٥")` returns `5` (Python `int()` accepts Unicode Nd digits), diverging from
  `services/fees.py:40` which deliberately uses `re.ASCII`. No crash and no wrong value (it
  parses to the correct integer and must still exactly equal the stored ASCII int); a
  consistency/hardening nit. (Same module as CANON-2 but a distinct, benign behaviour.)

---

## Accepted risks (documented / intentional — not defects)

- **HTTP call held across a row lock (F-B2):** `create_payment` holds `FOR UPDATE` across
  `client.get_link` and `process_callback` holds it across `client.verify` (default 15s gateway
  timeout, default `QueuePool`). This serialization is **intentional** and is what makes
  invariant 10 hold; the residual is a capacity/DoS-amplification concern under callback bursts,
  not a correctness bug. Consistent with register topic 8/17b.
- **Signature-storm alert write (C-minor / F-B4):** the invalid-signature branch
  (`callback.py:124-138`) does an unauthenticated `create_alert` + `commit` directly (not via the
  SAVEPOINT). Safe — no financial state, bounded to ~1 write per 600s window by
  `SignatureFailureTracker`, and `get_db` closes on exit. Register topic 23-adjacent.
- **`CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` (D-3):** off by default, root-only, documented
  "not recommended". Combined with CANON-3 it means an update can be entirely unverified.
- **Interrupted-restore + manual `start`:** the restore tool never restarts writers on a partial
  DB, but an operator manually running `centralpay start` afterward would, and `/health/ready`
  only does `SELECT 1`. Operator override against printed warnings; post-release hardening (a
  restore-in-progress sentinel that `start` refuses).

---

## External validation gaps (NOT code defects — remain open per their scope)

- **B1** — installer never executed on a real Ubuntu host (`REAL_HOST_VALIDATION.md`).
- **B2** — CentralPay contract never observed for real: verify schema, verify-after-verify
  idempotency (relevant to CANON-7), TOMAN unit, fee-bearing payment, real Caddy TLS
  (`STAGING_VALIDATION.md`).
- **B3** — live Telegram validation of the admin bot (`ADMIN_BOT_VALIDATION.md`); the payment
  path does not depend on it.
- **B5** — the release workflow has never run green: Docker build, Trivy scan, SBOM, and artifact
  packaging are CI-delegated and unverified locally.
- **Branch protection / required-status-checks** are not in the repository tree, so whether CI
  actually **gates a merge** is unverifiable from source (external config gap).
- **gitleaks license note:** `gitleaks/gitleaks-action@v2` needs `GITLEAKS_LICENSE` for
  org-owned repos; the repo is a personal account today, so N/A — but the secret-scan job would
  break silently if the repo moves under an org.

---

## Rejected candidate findings (false-positive appendix)

- **SSRF via configuration — REJECTED.** URL validators (`app/config.py`) correctly reject
  userinfo, `%`-encoding, backslash, protocol-relative, zero-padded/dangling ports, oversized
  hosts, and IPv6 tricks; the gateway `redirectUrl` is validated HTTPS-only/no-userinfo and only
  **returned to the payer's browser** — the bridge never fetches it. The only server-side
  outbound calls are to the **configured** gateway and bot URLs. No attacker-controlled URL is
  ever fetched.
- **IPv4-mapped IPv6 misclassification — REJECTED.** `_is_private_bot_host` classifies
  `[::ffff:169.254.169.254]` as private (correct — link-local) and `[::ffff:8.8.8.8]` as public
  (correct). No public-as-private misclassification; config-only path anyway.
- **Any "double credit / false verification" claim — REJECTED.** Blocked by the verified-status
  short-circuit (`verification.py:276`), the `FOR UPDATE` row lock (`:240`),
  `uq_payments_reference_id` UNIQUE (`models.py:126`), and the
  `ck_payments_delivery_requires_verification` + `ck_payments_payable_equals_amount_plus_fee`
  CHECK constraints (`models.py:185-195`). Verified on real PostgreSQL by Agent B.
- **Aborted-transaction continuation — REJECTED.** Every caught DB error is followed by
  `db.rollback()` / `session.close()`; the caught non-DB errors (`CentralPayError`,
  `httpx.HTTPError`) occur in the external HTTP call and do not poison the DB transaction.
- **Deadlock / lock-ordering cycle — REJECTED.** All multi-row scans use `FOR UPDATE SKIP
  LOCKED`; result-recording takes a single PK row lock; no worker holds two payment locks at
  once.

---

## Unresolved questions

- **CANON-3 residual (B2/B5):** binding the deployed tree to the verified artifact vs. adopting
  signed tags is a release-owner decision; the practical exposure depends on the GitHub/tag
  threat model, which cannot be settled from source.
- **CANON-7 (B2):** whether CentralPay `verify.php` is genuinely idempotent for
  verify-after-verify — required to fully close the crash-window recovery story — needs real
  gateway evidence.
- **Merge-gating (external config):** whether the CI jobs are set as required status checks is
  not visible in the repository.

---

## B4 disposition

**Confirmed code defects exist.** Per the review instructions ("if a confirmed code defect is
found: document it, keep B4 and the release blocked, do not fix it in this branch, recommend a
separate focused remediation PR"), the verdict is:

### `B4_FAILED_CONFIRMED_CODE_BLOCKERS`

The blocking confirmed defects are:

1. **CANON-1** (MEDIUM, financial correctness) — installer rerun can silently ship a **0% fee**
   and report success.
2. **CANON-2** (LOW, but genuine runtime-code bugs on the callback and worker trust boundaries)
   — `isdigit()`-gated `int()` crashes at `centralpay.py:85-95` and `bot.py:71-81`, violating the
   codebase's own tested "never a gateway-value-driven 500; route to manual review" invariant.
3. **CANON-3** (MEDIUM) — the update integrity control is decoupled from the deployed bytes and
   the risk register overstates it.

Importantly, **no confirmed defect can move money incorrectly at runtime**: all eighteen
financial invariants hold, and every runtime failure mode (CANON-2, CANON-6, CANON-7) fails
closed or fails safe (500/rollback, stuck-but-uncredited, or automatic manual review). The only
finding that yields an *incorrect financial value* is CANON-1, an installer/ops bug outside the
invariant-protected runtime.

**Recommended remediation (a single focused PR on a fresh branch — NOT this audit branch):**

1. **CANON-1** — persist the operator's fee percent and re-read it on the keep-existing rerun
   (or refuse to create a policy when the rate was never supplied). *Highest priority.*
2. **CANON-2** — replace both `isdigit()`-then-`int()` gates with `try/except ValueError` (or
   `isdecimal()` under `re.ASCII`) routing to the existing safe paths; add regression tests for
   `"²"`, `"--5"`, and a `\xb2` Retry-After.
3. **CANON-3 + CANON-5** — bind the deployed checkout to the verified artifact (or `git
   verify-tag`) and correct register topic 19; fix the Dockerfile version label.
4. **CANON-4 / CANON-8** — SHA-pin GitHub Actions and add a dependency lock (may be tracked as
   documented backlog).

CANON-6, CANON-7, CANON-9, and the accepted risks are safe to ship as documented backlog.

**B1, B2, B3, and B5 remain open on their existing scope. `PRODUCTION_VALIDATION_STATUS:
INCOMPLETE`. This system is NOT production-ready.**
