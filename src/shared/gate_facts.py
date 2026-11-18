# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The approval-gate figures, computed once from the gate's status data (#1307, ADR-032).

The gate shows each engine's query count before and after the reality check and its share of
the total. ``src.shared.gate_status`` builds the status data for the assignment version the
gate states. ``scripts/print_summary.py`` prints the block from this, and the block's
renderer (``src.report.printed_summary``) only formats it; the assignments API returns the
same figures as ``gate_facts`` for the UI gate (#2326).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.shared.cache_change import cache_change_sentence
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.target_databases import engine_scope_display_order


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# How many cache candidates the gate names; the rest are counted ("and N more").
CACHE_READS_SHOWN = 5

# The most table names a migration-wave gate prints for one list (#551, one rule since
# #2171): synthesis lists "the other k tables" only up to this many, and a rendered gate
# leaves a longer list of Migration map rows to its count and a pointer to the map,
# which lists each table with its owner and readers.
GATE_LIST_MAX = 8


def cache_candidate_reads(
    query_assignments: Iterable[Mapping[str, Any]],
    queries: Iterable[Mapping[str, Any]],
    limit: int = CACHE_READS_SHOWN,
) -> dict[str, Any] | None:
    """The hottest reads the cache overlay fronts, with their share of calls (#462).

    ``reads`` holds the top ``limit`` in-scope cached queries by calls per second (ties by
    query id), each with its tables, owner engine, cache pattern and share of the in-scope
    workload's calls, the same denominator as the overlay's ``call_share_percent``; ``more``
    counts the rest. None when nothing is cached. The chat gate and the UI gate both read this.
    """
    rate = {q.get("query_id"): float(q.get("calls_per_second") or 0) for q in queries}
    in_scope = [qa for qa in query_assignments if qa.get("in_scope", True)]
    cached = [qa for qa in in_scope if qa.get("cache_engine")]
    if not cached:
        return None
    total = sum(rate.get(qa.get("query_id"), 0.0) for qa in in_scope)
    ranked = sorted(
        cached, key=lambda qa: (-rate.get(qa.get("query_id"), 0.0), str(qa["query_id"]))
    )
    reads = []
    for qa in ranked[:limit]:
        cps = rate.get(qa.get("query_id"), 0.0)
        reads.append(
            {
                "query_id": qa["query_id"],
                "tables": [str(t) for t in qa.get("source_tables") or []],
                "owner": qa.get("assigned_engine", ""),
                "pattern": qa.get("cache_pattern") or "",
                "calls_per_second": round(cps, 3),
                "call_share_percent": round(min(100.0, cps / total * 100), 1) if total else 0.0,
            }
        )
    return {"reads": reads, "more": max(0, len(ranked) - len(reads))}


def source_table_count(collector: Mapping[str, Any]) -> int:
    """How many source tables the collection names (#2326): ``database_schema.tables``, or
    the ``schema.tables`` an older collection wrote."""
    schema = collector.get("database_schema") or collector.get("schema") or {}
    tables = schema.get("tables") if isinstance(schema, Mapping) else None
    return len(tables) if isinstance(tables, list) else 0


def gate_block_facts(
    gate: Mapping[str, Any], *, collector: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Per-engine rows, the totals and whether the reality check changed anything.

    ``gate`` is the status data ``src.shared.gate_status.gate_status`` builds for the
    assignment version the gate states (``before_distribution``, ``after_distribution``,
    ``eliminated_engines``, ``consolidations``, and for a scoped job ``engine_scope`` and
    ``unservable_query_count``, the count of queries no engine in the scope can serve,
    #1574; the chat status line's older ``unservable_queries`` is read too). A row's
    ``dropped`` comes from ``eliminated_engines`` only, never from a zero or nonzero count.
    Rows run from the largest final count down, ties by engine id. ``source_table_count``
    is the gate's own when it has one (the version's report states it), else counted from
    ``collector`` when given (#2326), else None.
    """
    before = gate.get("before_distribution") or {}
    after = gate.get("after_distribution") or {}
    eliminated = {str(e) for e in gate.get("eliminated_engines") or []}
    total_after = sum(_count(v) for v in after.values())
    total_before = sum(_count(v) for v in before.values())
    engines = sorted(
        {str(e) for e in before} | {str(e) for e in after},
        key=lambda e: (-_count(after.get(e)), e),
    )
    rows = [
        {
            "engine": e,
            "before": _count(before.get(e)),
            "after": _count(after.get(e)),
            "share_percent": (
                round(_count(after.get(e)) * 100 / total_after, 1) if total_after else 0.0
            ),
            "dropped": e in eliminated,
        }
        for e in engines
    ]
    unchanged = (
        {e: _count(before.get(e)) for e in engines} == {e: _count(after.get(e)) for e in engines}
        and not eliminated
        and not (gate.get("consolidations") or [])
    )
    designed = [str(e) for e in gate.get("schema_design_engines") or []]
    cache = [e for e in designed if e in CACHE_ENGINES]
    unservable = _count(gate.get("unservable_query_count", gate.get("unservable_queries")))
    overlay = gate.get("cache_overlay")
    return {
        "rows": rows,
        # #1252: the reads routing put under the cache overlay: cache candidates, not yet
        # designed cached reads.
        "cache_candidates": (
            {
                # The candidates synthesis stored when it rewrote the overlay (never the
                # designed count), else the overlay's own: no drop has happened yet.
                "read_count": _count(
                    overlay.get("candidate_query_count", overlay.get("query_count"))
                ),
                "call_share_percent": overlay.get(
                    "candidate_call_share_percent", overlay.get("call_share_percent")
                ),
                # #462: the hottest of them, named; the rest counted.
                "reads": list((gate.get("cache_reads") or {}).get("reads") or []),
                "more": _count((gate.get("cache_reads") or {}).get("more")),
                # #1400: what the design covers, once synthesis stored the candidates.
                "change": (
                    cache_change_sentence(
                        _count(overlay["candidate_query_count"]), _count(overlay.get("query_count"))
                    )
                    if "candidate_query_count" in overlay
                    else None
                ),
            }
            if isinstance(overlay, Mapping)
            else None
        ),
        # The engines the architecture keeps, largest first, then the cache layer (the page's
        # "Recommended architecture: A, B and C" headline).
        "surviving": [r["engine"] for r in rows if r["after"] and not r["dropped"]] + cache,
        "cache_engine": cache[0] if cache else None,
        "total_before": total_before,
        "total_after": total_after,
        "unchanged": unchanged,
        # #1574: the customer's engine scope, in the order the deliverables name it, and
        # the queries no engine in it can serve (no engine, so in no row), with the total
        # of every query the statement counts them against.
        "engine_scope": (
            engine_scope_display_order(str(e) for e in gate["engine_scope"])
            if gate.get("engine_scope")
            else None
        ),
        "unservable_query_count": unservable,
        "query_total_count": total_after + unservable,
        # #2326: the source tables, for the UI gate's figures: the version's report's, else
        # the collection's; None when neither was given (the chat gate block does not state it).
        "source_table_count": (
            gate["source_table_count"]
            if isinstance(gate.get("source_table_count"), int)
            else source_table_count(collector) if collector is not None else None
        ),
    }
