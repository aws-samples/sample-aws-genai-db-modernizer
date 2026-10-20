"""Each kept engine's cost, sized for the queries the final assignment gives it (#1195).

The analysis estimates are made before routing, so each one sizes its engine for the
whole collection: on wordpress Aurora MySQL was billed as a db.r6g.xlarge for all 222
calls a second although it serves 67 of the 107 queries. ``sized_cost_estimate`` re-runs
the engine's own estimator, unchanged, on the queries the effective assignment gives it
(in scope only) and the tables those queries touch. The cache layer is sized from the
reads its overlay fronts. An owner engine still pays for the reads the cache fronts: a
cache-aside miss reaches it, and the miss rate is unknown. Wave 1 moves the whole source
database, so the tables no in-scope query reads (``unqueried_tables``) are stored on wave
1's Aurora engine (#1378).

The routing score never reads this: its cost dimension is its own per-query model
(``query_cost``, ADR-031 Decision 5), so the TCO can change without moving a query.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES
from src.agents.referee.table_resolution import SourceScope, is_engine_system_object
from src.contracts.analysis_output import CostEstimate
from src.shared.catalog.prices import price_basis
from src.shared.engine_names import display_engine
from src.shared.source_cache import CollectionCache
from src.tools.analysis.cost_estimators import COST_ESTIMATORS

ASSIGNED_QUERIES = "assigned_queries"
CACHE_OVERLAY = "cache_overlay"
NO_IN_SCOPE_QUERIES = "no_in_scope_queries"

# The TCO assumption stated with any cache layer sized on its fronted reads: the owner
# engines are billed for those reads too (a cache-aside miss reaches them; the miss rate
# is unknown, so every read is counted as a miss).
CACHE_MISS_ASSUMPTION = (
    "Reads the cache layer fronts are also billed on their owner engines, as if every "
    "one missed the cache (conservative)"
)

# The statement types every estimator counts as reads and as writes.
_READS = frozenset({"SELECT"})
_WRITES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})


def _in_scope(assignment: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    rows = (assignment or {}).get("query_assignments") or []
    return [qa for qa in rows if isinstance(qa, Mapping) and qa.get("in_scope", True)]


def engine_queries(assignment: Mapping[str, Any] | None, engine: str) -> tuple[str, list[str]]:
    """The basis and the in-scope query ids an engine is sized for.

    An engine that owns queries is sized for them (and any reads it also fronts); an
    engine that owns none and fronts reads is the cache layer, sized for those reads.
    """
    owned: list[str] = []
    fronted: list[str] = []
    for qa in _in_scope(assignment):
        qid = str(qa.get("query_id") or "")
        if qa.get("assigned_engine") == engine:
            owned.append(qid)
        elif qa.get("cache_engine") == engine and not qa.get("cache_dropped"):
            fronted.append(qid)
    if owned:
        return ASSIGNED_QUERIES, owned + fronted
    cache_layer = bool(fronted) or engine in CACHE_OVERLAY_ENGINES
    return (CACHE_OVERLAY if cache_layer else ASSIGNED_QUERIES), fronted


def _touched_tables(assignment: Mapping[str, Any] | None, qids: Iterable[str]) -> set[str]:
    wanted = set(qids)
    return {
        str(t)
        for qa in _in_scope(assignment)
        if qa.get("query_id") in wanted
        for t in qa.get("source_tables") or []
    }


def unqueried_tables(
    collector: Mapping[str, Any], assignment: Mapping[str, Any] | None
) -> list[Mapping[str, Any]]:
    """The collected tables no in-scope query reads, which wave 1 still moves (#1378).

    Engine-internal objects stay behind, as in wave 1 (``is_engine_system_object``).
    Empty with no assignment (the analysis estimates already cover every table) and with
    no query in scope (every engine is sized to nothing, #1195)."""
    in_scope = _in_scope(assignment)
    if not in_scope:
        return []
    read = {str(t) for qa in in_scope for t in qa.get("source_tables") or []}
    source = SourceScope.from_collector(collector)
    return [
        t
        for t in (collector.get("database_schema") or {}).get("tables") or []
        if isinstance(t, Mapping)
        and t.get("table_id")
        and str(t["table_id"]) not in read
        and not is_engine_system_object(str(t["table_id"]), source)
    ]


def unqueried_assumption(summary: Mapping[str, Any]) -> str:
    """The TCO assumption line that says which engine stores the tables no in-scope query
    reads (#1378), from ``tco_analysis.unqueried_tables``."""
    n = int(summary.get("table_count") or 0)
    gb = float(summary.get("storage_gb") or 0.0)
    size = f"{gb:,.2f}".rstrip("0").rstrip(".") if gb >= 0.01 else "<0.01"
    what = f"{n} source {'table' if n == 1 else 'tables'} no in-scope query reads ({size} GB)"
    verb, it = ("is", "it") if n == 1 else ("are", "them")
    engine = summary.get("stored_on")
    if engine:
        return f"{what} {verb} stored on {display_engine(str(engine))}, where wave 1 moves {it}"
    return (
        f"{what} {verb} stored on no engine in this estimate: wave 1 moves {it} to no engine "
        "the estimate prices"
    )


def storage_gb(tables: Iterable[Mapping[str, Any]]) -> float:
    """The tables' source size, in GB."""
    return round(sum(float(t.get("size_mb") or 0.0) for t in tables) / 1024, 4)


def _sizing(
    basis: str,
    queries: list[Mapping[str, Any]],
    tables: list[Mapping[str, Any]],
    unqueried: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    unqueried = unqueried or []

    def cps(q: Mapping[str, Any]) -> float:
        return float(q.get("calls_per_second") or 0.0)

    def kind(q: Mapping[str, Any]) -> str:
        return str(q.get("query_type") or "").upper()

    return {
        "basis": basis,
        "sized_query_count": len(queries),
        "calls_per_second": round(sum(cps(q) for q in queries), 2),
        "read_calls_per_second": round(sum(cps(q) for q in queries if kind(q) in _READS), 2),
        "write_calls_per_second": round(sum(cps(q) for q in queries if kind(q) in _WRITES), 2),
        "sized_table_count": len(tables),
        # #1378: the tables no in-scope query reads that this engine also stores.
        "unqueried_table_count": len(unqueried),
        "storage_gb": storage_gb([*tables, *unqueried]),
    }


def sized_cost_estimate(
    engine: str,
    collector: Mapping[str, Any],
    assignment: Mapping[str, Any] | None,
    unqueried: list[Mapping[str, Any]] | None = None,
) -> tuple[CostEstimate, dict[str, Any]] | None:
    """The engine's own estimate over its assigned queries, and the basis it was sized on.

    ``unqueried`` (#1378) is the tables no in-scope query reads, given for the engine
    that stores them (wave 1's Aurora engine): they are priced with its own tables.

    None when there is no assignment to size from or no estimator for the engine; the
    caller then keeps the analysis estimate. An assignment with queries but none in scope
    (the customer took every one out of the iteration) sizes every engine to nothing:
    $0, basis ``no_in_scope_queries``, never the whole-collection figure.
    """
    estimator = COST_ESTIMATORS.get(engine)
    rows = (assignment or {}).get("query_assignments") or []
    if estimator is None or not rows:
        return None
    if not _in_scope(assignment):
        nothing = CostEstimate(
            monthly_cost_usd=0.0,
            cost_components={"pricing_mode": "on-demand"},
            pricing_assumptions=["No query of the assignment is in scope: nothing to size."],
        )
        return nothing, _sizing(NO_IN_SCOPE_QUERIES, [], [])
    basis, qids = engine_queries(assignment, engine)
    wanted = set(qids)
    patterns = (collector.get("queries") or {}).get("query_patterns") or []
    queries = [q for q in patterns if isinstance(q, Mapping) and q.get("query_id") in wanted]
    touched = _touched_tables(assignment, qids)
    schema = collector.get("database_schema") or {}
    tables = [
        t
        for t in schema.get("tables") or []
        if isinstance(t, Mapping) and str(t.get("table_id") or "") in touched
    ]
    stored = list(unqueried or [])
    # The collection's buffer-cache figure is decided on the whole collection, as routing
    # does (#1197), not again on this engine's few queries.
    subset = CollectionCache.decided_in(
        collector,
        {
            **collector,
            "database_schema": {**schema, "tables": [*tables, *stored]},
            "queries": {**(collector.get("queries") or {}), "query_patterns": queries},
        },
    )
    estimate = estimator(subset, str(price_basis()["region"]))
    return estimate, _sizing(basis, queries, tables, stored)
