#!/usr/bin/env bash
# CentralPay Bridge PostgreSQL backup.
# Run directly, via "centralpay backup", or by the systemd timer.
#
# - pg_dump custom format, executed INSIDE the db container (client and
#   server versions always match)
# - exclusive lock: no concurrent backups, and no backup during a restore
# - atomic creation (.partial then rename); a partial file is never a backup
# - validated before rename: non-empty, PGDMP magic, pg_restore --list
# - a FILE.ok marker and a FILE.manifest sidecar (sha256, size, versions)
#   are written only after successful validation
# - retention from BACKUP_RETENTION_DAYS (default 14 days); the newest
#   validated backup is NEVER deleted; retention failure never makes a
#   successful backup look failed
# - never logs credentials or DATABASE_URL
#
# BACKUP_DRY_RUN=1 prints the planned actions without touching Docker or
# deleting anything (used by tests).

set -Eeuo pipefail
umask 077

INSTALL_DIR="${CENTRALPAY_INSTALL_DIR:-/opt/centralpay-bridge}"
CONFIG_DIR="${CENTRALPAY_CONFIG_DIR:-/etc/centralpay-bridge}"
BACKUP_DIR="${CENTRALPAY_BACKUP_DIR:-/var/backups/centralpay-bridge}"
ENV_FILE="${CONFIG_DIR}/centralpay.env"
DRY_RUN="${BACKUP_DRY_RUN:-0}"
LOCK_FILE="${BACKUP_DIR}/.backup.lock"

log()  { printf '[centralpay-backup] %s\n' "$*"; }

dc() { docker compose --project-directory "$INSTALL_DIR" "$@"; }

record_outcome() {
    # Best-effort operational record for admin alerts and /backup_status.
    # Never fails the backup itself and passes no secrets.
    dc exec -T api python -m app.ops backup-event "$@" >/dev/null 2>&1 || true
}

fail() {
    printf '[centralpay-backup] ERROR: backup_failed: %s\n' "$*" >&2
    record_outcome failure --detail "$*"
    exit 1
}

trap 'printf "[centralpay-backup] Backup FAILED.\n" >&2' ERR

retention_days() {
    local days
    days=$(grep -E '^BACKUP_RETENTION_DAYS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
    if [[ "$days" =~ ^[0-9]+$ ]] && [[ "$days" -gt 0 ]]; then
        echo "$days"
    else
        echo 14
    fi
}

validate_archive() {
    # Zero-byte, truncated, plain-SQL, and corrupted files must all fail
    # BEFORE the file can be renamed into a real backup.
    local f="$1"
    [[ -s "$f" ]] || { log "backup_validation_failed: empty file"; return 1; }
    [[ "$(head -c 5 "$f" 2>/dev/null)" == "PGDMP" ]] \
        || { log "backup_validation_failed: not a custom-format archive (bad magic)"; return 1; }
    dc exec -T db pg_restore --list < "$f" > /dev/null \
        || { log "backup_validation_failed: pg_restore rejected the archive"; return 1; }
}

write_manifest() {
    # Non-secret metadata sidecar, written atomically. Verified by
    # "centralpay restore" before any destructive action.
    local f="$1" sha size app_version server_version revision tmp
    sha=$(sha256sum -- "$f" | cut -d' ' -f1)
    size=$(stat -c%s -- "$f")
    app_version=$(grep -oE 'APP_VERSION = "[^"]+"' "${INSTALL_DIR}/app/version.py" 2>/dev/null \
        | cut -d'"' -f2 || true)
    server_version=$(dc exec -T db psql -U centralpay -d centralpay -tAc \
        "SHOW server_version" 2>/dev/null || true)
    revision=$(dc exec -T db psql -U centralpay -d centralpay -tAc \
        "SELECT version_num FROM alembic_version" 2>/dev/null || true)
    tmp="${f}.manifest.partial"
    {
        echo "backup_file=$(basename -- "$f")"
        echo "sha256=${sha}"
        echo "size_bytes=${size}"
        echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "app_version=${app_version:-unknown}"
        echo "postgres_version=${server_version:-unknown}"
        echo "alembic_revision=${revision:-unknown}"
        echo "validation=passed"
    } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "${f}.manifest"
    log "backup_manifest_written sha256=${sha:0:12}... size=${size}"
}

run_retention() {
    # Delete expired backups (and their sidecars), but never the newest
    # validated backup, never .partial files, never symlinks (-type f).
    local days="$1" newest_valid old
    newest_valid=$(find "$BACKUP_DIR" -maxdepth 1 -name 'centralpay-*.dump' -type f \
        -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | cut -d' ' -f2- | while read -r f; do
            [[ -f "${f}.ok" ]] && { echo "$f"; break; }
        done)
    while IFS= read -r old; do
        [[ -n "$old" ]] || continue
        if [[ "$old" == "$newest_valid" ]]; then
            log "Keeping newest validated backup despite age: ${old}"
            continue
        fi
        log "Removing expired backup: ${old}"
        rm -f -- "$old" "${old}.ok" "${old}.manifest"
    done < <(find "$BACKUP_DIR" -maxdepth 1 -name 'centralpay-*.dump' -type f \
        -mtime "+${days}" 2>/dev/null)
}

main() {
    local timestamp file partial days
    timestamp=$(date -u +%Y%m%d-%H%M%S)
    file="${BACKUP_DIR}/centralpay-${timestamp}.dump"
    partial="${file}.partial"
    days=$(retention_days)

    if [[ "$DRY_RUN" == "1" ]]; then
        log "DRY RUN: would create ${file} (retention ${days} days, keep-newest always)"
        exit 0
    fi

    [[ -f "${INSTALL_DIR}/docker-compose.yml" ]] || fail "No installation in ${INSTALL_DIR}."
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"

    # Exclusive lock: no concurrent backups, and mutual exclusion with
    # "centralpay restore" (which holds the same lock and exports
    # CENTRALPAY_BACKUP_LOCK_HELD=1 for its pre-restore backup).
    if [[ "${CENTRALPAY_BACKUP_LOCK_HELD:-0}" != "1" ]]; then
        exec 9>"$LOCK_FILE"
        flock -n 9 || fail "another backup or restore is already running"
    fi

    # Timestamped names make collisions a same-second rerun; never overwrite.
    [[ ! -e "$file" ]] || fail "target file already exists: ${file}"

    log "backup_started ${file}"
    if ! dc exec -T db pg_dump -U centralpay -d centralpay --format=custom > "$partial"; then
        rm -f -- "$partial"
        fail "pg_dump failed. Is the database container running? (centralpay status)"
    fi

    # Validate before the file is considered a backup at all.
    if ! validate_archive "$partial"; then
        rm -f -- "$partial"
        fail "Backup validation failed; partial file removed."
    fi

    mv "$partial" "$file"
    chmod 600 "$file"
    write_manifest "$file"
    : > "${file}.ok"
    chmod 600 "${file}.ok"
    log "backup_completed ${file} ($(du -h -- "$file" | cut -f1))"
    record_outcome success --size "$(du -h -- "$file" | cut -f1)" \
        --file-name "$(basename -- "$file")" --retention-days "$days"

    # Retention runs AFTER the successful backup is recorded. A retention
    # problem is reported loudly but never converts a successful backup
    # into a failure.
    if run_retention "$days"; then
        log "backup_retention_completed (${days} days, keep-newest always)"
    else
        printf '[centralpay-backup] WARNING: backup_retention_failed — the new backup itself succeeded; investigate old-file cleanup manually.\n' >&2
    fi

    log "Done."
}

main "$@"
