# Real-host installer validation — 0.6.0-rc1

**Status: NOT PERFORMED — RELEASE BLOCKER (B1).**

The development environment for this release candidate has no access to
a real or virtual Ubuntu host (no VM, no secondary container with
systemd/Docker, no root SSH target). A genuine end-to-end run of

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

has therefore **never been executed**. Per the release instructions,
this is recorded honestly as a release blocker rather than claimed as
complete: **0.6.0-rc1 must not be tagged until this validation has been
performed on a real host and the results are recorded below.**

## What HAS been validated (and how)

| Check | Method | Status |
|---|---|---|
| `install.sh` syntax + ShellCheck | `bash -n`, `shellcheck` (locally and in CI) | tested (real) |
| OS/arch gating, input validation, secret generation, idempotent re-run logic | unit tests (`tests/test_deployment.py`) driving installer functions | tested (mocked host) |
| `docker-compose.yml` validity (default and `admin-bot` profile) | `docker compose config` locally and in CI | tested (real, config only) |
| Docker image build (amd64, arm64) | delegated to CI (`docker` jobs) — Docker Hub is unreachable from the dev sandbox | tested in CI only |
| Caddy TLS issuance on a real domain | — | **not tested** |
| systemd units (backup timer) on a real host | unit-file content assertions only | **not tested (real)** |
| UFW rules on a real host | script logic tests only | **not tested (real)** |
| End-to-end `curl \| sudo bash` install | — | **not tested** |
| `centralpay update` / `rollback` against a real GitHub release | logic + checksum-verification tests with fixtures | **not tested (real)** |

## Supported vs. validated OS matrix

| Ubuntu | Installer accepts | CI test matrix | Real-host evidence recorded |
|---|---|---|---|
| 22.04 | yes | yes | **none** |
| 24.04 | yes | yes | **none** |
| 26.04 | yes | no | **none** |

The operator has reported using an Ubuntu 26.04 VPS, but no sanitized
evidence (OS/kernel/Docker versions, dates, logs) has been supplied, so
**no result is recorded** — this file only ever records supplied,
sanitized evidence. Accepting 26.04 in the installer's OS check is code
support only; it does not prove production validation on any Ubuntu
version, and CI exercises 22.04/24.04 runners only. Blocker B1 requires
separately recorded evidence for 22.04 and 24.04 (26.04 evidence is
additionally welcome but does not substitute).

## Required procedure (to close blocker B1)

Run on fresh Ubuntu 22.04 **and** 24.04 hosts (amd64; arm64 if
available), each with a real DNS record and ports 80/443 open:

1. `curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash`
   — answer prompts from `/dev/tty`; confirm no secret is ever echoed.
2. `centralpay status` — all services healthy; `centralpay diagnose`
   shows no failures.
3. Caddy obtains a real certificate; `https://<domain>/health/live`
   and `https://<domain>/health/ready` both return 200.
4. `centralpay backup`, then `centralpay backups` and a
   `centralpay restore FILE` round-trip per `BACKUP_RESTORE_FA.md`.
5. A full sandbox payment (see `STAGING_VALIDATION.md`).
6. `centralpay update --check`, then `centralpay update` against a
   published release tag — checksum verification must pass; then
   `centralpay rollback`.
7. Re-run the installer on the installed host — it must detect the
   existing installation and remain idempotent.
8. Record OS version, kernel, Docker version, date, operator, and any
   deviations in this file; attach sanitized logs (no secrets).

## Results

_None recorded. Blocker open._
