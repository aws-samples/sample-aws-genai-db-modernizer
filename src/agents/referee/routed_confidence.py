"""Routed confidence: how well each engine fits the work it was actually given (#152).

``analysis_confidence`` averages an engine's suitability over every table it
analyzed, so OpenSearch reads 2% on discourse (311 tables, almost none of them
search material) whatever the queries routed to it are. This module measures the
routed workload instead:

- **Owner engines**: the mean per-query fit of the in-scope queries the effective
  assignment routes to the engine (``assigned_engine``, so a customer override
  counts for the engine the query now has). The fit is Reality Check's
  ``_engine_fit_score`` (table confidence over the query's ``tables_accessed``,
  plus or minus the capability-signal bonus), recomputed here. The per-query
  ``confidence`` stored on the assignment is not used: Reality Check does not
  update it when it moves a query, so it would describe the engine the query left.
- **Cache layer** (owns no query, #296): the mean fit of the in-scope reads it
  fronts (``cache_engine``). The table part is the cache analysis's confidence for
  the tables of the cached reads. The bonus comes from the overlay's own
  classification of each read (``qa.cache_pattern``): a ``top_n`` read earns the
  sorted-set bonus and a ``session_lookup`` the session-store bonus; a
  ``point_lookup`` or ``reference_read`` earns none. Triage signals are not used
  for the cache: ``leaderboard_pattern`` also fires on reads the overlay classified
  as point lookups, and the owner's capability signals (``key_value_lookups``, ...)
  would penalise the cache for a capability the owner provides on a miss.
  Basis ``cached_reads`` says which of the two a figure is.

**Evidence.** A fit is backed by table-level evidence only when a query touches a
real source table the engine's analysis rated. A query whose ``tables_accessed`` is
the collector's ``"unknown"`` placeholder (or any name that is not a source table)
falls back to Reality Check's basic CRUD baseline plus the signal bonus, so its fit
is a constant, not a measurement. ``evidence`` is ``"table"`` when every routed
query is table-backed, ``"partial"`` when some are, ``"signal_only"`` when none is;
``unbacked_queries`` counts the rest. Pseudo-tables never count in ``tables``.

An engine with no routed query has no routed confidence (``None``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES
from src.agents.referee.reality_check import _build_query_signals, _engine_fit_score
from src.shared.signal_labels import WORKLOAD_CHARACTERISTIC_SIGNALS

BASIS_OWNED = "owned_queries"
BASIS_CACHED = "cached_reads"

EVIDENCE_TABLE = "table"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_SIGNAL_ONLY = "signal_only"

# The collector's placeholder when it cannot parse a query's tables.
PSEUDO_TABLES = frozenset({"", "unknown"})

# The overlay's cache patterns that earn the cache bonus, as the signal whose
# capability ElastiCache has (leaderboard_simple / session_store, see reality_check).
CACHE_PATTERN_BONUS_SIGNAL: dict[str, str] = {
    "top_n": "leaderboard_pattern",
    "session_lookup": "session_store",
}

# A signal "leads" a routed workload only as a strict plurality covering at least
# this share of the routed queries; otherwise the rationale names no lead.
LEAD_MIN_SHARE = 0.25


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
    evidence: str | None = None
    unbacked_queries: int = 0


def _lead(counts: Counter[str], n_queries: int) -> tuple[str | None, int]:
    """The strict-plurality item covering at least LEAD_MIN_SHARE, else ``(None, 0)``."""
    if not counts or not n_queries:
        return None, 0
    ranked = counts.most_common()
    top, n = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == n:
        return None, 0
    if n / n_queries < LEAD_MIN_SHARE:
        return None, 0
    return top, n


def _lead_signal(
    query_ids: list[str], query_signals: Mapping[str, list[str]]
) -> tuple[str | None, int]:
    """The pattern signal that leads an engine's routed queries, with its count.

    Workload characteristics (low-frequency reads, ...) never lead.
    """
    counts: Counter[str] = Counter()
    for qid in query_ids:
        for sig in set(query_signals.get(qid, [])):
            if sig and sig not in WORKLOAD_CHARACTERISTIC_SIGNALS:
                counts[sig] += 1
    return _lead(counts, len(query_ids))


def routed_fits(
    assignment: Mapping | None,
    triage: Mapping,
    query_patterns: Iterable[Mapping],
    analysis_outputs: Mapping[str, Mapping],
    source_tables: Iterable[str] | None = None,
) -> dict[str, RoutedFit]:
    """``{engine: RoutedFit}`` for every engine in ``analysis_outputs``; {} without assignment.

    ``source_tables`` (the collector's table ids) decides which names in
    ``tables_accessed`` are real tables; without it only the placeholders are
    dropped.
    """
    if not assignment:
        return {}
    qas = [qa for qa in assignment.get("query_assignments") or [] if qa.get("in_scope", True)]
    query_map = {str(q.get("query_id")): dict(q) for q in query_patterns}
    query_signals = _build_query_signals(list(triage.get("signals") or []))
    outputs = {e: dict(a or {}) for e, a in analysis_outputs.items()}
    known = set(source_tables) if source_tables is not None else None

    def real_tables(qid: str) -> set[str]:
        # The fit reads tables_accessed; so do the counts, minus the pseudo-tables
        names = set(query_map.get(qid, {}).get("tables_accessed") or []) - PSEUDO_TABLES
        return names & known if known is not None else names

    result: dict[str, RoutedFit] = {}
    for engine in analysis_outputs:
        if engine in CACHE_OVERLAY_ENGINES:
            basis = BASIS_CACHED
            routed = [qa for qa in qas if qa.get("cache_engine") == engine]
            bonus = {
                qa["query_id"]: CACHE_PATTERN_BONUS_SIGNAL.get(str(qa.get("cache_pattern") or ""))
                for qa in routed
            }
            signals = {qid: [sig] if sig else [] for qid, sig in bonus.items()}
        else:
            basis = BASIS_OWNED
            routed = [qa for qa in qas if qa.get("assigned_engine") == engine]
            signals = query_signals
        if not routed:
            result[engine] = RoutedFit(None, basis, 0, 0)
            continue

        rated = {r.get("table_id") for r in outputs[engine].get("table_recommendations") or []}
        ids = [qa["query_id"] for qa in routed]
        tables = set().union(*(real_tables(qid) for qid in ids))
        unbacked = sum(1 for qid in ids if not (real_tables(qid) & rated))
        evidence = (
            EVIDENCE_TABLE
            if unbacked == 0
            else EVIDENCE_SIGNAL_ONLY if unbacked == len(ids) else EVIDENCE_PARTIAL
        )

        fits = [_engine_fit_score(engine, qa, signals, query_map, outputs) for qa in routed]
        if basis == BASIS_CACHED:
            # The cache is named by the shape of the reads it fronts (point lookup, ...)
            patterns = Counter(str(qa.get("cache_pattern") or "") for qa in routed)
            patterns.pop("", None)
            lead, lead_n = _lead(patterns, len(routed))
        else:
            lead, lead_n = _lead_signal(ids, query_signals)
        result[engine] = RoutedFit(
            confidence=round(sum(fits) / len(fits)),
            basis=basis,
            queries=len(routed),
            tables=len(tables),
            lead_signal=lead,
            lead_count=lead_n,
            evidence=evidence,
            unbacked_queries=unbacked,
        )
    return result
