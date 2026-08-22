# CentralPay Bridge Documentation Map

This file defines which repository documents are **living/current**, which are **historical evidence**, and which are **release/validation snapshots**.

The distinction matters: CentralPay Bridge has accumulated audits and review reports over many commits. Historical reports are intentionally preserved exactly as evidence of what was found at that time; they are not automatically rewritten after every later fix.

## Reading order

For the current implementation, start here:

1. [README.md](README.md) — current project overview
2. [AGENTS.md](AGENTS.md) — authoritative engineering/financial contract
3. [SECURITY.md](SECURITY.md) — current security policy/posture
4. [OPERATIONS_FA.md](OPERATIONS_FA.md) — current operator runbook
5. [PRODUCTION_CHECKLIST_FA.md](PRODUCTION_CHECKLIST_FA.md) — current production checklist
6. [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) — current migration chain/procedure
7. [MONITORING.md](MONITORING.md) — current monitoring/alerting architecture and operator runbook

When a historical audit conflicts with current source or one of the living documents above, **current source + current tests + living contract win**. The old audit remains valuable as historical evidence.

## Living / authoritative documentation

These files are expected to stay synchronized with current code:

| File | Role |
| --- | --- |
| `README.md` | Current English project overview, architecture, operations, release/update model |
| `README_FA.md` | Current Persian project overview |
| `AGENTS.md` | Authoritative engineering contract and non-negotiable financial/security invariants |
| `SECURITY.md` | Current security policy, trust boundaries, accepted architectural limitations |
| `INSTALL_FA.md` | Current Persian installation guide |
| `OPERATIONS_FA.md` | Current Persian operations/runbook |
| `BACKUP_RESTORE_FA.md` | Current backup/restore runbook |
| `ADMIN_BOT_FA.md` | Current administrator Telegram-bot behavior and command contract |
| `PRODUCTION_CHECKLIST_FA.md` | Current production/release operational checklist |
| `MIGRATION_GUIDE.md` | Current Alembic migration chain and upgrade/rollback guidance |
| `MONITORING.md` | Current monitoring/alerting architecture, check/threshold reference, incident lifecycle, and operator runbook |
| `CENTRALPAY_CONTRACT_ASSUMPTIONS.md` | Current external CentralPay contract/risk assumptions and fail-closed behavior |
| `CHANGELOG.md` | Version/change history; append/update as release work lands |

## Architecture / design references

These are useful technical references. They may include implementation-history narrative, so verify details against current source when making a new change.

| File | Role |
| --- | --- |
| `RATE_LIMITING_ARCHITECTURE.md` | Detailed rate-limit/client-IP architecture, including follow-up safe-replay fixes |
| `RELEASE_RISK_REGISTER.md` | Cumulative RC risk/triage history. Individual entries reflect the state when they were written; do not use old blocker wording alone as the current readiness decision. |

## Historical audit snapshots

These are retained deliberately and should normally **not** be rewritten to pretend they were produced against newer code.

| File | Snapshot purpose |
| --- | --- |
| `FINAL_FINANCIAL_AUDIT.md` | Financial-correctness audit at its original commit |
| `FINANCIAL_INVARIANTS.md` | Detailed invariant audit/test snapshot at its original commit |
| `FINANCIAL_TEST_MATRIX.md` | Financial test-coverage matrix snapshot |
| `FINANCIAL_CRASH_MATRIX.md` | Crash-window/recovery audit snapshot |
| `ZERO_BASE_AUDIT_0.6.0_RC1.md` | Zero-base audit for the 0.6.0-rc1 line at the audited commit |
| `ADVERSARIAL_REVIEW_0.6.0_RC1.md` | Original adversarial review; intentionally retains the findings that existed then |
| `ADVERSARIAL_REVIEW_B4_RECHECK_c68e86e4.md` | Follow-up recheck proving earlier B4 findings against a later commit |
| `SECURITY_HARDENING_AUDIT.md` | Security-hardening audit snapshot |
| `ADMIN_BOT_OPS_VISIBILITY_AUDIT.md` | Admin-bot operational-visibility audit snapshot |
| `DEFERRED_REVIEW.md` | Original deferred-review backlog/history; later triage lives in the risk register and source history |

