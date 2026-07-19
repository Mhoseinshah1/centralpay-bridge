"""Migrate-service least privilege, proven by execution: alembic runs with
exactly the environment the Compose migrate service receives — DATABASE_URL
plus the fixed non-secret masks, and no real application, CentralPay, bot,
callback, inbound, or Telegram credential.

Requires TEST_DATABASE_URL (disposable PostgreSQL), like the other
integration tests.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("postgresql"),
    reason="TEST_DATABASE_URL with a postgresql URL is required",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_alembic_upgrade_with_migrate_masked_environment():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    overrides = {
        key: str(value)
        for key, value in compose["services"]["migrate"]["environment"].items()
    }
    # The whole subprocess environment: no inherited credentials at all.
    env = {"PATH": os.environ["PATH"], "DATABASE_URL": TEST_DATABASE_URL, **overrides}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic upgrade failed under the masked migrate environment:\n"
        f"{result.stdout}\n{result.stderr}"
    )
