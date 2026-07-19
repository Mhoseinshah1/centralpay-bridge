#!/usr/bin/env bash
# CentralPay Bridge interactive installer.
#
# Usage (from a server):
#   curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
#
# The script is piped into bash, so ALL interactive input is read from
# /dev/tty, never from stdin. Secrets are read silently and never echoed.
#
# Safe to rerun: existing configuration and generated secrets are detected
# and reused unless the administrator chooses to reconfigure.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_URL="${CENTRALPAY_REPO_URL:-https://github.com/Mhoseinshah1/centralpay-bridge.git}"
GIT_REF="${CENTRALPAY_REF:-main}"
INSTALL_DIR="${CENTRALPAY_INSTALL_DIR:-/opt/centralpay-bridge}"
CONFIG_DIR="${CENTRALPAY_CONFIG_DIR:-/etc/centralpay-bridge}"
BACKUP_DIR="${CENTRALPAY_BACKUP_DIR:-/var/backups/centralpay-bridge}"
ENV_FILE="${CONFIG_DIR}/centralpay.env"
CADDYFILE="${CONFIG_DIR}/Caddyfile"
DB_PASSWORD_FILE="${CONFIG_DIR}/db_password"
CREDENTIALS_FILE="${CONFIG_DIR}/credentials.txt"
MIN_DISK_MB=5000
MIN_MEMORY_MB=750
MIN_DOCKER_MAJOR=24
CENTRALPAY_HOST="centralapi.org"

# ---------------------------------------------------------------------------
# Output helpers (never used to print secret values)
# ---------------------------------------------------------------------------

log()  { printf '\033[1;32m[centralpay]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[centralpay] WARNING:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[centralpay] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

on_error() {
    local line="$1"
    printf '\033[1;31m[centralpay] Installation failed near line %s.\033[0m\n' "$line" >&2
    printf 'No secrets were printed. Partial state may exist in %s and %s.\n' \
        "$INSTALL_DIR" "$CONFIG_DIR" >&2
    printf 'Rerunning the installer is safe.\n' >&2
}
trap 'on_error $LINENO' ERR

# ---------------------------------------------------------------------------
# Validation helpers (pure functions; unit-testable)
# ---------------------------------------------------------------------------

validate_ubuntu() {
    # $1 = ID from /etc/os-release, $2 = VERSION_ID
    local os_id="$1" version_id="$2"
    if [[ "$os_id" != "ubuntu" ]]; then
        return 1
    fi
    case "$version_id" in
        22.04|24.04|26.04) return 0 ;;
        *) return 1 ;;
    esac
}

normalize_architecture() {
    # $1 = uname -m; prints amd64/arm64 or fails
    case "$1" in
        x86_64|amd64) echo "amd64" ;;
        aarch64|arm64) echo "arm64" ;;
        *) return 1 ;;
    esac
}

validate_domain() {
    # RFC-1123-ish hostname with at least one dot, no scheme, no path.
    [[ "$1" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}[a-zA-Z0-9])?)+$ ]]
}

