"""Every agent type needs a storage directory, and that directory must be writable.

Three lists have to agree for a phase to start at all:

  1. ``_AGENTS`` in ``atx_entrypoint.py`` — the agent type and its storage dir
  2. the ``mkdir`` in ``Dockerfile.atx``
  3. the ownership fix that follows, since the container runs as non-root

The third was a hand-maintained repetition of the second, and adding six
schema_* directories to one and not the other shipped a broken image: those
containers started as ``appuser`` against root-owned directories and died with
``PermissionError`` on ``/tmp/<agent>_agent/queue`` while the SDK constructed its
request queue. The runtime never left ``INVOKING``, so the orchestrator reported a
60-second dispatch timeout — a symptom that points away from the cause.

The Dockerfile now derives ownership from the directories that exist rather than
repeating the list. These tests pin what remains: that every registered agent has
a directory, and that the Dockerfile creates exactly those.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.atx_orchestrator.atx_entrypoint import _AGENTS

DOCKERFILE = Path(__file__).resolve().parents[3] / "src/atx_orchestrator/Dockerfile.atx"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text()


def _created_dirs(dockerfile: str) -> set[str]:
    """Agent storage dirs the Dockerfile creates.

    Scoped to the text before ``useradd`` rather than to a regex over the mkdir
    block: the block spans backslash continuations and comments, and a
    block-shaped pattern silently matched nothing when the file was reformatted.
    Everything before ``useradd`` is where directories are created.
    """
    head = dockerfile[: dockerfile.index("useradd")]
    return set(re.findall(r"/tmp/[a-z0-9_]+_agent\b", head))


class TestStorageDirectories:
    def test_every_agent_type_has_a_distinct_directory(self) -> None:
        dirs = list(_AGENTS.values())
        assert len(dirs) == len(set(dirs)), "two agent types share a storage directory"

    def test_dockerfile_creates_a_directory_for_every_agent_type(self, dockerfile: str) -> None:
        """A missing directory fails at container start, not at import."""
        missing = sorted(set(_AGENTS.values()) - _created_dirs(dockerfile))
        assert not missing, f"agent types with no mkdir in Dockerfile.atx: {missing}"

    def test_dockerfile_creates_no_orphan_directories(self, dockerfile: str) -> None:
        """An extra directory means an agent type was removed and this was not."""
        orphans = sorted(_created_dirs(dockerfile) - set(_AGENTS.values()))
        assert not orphans, f"directories created for no agent type: {orphans}"


class TestOwnershipIsDerivedNotRepeated:
    """Guards the fix, not just the symptom.

    A second hand-maintained list would reintroduce the original defect, so assert
    the chown discovers directories rather than naming them.
    """

    def test_chown_does_not_enumerate_agent_directories(self, dockerfile: str) -> None:
        chown_block = dockerfile[dockerfile.index("useradd") :]
        chown_block = chown_block[: chown_block.index("USER appuser")]
        enumerated = re.findall(r"/tmp/[a-z0-9_]+_agent\b", chown_block)
        assert not enumerated, (
            "chown enumerates agent directories again: "
            f"{sorted(set(enumerated))}. Derive them instead — a repeated list is "
            "what shipped the PermissionError."
        )

    def test_chown_covers_agent_directories_by_pattern(self, dockerfile: str) -> None:
        chown_block = dockerfile[dockerfile.index("useradd") :]
        chown_block = chown_block[: chown_block.index("USER appuser")]
        assert "_agent" in chown_block and "chown -R appuser:appuser" in chown_block, (
            "chown no longer appears to cover the agent directories; containers run "
            "as appuser and the SDK creates <dir>/queue/requests at startup"
        )
