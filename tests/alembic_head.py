"""The current Alembic head revision, read from the real script directory.

Several PostgreSQL migration tests assert that ``alembic upgrade head`` lands
on the expected revision. Those assertions used to hardcode the literal
(``"0012"``), so EVERY new migration broke three unrelated test files that had
nothing to say about the new migration — a pure maintenance trap that adds no
safety: they are asserting "we reached head", not "head is specifically 0012".

Reading the head from ``alembic/versions`` keeps that assertion meaningful
(the upgrade really did reach the tip of the chain) while making it
self-maintaining. It also validates the chain itself as a side effect:
``ScriptDirectory.get_current_head()`` raises if the revisions do not form a
single unbranched chain, so importing this module is already a chain check.

The exact head value is pinned separately, on purpose, by
``tests/test_migration_chain.py`` — one deliberate place where bumping the
number is a conscious act, instead of eight incidental ones.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_head() -> str:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None, "alembic/versions has no head revision"
    return head


ALEMBIC_HEAD: str = _resolve_head()
