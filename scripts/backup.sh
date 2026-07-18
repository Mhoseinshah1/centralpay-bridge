#!/usr/bin/env bash
# CentralPay Bridge PostgreSQL backup.
# Run directly, via "centralpay backup", or by the systemd timer.
#
# - pg_dump custom format, atomic creation (.partial then rename)
# - validated with pg_restore --list; a FILE.ok marker records success
# - retention from BACKUP_RETENTION_DAYS (default 14 days)
# - the newest validated backup is NEVER deleted, regardless of age
#
# BACKUP_DRY_RUN=1 prints the planned actions without touching Docker or
# deleting anything (used by tests).

set -Eeuo pipefail

INSTALL_DIR="${CENTRALPAY_INSTALL_DIR:-/opt/centralpay-bridge}"
CONFIG_DIR="${CENTRALPAY_CONFIG_DIR:-/etc/centralpay-bridge}"
BACKUP_DIR="${CENTRALPAY_BACKUP_DIR:-/var/backups/centralpay-bridge}"
ENV_FILE="${CONFIG_DIR}/centralpay.env"
DRY_RUN="${BACKUP_DRY_RUN:-0}"

log()  { printf '[centralpay-backup] %s\n' "$*"; }
fail() { printf '[centralpay-backup] ERROR: %s\n' "$*" >&2; exit 1; }

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

    log "Creating backup ${file}..."
    umask 077
    if ! docker compose --project-directory "$INSTALL_DIR" exec -T db \
        pg_dump -U centralpay -d centralpay --format=custom > "$partial"; then
        rm -f "$partial"
        fail "pg_dump failed. Is the database container running? (centralpay status)"
    fi

    # Validate before the file is considered a backup at all.
    if ! docker compose --project-directory "$INSTALL_DIR" exec -T db \
        pg_restore --list < "$partial" > /dev/null; then
        rm -f "$partial"
        fail "Backup validation failed (pg_restore --list rejected the dump)."
    fi

    mv "$partial" "$file"
    chmod 600 "$file"
    : > "${file}.ok"
    chmod 600 "${file}.ok"
    log "Backup created and validated: ${file} ($(du -h "$file" | cut -f1))"

    # Retention: delete backups older than N days, but never the newest
    # validated backup.
    local newest_valid
    newest_valid=$(find "$BACKUP_DIR" -name 'centralpay-*.dump' -type f -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | cut -d' ' -f2- | while read -r f; do
            [[ -f "${f}.ok" ]] && { echo "$f"; break; }
        done)
    local old
    while IFS= read -r old; do
        [[ -n "$old" ]] || continue
        if [[ "$old" == "$newest_valid" ]]; then
            log "Keeping newest validated backup despite age: ${old}"
            continue
        fi
        log "Removing expired backup: ${old}"
        rm -f "$old" "${old}.ok"
    done < <(find "$BACKUP_DIR" -name 'centralpay-*.dump' -type f -mtime "+${days}" 2>/dev/null)

    log "Done. Retention: ${days} days."
}

main "$@"
