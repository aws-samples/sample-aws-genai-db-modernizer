"""Routed confidence: how well each engine fits the work it was actually given (#152).

``analysis_confidence`` averages an engine's suitability over every table it
analyzed, so OpenSearch reads 2% on discourse (311 tables, almost none of them
search material) while the 3 text-search queries routed to it fit it well. This
module measures the routed workload instead:

- **Owner engines**: the mean per-query fit of the in-scope queries the effective
  assignment routes to the engine (``assigned_engine``). The fit is Reality Check's
  ``_engine_fit_score`` (table confidence over the query's tables, plus or minus the
  capability-signal bonus), recomputed here. The per-query ``confidence`` stored on
  the assignment is not used: Reality Check does not update it when it moves a
  query, so it would describe the engine the query left.
- **Cache layer** (owns no query, #296): the mean fit of the in-scope reads it
  fronts (``cache_engine``), counting only the cache hint signals (session lookup,
  top-N). The owner's capability signals are not counted: a cached key-value
  lookup is still answered by its owner on a miss, and the overlay's eligibility
  rules already require a cacheable shape, so penalising the cache for lacking
  ``key_value_lookup`` would measure the wrong thing. What is left is how well the
  cache analysis rates the tables of the cached reads, plus the bonus for a cache
  pattern. Basis ``cached_reads`` says which of the two a figure is.

An engine with no routed query has no routed confidence (``None``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.agents.referee.cache_overlay import CACHE_HINT_SIGNALS, CACHE_OVERLAY_ENGINES
from src.agents.referee.reality_check import (
    ENGINE_CAPABILITIES,
    SIGNAL_TO_CAPABILITY,
    _build_query_signals,
    _engine_fit_score,
)
from src.shared.signal_labels import WORKLOAD_CHARACTERISTIC_SIGNALS

BASIS_OWNED = "owned_queries"
BASIS_CACHED = "cached_reads"


@dataclass(frozen=True)
class RoutedFit:
    """The routed workload of one engine and how well it fits it."""

    confidence: int | None
    basis: str
    queries: int
    tables: int
    # Owners: the triage signal that leads the routed queries. Cache layer: the
    # most common ``cache_pattern`` of the reads it fronts (``point_lookup``, ...).
    lead_signal: str | None = None
    lead_count: int = 0


def _lead_signal(
    engine: str, query_ids: list[str], query_signals: Mapping[str, list[str]]
) -> tuple[str | None, int]:
    """The pattern signal that best names an engine's routed queries, with its count.

    Workload characteristics (low-frequency reads, ...) never lead. Among the rest,
    a signal whose capability the engine has comes first (that is why the queries
    are there), then the most frequent, then the name.
    """
    counts: Counter[str] = Counter()
    for qid in query_ids:
        for sig in set(query_signals.get(qid, [])):
            if sig and sig not in WORKLOAD_CHARACTERISTIC_SIGNALS:
                counts[sig] += 1
    if not counts:
        return None, 0
    caps = ENGINE_CAPABILITIES.get(engine, set())
    matched = {s for s in counts if SIGNAL_TO_CAPABILITY.get(s) in caps}
    sig = min(counts, key=lambda s: (s not in matched, -counts[s], s))
    return sig, counts[sig]


def routed_fits(
    assignment: Mapping | None,
    triage: Mapping,
    query_patterns: Iterable[Mapping],
    analysis_outputs: Mapping[str, Mapping],
) -> dict[str, RoutedFit]:
    """``{engine: RoutedFit}`` for every engine in ``analysis_outputs``; {} without assignment."""
    if not assignment:
        return {}
    qas = [qa for qa in assignment.get("query_assignments") or [] if qa.get("in_scope", True)]
    query_map = {str(q.get("query_id")): dict(q) for q in query_patterns}
    query_signals = _build_query_signals(list(triage.get("signals") or []))
    outputs = {e: dict(a or {}) for e, a in analysis_outputs.items()}

    result: dict[str, RoutedFit] = {}
    for engine in analysis_outputs:
        if engine in CACHE_OVERLAY_ENGINES:
            basis = BASIS_CACHED
            routed = [qa for qa in qas if qa.get("cache_engine") == engine]
            signals = {
                qa["query_id"]: [
                    s for s in query_signals.get(qa["query_id"], []) if s in CACHE_HINT_SIGNALS
                ]
                for qa in routed
            }
        else:
            basis = BASIS_OWNED
            routed = [qa for qa in qas if qa.get("assigned_engine") == engine]
            signals = query_signals
        ids = [qa["query_id"] for qa in routed]
        tables = {
            t
            for qa in routed
            for t in (
                query_map.get(qa["query_id"], {}).get("tables_accessed")
                or qa.get("source_tables")
                or []
            )
        }
        if not routed:
            result[engine] = RoutedFit(None, basis, 0, 0)
            continue
        fits = [_engine_fit_score(engine, qa, signals, query_map, outputs) for qa in routed]
        if basis == BASIS_CACHED:
            # The cache is named by the shape of the reads it fronts (point lookup, ...)
            patterns = Counter(str(qa.get("cache_pattern") or "") for qa in routed)
            patterns.pop("", None)
            lead, lead_n = (
                min(patterns.items(), key=lambda kv: (-kv[1], kv[0])) if patterns else (None, 0)
            )
        else:
            lead, lead_n = _lead_signal(engine, ids, query_signals)
        result[engine] = RoutedFit(
            confidence=round(sum(fits) / len(fits)),
            basis=basis,
            queries=len(routed),
            tables=len(tables),
            lead_signal=lead,
            lead_count=lead_n,
        )
    return result
