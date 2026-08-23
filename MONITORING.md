# Monitoring and alerting

A lightweight, opt-in monitoring subsystem (`app.monitor`) that detects
operational failures automatically and notifies administrators through the
**existing** Telegram admin bot — before customers have to report the
problem. Disabled by default (`MONITOR_ENABLED=false`).

This is deliberately not a Prometheus/Grafana stack: a modest single-host
Compose installation, bounded queries, and a single dedicated process are
enough for this project's scale.

## Architecture

```
                         +----------------+
                         |     Caddy      |
                         +-------+--------+
                                 |
                                 v
                         +-------+--------+
                         |      API       |
                         +-------+--------+
                                 |
                                 v
                             PostgreSQL
                                 ^
                                 |
           +---------------------+----------------------+
           |                     |                      |
        Worker               Admin Bot               Monitor
                                                     |
                                                     v
                                               admin_alerts (outbox)
                                                     |
                                                     v
                                               Telegram Admin
```

**Why a dedicated `monitor` service, not part of the worker or the admin
bot:**

- If the worker itself dies, something else must detect the stale
  heartbeat — a check running *inside* the worker cannot report the
  worker's own death. The monitor is a separate container.
- The admin bot already has an in-process `HealthMonitor`
  (`app/adminbot/health.py`) covering API/DB/worker-heartbeat/backup/retry-
  queue with debounced alerting — but its dedup state (`alerted: set[str]`)
  is **in-memory only** and resets on every admin-bot restart. The
  monitoring subsystem exists specifically to close that durability gap
  with a real, persisted incident table (`monitor_incidents`) — see
  "Incident lifecycle" below. `HealthMonitor` is untouched and keeps
  running; the two overlap on a few basic checks (API/DB/worker heartbeat)
  by design, which is acceptable redundancy, not a conflict.
- Alert **delivery** is not duplicated: the monitor writes to the exact
  same `admin_alerts` outbox (`app.adminbot.alerts.create_alert`) the rest
  of the application already uses, and the existing admin-bot delivery
  loop (`alert_delivery_pass`) sends it. The monitor process itself never
  imports the Telegram client and never needs `ADMIN_BOT_TOKEN`.

