# Admin Bot & Ops Visibility — architecture audit

Audit performed before implementing the "Admin Bot & Ops Visibility" roadmap
item. Read directly from source (`app/adminbot/*`, `app/services/*`,
`app/cli.py`, `app/ops.py`) plus the existing Graphify graph for call-path
confirmation. Conclusion up front: **the admin bot already implements almost
every target capability in the roadmap item.** This document records what
already exists, the few genuine gaps found, and the security/architecture
decisions made before writing any code.

## 1. Existing admin bot inventory

| File | Role |
|---|---|
| `app/adminbot/runner.py` | `AdminBotService` — long-polling runtime, one `MessageHandler(filters.TEXT, ...)`. **No `CallbackQueryHandler` exists today** — see §4. |
| `app/adminbot/auth.py` | `UpdateContext`, `is_authorized()`, `log_unauthorized()` — numeric Telegram ID allow-list, private-chat-only, generic denial. |
| `app/adminbot/commands.py` | `CommandHandlers` — dispatch table (`_registry`) and all 13 command bodies (`cmd_start` … `cmd_fee`). |
| `app/adminbot/queries.py` | Telegram-agnostic read-only SQL helpers over `app.models` (explicitly documented as reusable — `app.services.stuck_payments` already imports it). |
| `app/adminbot/format.py` | Persian/HTML rendering, escaping, Jalali dates, `fmt_duration_fa`/`fmt_duration_ago_fa` (human-readable ages already exist). |
| `app/adminbot/alerts.py` | Alert outbox: `on_audit_event` → `_map_event` (creation), claim/deliver/record (delivery), SKIP LOCKED + claim-ownership discard guard mirroring the notification worker. |
| `app/adminbot/health.py` | `run_health_checks` + `HealthMonitor` — consecutive-failure/-recovery thresholded `service_unhealthy`/`service_recovered` alerts (api, db, worker heartbeat, retry-queue stall, backup age, alert-queue stall). |
| `app/adminbot/reports.py` | Daily report, queued through the same alert outbox, dedup by date. |
| `app/adminbot/reply_delivery.py` | Chunk-retry transport for command replies (budgeted retries, no business logic). |
| `app/services/stuck_payments.py` | **Single source of truth** for `NEEDS_ATTENTION` / `WAITING_GATEWAY` / `EXPIRED` classification — shared verbatim by `app/cli.py`'s `centralpay stuck` and the admin bot's `/stuck`, `/waiting`, `/expired`. |
| `app/services/bulk_resend.py` | The only existing *mutating* admin-bot command's backing service (`/resend_failed`) — idempotent-mode-gated, requeues for delivery only, never a claim of credit. |

### Commands already implemented (all read-only except `/resend_failed`)

`/start /help /status /health /recent /stuck /waiting /expired /manual_review
/resolved_reviews /errors /payment /retry_queue /resend_failed
/backup_status /version /fee`

Every command goes through `CommandHandlers.handle()`, which:
1. Checks `is_authorized()` first; on failure logs + records
   `admin_bot_unauthorized_access` and returns a **generic** denial
   (`GENERIC_DENIAL`) — no information about which command exists, why it
   was denied, or system state.