validate_email() {
    [[ "$1" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]
}

validate_positive_int() {
    [[ "$1" =~ ^[0-9]+$ ]] && [[ "$1" -gt 0 ]]
}

validate_admin_ids() {
    # Comma-separated positive numeric Telegram user IDs, never usernames.
    [[ "$1" =~ ^[0-9]+(,[0-9]+)*$ ]]
}

validate_fee_percent() {
    # EXACTLY the language app/services/fees.py parse_rate_percent accepts:
    # 0..100 inclusive, at most two decimal places, ASCII digits only, no
    # signs / whitespace / exponents / commas / newlines. Pure string and
    # integer comparison — no float arithmetic. 100, 100.0, 100.00 pass;
    # 100.01, 101, 999 fail. Keep the two implementations in lockstep.
    local LC_ALL=C
    local value="$1" whole frac
    [[ "$value" =~ ^([0-9]{1,3})(\.([0-9]{1,2}))?$ ]] || return 1
    whole="${BASH_REMATCH[1]}"
    frac="${BASH_REMATCH[3]:-}"
    # 10#: force base-10 so leading zeros (e.g. 007) stay decimal.
    (( 10#$whole <= 100 )) || return 1
    if (( 10#$whole == 100 )) && [[ -n "$frac" ]] && (( 10#$frac != 0 )); then
        return 1
    fi
    return 0
}

validate_report_time() {
    [[ "$1" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]$ ]]
}

validate_bot_input_scheme() {
    # Case-insensitive scheme gate for the bot URL question. Returns:
    #   0 — https:// in any ASCII case, or scheme-less input (a domain,
    #       optionally with :port and /path);
    #   2 — cleartext http:// in any ASCII case;
    #   3 — any other explicit scheme (ftp://, file://, ws://,
    #       javascript:, ...), which must never be silently rewritten
    #       to https://.
    # Never prints the input (or anything else).
    local lowered="${1,,}" after
    case "$lowered" in
        https://*) return 0 ;;
        http://*) return 2 ;;
        *://*) return 3 ;;
    esac
    if [[ "$lowered" == *:* ]]; then
        # host:port[/path] is domain input; anything else with a colon is
        # a scheme (javascript:alert(1), mailto:x, ...).
        after="${lowered#*:}"
        [[ "$after" =~ ^[0-9]+(/.*)?$ ]] && return 0
        return 3
    fi
    return 0
}

normalize_bot_url() {
    # Accepts "bot.example.com", "https://bot.example.com" (scheme in any
    # ASCII case) or a full endpoint; prints the complete bot payment
    # endpoint URL with the scheme canonicalized to lowercase https://.
    # Fails (printing nothing) on http:// or any unsupported scheme, so a
    # caller that skips the gate still cannot mint an insecure URL.
    local input="$1" base
    validate_bot_input_scheme "$input" || return 1
    if [[ "${input,,}" == https://* ]]; then
        base="https://${input#*://}"
    else
        base="https://$input"
    fi
    base="${base%/}"
    if [[ "$base" == */api/payment ]]; then
        echo "$base"
    else
        echo "$base/api/payment"
    fi
}

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        fail "This installer must run as root: curl -fsSL ... | sudo bash"
    fi
}

check_os() {
    [[ -r /etc/os-release ]] || fail "Cannot read /etc/os-release."
    # shellcheck disable=SC1091
    . /etc/os-release
    if ! validate_ubuntu "${ID:-unknown}" "${VERSION_ID:-unknown}"; then
        fail "Unsupported operating system: ${PRETTY_NAME:-unknown}. Supported: Ubuntu 22.04, 24.04, 26.04 LTS."
    fi
    log "Operating system: ${PRETTY_NAME}"
}

check_arch() {
    local machine arch
    machine="$(uname -m)"
    if ! arch="$(normalize_architecture "$machine")"; then
        fail "Unsupported architecture: ${machine}. Supported: amd64 (x86_64) and arm64 (aarch64)."
    fi
    log "Architecture: ${arch}"
}

check_resources() {
    local disk_mb mem_mb
    disk_mb=$(df -Pm / | awk 'NR==2 {print $4}')
    if [[ "$disk_mb" -lt "$MIN_DISK_MB" ]]; then
        fail "Insufficient disk space: ${disk_mb}MB free, ${MIN_DISK_MB}MB required."
    fi
    mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
    if [[ "$mem_mb" -lt "$MIN_MEMORY_MB" ]]; then
        fail "Insufficient memory: ${mem_mb}MB total, ${MIN_MEMORY_MB}MB required."
    fi
    if [[ "$mem_mb" -lt 1500 ]]; then
        warn "Only ${mem_mb}MB memory. 2GB or more is recommended."
    fi
    command -v curl >/dev/null 2>&1 || fail "curl is required but not installed."
    log "Resources: ${disk_mb}MB disk free, ${mem_mb}MB memory."
}

check_ports() {
    local port owner
    for port in 80 443; do
        owner=$(ss -ltnpH "sport = :$port" 2>/dev/null | head -1 || true)
        if [[ -n "$owner" ]]; then
            if [[ "$owner" == *docker* ]] && docker ps --format '{{.Names}}' 2>/dev/null | grep -q caddy; then
                log "Port ${port} is used by the existing CentralPay Caddy container (rerun OK)."
            else
                printf '%s\n' "$owner" >&2
                fail "Port ${port} is already in use by the process above. Stop it and rerun."
            fi
        fi
    done
}

# ---------------------------------------------------------------------------
# Interactive input (always from /dev/tty; secrets silent)
# ---------------------------------------------------------------------------

ask() {
    # $1 var name, $2 prompt, $3 default (may be empty)
    local var="$1" prompt="$2" default="${3:-}" value
    while true; do
        if [[ -n "$default" ]]; then
            read -r -p "$prompt [$default]: " value < /dev/tty
            value="${value:-$default}"
        else
            read -r -p "$prompt: " value < /dev/tty
        fi
        if [[ -n "$value" ]]; then
            printf -v "$var" '%s' "$value"
            return 0
        fi
        echo "A value is required." > /dev/tty
    done
}

ask_optional() {
    local var="$1" prompt="$2" value
    read -r -p "$prompt (optional, press Enter to skip): " value < /dev/tty
    printf -v "$var" '%s' "$value"
}

ask_secret() {
    # Silent read; the value is never echoed back.
    local var="$1" prompt="$2" value
    while true; do
        read -r -s -p "$prompt: " value < /dev/tty
        echo > /dev/tty
        if [[ -n "$value" ]]; then
            printf -v "$var" '%s' "$value"
            return 0
        fi
        echo "A value is required." > /dev/tty
    done
}

gather_input() {
    log "Configuration questions (input is read from the terminal):"

    while true; do
        ask PAYMENT_DOMAIN "1/10 Payment domain (e.g. pay.example.com)"
        validate_domain "$PAYMENT_DOMAIN" && break
        echo "Invalid domain format." > /dev/tty
    done

    while true; do
        ask BOT_INPUT "2/10 Bot API base domain or URL (e.g. https://bot.example.com)"
        # The bot Token header must never cross the network without TLS:
        # cleartext http:// input is rejected here in ANY letter case, and
        # the application enforces the same contract at startup (the
        # insecure escape hatch exists only for private mock bots and is
        # never set by the installer). Unsupported schemes (ftp, file, ws,
        # javascript, ...) fail here instead of being silently rewritten
        # to https://. Only these fixed messages are printed — never the
        # rejected input.
        scheme_rc=0
        validate_bot_input_scheme "$BOT_INPUT" || scheme_rc=$?
        if [[ "$scheme_rc" -eq 0 ]]; then
            break
        elif [[ "$scheme_rc" -eq 2 ]]; then
            echo "Cleartext http:// is not allowed for the bot URL; use https://." > /dev/tty
        else
            echo "Unsupported URL scheme for the bot URL; use https://." > /dev/tty
        fi
    done
    BOT_PAYMENT_NOTIFY_URL="$(normalize_bot_url "$BOT_INPUT")"
    log "Bot notification endpoint: ${BOT_PAYMENT_NOTIFY_URL}"

    # CentralPay issues a single API key that authenticates both
    # getLink.php and verify.php; one prompt fills both variables. The
    # application keeps two variables so a future split key needs no
    # contract change.
    ask_secret CENTRALPAY_API_KEY "3/10 CentralPay API key"
    CENTRALPAY_GETLINK_API_KEY="$CENTRALPAY_API_KEY"
    CENTRALPAY_VERIFY_API_KEY="$CENTRALPAY_API_KEY"

    ask_secret BOT_NOTIFY_TOKEN "4/10 Bot /token2 value"
    ask_optional TELEGRAM_BOT_USERNAME "5/10 Telegram bot username"

    while true; do
        ask TLS_EMAIL "6/10 Email for automatic TLS certificates"
        validate_email "$TLS_EMAIL" && break
        echo "Invalid email format." > /dev/tty
    done

    while true; do
        ask MIN_PAYMENT_AMOUNT_TOMAN "7/10 Minimum payment amount in TOMAN" "1000"
        validate_positive_int "$MIN_PAYMENT_AMOUNT_TOMAN" && break
        echo "Must be a positive integer." > /dev/tty
    done

    while true; do
        ask MAX_PAYMENT_AMOUNT_TOMAN "8/10 Maximum payment amount in TOMAN" "100000000"
        if validate_positive_int "$MAX_PAYMENT_AMOUNT_TOMAN" \
            && [[ "$MAX_PAYMENT_AMOUNT_TOMAN" -gt "$MIN_PAYMENT_AMOUNT_TOMAN" ]]; then
            break
        fi
        echo "Must be a positive integer greater than the minimum." > /dev/tty
    done

    while true; do
        ask PAYMENT_FEE_PERCENT "9/10 Payment fee percentage (0-100, up to 2 decimals)" "0"
        validate_fee_percent "$PAYMENT_FEE_PERCENT" && break
        echo "Use 0 to 100 inclusive with at most two decimal places (e.g. 0, 10, 7.5, 2.25, 100)." > /dev/tty
    done

    while true; do
        ask BOT_NOTIFY_RETRY_MODE "10/10 Bot notification retry mode (safe/idempotent)" "safe"
        case "$BOT_NOTIFY_RETRY_MODE" in
            safe|idempotent) break ;;
            *) echo "Choose 'safe' or 'idempotent'. Use 'idempotent' ONLY if the bot developer confirmed duplicate order_id delivery is safe." > /dev/tty ;;
        esac
    done

    gather_admin_bot_input
}

gather_admin_bot_input() {
    # Optional administrator Telegram bot. Disabled by default; the token is
    # read silently and never echoed or logged.
    ADMIN_BOT_ENABLED="false"
    ADMIN_BOT_TOKEN=""
    ADMIN_TELEGRAM_IDS=""
    ADMIN_BOT_PAYMENT_SUCCESS_ALERTS="false"
    ADMIN_BOT_DAILY_REPORT_ENABLED="true"
    ADMIN_BOT_DAILY_REPORT_TIME="09:00"
    ADMIN_BOT_TIMEZONE="Asia/Tehran"

    local answer
    read -r -p "Enable administrator Telegram bot? [y/N]: " answer < /dev/tty
    [[ "$answer" =~ ^[Yy] ]] || return 0

    ADMIN_BOT_ENABLED="true"
    ask_secret ADMIN_BOT_TOKEN "Telegram bot token (from BotFather)"
    while true; do
        ask ADMIN_TELEGRAM_IDS "Administrator Telegram numeric IDs (comma-separated)"
        validate_admin_ids "$ADMIN_TELEGRAM_IDS" && break
        echo "Only positive numeric Telegram user IDs separated by commas (e.g. 123456789,987654321)." > /dev/tty
    done
    read -r -p "Enable successful-payment alerts (can be noisy)? [y/N]: " answer < /dev/tty
    [[ "$answer" =~ ^[Yy] ]] && ADMIN_BOT_PAYMENT_SUCCESS_ALERTS="true"
    read -r -p "Enable daily report? [Y/n]: " answer < /dev/tty
    [[ "$answer" =~ ^[Nn] ]] && ADMIN_BOT_DAILY_REPORT_ENABLED="false"
    if [[ "$ADMIN_BOT_DAILY_REPORT_ENABLED" == "true" ]]; then
        while true; do
            ask ADMIN_BOT_DAILY_REPORT_TIME "Daily report time (HH:MM)" "09:00"
            validate_report_time "$ADMIN_BOT_DAILY_REPORT_TIME" && break
            echo "Use HH:MM 24-hour format." > /dev/tty
        done
    fi
    ask ADMIN_BOT_TIMEZONE "Timezone for reports" "Asia/Tehran"
}

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

docker_ready() {
    command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

check_docker_version() {
    local major
    major=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1 || echo 0)
    if [[ "${major:-0}" -lt "$MIN_DOCKER_MAJOR" ]]; then
        warn "Docker ${major}.x detected; ${MIN_DOCKER_MAJOR}.x or newer is recommended."
    fi
}

install_docker() {
    log "Installing Docker Engine from the official Docker apt repository..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg git openssl >/dev/null
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin >/dev/null
    systemctl enable --now docker
    log "Docker installed: $(docker --version)"
}

ensure_docker() {
    if docker_ready; then
        log "Docker present: $(docker --version); $(docker compose version --short 2>/dev/null || true)"
        check_docker_version
    else
        install_docker
    fi
    command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git >/dev/null; }
    command -v openssl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq openssl >/dev/null; }
}

# ---------------------------------------------------------------------------
# Network checks
# ---------------------------------------------------------------------------

check_dns() {
    DNS_READY=false
    local server_ip domain_ip
    server_ip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)
    domain_ip=$(getent ahostsv4 "$PAYMENT_DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)
    if [[ -z "$domain_ip" ]]; then
        warn "DNS for ${PAYMENT_DOMAIN} does not resolve yet."
    elif [[ -n "$server_ip" && "$domain_ip" == "$server_ip" ]]; then
        DNS_READY=true
        log "DNS OK: ${PAYMENT_DOMAIN} -> ${domain_ip} (this server)."
    else
        warn "DNS for ${PAYMENT_DOMAIN} resolves to ${domain_ip}, but this server appears to be ${server_ip:-unknown}."
    fi
    if [[ "$DNS_READY" != "true" ]]; then
        warn "Installation will continue, but TLS certificates stay PENDING until DNS points here."
        warn "After fixing DNS, run: centralpay ssl"
    fi
}

check_outbound() {
    # Reachability only — no credentials are ever sent during preflight.
    if curl -fsS --max-time 10 -o /dev/null "https://${CENTRALPAY_HOST}" 2>/dev/null \
        || [[ "$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "https://${CENTRALPAY_HOST}" 2>/dev/null)" != "000" ]]; then
        log "Outbound HTTPS to CentralPay (${CENTRALPAY_HOST}): reachable."
    else
        warn "Cannot reach https://${CENTRALPAY_HOST}. Payments will fail until outbound HTTPS works."
    fi
    local bot_host code
    bot_host=$(echo "$BOT_PAYMENT_NOTIFY_URL" | sed -E 's#^https?://([^/:]+).*#\1#')
    code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "https://${bot_host}" 2>/dev/null || echo "000")
    if [[ "$code" != "000" ]]; then
        log "Bot API host (${bot_host}): reachable (HTTP ${code})."
    else
        warn "Cannot reach the bot API host (${bot_host}). Notifications will fail until it is reachable."
    fi
}

# ---------------------------------------------------------------------------
# Deployment files, secrets, configuration
# ---------------------------------------------------------------------------

fetch_repository() {
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        log "Refreshing deployment files in ${INSTALL_DIR} (ref: ${GIT_REF})..."
        git -C "$INSTALL_DIR" fetch --depth 1 origin "$GIT_REF"
        git -C "$INSTALL_DIR" checkout -q FETCH_HEAD
    else
        log "Cloning ${REPO_URL} (ref: ${GIT_REF}) into ${INSTALL_DIR}..."
        rm -rf "$INSTALL_DIR"
        git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" "$INSTALL_DIR"
    fi
    chmod 755 "$INSTALL_DIR"
}

generate_secret() {
    openssl rand -hex 32
}

load_or_generate_secrets() {
    if [[ -f "$ENV_FILE" ]]; then
        log "Existing configuration found; reusing generated secrets."
        INBOUND_API_KEY=$(grep -E '^INBOUND_API_KEY=' "$ENV_FILE" | cut -d= -f2- || true)
        CALLBACK_HMAC_SECRET=$(grep -E '^CALLBACK_HMAC_SECRET=' "$ENV_FILE" | cut -d= -f2- || true)
        POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2- || true)
    fi
    [[ -n "${INBOUND_API_KEY:-}" ]] || INBOUND_API_KEY=$(generate_secret)
    [[ -n "${CALLBACK_HMAC_SECRET:-}" ]] || CALLBACK_HMAC_SECRET=$(generate_secret)
    [[ -n "${POSTGRES_PASSWORD:-}" ]] || POSTGRES_PASSWORD=$(openssl rand -hex 24)
}

render_template() {
    # $1 template file, $2 destination. Uses only fixed {{PLACEHOLDER}} names.
    sed \
        -e "s|{{PAYMENT_DOMAIN}}|${PAYMENT_DOMAIN}|g" \
        -e "s|{{TLS_EMAIL}}|${TLS_EMAIL}|g" \
        -e "s|{{POSTGRES_PASSWORD}}|${POSTGRES_PASSWORD}|g" \
        -e "s|{{INBOUND_API_KEY}}|${INBOUND_API_KEY}|g" \
        -e "s|{{CALLBACK_HMAC_SECRET}}|${CALLBACK_HMAC_SECRET}|g" \
        -e "s|{{CENTRALPAY_GETLINK_API_KEY}}|${CENTRALPAY_GETLINK_API_KEY}|g" \
        -e "s|{{CENTRALPAY_VERIFY_API_KEY}}|${CENTRALPAY_VERIFY_API_KEY}|g" \
        -e "s|{{CENTRALPAY_USER_ID}}|${CENTRALPAY_USER_ID:-1}|g" \
        -e "s|{{MIN_PAYMENT_AMOUNT_TOMAN}}|${MIN_PAYMENT_AMOUNT_TOMAN}|g" \
        -e "s|{{MAX_PAYMENT_AMOUNT_TOMAN}}|${MAX_PAYMENT_AMOUNT_TOMAN}|g" \
        -e "s|{{TELEGRAM_BOT_USERNAME}}|${TELEGRAM_BOT_USERNAME}|g" \
        -e "s|{{BOT_PAYMENT_NOTIFY_URL}}|${BOT_PAYMENT_NOTIFY_URL}|g" \
        -e "s|{{BOT_NOTIFY_TOKEN}}|${BOT_NOTIFY_TOKEN}|g" \
        -e "s|{{BOT_NOTIFY_RETRY_MODE}}|${BOT_NOTIFY_RETRY_MODE}|g" \
        -e "s|{{ADMIN_BOT_ENABLED}}|${ADMIN_BOT_ENABLED:-false}|g" \
        -e "s|{{ADMIN_BOT_TOKEN}}|${ADMIN_BOT_TOKEN:-}|g" \
        -e "s|{{ADMIN_TELEGRAM_IDS}}|${ADMIN_TELEGRAM_IDS:-}|g" \
        -e "s|{{ADMIN_BOT_PAYMENT_SUCCESS_ALERTS}}|${ADMIN_BOT_PAYMENT_SUCCESS_ALERTS:-false}|g" \
        -e "s|{{ADMIN_BOT_DAILY_REPORT_ENABLED}}|${ADMIN_BOT_DAILY_REPORT_ENABLED:-true}|g" \
        -e "s|{{ADMIN_BOT_DAILY_REPORT_TIME}}|${ADMIN_BOT_DAILY_REPORT_TIME:-09:00}|g" \
        -e "s|{{ADMIN_BOT_TIMEZONE}}|${ADMIN_BOT_TIMEZONE:-Asia/Tehran}|g" \
        "$1" > "$2"
}

write_configuration() {
    log "Writing configuration to ${CONFIG_DIR} (secrets never printed)..."
    install -d -m 0700 "$CONFIG_DIR"
    install -d -m 0700 "$BACKUP_DIR"

    umask 077
    render_template "${INSTALL_DIR}/deploy/centralpay.env.template" "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    printf '%s' "$POSTGRES_PASSWORD" > "$DB_PASSWORD_FILE"
    chmod 600 "$DB_PASSWORD_FILE"

    render_template "${INSTALL_DIR}/deploy/caddy/Caddyfile.template" "$CADDYFILE"
    chmod 600 "$CADDYFILE"

    cat > "$CREDENTIALS_FILE" <<EOF
CentralPay Bridge — installation summary ($(date -u +%Y-%m-%dT%H:%M:%SZ))

Payment API URL:      https://${PAYMENT_DOMAIN}/api/custom-payment
Inbound API key:      ${INBOUND_API_KEY}
Callback URL:         https://${PAYMENT_DOMAIN}/api/centralpay/callback
Health URL:           https://${PAYMENT_DOMAIN}/health/ready
Bot notification URL: ${BOT_PAYMENT_NOTIFY_URL}
Installed at:         $(date -u +%Y-%m-%dT%H:%M:%SZ)

Administrator bot:    ${ADMIN_BOT_ENABLED:-false}
Administrator IDs:    ${ADMIN_TELEGRAM_IDS:-none}

View again with: centralpay credentials
EOF
    chmod 600 "$CREDENTIALS_FILE"
    umask 022
}

configure_firewall() {
    command -v ufw >/dev/null 2>&1 || return 0
    if ufw status | grep -q "Status: active"; then
        log "UFW is active: allowing 80/tcp and 443/tcp (SSH untouched)."
        ufw allow 80/tcp >/dev/null
        ufw allow 443/tcp >/dev/null
    else
        log "UFW installed but inactive; leaving it unchanged. If you enable it later, first: ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp"
    fi
}

install_management_command() {
    install -m 0755 "${INSTALL_DIR}/scripts/centralpay" /usr/local/bin/centralpay
    # Deployment scripts get explicit safe modes: a plain git clone does not
    # guarantee the executable bit, and a non-executable backup.sh broke the
    # systemd backup timer with "Permission denied" on real hosts.
    chown root:root "${INSTALL_DIR}/scripts/backup.sh" "${INSTALL_DIR}/scripts/centralpay"
    chmod 0750 "${INSTALL_DIR}/scripts/backup.sh"
    chmod 0755 "${INSTALL_DIR}/scripts/centralpay"
    log "Installed management command: /usr/local/bin/centralpay"
}

ensure_initial_fee_policy() {
    # Runs AFTER migrations. Creates the initial fee policy through the
    # typed Python operations command (never shell SQL). --ensure-initial
    # makes this a no-op when any policy already exists, so a rerun can
    # never reset or silently replace an operator's fee configuration —
    # changing an existing fee always requires the explicit
    # 'centralpay fee set' command.
    local percent="${PAYMENT_FEE_PERCENT:-0}"
    # Belt and braces: the prompt already validated this, but the value may
    # come from a rerun environment — never hand an unvalidated rate to the
    # typed parser only to fail late.
    validate_fee_percent "$percent" \
        || fail "Invalid payment fee percentage '${percent}' (allowed: 0-100 with up to 2 decimals)."
    cd "$INSTALL_DIR"
    if docker compose run --rm migrate python -m app.ops fee set "$percent" \
        --note "Initial installation fee" --actor installer --ensure-initial; then
        log "Fee policy ensured (${percent}%; existing policy history is never overwritten)."
    else
        # The installation must NEVER report success while the operator's
        # requested fee configuration was silently not applied.
        fail "Could not ensure the initial fee policy (${percent}%). Fix the error above and re-run the installer, or apply it manually with: centralpay fee set ${percent} --note 'Initial installation fee'"
    fi
}

install_backup_timer() {
    install -m 0644 "${INSTALL_DIR}/deploy/systemd/centralpay-backup.service" /etc/systemd/system/
    install -m 0644 "${INSTALL_DIR}/deploy/systemd/centralpay-backup.timer" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now centralpay-backup.timer
    log "Daily backups scheduled (03:15, retention ${BACKUP_RETENTION_DAYS:-14} days)."
}

deploy_stack() {
    log "Building images and starting services (migrations run first)..."
    cd "$INSTALL_DIR"
    local -a profile_args=()
    if grep -qE '^ADMIN_BOT_ENABLED=true$' "$ENV_FILE" 2>/dev/null; then
        profile_args+=(--profile admin-bot)
    fi
    docker compose "${profile_args[@]}" build --quiet
    if ! docker compose "${profile_args[@]}" up -d --wait; then
        docker compose "${profile_args[@]}" ps >&2 || true
        echo >&2
        docker compose logs --tail 40 migrate >&2 || true
        fail "Deployment failed. If the 'migrate' service failed above, the database schema migration did not succeed and no application service was started with incompatible code. Fix the issue and rerun the installer or 'centralpay migrate'."
    fi
}

verify_deployment() {
    API_HEALTH="unknown"; DB_HEALTH="unknown"; WORKER_STATE="unknown"; TLS_STATE="pending"
    if docker compose -f "${INSTALL_DIR}/docker-compose.yml" --project-directory "$INSTALL_DIR" \
        exec -T api curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then
        API_HEALTH="ready"
    fi
    if docker compose --project-directory "$INSTALL_DIR" exec -T db pg_isready -U centralpay -d centralpay >/dev/null 2>&1; then
        DB_HEALTH="ready"
    fi
    WORKER_STATE=$(docker compose --project-directory "$INSTALL_DIR" ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk '$1=="worker" {print $2}' || echo unknown)
    if [[ "$DNS_READY" == "true" ]] \
        && curl -fsS --max-time 20 "https://${PAYMENT_DOMAIN}/health/live" >/dev/null 2>&1; then
        TLS_STATE="active"
    fi
}

print_summary() {
    cat <<EOF

============================================================
CentralPay Bridge installed successfully
============================================================

Custom gateway API URL:
  https://${PAYMENT_DOMAIN}/api/custom-payment

Custom gateway API token:
  ${INBOUND_API_KEY}

Callback URL:
  https://${PAYMENT_DOMAIN}/api/centralpay/callback

Health URL:
  https://${PAYMENT_DOMAIN}/health/ready

Bot notification URL:
  ${BOT_PAYMENT_NOTIFY_URL}

Status:
  HTTPS active:        ${TLS_STATE}
  DNS points here:     ${DNS_READY}
  API health:          ${API_HEALTH}
  Database health:     ${DB_HEALTH}
  Worker state:        ${WORKER_STATE}

Credentials file:
  ${CREDENTIALS_FILE}

Commands:
  centralpay status
  centralpay logs
  centralpay diagnose
  centralpay backup
  centralpay credentials
EOF
    if grep -qE '^ADMIN_BOT_ENABLED=true$' "$ENV_FILE" 2>/dev/null; then
        local admin_count
        admin_count=$(grep -E '^ADMIN_TELEGRAM_IDS=' "$ENV_FILE" | cut -d= -f2- | tr ',' '\n' | grep -c . || echo 0)
        cat <<EOF

Administrator bot:
  enabled (${admin_count} administrator ID(s); full list in ${CREDENTIALS_FILE})

Commands:
  centralpay admin-bot status
  centralpay admin-bot logs
  centralpay admin-bot test-alert
EOF
    fi
    if [[ "$DNS_READY" != "true" ]]; then
        cat <<EOF

NOTE: DNS does not point to this server yet, so HTTPS is NOT ready.
      Point ${PAYMENT_DOMAIN} to this server, then run: centralpay ssl
EOF
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    log "CentralPay Bridge installer"
    require_root
    check_os
    check_arch
    check_resources

    KEEP_EXISTING=false
    if [[ -f "$ENV_FILE" ]]; then
        local answer
        read -r -p "Existing installation detected. Keep existing configuration? [Y/n]: " answer < /dev/tty
        if [[ ! "$answer" =~ ^[Nn] ]]; then
            KEEP_EXISTING=true
        fi
    fi

    if [[ "$KEEP_EXISTING" == "true" ]]; then
        log "Reusing configuration from ${ENV_FILE}."
        PAYMENT_DOMAIN=$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2- | sed -E 's#^https?://##')
        BOT_PAYMENT_NOTIFY_URL=$(grep -E '^BOT_PAYMENT_NOTIFY_URL=' "$ENV_FILE" | cut -d= -f2-)
        BOT_NOTIFY_RETRY_MODE=$(grep -E '^BOT_NOTIFY_RETRY_MODE=' "$ENV_FILE" | cut -d= -f2-)
        BACKUP_RETENTION_DAYS=$(grep -E '^BACKUP_RETENTION_DAYS=' "$ENV_FILE" | cut -d= -f2- || echo 14)
    else
        gather_input
    fi

    check_ports
    ensure_docker
    check_dns
    check_outbound
    fetch_repository

    if [[ "$KEEP_EXISTING" != "true" ]]; then
        load_or_generate_secrets
        write_configuration
    fi

    configure_firewall
    install_management_command
    install_backup_timer
    deploy_stack
    ensure_initial_fee_policy
    verify_deployment
    print_summary
}

# Allow tests to source the functions without executing the installer.
if [[ "${CENTRALPAY_INSTALL_SOURCE_ONLY:-0}" != "1" ]]; then
    main "$@"
fi