**Resource footprint:** one extra container running one lightweight,
synchronous Python loop. Cheap checks run every `MONITOR_INTERVAL_SECONDS`
(default 60s); the one expensive check (`db_integrity`, reusing
`centralpay db-check`'s SQL) runs on its own far slower cadence
(`MONITOR_DB_INTEGRITY_INTERVAL_SECONDS`, default 30 minutes) — tracked as
"cycles since it last actually completed", not a raw tick count, so a
transient failure during its slot retries on the very next cycle instead of
silently waiting a full cadence period before trying again. Every query
is bounded (a handful of `COUNT`/`MIN` statements against indexed columns,
or a small rolling-window `COUNT(DISTINCT payment_id)`) — no full-table
scans, no per-healthy-cycle writes (a healthy check writes nothing at
all). The loop is single-threaded and synchronous, so a slow pass cannot
cause overlapping copies of itself to pile up: the next cycle only starts
once the previous one has fully returned.

## Checks

| Check key | What it does | Reused primitive |
| --- | --- | --- |
| `public_ready` | `GET {PUBLIC_BASE_URL}/health/ready` over the real internet (bounded timeouts, no redirects followed, TLS/connection errors and a malformed/unhealthy body all fail) | — |
| `database` | `SELECT 1` | `app.adminbot.queries.database_ok` |
| `worker_heartbeat:notification-worker` | Age of the newest heartbeat row, against cutoffs that scale with the worker's own polling interval (`max(configured cutoff, poll_interval * 6)`) so a longer-than-default interval is never falsely reported stale every cycle | `app.adminbot.queries.latest_worker_heartbeat` |
| `worker_heartbeat:reconciliation-worker` | Same, only when `RECONCILIATION_ENABLED=true` | same |
| `worker_heartbeat:admin-bot-delivery` | Same, only when `ADMIN_BOT_ENABLED=true` — the admin bot's own alert-delivery loop (`app.adminbot.runner`) writes this row itself, since its container-liveness heartbeat file lives in its own tmpfs and is otherwise invisible to the dedicated monitor | same |
| `notification_backlog` | Count of `bot_notify_pending` payments + oldest one's age (excludes `manual_review` and every resolved/accepted row) | `count_by_status`, `oldest_pending_notification_age_seconds` |
| `manual_review` | Count of genuinely **unresolved** manual reviews + oldest age + reason buckets (a row resolved via `centralpay review resolve` is never counted) | `count_open_manual_reviews`, `oldest_open_manual_review_age_seconds`, `open_manual_review_reason_buckets` |
| `reconciliation` | CRITICAL on two bounded populations, never on an unbounded historical total: (1) `exhausted_not_aged_out` — still within the reconciliation lifetime, always alarms; (2) `exhausted_recent` — attempts-exhausted including already-aged-out rows, but bounded to `MONITOR_RECONCILIATION_EXHAUSTED_RECENT_WINDOW_SECONDS` (default 24h) since the payment's last attempt, so a payment reconciliation gave up on recently keeps alarming even after it ALSO ages out, but an old historical backlog whose last attempt was long ago eventually stops keeping an otherwise-healthy system critical. `exhausted_historical_total` (unbounded, all-time) is reported alongside for operator context only and never drives severity. Also warns/alarms on how long the oldest due row has waited since becoming eligible (not payment-link age, so a slow-paying customer alone never trips it). Ordinary `gateway_not_paid` + `reconciliation_retry_scheduled` activity is never itself an incident | `app.services.reconciliation_status.build_reconciliation_status_snapshot` |
| `backup` | Newest `*.dump` under `CENTRALPAY_BACKUP_DIR` with a validated `.ok` sidecar, its age, AND its `.manifest` metadata sidecar (present, well-formed, filename/size-consistent, checksum field shaped like a real sha256 — never the dump's own bytes, see below) | plain, bounded directory listing (read-only bind mount) + a small text-file parse |
| `disk_space` | Free space (percent + absolute floor) of the filesystem backing `CENTRALPAY_BACKUP_DIR` — the one filesystem this project's single-host topology shares between PostgreSQL data, backups, and the application runtime | `shutil.disk_usage` |
| `gateway_failure_burst` | Distinct payments affected, in a rolling window, by: any `centralpay_getlink_failed` (no "ordinary outcome" variant exists for it); a `centralpay_verify_failed` row with `stage == "transport"` (a real `CentralPayError`); or a `stage == "gateway"` row whose `reason` is `gateway_error_field`, `gateway_response_invalid`, or `gateway_missing_data` — CentralPay's response carrying a service-level error field, or neither a clear success nor failure marker at all, rather than an explicit per-payment rejection. Never `gateway_not_paid`, and never a `stage == "gateway"` row with `reason == "gateway_rejected"` (CentralPay explicitly answering one payment as unsuccessful — an ordinary, expected, high-frequency payer outcome, not a gateway outage) | bounded `COUNT(DISTINCT payment_id)` |
| `bot_failure_burst` | Distinct payments affected by `bot_notification_failed` in a rolling window (a payment retried 6 times still counts once) | same |
| `db_integrity` | The exact same checks as `centralpay db-check` (duplicate order/reference ids, orphan events, invalid fee snapshots, sequence drift, …), read-only | `app.ops.run_db_checks` |

Every check function lives in `app/services/monitor_checks.py` and is
covered by unit tests in `tests/test_monitor_checks.py`.

### Backup manifest validation

`scripts/backup.sh`'s `write_manifest()` writes a small `<dump>.manifest`
sidecar (plain `key=value` lines: `backup_file`, `sha256`, `size_bytes`,
`created_at`, `app_version`, `postgres_version`, `alembic_revision`,
`validation`) next to every validated backup. The `backup` check parses
that sidecar and requires it to be present, well-formed, name the SAME
dump file, and agree with the dump's actual size — plus that its `sha256`
field is *shaped* like a real digest (64 hex characters). A missing,
malformed, or inconsistent manifest is reported `critical`/
`backup_manifest_invalid` (with the specific `manifest_issue` in
`details`) regardless of the dump's age, because the recoverability
evidence for that backup is incomplete — a dump/`.ok` pair alone is no
longer treated as sufficient.

This is deliberately **metadata-only**: the monitor never reads or hashes
the dump file's own (multi-hundred-MB, growing) bytes — that would make a
periodic check itself a load problem. Byte-level checksum verification
against the recorded `sha256` remains the job of the canonical tooling
(`scripts/backup.sh`'s own `validate_archive` at creation time, and a
manual/restore-time `pg_restore --list` or checksum comparison) — never
this periodic monitor's.

### Deliberately not implemented as a separate metric

Nothing on the roadmap was skipped, but two items are folded into existing
checks rather than becoming their own line: disk space is checked once
(not once per directory) because this deployment's PostgreSQL data volume,
backup directory, and application runtime normally share one host
filesystem — checking it twice would be a duplicate, not extra coverage.
"Abnormally old eligible work" for reconciliation is the same
`oldest_overdue_seconds` value the `reconciliation` check already reports
— how long the oldest due row has been waiting since it became eligible
for an attempt (`reconciliation_next_at`), never how old its payment link
is, so an ordinary slow-paying customer never trips it — not a second
independent check.

## Severities and thresholds

Three levels: `ok` 🟢 / `warning` 🟠 / `critical` 🔴. Every threshold is a
validated `MONITOR_*` setting in `app/config.py` (`MONITOR_*_WARNING_*`
must be less than the matching `MONITOR_*_CRITICAL_*`, enforced at
startup — see `.env.example` for the full list with its defaults and
`deploy/centralpay.env.template` for the production defaults). Nothing is
hardcoded outside `Settings`.

Overall status shown by `/monitor` and `centralpay monitor check` is the
worst status among every individual check (`app.services.monitor_checks.
overall_status`).

## Incident lifecycle and deduplication

Every check result is applied to durable state in `MonitorIncident`
(`app.services.monitor_incidents.record_check_result`), never directly to
Telegram:

```
HEALTHY
  |  condition crosses a warning/critical threshold
  v
OPEN INCIDENT  --------->  ONE admin_alerts row ("monitor_incident_opened")
  |
  |  still unhealthy next cycle
  v
(row updated: last_seen_at advances) --------> NO new alert
  |
  |  condition gets WORSE (warning -> critical)
  v
ESCALATED  ------------->  ONE admin_alerts row ("monitor_incident_escalated")
  |
  |  condition recovers (any status -> ok)
  v
RESOLVED  --------------->  ONE admin_alerts row ("monitor_incident_resolved")
  |
  v
healthy stays healthy: nothing is ever created or sent
```

A de-escalation that is still unhealthy (critical → warning) updates the
incident's recorded severity silently — only a *worsening* condition is an
escalation event worth a new message.

**Catch-up delivery:** an incident can open while Telegram delivery is
unavailable (`ADMIN_BOT_ENABLED=false`, or the loser of an open-race that
ran with delivery disabled at that moment) — its row is still persisted,
just never queued for delivery (`last_alerted_at` stays `None`). The very
next cycle that observes the incident still open, with delivery now
available, queues exactly one catch-up "opened" alert at the incident's
*current* severity — this is what makes the documented `monitor enable`
then `admin-bot enable` order (or the reverse) always end with an
administrator actually hearing about anything still open, never silently
dropping it because the transition itself happened earlier. The same
applies if an already-alerted incident ESCALATES further while delivery is
unavailable: the escalation branch resets `last_alerted_at` back to `None`
(the earlier alert was for a since-superseded, lower severity), so this
same catch-up path fires once delivery resumes — an administrator is never
left believing an incident is still at whatever severity it was last
actually told about.

The same catch-up path also fires if an alert WAS successfully queued but
Telegram delivery then permanently failed (every retry up to
`ADMIN_BOT_ALERT_MAX_ATTEMPTS` exhausted, the outbox row settling as
`failed`) — `MonitorIncident.last_alert_id` records which `admin_alerts`
row `last_alerted_at` refers to, so the next unhealthy cycle can tell
"queued but never delivered" apart from "queued and pending/delivered" and
re-queue a fresh alert instead of treating the incident as already handled
forever.

`ADMIN_BOT_*` settings (including `ADMIN_BOT_ENABLED` and the per-category
alert toggles) are read once at container start, same as every
`MONITOR_*` setting. To avoid requiring a separate manual restart every
time, `centralpay admin-bot enable`/`centralpay admin-bot disable`
automatically restart an already-running `monitor` container so it picks
up the change immediately — a no-op if the monitor isn't currently
running.

**Durability and concurrency**, precisely:

- `MonitorIncident` rows survive a container restart — dedup state is
  never held only in memory (unlike the admin bot's `HealthMonitor`).
- At most one **open** row can exist per `check_key`, enforced by a
  PostgreSQL **partial unique index**
  (`uq_monitor_incidents_open_check_key`, `WHERE status = 'open'`) —
  not application logic alone. Opening a new incident is an optimistic
  `INSERT`; a concurrent `INSERT` for the same still-open condition hits
  the constraint and gets an `IntegrityError`, which is caught and treated
  as "a racing monitor instance already opened it," falling back to
  updating that row — never a duplicate row or a duplicate alert. This is
  the same idiom `app.services.payments` already uses for `bot_order_id`
  races, and it is proven under real concurrent threads/transactions in
  `tests/integration/test_monitor_incidents_pg.py` (`pytest -m postgres`).
- A resolved incident's row is **kept** (history, visible via
  `centralpay monitor incidents --all`) — a check that flaps
  open → resolved → open again gets a **new**, independent row for the
  new episode; the resolved one is never resurrected or reused.
- If the admin bot is disabled entirely (`ADMIN_BOT_ENABLED=false`), the
  incident lifecycle still runs and stays fully accurate — it simply never
  queues an `admin_alerts` row (which would otherwise pile up
  permanently undelivered).
- Each check key maps to one of the admin bot's per-category alert toggles
  — `ADMIN_BOT_HEALTH_ALERTS` (`public_ready`, `database`,
  `worker_heartbeat:*`, `reconciliation`, `disk_space`, `db_integrity`),
  `ADMIN_BOT_BACKUP_ALERTS` (`backup`), `ADMIN_BOT_MANUAL_REVIEW_ALERTS`
  (`manual_review`), `ADMIN_BOT_ERROR_ALERTS` (`notification_backlog`,
  `gateway_failure_burst`, `bot_failure_burst`) — the same categories
  `app.adminbot.alerts` already applies to non-monitor events. Turning one
  category off never affects another, and the incident itself is still
  always persisted regardless.

**Alert payload safety:** only a fixed, small subset of a check's details
(`check`, `detail`, `count`) is ever forwarded into the Telegram-bound
`admin_alerts` payload — never the full `CheckResult.details` dict, so a
future check that puts something sensitive-shaped into its own details can
never leak it through an alert (it stays on the `MonitorIncident` row,
which is a host-CLI-only, never-Telegram surface). See
`tests/test_monitor_incidents.py::test_alert_payload_never_leaks_beyond_the_safe_allowlist`.

## Behavior during a database outage

`run_all_checks` explicitly classifies every check as DB-independent
(`public_ready`, `backup`, `disk_space` — they never touch the `db`
session) or DB-dependent (everything else: `database`,
`worker_heartbeat:*`, `notification_backlog`, `manual_review`,
`reconciliation`, `gateway_failure_burst`, `bot_failure_burst`,
`db_integrity`). The DB-independent checks always run, even when
PostgreSQL itself is unreachable. If the initial `database` probe (`SELECT
1`) already fails, or a later DB-dependent check's own query raises mid-
pass (the connection dying partway through, not only at the start), no
further SQL is attempted against that session for the rest of the pass —
every DB-dependent check that had to be skipped gets a
`critical`/`database_unavailable` result (`details.dependency ==
"database"`) instead of being silently reported healthy or making the
whole pass crash.

This means `centralpay monitor check` stays fully usable DURING an
outage, PROVIDED the `monitor` container is already running — it shows
`public_ready`, `backup`, and `disk_space` at their real status and every
DB-dependent check as `database_unavailable`, rather than raising an
unhandled exception. app.monitor's own background loop already tolerated
a raised pass (it logs `monitor_pass_failed` and retries next cycle) —
this makes that tolerance produce a structured, useful snapshot instead
of nothing, for the CLI's on-demand, human-triggered surface that doesn't
go through the incident-recording pipeline at all.

**The "already running" qualifier matters**: `centralpay monitor check`
runs `docker compose exec monitor ...` (`scripts/centralpay`), which
requires an existing, running `monitor` container — it never starts a new
one. `docker-compose.yml`'s `monitor` service declares `depends_on: db:
condition: service_healthy`, so Compose refuses to (re)start it while
PostgreSQL is unhealthy. If the monitor container was already up when the
outage began, it keeps running and `monitor check` keeps working exactly
as described above. If it needs to (re)start DURING the outage — a host
reboot, or the container being recreated for an unrelated reason — it
cannot come up until PostgreSQL recovers, and `monitor check` is
unavailable for that window too. In that specific case, fall back to the
same signals documented for a fully unreachable database below: the
monitor's own Docker healthcheck, host-level container monitoring, or an
external uptime probe.

**The admin bot's `/monitor` is NOT covered by this**, even though it
calls the same `run_all_checks`: `CommandHandlers.handle`
(`app/adminbot/commands.py`) unconditionally records and commits an
`admin_command_received` audit event *before* invoking any command
handler, on every command including `/monitor` — that commit itself
requires a working database and raises first, so `/monitor` never even
reaches `run_all_checks` during a full outage. Making every admin-bot
command's audit trail best-effort during a database outage is a
separate, broader change to the command-dispatch path (touching every
command, not just this one diagnostic), out of scope here — during a
genuine full PostgreSQL outage, use the host CLI's `centralpay monitor
check` instead.

**What this does NOT fix:** app.monitor's own loop still cannot durably
record a "PostgreSQL is down" incident or Telegram alert, because
persisting either one requires writing to the very PostgreSQL instance
that is unreachable — see "The database itself is fully unreachable" in
the runbook below. Graceful degradation and durable persistence are two
different problems; this subsystem solves the first and documents the
second as an accepted, external-monitoring limitation.

## `/monitor` (admin bot) and CLI

**Admin bot** (Telegram, read-only, same numeric-ID authorization as every
other command):

```
/monitor
```

Runs the full check set (including `db_integrity` — acceptable on this
command since it is human-triggered and infrequent, the same reasoning
`centralpay db-check` itself already relies on) and renders a concise
Persian summary with per-check status and an overall line. It can only
**read** monitoring state; it can never acknowledge, resolve, or silence
an incident — that stays a host-CLI-only operation.

**Host CLI** (`scripts/centralpay`):

```
centralpay monitor check [--json]        # run every check now
centralpay monitor incidents [--all] [--limit N] [--json]   # open (default) or all incidents
```

Each subcommand is routed to whichever container actually satisfies its
dependencies, never blindly to `monitor`:

- `monitor check` requires `MONITOR_ENABLED=true` and runs inside the
  `monitor` container specifically — it's the only one with the read-only
  backup-directory bind mount the backup/disk checks need (the same
  reasoning `reconciliation status`/`reconcile` are routed to `worker`,
  never `api`). Exits `0` when every check is `ok`, `1` otherwise —
  suitable for a cron/alerting wrapper on top of the built-in Telegram
  delivery.
- `monitor incidents` is a pure database read with no filesystem
  dependency, so it runs inside the always-on `api` container instead —
  never gated on `MONITOR_ENABLED` and never requiring the `monitor`
  container to be running. It only reads persisted `MonitorIncident` rows;
  it never re-runs a check. `--limit` (default 50) caps how many rows are
  returned, most recently opened first; raise it (or pass `--all --limit`
  with a larger value) to see further back than the default page.

## Configuration

All settings are validated `MONITOR_*` fields on `app.config.Settings`
(see `.env.example` for the full, commented list of names and defaults).
Nothing here has a floating hardcoded default outside `Settings`.

- `MONITOR_ENABLED` (default `false`) — the master switch. The compose
  service is additionally gated behind a Compose profile
  (`--profile monitor`, added automatically by `scripts/centralpay` when
  this is `true`), so a fresh install never runs the container until an
  operator opts in.
- `MONITOR_INTERVAL_SECONDS` (default 60) — cheap-check cadence.
- `MONITOR_DB_INTEGRITY_INTERVAL_SECONDS` (default 1800 = 30 min) —
  expensive-check cadence, deliberately far slower.
- Every `MONITOR_*_WARNING_*` / `MONITOR_*_CRITICAL_*` pair — see the
  table above and `.env.example`.
- `CENTRALPAY_BACKUP_DIR` — shared with `scripts/backup.sh`; set it inside
  `centralpay.env` (see `deploy/centralpay.env.template`) if you change it
  from the default (`/var/backups/centralpay-bridge`). `scripts/centralpay`
  reads it from `centralpay.env` and exports it into its own process
  environment before invoking Docker Compose, because Compose's
  `${CENTRALPAY_BACKUP_DIR:-...}` bind-mount interpolation in
  `docker-compose.yml` reads the invoking shell's environment, never
  `env_file:` — without that export the `monitor`/`admin-bot` containers'
  read-only bind mount would silently fall back to the default host path
  regardless of what `centralpay.env` says.

## Disabling monitoring safely

`MONITOR_ENABLED=false` (the default) is sufficient on its own: the
`monitor` service never starts (Compose profile gating), no check ever
runs, and no `MonitorIncident`/`admin_alerts` row is ever created by it.
Existing incident history, if any, is left untouched in the database (it
is never deleted) and remains queryable with
`centralpay monitor incidents --all` even while disabled.

Operationally:

```bash
centralpay monitor disable   # stops the container; MONITOR_ENABLED=false
centralpay monitor enable    # starts it; MONITOR_ENABLED=true
centralpay monitor status    # container + configuration state
centralpay monitor logs      # follow logs
```

`monitor disable` (and `admin-bot disable`) verify the container actually
stopped before persisting `*_ENABLED=false` — if `docker compose stop`
fails (a Docker daemon hiccup, a timeout, ...) while the container is
still genuinely running, the command fails loudly and the configuration
is left unchanged, rather than reporting success while a restart-enabled
container keeps polling and creating incidents/alerts in the background.

Disabling monitoring **never** affects payment processing, the
notification worker, or reconciliation — the monitor process has no code
path that writes to `payments`/`payment_events`, and every check it runs
is read-only against those tables (verified on real PostgreSQL by
`tests/integration/test_monitor_incidents_pg.py::
test_monitoring_reads_never_mutate_financial_state`).

## Operator runbook / troubleshooting

- **`/monitor` or `centralpay monitor check` shows a check as
  `critical`/`warning` that looks wrong** — read the `reason` and
  `details` fields; every check documents its exact condition above. For
  `db_integrity`, cross-check with `centralpay db-check --details`.
- **An incident won't clear even though the underlying problem is fixed**
  — the next check cycle (`MONITOR_INTERVAL_SECONDS`, default every
  60s) re-evaluates every condition; `db_integrity` only re-evaluates on
  its own slower cadence (default 30 min) unless you run
  `centralpay monitor check` by hand, which always evaluates it fresh.
- **No Telegram alerts arrive at all** — check `centralpay admin-bot
  status` (delivery requires the admin bot enabled too) and
  `centralpay monitor incidents` (proves whether incidents are even being
  detected, independent of delivery).
- **`backup`/`disk_space` always show as unreadable/critical** — two
  independent causes:
  1. The `monitor` (and `admin-bot`, for `/monitor`) container's read-only
     bind mount is missing or points at the wrong HOST directory — confirm
     with `centralpay monitor logs` that `CENTRALPAY_BACKUP_DIR` in the env
     file matches the actual host backup path (the container always looks
     at the fixed in-container path `/var/backups/centralpay-bridge`,
     regardless of that variable — only the bind mount's host side moves).
  2. **Permissions on an installation from before this feature existed**:
     `install.sh` now creates the backup directory `0750`, group-owned by
     the fixed GID `10001` baked into the application image, so the
     monitor/admin-bot containers (which run as that same non-root user)
     can list backup filenames/timestamps — never read a backup's
     contents, which stay `0600` root-owned per file. An installation from
     before this change keeps its old `0700` mode until fixed; on such a
     host, run once as root:
     `chgrp 10001 /var/backups/centralpay-bridge && chmod 0750 /var/backups/centralpay-bridge`
     (adjust the path if `CENTRALPAY_BACKUP_DIR` was customized).
  3. **`backup_manifest_invalid` on a real backup**: `scripts/backup.sh`'s
     `write_manifest()` makes each `.manifest` sidecar (non-secret
     metadata — never the dump itself) group-readable by GID `10001` so
     the monitor can parse it; a manifest written before this change
     stays `0600` root-owned until the next backup rotation overwrites it
     (daily by default). To fix an existing one without waiting, run once
     as root: `chgrp 10001 /var/backups/centralpay-bridge/*.manifest &&
     chmod 640 /var/backups/centralpay-bridge/*.manifest`.
- **The database itself is fully unreachable** — `run_all_checks` itself
  degrades gracefully (see "Behavior during a database outage" above):
  `centralpay monitor check` keeps working, showing
  `public_ready`/`backup`/`disk_space` at their real status and every
  DB-dependent check as `database_unavailable`. The admin bot's
  `/monitor` does NOT share this — its own mandatory command-audit write
  fails first (see "Behavior during a database outage" above), so use
  the host CLI during a genuine full outage. What remains the one
  scenario Telegram alerting genuinely cannot cover is DURABLE,
  PROACTIVE notification: an open incident and its alert both need to be
  written to the very PostgreSQL instance that is down, so app.monitor's
  own background loop detects the outage on every pass but cannot
  persist it as a durable incident or a Telegram message until
  PostgreSQL is back — and once it is, the very next healthy cycle just
  closes out as "was never open," with no after-the-fact alert for the
  outage window. The monitor's OWN heartbeat file is deliberately NOT
  touched on a failed pass (see `app/monitor.py`), so its Docker
  healthcheck goes unhealthy independently of Postgres within
  `max(MONITOR_INTERVAL_SECONDS * 6, 180)` seconds. During exactly this
  window, the signals that DO remain available are: `docker
  inspect`/`centralpay status`/`centralpay monitor status` (the
  monitor's own Postgres-independent container health), any host-level
  process/service monitoring already watching the `db` container, and
  external uptime/infrastructure monitoring (e.g. Uptime Kuma,
  Prometheus/Alertmanager, or a host watchdog) pointed at this
  deployment from outside it — none of which this application-level
  subsystem provides itself. Treat a genuine "is Postgres up at all"
  alert as a job for that external layer, not this subsystem; wiring one
  up is a deliberately separate, future piece of work, not part of this
  monitor.
- **Two monitor containers somehow both running** — by design this is
  safe (see "Incident lifecycle and deduplication" above): at most one
  open incident and one alert per condition regardless. It is still not a
  supported topology to run intentionally; `centralpay monitor enable`
  only ever starts one.
