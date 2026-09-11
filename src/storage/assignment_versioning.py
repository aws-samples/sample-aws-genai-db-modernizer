"""Single source of truth for resolving assignment artifact versions (ADR-028).

Assignment artifacts are written under ``<db>/<job>/assignment/v<N>/assignment.json``
with a monotonically increasing ``N``. Historically at least six call sites each
re-derived "the latest version" with a slightly different `max(vN)` scan (and one
probed ``v10 -> v1`` by read). ADR-028 collapses them onto the two helpers here so
the resolution rule is stated once: the highest committed version in the lineage.

These helpers are duck-typed on the ``store`` argument: they only require
``list_prefix(prefix) -> Iterable[str]`` returning keys relative to the store
root, exactly as every ArtifactStore already provides. No storage type is
imported, so this stays a leaf module the whole codebase can depend on.

Callers that must never receive 0 (schema design and synthesis always operate on
at least the v1 the assessment core writes, per ADR-026) apply that "coerce to 1"
policy themselves; this module reports the raw truth (0 when nothing exists).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class _Lister(Protocol):
    def list_prefix(self, prefix: str) -> Iterable[str]: ...


def _assignment_prefix(database_name: str, job_id: str) -> str:
    return f"{database_name}/{job_id}/assignment/"


def assignment_artifact_path(database_name: str, job_id: str, version: int) -> str:
    """Return the artifact key for a specific assignment version."""
    return f"{_assignment_prefix(database_name, job_id)}v{version}/assignment.json"


def _existing_versions(store: _Lister, database_name: str, job_id: str) -> list[int]:
    """Return every assignment version number found under the prefix (unsorted).

    Parses the ``vN`` directory component of each listed key; ignores anything
    that is not a ``v<digits>`` segment (e.g. the legacy flat ``assignments.json``,
    which no writer produces but which may still exist in old jobs).
    """
    prefix = _assignment_prefix(database_name, job_id)
    versions: list[int] = []
    for key in store.list_prefix(prefix):
        parts = str(key).replace(prefix, "").split("/")
        if parts and parts[0].startswith("v"):
            try:
                versions.append(int(parts[0][1:]))
            except ValueError:
                continue
    return versions


def resolve_effective_assignment_version(store: _Lister, database_name: str, job_id: str) -> int:
    """Return the highest committed assignment version, or 0 when none exists.

    This is the one authoritative "which version is effective" rule (ADR-028).
    Returns 0 rather than raising when no versioned assignment is present; the
    ATX schema/synthesis path coerces that to 1 at its own call sites.
    """
    versions = _existing_versions(store, database_name, job_id)
    return max(versions) if versions else 0


def next_assignment_version(store: _Lister, database_name: str, job_id: str) -> int:
    """Return the version number a new assignment write should use (highest + 1).

    Starts at 1 when no assignment exists, so a fresh job's first write is v1.
    """
    return resolve_effective_assignment_version(store, database_name, job_id) + 1
