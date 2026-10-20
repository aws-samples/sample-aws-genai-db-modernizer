"""Scope an analysis decision trace to the queries one design call designs (#1060).

The analysis runs on the whole collection, so its decision trace covers every
query in the workload, including those assigned to other engines. A design
call (one group, one single pass, one cascade pass) must see the evidence for
its own queries and tables only: a trace for other queries tempts the
designer to model them.

Scoping is closed by default. Every key of the trace is either handled here
(scoped to the call's queries or tables) or listed in ``WORKLOAD_WIDE_KEYS``
as carrying no query or table. A key in neither is dropped, and
``tests/unit/agents/schema_design/test_group_decision_trace.py`` fails when
either analysis writes a key that is in neither list.
"""

from __future__ import annotations

from typing import Any

from src.tools.analysis.documentdb_analysis_tools import (
    _detect_unsupported_features,
    _detect_version_dependent_features,
)
from src.tools.sql.query_facts import source_engine_of

# Keys that name no query and no table: the trace's identity, contract version (a
# typed trace's, so the designer still checks it) and run status.
WORKLOAD_WIDE_KEYS = frozenset({"contract_version", "trace_version", "agent", "llm_advisor"})


def _names(tables: list[dict]) -> set[str]:
    """Every id and name a table is known by in the trace."""
    out: set[str] = set()
    for t in tables:
        out.update(x for x in (t.get("table_id"), t.get("table_name")) if x)
    return out


def scope_decision_trace(trace: dict[str, Any], collector: dict[str, Any]) -> dict[str, Any]:
    """The part of ``trace`` that concerns the queries and tables of ``collector``.

    ``collector`` is the collector output the design call receives (a group's,
    or the assignment-filtered one). Returns a new dict; ``trace`` is not
    changed. Per-query entries are kept for the call's queries, per-table
    entries for its tables, and the summary counts and the DocumentDB feature
    lists are recomputed from what is kept. Keys not handled here and not in
    ``WORKLOAD_WIDE_KEYS`` are dropped.
    """
    queries = (collector.get("queries") or {}).get("query_patterns") or []
    qids = {q.get("query_id") for q in queries}
    tables = _names((collector.get("database_schema") or {}).get("tables") or [])

    def of_queries(items: Any) -> list:
        return [i for i in items or [] if i.get("query_id") in qids]

    def of_tables(items: Any) -> list:
        return [i for i in items or [] if i.get("table_id") in tables]

    def of_pair(items: Any) -> list:
        return [
            i
            for i in items or []
            if i.get("parent_table") in tables and i.get("child_table") in tables
        ]

    def pattern_summary(p: dict) -> dict:
        # The counts of queries and calls cover the whole workload: not kept.
        kept = {
            k: v
            for k, v in p.items()
            if k not in ("queries_matched_count", "total_calls_per_second")
        }
        kept["tables_involved"] = [t for t in p.get("tables_involved") or [] if t in tables]
        return kept

    scoped: dict[str, Any] = {k: v for k, v in trace.items() if k in WORKLOAD_WIDE_KEYS}
    handlers = {
        "query_matches": of_queries,
        "pattern_summaries": lambda xs: [
            pattern_summary(p) for p in xs or [] if set(p.get("tables_involved") or []) & tables
        ],
        "workload_classifications": of_tables,
        "recommendation_derivations": of_tables,
        "embedding_candidates": of_pair,
        "denormalization_strategies": of_pair,
        "polymorphic_tables": lambda xs: [t for t in xs or [] if t in tables],
        "table_groups": lambda xs: [
            g
            for g in (
                {**g, "tables": [t for t in g.get("tables") or [] if t in tables]} for g in xs or []
            )
            if len(g["tables"]) > 1
        ],
        "relationships": lambda xs: [
            r
            for r in xs or []
            if {
                r.get(k) for k in ("from_table", "to_table", "junction_table", "table_a", "table_b")
            }
            - {None, ""}
            <= tables
        ],
        "timestamp_columns": lambda ts: {
            **(ts or {}),
            "name_fallbacks": of_queries((ts or {}).get("name_fallbacks")),
        },
    }
    for key, handler in handlers.items():
        if key in trace:
            scoped[key] = handler(trace[key])

    if isinstance(trace.get("documentdb_compatibility"), dict):
        engine = source_engine_of(collector)
        scoped["documentdb_compatibility"] = {
            **trace["documentdb_compatibility"],
            "version_dependent_features_detected": _detect_version_dependent_features(
                queries, engine
            ),
            "unsupported_features_detected": _detect_unsupported_features(queries, engine),
        }

    if isinstance(trace.get("summary"), dict):
        summary = dict(trace["summary"])
        summary["queries_analyzed"] = len(queries)
        matches = scoped.get("query_matches")
        if matches is not None:
            matched = [
                m for m in matches if m.get("matched_patterns") or m.get("matched_anti_patterns")
            ]
            summary["queries_matched"] = len(matched)
            summary["queries_unmatched"] = len(queries) - len(matched)
            summary["anti_patterns_detected"] = len(
                {a for m in matches for a in m.get("matched_anti_patterns") or []}
            )
        if "pattern_summaries" in scoped:
            summary["patterns_detected"] = len(scoped["pattern_summaries"])
        if "workload_classifications" in scoped:
            kinds = [c.get("workload_type") for c in scoped["workload_classifications"]]
            summary["tables_search"] = kinds.count("SEARCH")
            summary["tables_timeseries"] = kinds.count("TIMESERIES")
            summary["tables_not_suitable"] = kinds.count("NOT_SUITABLE")
        scoped["summary"] = summary
    return scoped
