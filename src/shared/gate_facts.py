"""The approval-gate figures, computed once from the reality-check status data (#1307, ADR-032).

The gate shows each engine's query count before and after the reality check and its share of
the total. ``scripts/print_summary.py`` prints the block from this; the block's renderer
(``src.report.printed_summary``) only formats it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.shared.cache_change import cache_change_sentence
from src.shared.cache_engines import CACHE_ENGINES


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def gate_block_facts(gate: Mapping[str, Any]) -> dict[str, Any]:
    """Per-engine rows, the totals and whether the reality check changed anything.

    ``gate`` is the reality-check status data ``scripts/run_assessment.py`` prints
    (``before_distribution``, ``after_distribution``, ``eliminated_engines``,
    ``consolidations``). A row's ``dropped`` comes from ``eliminated_engines`` only, never
    from a zero or nonzero count (an eliminated engine can keep a count for an out-of-scope
    query). Rows run from the largest final count down, ties by engine id.
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
    }
