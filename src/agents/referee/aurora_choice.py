"""Pick one Aurora engine when both Aurora engines are candidates (#288).

Shared by assignment resolution and the Reality Check correction passes so they
agree. The engine matching the source database's dialect wins (a MySQL source
goes to Aurora MySQL); otherwise the engine with more queries, with Aurora
PostgreSQL as the tie-break, as Reality Check's absorption pass does. Never the
first element of a set: set order depends on PYTHONHASHSEED.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

AURORA_ENGINES = frozenset({"aurora_postgresql", "aurora_mysql"})


def source_database_engine(collector_output: Mapping) -> str:
    """The collector's source database engine (e.g. ``"mysql"``), lower-cased."""
    metadata = collector_output.get("metadata") or {}
    return str((metadata.get("source_database") or {}).get("engine") or "").lower()


def source_database_version(collector_output: Mapping) -> str | None:
    """The collector's source database version (e.g. ``"8.0.45"``), or ``None`` (#321).

    Used only to state what the migration-waves builder checked for the
    Aurora wave's homogeneity note -- never to infer a feature or extension
    gap the collector itself did not report.
    """
    metadata = collector_output.get("metadata") or {}
    version = (metadata.get("source_database") or {}).get("version")
    return str(version) if version else None


def pick_aurora_engine(
    candidates: Iterable[str],
    source_engine: str | None = None,
    query_counts: Mapping[str, int] | None = None,
) -> str | None:
    """The Aurora engine to use among ``candidates``, or ``None`` if there is none.

    ``source_engine`` is the source database engine (``"mysql"``, ``"postgresql"``,
    ...); ``query_counts`` maps engine to the queries it already serves.
    """
    pool = sorted(AURORA_ENGINES.intersection(candidates))
    if not pool:
        return None
    matched = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())
    if matched in pool:
        return matched
    counts = query_counts or {}
    return min(pool, key=lambda e: (-counts.get(e, 0), e != "aurora_postgresql"))
