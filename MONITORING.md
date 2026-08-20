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
(`MONITOR_DB_INTEGRITY_INTERVAL_SECONDS`, default 30 minutes). Every query
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
| `worker_heartbeat:notification-worker` | Age of the newest heartbeat row | `app.adminbot.queries.latest_worker_heartbeat` |
| `worker_heartbeat:reconciliation-worker` | Same, only when `RECONCILIATION_ENABLED=true` | same |
| `notification_backlog` | Count of `bot_notify_pending` payments + oldest one's age (excludes `manual_review` and every resolved/accepted row) | `count_by_status`, `oldest_pending_notification_age_seconds` |
| `manual_review` | Count of genuinely **unresolved** manual reviews + oldest age + reason buckets (a row resolved via `centralpay review resolve` is never counted) | `count_open_manual_reviews`, `oldest_open_manual_review_age_seconds`, `open_manual_review_reason_buckets` |
| `reconciliation` | Reconciliation-exhausted count + backlog age approaching the hard lifetime. Ordinary `gateway_not_paid` + `reconciliation_retry_scheduled` activity is never itself an incident | `app.services.reconciliation_status.build_reconciliation_status_snapshot` |
| `backup` | Newest `*.dump` under `CENTRALPAY_BACKUP_DIR` with a validated `.ok` sidecar, and its age | plain, bounded directory listing (read-only bind mount) |
| `disk_space` | Free space (percent + absolute floor) of the filesystem backing `CENTRALPAY_BACKUP_DIR` — the one filesystem this project's single-host topology shares between PostgreSQL data, backups, and the application runtime | `shutil.disk_usage` |
| `gateway_failure_burst` | Distinct payments affected by `centralpay_getlink_failed`/`centralpay_verify_failed` in a rolling window — genuine transport/server failures, never `gateway_not_paid` | bounded `COUNT(DISTINCT payment_id)` |
| `bot_failure_burst` | Distinct payments affected by `bot_notification_failed` in a rolling window (a payment retried 6 times still counts once) | same |
| `db_integrity` | The exact same checks as `centralpay db-check` (duplicate order/reference ids, orphan events, invalid fee snapshots, sequence drift, …), read-only | `app.ops.run_db_checks` |

Every check function lives in `app/services/monitor_checks.py` and is
covered by unit tests in `tests/test_monitor_checks.py`.

### Deliberately not implemented as a separate metric

Nothing on the roadmap was skipped, but two items are folded into existing
checks rather than becoming their own line: disk space is checked once
(not once per directory) because this deployment's PostgreSQL data volume,
backup directory, and application runtime normally share one host
filesystem — checking it twice would be a duplicate, not extra coverage.
"Abnormally old eligible work" for reconciliation is the same
`oldest_due_age_seconds` value the `reconciliation` check already reports,
not a second independent check.

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
dropping it because the transition itself happened earlier.

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

**Alert payload safety:** only a fixed, small subset of a check's details
(`check`, `detail`, `count`) is ever forwarded into the Telegram-bound
`admin_alerts` payload — never the full `CheckResult.details` dict, so a
future check that puts something sensitive-shaped into its own details can
never leak it through an alert (it stays on the `MonitorIncident` row,
which is a host-CLI-only, never-Telegram surface). See
`tests/test_monitor_incidents.py::test_alert_payload_never_leaks_beyond_the_safe_allowlist`.

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
centralpay monitor incidents [--all] [--json]   # open (default) or all incidents
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
  it never re-runs a check.

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
- `CENTRALPAY_BACKUP_DIR` — shared with `scripts/backup.sh`; must match
  the host path if you change it from the default
  (`/var/backups/centralpay-bridge`), since the `monitor` and `admin-bot`
  containers bind-mount this exact path read-only.

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
- **The database itself is fully unreachable** — this is the one scenario
  Telegram alerting cannot cover: an open incident and its alert both need
  to be written to the very PostgreSQL instance that is down, so a genuine
  full outage can be detected (`database`/`public_ready` go critical in
  the monitor's own logs) but not delivered as a durable incident or a
  Telegram message until PostgreSQL is back — and once it is, the very
  next healthy cycle just closes out as "was never open," with no
  after-the-fact alert for the outage window. The monitor's OWN heartbeat
  file is deliberately NOT touched on a failed pass (see `app/monitor.py`),
  so its Docker healthcheck goes unhealthy independently of Postgres
  within `max(MONITOR_INTERVAL_SECONDS * 6, 180)` seconds — `docker
  inspect`/`centralpay status`/`centralpay monitor status` remain a valid,
  Postgres-independent liveness signal for exactly this case. Treat a
  genuine "is Postgres up at all" alert as a job for host/infrastructure-
  level monitoring outside this application, not this subsystem.
- **Two monitor containers somehow both running** — by design this is
  safe (see "Incident lifecycle and deduplication" above): at most one
  open incident and one alert per condition regardless. It is still not a
  supported topology to run intentionally; `centralpay monitor enable`
  only ever starts one.
