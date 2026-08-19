#!/usr/bin/env bash
# CI-only shim for pg_dump/pg_restore: runs the tool INSIDE the postgres:16
# GitHub Actions service container instead of a separately-installed host
# client package.
#
# Why: the client major version must be >= the server's (pg_dump/pg_restore
# abort on a newer-major server; see
# tests/integration/test_backup_restore.py::_find_pg_tool). Installing a
# matching client via apt/PGDG on the runner depends on external
# package-index network access, which caused repeated CI hangs at "Install
# PostgreSQL 16 client tools". The service container already ships a
# version-matched pg_dump/pg_restore, so use those directly via `docker
# exec` -- no apt, no PGDG, no network dependency beyond the container
# that's already running.
#
# Install as both `pg_dump` and `pg_restore` (e.g. copy to pg_dump, then
# symlink pg_restore -> pg_dump) and put the directory on PATH ahead of any
# other pg_dump/pg_restore. Requires POSTGRES_SERVICE_CONTAINER set to the
# service container's id (GitHub Actions: ${{ job.services.postgres.id }}).
#
# stdin/stdout are passed through untouched -- pg_dump/pg_restore move
# binary archive data over them -- so all wrapper diagnostics go to stderr.
set -euo pipefail

tool=$(basename -- "$0")
: "${POSTGRES_SERVICE_CONTAINER:?POSTGRES_SERVICE_CONTAINER must be set to the postgres service container id}"

exec docker exec --interactive \
  --env "PGPASSWORD=${PGPASSWORD:-}" \
  "$POSTGRES_SERVICE_CONTAINER" "$tool" "$@"