2. Records `admin_command_received` / `_succeeded` / `_failed` audit events
   around every invocation (already satisfies rule D: "structured audit/log
   events for admin reads").
3. Runs the handler inside its own `with self._session_factory() as db:`
   block — a fresh, short-lived session per command, closed immediately
   after. No handler holds a session open across a network call.

## 2. What's already CLI-only (confirmed, must stay that way)

`app/ops.py` (`centralpay fee set/schedule/cancel`, `review
acknowledge/resolve/resend`, `notification accept`, `backup-event`,
`test-alert`, `db-check`, `privacy-audit`) and `app/cli.py`'s mutating
paths (`reconcile`, `recover-aged-out --confirm`) are the **only** places
that mutate payment/fee/review state outside the notification/reconciliation
workers themselves. None of these have any Telegram-side counterpart today,
and per the task's explicit instruction this PR adds none:

- `centralpay notification accept ORDER_ID --note ... --yes` (PR #64) stays
  CLI-only — no audited mutation framework for it exists in the admin bot,
  so the default (do not add) applies.
- `centralpay review resolve/resend/acknowledge` stay CLI-only. The admin
  bot's `/manual_review` and `/resolved_reviews` are display-only views of
  the same rows; the "resolved" label used by `/resolved_reviews` is
  produced entirely by the CLI-side mutation and just displayed here.
- `centralpay fee set/schedule/cancel` stay CLI-only; `/fee` is
  read-only, and its help text already says so explicitly.
- `/resend_failed` (the one existing bot-side mutation) is audited here as
  requested, not extended: it only ever moves `bot_notify_pending` rows
  back into the worker's own retry queue (never touches the customer bot),
  is hard-gated on `bot_notify_retry_mode == "idempotent"`, and its
  `confirm` step requires the caller to have already seen the preview. No
  changes made to it in this PR.

## 3. Genuine gaps found (the only things this PR changes)

Everything else the roadmap item asks for was already present verbatim —
`/stuck`, `/waiting`, `/expired` already share `app.services.stuck_payments`
with the CLI; `/manual_review` + `/resolved_reviews` already show
reason/age/order/amount/gateway state; `/payment` already shows almost the
full field list. Four precise, narrow gaps were found by diffing the
existing output against the roadmap item's field lists line by line:

1. **`/status` (the "system overview" command) doesn't expose
   `needs_attention` / `waiting_gateway` / `expired` counts or a
   human-readable backup age**, even though `/stuck` computes the first
   three from functions already imported into `commands.py`
   (`stuck_service.count_waiting`, `count_expired`, `count_other_attention`,
   plus `queries.bot_delivery_snapshot(...).total` for the bot-delivery
   half of `needs_attention` — the exact same primitives `/stuck`'s own
   header already calls) and `format.fmt_duration_ago_fa` already exists
   for the age string. Also missing: the git short SHA (already shown by
   `/version` via `self._settings.git_commit_sha`, trivial to reuse here).
2. **`/payment` (`app.cli._payment_summary`'s own field list is the
   roadmap item's field list, verbatim)** is missing exactly two of the
   seventeen fields it asks for: `gateway_verified_at` (the raw timestamp —
   only the derived OK/PENDING boolean is shown today) and
   `bot_last_error_code` (not shown anywhere in the admin bot, though it's
   on `Payment` and already displayed by `app.cli._payment_summary`).
3. **`/retry_queue` doesn't show `bot_notify_attempts`, `bot_last_http_status`,
   or `bot_last_error_code` per entry** — only order id, reason/queued
   label, and next-retry time. The roadmap item's notification-visibility
   list asks for attempt count and last HTTP status/error code explicitly.
4. **Reconciliation exhaustion never raises an admin alert.**
   `app/services/reconciliation.py` already records a dedicated
   `reconciliation_exhausted` event (`level="error"`, fired exactly once
   per payment when `attempt >= reconciliation_max_attempts`, never for
   ordinary `gateway_not_paid` retries), but `alerts.py::_map_event` has no
   branch for it — every other error-class event
   (`centralpay_getlink_failed`, `centralpay_verify_failed`) already maps
   to an alert; this one silently doesn't. This is exactly the "reconciliation
   needs_attention" alert the roadmap item asks for, and is naturally
   non-noisy: it fires once per payment, never on routine
   `gateway_not_paid` polling (that path is `reconciliation_retry_scheduled`
   / `reconciliation_gateway_not_paid`, deliberately left unmapped).
5. **Payment lookup ambiguity is not uniformly safe.** `app.cli._find_payment`
   (used by `payment`, `reconcile`, `recover-aged-out`) refuses — raising
   `AmbiguousOrderIdError` — when a numeric identifier matches one
   payment's `bot_order_id` and a *different* payment's `gateway_order_id`,
   rather than silently picking one. `app.adminbot.queries.find_payment`
   has no equivalent protection: it tries `bot_order_id` first and returns
   immediately on a hit, without ever checking whether the same numeric
   string ambiguously also names a different payment's `gateway_order_id`.
   An operator could be shown the wrong payment's details with no
   indication anything was ambiguous. Per implementation rule A ("if
   existing logic is CLI-bound, extract a small pure/read-only service
   layer and make both CLI and admin bot use it"), the lookup is extracted
   into `app.services.payment_lookup` and reused by both — see §5.

## 4. Security-boundary decision: no inline-keyboard/callback layer added

The roadmap item's suggested top-level layout (`Status / Payments / Manual
Review / Stuck / Reconciliation / Notifications / Recent Alerts` as
buttons) explicitly allows deviating "if existing bot architecture has a
better established pattern." It does: **the bot is a flat, typed-command
interface today (like a CLI over Telegram) with zero inline-keyboard or
`CallbackQueryHandler` infrastructure anywhere in `runner.py`.** Every
capability the roadmap item lists is already reachable by one of the
existing thirteen commands (plus the four content gaps closed below).

Introducing buttons would mean adding an entirely new, currently
nonexistent attack surface — a callback-data encode/decode/validation
layer, a new `CallbackQueryHandler` dispatch path parallel to
`on_message`, and a second way to reach every command — for zero net new
capability, in a PR whose explicit brief is "primarily READ-ONLY
observability" and "do NOT introduce unsafe mutation commands." That
tradeoff is the wrong one for this PR. **Decision: no callback/button
layer is added.** The existing text-command surface is extended in place;
this keeps the diff small, keeps every new field reachable through
already-authorized, already-audited command handlers, and adds no new
input-validation surface. If the user later wants inline keyboards as a
UX layer, that is a separable follow-up with its own review, not bundled
into a visibility PR.

## 5. Shared services reused / extracted

- **Reused, unchanged:** `app.services.stuck_payments` (`count_waiting`,
  `count_expired`, `count_other_attention`,
  `_bot_delivery_manual_review_conditions` via
  `queries.bot_delivery_snapshot`) for every stuck/reconciliation number;
  `app.adminbot.format.fmt_duration_ago_fa` for the backup-age string;
  `Settings.git_commit_sha` (already read by `/version`) for the overview's
  commit line.
- **Extracted:** `app.cli._find_payment` / `AmbiguousOrderIdError` /
  `_POSTGRES_BIGINT_MAX` move to a new, framework-agnostic
  `app/services/payment_lookup.py` (`find_payment_by_order_id`,
  `AmbiguousOrderIdError`). `app.cli._find_payment` becomes a one-line
  wrapper calling the shared function — its name, its call sites, and the
  four existing test files that monkeypatch `cli_module._find_payment` for
  race-injection (`test_cli_reconcile.py`, `test_aged_out_recovery.py`,
  `test_aged_out_recovery_pg.py`, `test_reconcile_inspect_pg.py`) are
  **all unaffected**, since monkeypatching a module attribute still
  intercepts every bare-name call site inside `app.cli`, and `app.ops`'s
  `from app.cli import AmbiguousOrderIdError, _find_payment` still resolves
  to the same names. `app.adminbot.queries.find_payment` now calls the
  same shared function first (gaining the ambiguity guard), then falls
  back to its own existing (CLI does not have this) unambiguous
  `reference_id` match — so the CLI's behavior is unchanged byte-for-byte,
  and the admin bot gains safety without losing its existing convenience
  lookup.

## 6. What stays exactly as-is (audited, not touched)

- `/resend_failed`'s idempotent-mode gate and preview/confirm flow —
  audited above, no change.
- The notification worker, reconciliation worker, alert delivery
  claim/discard-guard logic, and every financial field/status-transition —
  untouched. This PR only adds read paths and one alert-creation branch
  (which itself only inserts a row into the pre-existing `admin_alerts`
  outbox from inside the reconciliation worker's *own already-open*
  transaction via the pre-existing `on_audit_event` hook — the same
  mechanism every other alert type already uses).
- No new database tables, columns, or migrations — every field surfaced
  already exists on `Payment` / `PaymentEvent` / `AdminAlert`.
