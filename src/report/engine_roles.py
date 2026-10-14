"""Each target engine's role in the architecture (owner, cache, search read model)."""

from __future__ import annotations

from typing import Any

_CACHE_ENGINES = {"elasticache", "memorydb"}
# Search engines are read models (#303): their data is synced from the engine
# that owns each table, so they never hold system-of-record data, never need a
# "data migration", and come after their owners in the wave plan.
_SEARCH_ENGINES = {"opensearch"}
# Only a relational engine can be the retained, source-compatible core.
_RELATIONAL_ENGINES = {"aurora", "aurora_mysql", "aurora_postgresql"}
SEARCH_READ_MODEL = "Search read model"

_ROLE_STROKE = {
    "Retained": "#1F7A3D",
    "Migration target": "#146EB4",
    "Cache layer": "#8B5CF6",
    SEARCH_READ_MODEL: "#2EA597",
    # Muted grey: an engine that was evaluated but serves nothing is not an active
    # component of the target architecture, and must not read as one in the diagram.
    "Evaluated": "#6B7280",
    "Assessed": "#6B7280",
}


def _engine_role(
    engine: str,
    recommended: set,
    schema_designs: dict[str, Any],
    workload: float | None = None,
) -> str:
    """Role of an engine in the target architecture.

    ``recommended_architecture.databases`` lists only net-new migration targets —
    it drops the retained relational core and cache engines because it filters on
    a ``schema_design_available`` flag that is False for both (defect (d) for the
    cache, which has a completed design the flag does not count; by design for a
    retained engine, which has no migration design). Roles are therefore derived
    from the engine kind and its design status, not from that list alone.

    An engine carrying **no workload** is ``Evaluated``: triage selected it, analysis
    scored it, and the assignment then routed nothing to it because another engine won
    every query. It is *not* ``Retained`` — retained means the engine still serves
    queries, which is true of the source relational core and false of an engine that
    won none. Calling it Retained told the customer to provision an engine holding
    nothing, and (via ``no_move`` in ``pptx_report``) pulled the Wave 1 confidence down
    to a floor set by an engine doing no work.

    Judged on workload rather than on the engine's name because the synthesis report
    carries no ``source_engine`` field. If every query migrated off the source engine it
    would read ``Evaluated`` rather than "retired" — rare, and Evaluated still claims
    nothing false.
    """
    if engine in _CACHE_ENGINES:
        return "Cache layer"
    if engine in _SEARCH_ENGINES and workload:
        # A read model whatever its design status: never "Retained" (#296/#303)
        return SEARCH_READ_MODEL
    if engine in recommended:
        return "Migration target"
    # Ordered deliberately: a cache engine or a real migration target keeps its role
    # whatever its workload, and this test precedes the status checks because the
    # ``skipped``/``not_available`` branch below is the one being corrected.
    if not workload:
        return "Evaluated"
    status = (schema_designs.get(engine) or {}).get("status")
    if status in ("not_available", "skipped"):
        # Retained means the source-compatible relational core; any other engine
        # carrying queries without a design is still a migration target
        return "Retained" if engine in _RELATIONAL_ENGINES else "Migration target"
    if status == "completed":
        return "Migration target"
    return "Assessed"
