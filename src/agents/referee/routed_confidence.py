"""Routed confidence: how well each engine fits the work it was actually given (#152).

``analysis_confidence`` averages an engine's suitability over every table it
analyzed, so OpenSearch reads 2% on discourse (311 tables, almost none of them
search material) whatever the queries routed to it are. This module measures the
routed workload instead:

- **Owner engines**: the mean per-query fit of the in-scope queries the effective
  assignment routes to the engine (``assigned_engine``, so a customer override
  counts for the engine the query now has). The fit is the resolver's own score
  (``query_scoring.score_query_for_engine``, #490/#549): the hard exclusion and
  capability gates, then table confidence over the query's ``tables_accessed``
  minus the resolver's anti-pattern penalty for most engines, but for
  ``PATTERN_ONLY_ENGINES`` (OpenSearch, #475) the query's own matched pattern
  score (or 0 when it has none) instead -- table confidence never applies to
  those engines, since it reflects the table as a whole, not this query. A
  gated engine (``EngineScore.score is None``) reports 0 here, same as a real
  score of 0 would -- a routed query is already on this engine in the
  assignment, so a gate firing here says the gate and the assignment disagree,
  not that the number is meaningless. The per-query ``confidence`` stored on
  the assignment is not used: Reality Check does not update it when it moves a
  query, so it would describe the engine the query left.
- **Cache layer** (owns no query, #296): the mean fit of the in-scope reads it
  fronts (``cache_engine``), the same resolver score computed against the cache
  engine's own analysis and ``workload_analysis`` (its own detected patterns and
  anti-patterns, same as any other engine) -- not the overlay's ``cache_pattern``
  classification, which only decides who the cache fronts (``cache_overlay.py``),
  not how well it fits what it fronts. Basis ``cached_reads`` says which of the
  two a figure is.

**Evidence.** For most engines, a fit is backed by table-level evidence only when a
query touches a real source table the engine's analysis rated; a query whose
``tables_accessed`` is the collector's ``"unknown"`` placeholder (or any name that
is not a source table) falls back to the resolver's own last-resort average (or
0), so its fit is not real per-query evidence. ``evidence`` is
``"table"`` when every routed query is table-backed, ``"partial"`` when some are,
``"signal_only"`` when none is; ``unbacked_queries`` counts the rest. For
``PATTERN_ONLY_ENGINES`` (OpenSearch), table recommendations exist but the
resolver's own scorer never reads them for this engine (see above), so "backed"
instead means the query matches one of the engine's own detected patterns --
real, query-level evidence -- and ``evidence`` is ``"pattern"`` (not ``"table"``)
when every routed query matches one. Pseudo-tables never count in ``tables``.

An engine with no routed query has no routed confidence (``None``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.reality_check import _build_query_signals
from src.shared.engine_capabilities import PATTERN_ONLY_ENGINES
from src.shared.signal_labels import WORKLOAD_CHARACTERISTIC_SIGNALS

BASIS_OWNED = "owned_queries"
BASIS_CACHED = "cached_reads"

EVIDENCE_TABLE = "table"
# A ``PATTERN_ONLY_ENGINES`` engine's own matched-pattern evidence (#475
# review, finding 4) -- never ``EVIDENCE_TABLE``, since the resolver's own
# scorer never reads a table recommendation for these engines.
EVIDENCE_PATTERN = "pattern"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_SIGNAL_ONLY = "signal_only"

# The collector's placeholder when it cannot parse a query's tables.
PSEUDO_TABLES = frozenset({"", "unknown"})

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
    # #490/#549: the one shared scoring context -- the fit below is the
    # resolver's own score (``query_scoring.score_query_for_engine``), not
    # this module's former private copy of the reality check's own,
    # separately drifting ``_engine_fit_score``. A synthetic ``collector``
    # (just the query patterns) covers ``from_pipeline``'s fallback capability
    # detection for a ``triage`` with no ``query_capabilities`` key at all;
    # every real pipeline ``triage.json`` has one, so this is only exercised
    # by a legacy or hand-built fixture.
    scoring_ctx = ScoringContext.from_pipeline(
        outputs, triage, {"queries": {"query_patterns": list(query_map.values())}}
    )

    def real_tables(qid: str) -> set[str]:
        # The fit reads tables_accessed; so do the counts, minus the pseudo-tables
        names = set(query_map.get(qid, {}).get("tables_accessed") or []) - PSEUDO_TABLES
        return names & known if known is not None else names

    result: dict[str, RoutedFit] = {}
    for engine in analysis_outputs:
        if engine in CACHE_OVERLAY_ENGINES:
            basis = BASIS_CACHED
            routed = [qa for qa in qas if qa.get("cache_engine") == engine]
        else:
            basis = BASIS_OWNED
            routed = [qa for qa in qas if qa.get("assigned_engine") == engine]
        if not routed:
            result[engine] = RoutedFit(None, basis, 0, 0)
            continue

        ids = [qa["query_id"] for qa in routed]
        tables = set().union(*(real_tables(qid) for qid in ids))
        if engine in PATTERN_ONLY_ENGINES:
            # #475 review, finding 4: the resolver's own scorer never
            # reads a table recommendation for this engine, so a table
            # existing is not evidence -- the query matching one of the
            # engine's own detected patterns is.
            pattern_matched_qids = {
                qid
                for pattern in outputs[engine].get("workload_analysis", {}).get("patterns_detected")
                or []
                for qid in (pattern.get("query_ids") or [])
            }
            backed_label = EVIDENCE_PATTERN
            unbacked = sum(1 for qid in ids if qid not in pattern_matched_qids)
        else:
            rated = {r.get("table_id") for r in outputs[engine].get("table_recommendations") or []}
            backed_label = EVIDENCE_TABLE
            unbacked = sum(1 for qid in ids if not (real_tables(qid) & rated))
        evidence = (
            backed_label
            if unbacked == 0
            else EVIDENCE_SIGNAL_ONLY if unbacked == len(ids) else EVIDENCE_PARTIAL
        )

        fits = []
        for qa in routed:
            engine_score = score_query_for_engine(
                query_map.get(qa["query_id"], {}), engine, scoring_ctx
            )
            fits.append(engine_score.score if engine_score.score is not None else 0.0)
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
