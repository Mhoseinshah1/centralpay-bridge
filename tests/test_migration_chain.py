"""The Alembic revision chain itself: shape, head, and metadata agreement.

These assertions are deliberately DUPLICATED against
``tests/alembic_head.py``'s dynamic lookup. That module makes the PostgreSQL
upgrade tests self-maintaining ("we reached head"); this file is the one
place where the head's exact value is pinned, so adding a migration is a
conscious act recorded in a diff rather than something eight unrelated tests
silently absorb.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.alembic_head import ALEMBIC_HEAD

REPO_ROOT = Path(__file__).resolve().parents[1]

# Bump this deliberately, in the same commit that adds the migration.
EXPECTED_HEAD = "0013"

# The full expected chain, oldest first. Written out rather than derived so a
# migration inserted in the wrong place, or a down_revision typo that silently
# reorders history, fails loudly.
EXPECTED_CHAIN = (
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "0011",
    "0012",
    "0013",
)


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_head_is_the_expected_revision(script_directory: ScriptDirectory) -> None:
    assert script_directory.get_current_head() == EXPECTED_HEAD


def test_dynamic_head_helper_agrees_with_the_pinned_head() -> None:
    """`tests/alembic_head.py` is what the PostgreSQL upgrade tests assert
    against; if it ever disagreed with the pinned value, those tests would be
    asserting something other than "we reached the head this repo declares"."""
    assert ALEMBIC_HEAD == EXPECTED_HEAD


def test_chain_is_linear_and_complete(script_directory: ScriptDirectory) -> None:
    """Exactly one head, exactly one base, and every revision linked by a
    single `down_revision` — no branches, no orphans, no gaps."""
    assert [rev.revision for rev in script_directory.get_revisions("heads")] == [
        EXPECTED_HEAD
    ]
    walked = tuple(
        reversed([rev.revision for rev in script_directory.walk_revisions("base", "heads")])
    )
    assert walked == EXPECTED_CHAIN


def test_every_revision_file_defines_upgrade_and_downgrade(
    script_directory: ScriptDirectory,
) -> None:
    """Forward-only in PRACTICE (0010+ downgrades are non-destructive pointer
    moves by default), but every revision must still define both functions so
    `alembic downgrade` never dies with an AttributeError mid-incident."""
    for revision in script_directory.walk_revisions("base", "heads"):
        source = Path(revision.path).read_text()
        assert "def upgrade()" in source, f"{revision.revision} has no upgrade()"
        assert "def downgrade()" in source, f"{revision.revision} has no downgrade()"