A statement such as “review not yet completed” inside one of these files means “not completed **at that snapshot**.” It is not automatically a statement about current `main`.

## Validation evidence

These files record or define validation work for a particular release/host/integration. Keep them as evidence and update only when performing the corresponding validation again.

| File | Role |
| --- | --- |
| `REAL_HOST_VALIDATION.md` | Installer/real-host validation evidence |
| `STAGING_VALIDATION.md` | Real/staging CentralPay contract validation evidence and cautions |
| `ADMIN_BOT_VALIDATION.md` | Live Telegram admin-bot validation evidence |

## Release snapshots

| File | Role |
| --- | --- |
| `RELEASE_NOTES_0.5.0_RC1.md` | Historical release notes for 0.5.0-rc1 |
| `RELEASE_NOTES_0.6.0_RC1.md` | Release notes for the 0.6.0-rc1 line; update when preparing a new RC if still applicable |

Release notes describe a release line, not every later `main` commit.

## Incident/postmortem documentation

| File | Role |
| --- | --- |
| `docs/incidents/2026-07-centralpay-cross-user-card-suggestions.md` | Postmortem for the payer-identity/card-suggestion incident and its remediation history |

Incident reports are immutable historical evidence unless an explicit correction/addendum is needed.

## Legacy comprehensive guide

| File | Role |
| --- | --- |
| `docs/راهنمای_جامع_کاربری_CentralPay_Bridge_FA.md` | Large legacy Persian handbook. Retained for reference, but **not authoritative** for current behavior; use the smaller living runbooks above first. |

A DOCX counterpart of the legacy handbook also exists under `docs/`. It is not the engineering source of truth.

## Removed obsolete artifacts

The documentation cleanup intentionally removes these tracked Markdown artifacts:

| Removed file | Reason |
| --- | --- |
| `CODEX_PHASE1_PROMPT.md` | One-time AI build prompt for the original implementation phase; no longer a project contract, runbook, test, or audit record |
| `docs/previews/payment-success-zedproxy-color-pop-review.md` | One-time UI/design review note; the tracked preview HTML/screenshots are sufficient if visual history is needed |

Their Git history remains available permanently, so deleting them from the current tree does not erase provenance.

## Documentation maintenance rules

1. **Do not rewrite historical audits to make them look current.** Add a later audit/recheck instead.
2. **Living docs must follow source.** If a PR changes public API, financial semantics, admin commands, migrations, deployment/update policy, backup/restore, or security boundaries, update the relevant living docs in the same PR.
3. **Applied migrations are source of truth for schema history.** A stale audit saying migration `0004` was current must not override the actual Alembic chain.
4. **CLI/help and admin-bot registries are source of truth for commands.** Documentation lists should be checked against code.
5. **Financial/concurrency claims require tests.** Graph/navigation output is useful for discovery, but PostgreSQL behavior and source are authoritative.
6. **No generated local tooling artifacts.** `.claude/`, `CLAUDE.md`, Graphify output/cache, or similar local tool state are not project documentation unless explicitly adopted by a separate decision.
7. **No secrets or production identifiers in docs.** Examples must remain synthetic.

## Current known documentation-sensitive areas

At the time this map was added, important current facts include:

- application version `0.6.0-rc1`
- Alembic head `0012`
- production update refs fail closed unless they are release tags, unless the explicit development opt-in is enabled
- admin bot is no longer accurately described as blanket read-only because `/resend_failed confirm` is a narrowly gated mutation
- the admin bot's own lightweight built-in health checks (`app/adminbot/health.py`) still keep consecutive-failure/recovery counters in process memory (a restart resets them) — that specific limitation is unchanged and not overclaimed as solved; separately, an optional, dedicated monitoring process (`app.monitor`, `MONITOR_ENABLED=false` by default) now exists with its own, more thorough check set, and its incident state lives in the durable `monitor_incidents` table (migration `0011`/`0012`) and survives a restart — do not describe *all* monitoring/incident state as process-memory-only, and do not describe persistent incident lifecycle as nonexistent
- Caddy access logging must redact callback `ct`/`sig` from both URI and `Referer`

If source changes any of those facts, update the living docs and this section in the same change.
