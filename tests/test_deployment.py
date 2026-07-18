"""Phase 3 deployment artifact tests: compose file, Caddyfile, installer,
management CLI, backup script, and environment template.

These are static/controlled checks only — no live installation, no Docker
daemon required, no destructive operations.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
CADDY_TEMPLATE = PROJECT_ROOT / "deploy" / "caddy" / "Caddyfile.template"
ENV_TEMPLATE = PROJECT_ROOT / "deploy" / "centralpay.env.template"
INSTALLER = PROJECT_ROOT / "install.sh"
MANAGEMENT = PROJECT_ROOT / "scripts" / "centralpay"
BACKUP_SCRIPT = PROJECT_ROOT / "scripts" / "backup.sh"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"

REQUIRED_ENV_VARS = [
    "PUBLIC_BASE_URL",
    "INBOUND_API_KEY",
    "CENTRALPAY_GETLINK_API_KEY",
    "CENTRALPAY_VERIFY_API_KEY",
    "BOT_PAYMENT_NOTIFY_URL",
    "BOT_NOTIFY_TOKEN",
    "BOT_NOTIFY_RETRY_MODE",
    "BOT_NOTIFY_MAX_ATTEMPTS",
    "BOT_NOTIFY_CONNECT_TIMEOUT_SECONDS",
    "BOT_NOTIFY_READ_TIMEOUT_SECONDS",
    "BOT_NOTIFY_WORKER_INTERVAL_SECONDS",
    "BOT_NOTIFY_CLAIM_TIMEOUT_SECONDS",
    "CALLBACK_HMAC_SECRET",
    "DATABASE_URL",
    "MIN_PAYMENT_AMOUNT_TOMAN",
    "MAX_PAYMENT_AMOUNT_TOMAN",
    "TELEGRAM_BOT_USERNAME",
    "LOG_LEVEL",
    "LOG_FORMAT",
]


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CENTRALPAY_INSTALL_SOURCE_ONLY": "1"},
    )


def installer_call(function_call: str) -> subprocess.CompletedProcess[str]:
    return run_bash(f"source {INSTALLER} && {function_call}")


# --- docker-compose.yml -----------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> dict[str, object]:
    loaded: dict[str, object] = yaml.safe_load(COMPOSE_FILE.read_text())
    return loaded


def test_compose_declares_required_services(compose):
    assert set(compose["services"]) >= {"api", "worker", "db", "caddy", "migrate"}


def test_only_caddy_publishes_ports(compose):
    for name, service in compose["services"].items():
        if name == "caddy":
            published = {str(port).split(":")[0] for port in service["ports"]}
            assert published == {"80", "443"}
        else:
            # The API and PostgreSQL must never be publicly exposed.
            assert "ports" not in service, f"service {name} must not publish host ports"


def test_migration_gates_api_and_worker(compose):
    for name in ("api", "worker"):
        depends = compose["services"][name]["depends_on"]
        assert depends["migrate"]["condition"] == "service_completed_successfully"
        assert depends["db"]["condition"] == "service_healthy"
    # The migration itself waits for a healthy database and runs once.
    migrate = compose["services"]["migrate"]
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    assert migrate["restart"] == "no"
    assert migrate["command"] == ["alembic", "upgrade", "head"]


def test_healthchecks_defined(compose):
    db_check = compose["services"]["db"]["healthcheck"]["test"]
    assert "pg_isready" in " ".join(db_check)
    api_check = compose["services"]["api"]["healthcheck"]["test"]
    assert "/health/ready" in " ".join(api_check)
    worker_check = compose["services"]["worker"]["healthcheck"]["test"]
    assert "heartbeat" in " ".join(worker_check).lower()


def test_log_rotation_configured(compose):
    for name, service in compose["services"].items():
        options = service["logging"]["options"]
        assert options["max-size"] == "20m", f"service {name}"
        assert options["max-file"] == "5", f"service {name}"


def test_no_hardcoded_credentials_in_compose():
    text = COMPOSE_FILE.read_text()
    assert "POSTGRES_PASSWORD:" not in text  # only POSTGRES_PASSWORD_FILE
    assert "password=" not in text.lower()
    # Secrets come from files outside the repository.
    assert "/etc/centralpay-bridge/" in text


def test_named_database_volume(compose):
    assert "db_data" in compose["volumes"]
    assert any("db_data" in volume for volume in compose["services"]["db"]["volumes"])


def test_restart_policies(compose):
    for name in ("api", "worker", "db", "caddy"):
        assert compose["services"][name]["restart"] == "unless-stopped", f"service {name}"


# --- Caddyfile --------------------------------------------------------------


def test_caddyfile_template_contents():
    text = CADDY_TEMPLATE.read_text()
    assert "{{PAYMENT_DOMAIN}}" in text
    assert "{{TLS_EMAIL}}" in text
    assert "admin off" in text
    assert "reverse_proxy api:8000" in text
    # The callback signature must be redacted from access logs.
    assert "replace sig REDACTED" in text
    # Security headers.
    for header in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options"):
        assert header in text
    assert "max_size" in text
    # Proxy-issued request IDs override any client value.
    assert "X-Request-ID" in text and "http.request.uuid" in text
    # Only public routes are proxied; everything else 404s.
    for route in (
        "/api/custom-payment",
        "/api/centralpay/callback",
        "/health/live",
        "/health/ready",
    ):
        assert route in text
    assert "respond \"Not found\" 404" in text


# --- environment template ---------------------------------------------------


def test_env_template_covers_required_variables():
    text = ENV_TEMPLATE.read_text()
    for var in REQUIRED_ENV_VARS:
        pattern = rf"^{var}=|^# ?{var}"
        assert re.search(pattern, text, re.MULTILINE), f"missing {var}"


def test_env_template_contains_no_real_secrets():
    text = ENV_TEMPLATE.read_text()
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        _, _, value = line.partition("=")
        # Values are placeholders, fixed defaults, or empty — never long
        # random-looking secrets.
        assert "{{" in value or len(value) < 64, f"suspicious value in template: {line}"


# --- installer --------------------------------------------------------------


def test_installer_strict_mode_and_error_trap():
    text = INSTALLER.read_text()
    assert "set -Eeuo pipefail" in text
    assert re.search(r"trap 'on_error \$LINENO' ERR", text)


def test_installer_reads_interactive_input_from_tty():
    text = INSTALLER.read_text()
    reads = [line for line in text.splitlines() if re.search(r"\bread -r", line)]
    assert reads, "installer must prompt interactively"
    for line in reads:
        assert "/dev/tty" in line, f"read without /dev/tty: {line.strip()}"


def test_installer_reads_secrets_silently():
    text = INSTALLER.read_text()
    # The secret prompt helper must use a silent read.
    assert re.search(r"read -r -s .*< /dev/tty", text)
    # Secret values never reach the terminal: any echo/printf of a secret
    # variable must be redirected into a file (e.g. the 0600 password file).
    secret_vars = (
        "CENTRALPAY_API_KEY",
        "CENTRALPAY_GETLINK_API_KEY",
        "CENTRALPAY_VERIFY_API_KEY",
        "BOT_NOTIFY_TOKEN",
        "POSTGRES_PASSWORD",
        "INBOUND_API_KEY_NEVER",  # sentinel: pattern sanity
    )
    for line in text.splitlines():
        for var in secret_vars:
            if re.search(rf"(echo|printf).*\$\{{?{var}\b", line):
                assert ">" in line, f"secret {var} printed to terminal: {line.strip()}"


def test_installer_rejects_unsupported_os():
    ok = installer_call("validate_ubuntu ubuntu 24.04")
    assert ok.returncode == 0
    for os_id, version in (("debian", "12"), ("ubuntu", "20.04"), ("centos", "9")):
        result = installer_call(f"validate_ubuntu {os_id} {version}")
        assert result.returncode != 0, f"{os_id} {version} must be rejected"


def test_installer_rejects_unsupported_architecture():
    for machine, expected in (("x86_64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")):
        result = installer_call(f"normalize_architecture {machine}")
        assert result.returncode == 0
        assert result.stdout.strip() == expected
    for machine in ("armv7l", "i686", "riscv64"):
        result = installer_call(f"normalize_architecture {machine}")
        assert result.returncode != 0, f"{machine} must be rejected"


def test_installer_domain_validation():
    assert installer_call("validate_domain pay.example.com").returncode == 0
    for bad in ("http://pay.example.com", "pay", "-bad.example.com", "a b.example.com"):
        assert installer_call(f"validate_domain '{bad}'").returncode != 0, bad


def test_installer_bot_url_normalization():
    cases = {
        "bot.example.com": "https://bot.example.com/api/payment",
        "https://bot.example.com": "https://bot.example.com/api/payment",
        "https://bot.example.com/": "https://bot.example.com/api/payment",
        "https://bot.example.com/api/payment": "https://bot.example.com/api/payment",
    }
    for given, expected in cases.items():
        result = installer_call(f"normalize_bot_url '{given}'")
        assert result.stdout.strip() == expected


def test_installer_email_validation():
    assert installer_call("validate_email ops@example.com").returncode == 0
    assert installer_call("validate_email not-an-email").returncode != 0


def test_installer_uses_cryptographic_secret_generation():
    text = INSTALLER.read_text()
    assert "openssl rand -hex" in text


def test_installer_secure_permissions():
    text = INSTALLER.read_text()
    assert "install -d -m 0700" in text  # secrets directory
    assert 'chmod 600 "$ENV_FILE"' in text
    assert 'chmod 600 "$DB_PASSWORD_FILE"' in text
    assert 'chmod 600 "$CREDENTIALS_FILE"' in text


def test_installer_never_prints_https_ready_when_dns_pending():
    text = INSTALLER.read_text()
    assert "centralpay ssl" in text
    assert "DNS_READY" in text


# --- management CLI ---------------------------------------------------------


def test_management_cli_syntax_and_help():
    syntax = subprocess.run(
        ["bash", "-n", str(MANAGEMENT)], capture_output=True, text=True, timeout=30
    )
    assert syntax.returncode == 0, syntax.stderr
    result = subprocess.run(
        ["bash", str(MANAGEMENT), "help"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    for command in (
        "status", "logs", "logs-errors", "restart", "stop", "start", "update",
        "migrate", "backup", "backups", "restore", "diagnose", "payment",
        "recent", "retry-queue", "manual-review", "credentials", "ssl",
        "version", "uninstall",
    ):
        assert command in result.stdout, f"help must document {command}"


def test_management_cli_never_dumps_env_file():
    text = MANAGEMENT.read_text()
    # The env file may be grepped for single non-secret values, but never
    # printed wholesale.
    assert not re.search(r"cat .*ENV_FILE", text)


def test_restore_requires_confirmation():
    text = MANAGEMENT.read_text()
    assert 'Type RESTORE to continue' in text
    assert '--yes' in text
    # Pre-restore backup and file verification happen before any destruction.
    assert "pg_restore --list" in text
    restore_body = text.split("cmd_restore()")[1].split("cmd_diagnose()")[0]
    assert restore_body.index("backup.sh") < restore_body.index("DROP DATABASE")


def test_uninstall_preserves_data_by_default():
    text = MANAGEMENT.read_text()
    assert "DELETE-DATA" in text
    assert "DELETE-BACKUPS" in text
    body = text.split("cmd_uninstall()")[1]
    # Plain "compose down" (without -v) runs unconditionally; "down -v" only
    # inside the DELETE-DATA confirmation branch.
    assert 'compose down' in body
    assert body.index('== "DELETE-DATA"') < body.index("compose down -v")


# --- backup script ----------------------------------------------------------


def test_backup_script_dry_run(tmp_path):
    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin",
            "BACKUP_DRY_RUN": "1",
            "CENTRALPAY_BACKUP_DIR": str(tmp_path),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert list(tmp_path.iterdir()) == []  # nothing was created


def test_backup_script_safety_properties():
    text = BACKUP_SCRIPT.read_text()
    assert "--format=custom" in text
    assert ".partial" in text  # atomic creation
    assert "pg_restore --list" in text  # validation
    assert "chmod 600" in text
    assert "newest_valid" in text  # newest valid backup is never deleted


def test_systemd_timer_schedule():
    timer = (PROJECT_ROOT / "deploy" / "systemd" / "centralpay-backup.timer").read_text()
    assert "OnCalendar=*-*-* 03:15:00" in timer
    assert "Persistent=true" in timer
    service = (PROJECT_ROOT / "deploy" / "systemd" / "centralpay-backup.service").read_text()
    assert "backup.sh" in service


# --- Dockerfile -------------------------------------------------------------


def test_dockerfile_properties():
    text = DOCKERFILE.read_text()
    assert text.count("FROM python:3.12-slim") == 2  # multi-stage
    assert "USER centralpay" in text  # non-root runtime
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert "PYTHONUNBUFFERED=1" in text
    assert "HEALTHCHECK" in text
    # Exec-form CMD for clean SIGTERM shutdown.
    assert re.search(r'CMD \["uvicorn"', text)
    # No secrets or env files are copied into layers.
    assert ".env" not in text
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text()
    assert ".env" in dockerignore


# --- ShellCheck (skipped when the binary is unavailable) --------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", [INSTALLER, MANAGEMENT, BACKUP_SCRIPT])
def test_shellcheck_clean(script):
    result = subprocess.run(
        ["shellcheck", str(script)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout


# --- backup/restore integrity audit ------------------------------------------


def test_backup_script_integrity_hardening():
    text = BACKUP_SCRIPT.read_text()
    # Exclusive lock shared with restore; reentrant for the pre-restore backup.
    assert "flock -n 9" in text
    assert "CENTRALPAY_BACKUP_LOCK_HELD" in text
    # Restrictive default permissions for every file the script creates.
    assert "umask 077" in text
    # Validation before rename: non-empty, custom-format magic, pg_restore.
    assert "PGDMP" in text
    assert "pg_restore --list" in text
    # Atomicity and no-overwrite.
    assert ".partial" in text
    assert "target file already exists" in text
    # Checksum manifest written atomically after validation.
    assert "sha256sum" in text
    assert ".manifest" in text
    assert "validation=passed" in text
    # Retention failure is loud but never converts a successful backup
    # into a failure; sidecars are removed together with expired dumps.
    assert "backup_retention_failed" in text
    assert '"${old}.manifest"' in text
    # Credentials are never read or logged (dump runs inside the container
    # against its local socket; the script never touches the DB password).
    assert "PGPASSWORD" not in text
    assert "$DATABASE_URL" not in text
    assert "${DATABASE_URL" not in text


def test_restore_preflight_and_failure_safety():
    text = MANAGEMENT.read_text()
    restore_body = text.split("cmd_restore()")[1].split("cmd_db_check()")[0]
    # Preflight: regular file, no symlink, magic bytes, checksum manifest.
    assert "! -L" in restore_body
    assert "PGDMP" in restore_body
    assert "sha256sum" in restore_body
    # Legacy files need an explicit extra confirmation; --yes cannot skip it.
    assert "RESTORE-LEGACY" in restore_body
    assert "--yes cannot accept a legacy backup" in restore_body
    # Exclusive lock shared with the backup script.
    assert "flock -n 9" in restore_body
    assert "CENTRALPAY_BACKUP_LOCK_HELD=1" in restore_body
    # All writers stopped, including the admin bot when enabled.
    assert "compose stop api worker" in restore_body
    assert "compose stop admin-bot" in restore_body
    # Partial-restore detection and explicit recovery guidance.
    assert "--exit-on-error" in restore_body
    assert "restore_failure_instructions" in restore_body
    # Post-restore integrity gate before services are started.
    assert "db-check --repair-sequences" in restore_body
    assert restore_body.index("db-check") < restore_body.index("compose up -d --wait")
    # Failure guidance never restarts services against a partial database.
    instructions = text.split("restore_failure_instructions()")[1].split("cmd_restore()")[0]
    assert "STOPPED" in instructions
    assert "centralpay restore" in instructions


def test_db_check_command_exposed():
    text = MANAGEMENT.read_text()
    assert "cmd_db_check" in text
    assert "db-check" in text
    result = subprocess.run(
        ["bash", str(MANAGEMENT), "help"], capture_output=True, text=True, timeout=30
    )
    assert "db-check" in result.stdout


# --- deployment security audit ----------------------------------------------

DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"


def test_no_privileged_or_host_namespaces(compose):
    for name, svc in compose["services"].items():
        assert not svc.get("privileged"), name
        assert svc.get("network_mode") != "host", name
        assert svc.get("pid") != "host", name
        assert svc.get("ipc") != "host", name


def test_no_docker_socket_or_broad_host_mounts(compose):
    for name, svc in compose["services"].items():
        for volume in svc.get("volumes", []):
            spec = volume if isinstance(volume, str) else str(volume)
            assert "docker.sock" not in spec, name
            source = spec.split(":")[0]
            assert source not in ("/", "/etc", "/root", "/home", "/var"), name


def test_app_services_fully_hardened(compose):
    """api/worker/migrate share the hardening profile the admin-bot service
    has run since Phase 4: immutable root fs, tmpfs /tmp, no capabilities,
    no privilege escalation."""
    for name in ("api", "worker", "migrate", "admin-bot"):
        svc = compose["services"][name]
        assert svc.get("read_only") is True, name
        assert "ALL" in svc.get("cap_drop", []), name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name
        assert any(str(t).startswith("/tmp") for t in svc.get("tmpfs", [])), name


def test_every_service_denies_privilege_escalation(compose):
    for name, svc in compose["services"].items():
        assert "no-new-privileges:true" in svc.get("security_opt", []), name


def test_caddy_cannot_reach_database(compose):
    services = compose["services"]
    # Caddy lives on the edge network only; PostgreSQL on internal only.
    assert services["caddy"]["networks"] == ["edge"]
    assert services["db"]["networks"] == ["internal"]
    assert sorted(services["api"]["networks"]) == ["edge", "internal"]
    for name in ("worker", "migrate", "admin-bot"):
        assert services[name]["networks"] == ["internal"], name
    # Caddy receives no application env file and no secrets.
    assert "env_file" not in services["caddy"]
    assert "secrets" not in services["caddy"]


def test_worker_masks_unneeded_secrets(compose):
    env = compose["services"]["worker"]["environment"]
    assert env["CENTRALPAY_GETLINK_API_KEY"] == "not-used-by-worker"
    assert env["CENTRALPAY_VERIFY_API_KEY"] == "not-used-by-worker"
    assert env["INBOUND_API_KEY"] == "not-used-by-worker-x"
    assert env["CALLBACK_HMAC_SECRET"] == "not-used-by-worker-x"


def test_dockerignore_excludes_sensitive_files():
    entries = {
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in (
        ".git", ".env", ".env.*", "credentials*", "*.dump", "*.sqlite",
        "*.pem", "*.key", "backups", ".idea", ".vscode", "tests", ".venv",
    ):
        assert required in entries, required


def test_dockerfile_nonroot_fixed_uid_no_secrets():
    text = DOCKERFILE.read_text()
    assert "USER centralpay" in text
    assert "--uid 10001" in text
    assert "--gid 10001" in text
    # Only explicit paths are copied — never the whole context, never env
    # files; secrets cannot be baked into layers.
    assert "COPY . " not in text
    assert ".env" not in text
    for line in text.splitlines():
        if line.startswith("COPY"):
            assert "secret" not in line.lower()


def test_caddy_redacts_signature_and_token_queries():
    text = CADDY_TEMPLATE.read_text()
    assert "replace sig REDACTED" in text
    assert "replace ct REDACTED" in text  # one-time callback token


def test_logs_commands_use_component_allowlist():
    text = MANAGEMENT.read_text()
    allow = text.split("validate_component()")[1].split("}")[0]
    assert "api|worker|db|caddy|admin-bot|migrate" in allow
    logs_body = text.split("cmd_logs()")[1].split("cmd_migrate()")[0]
    assert logs_body.count("validate_component") >= 2


def test_update_never_extracts_archives():
    """The release artifact is downloaded and checksum-verified only;
    deployment happens via git checkout of the pinned ref. No archive is
    ever extracted, so archive path-traversal/symlink attacks have no
    surface in the update or backup paths."""
    text = MANAGEMENT.read_text()
    assert "sha256sum -c" in text
    for extraction in ("tar -x", "tar x", "unzip", "tar --extract"):
        assert extraction not in text, extraction
    backup_text = BACKUP_SCRIPT.read_text()
    for extraction in ("tar -x", "unzip"):
        assert extraction not in backup_text


# --- single CentralPay API key -----------------------------------------------


def test_installer_asks_for_one_centralpay_key_filling_both_variables():
    """CentralPay issues a single API key used by both getLink.php and
    verify.php: the installer prompts once (hidden) and stores the same
    value in both environment variables."""
    text = INSTALLER.read_text()
    # Exactly one CentralPay key prompt, read silently.
    assert text.count('ask_secret CENTRALPAY_API_KEY "3/9 CentralPay API key"') == 1
    assert "getLink API key" not in text
    assert "verify API key" not in text
    # The single value fills both variables the application reads.
    assert 'CENTRALPAY_GETLINK_API_KEY="$CENTRALPAY_API_KEY"' in text
    assert 'CENTRALPAY_VERIFY_API_KEY="$CENTRALPAY_API_KEY"' in text
    # The key is never echoed or logged (no echo/printf/log line carries it
    # outside a file redirection; covered in detail by
    # test_installer_reads_secrets_silently, which includes
    # CENTRALPAY_API_KEY in its secret list).
    gather = text.split("gather_input()")[1].split("gather_admin_bot_input()")[0]
    assert gather.count("CENTRALPAY_API_KEY") == 3  # prompt + two assignments


def test_installer_rerun_preserves_existing_centralpay_keys():
    """Backward compatibility: on rerun, keeping the existing configuration
    skips gather_input and write_configuration entirely, so previously
    stored (possibly distinct) key values are never overwritten unless the
    operator explicitly chooses to reconfigure."""
    text = INSTALLER.read_text()
    assert "Keep existing configuration?" in text
    keep_branch = text.split("Keep existing configuration?")[1]
    # gather_input runs only in the reconfigure branch...
    assert 'if [[ "$KEEP_EXISTING" == "true" ]]' in keep_branch
    # ...and configuration writing is likewise gated.
    assert 'if [[ "$KEEP_EXISTING" != "true" ]]' in keep_branch
    write_gate = keep_branch.split('if [[ "$KEEP_EXISTING" != "true" ]]')[1]
    assert "write_configuration" in write_gate.split("\n    fi")[0]
