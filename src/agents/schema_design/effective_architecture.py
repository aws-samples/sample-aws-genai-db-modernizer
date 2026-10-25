"""The effective engine set a schema design call is told about (#213).

A designer writes ``unsupported_patterns`` workarounds, trade-offs and notes that can
name another engine ("OpenSearch would serve this search"). Without knowing which
engines the effective (post-reality-check) assignment still runs, it can name one the
reality check consolidated away. Synthesis grounding (#202) then can only drop or
annotate that text. So every design call's analysis input carries the effective
architecture, computed the way synthesis computes it:

- ``engines``: the engines with an in-scope query in the assignment version, a cache
  engine included while it fronts one (#296);
- ``eliminated_engines``: each candidate engine no longer in ``engines``, with the
  engine that absorbed its work (``absorbed_by``, None when no one engine did), from
  ``synthesis_grounding.eliminated_engines`` and the same post-check moves synthesis
  passes it (#215).

The skills tell the designer to name only an engine in ``engines``.
"""

from __future__ import annotations

from src.storage.artifact_store import ArtifactStore

EFFECTIVE_ARCHITECTURE_KEY = "effective_architecture"


def effective_engines(assignment: dict) -> list[str]:
    """The engines with an in-scope query in ``assignment`` (cache engines included)."""
    return sorted(
        {
            str(engine)
            for qa in assignment.get("query_assignments") or []
            if isinstance(qa, dict) and qa.get("in_scope", True)
            for engine in (qa.get("assigned_engine"), qa.get("cache_engine"))
            if engine
        }
    )


def effective_architecture(
    store: ArtifactStore, database_name: str, job_id: str, assignment_version: int
) -> dict | None:
    """``{"engines": [...], "eliminated_engines": [{"engine", "absorbed_by"}, ...]}``.

    None for a legacy version 0 or a version whose assignment is missing: there is
    no effective assignment to describe, and the designer works as before.
    """
    # Imported here: the referee modules import schema-design helpers of their own.
    from src.agents.referee.synthesis_grounding import eliminated_engines
    from src.agents.referee.synthesis_handler import _moves_since_reality_check
    from src.storage.assignment_versioning import read_reality_check_output

    if assignment_version <= 0:
        return None
    key = f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
    if not store.exists(key):
        return None
    assignment = store.read_json(key)
    if not isinstance(assignment, dict):
        return None
    engines = effective_engines(assignment)
    reality_check = read_reality_check_output(store, database_name, job_id, assignment_version)
    eliminated = eliminated_engines(
        engines,
        reality_check,
        _moves_since_reality_check(store, database_name, job_id, reality_check, assignment),
    )
    return {
        "engines": engines,
        "eliminated_engines": [
            {"engine": engine, "absorbed_by": absorber}
            for engine, absorber in sorted(eliminated.items())
        ],
    }
