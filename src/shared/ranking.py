"""Reading a synthesis ``ranking`` the way the deliverables do (#152).

A ranking entry carries two confidences:

- ``analysis_confidence`` (also ``confidence_score``, kept for backward
  compatibility): the engine's suitability averaged over every table it analyzed.
  It is kept for audit; it says nothing about the work the engine was given.
- ``routed_confidence``: the mean per-query fit of the queries the effective
  assignment routes to the engine (for the cache layer, of the reads it fronts).
  This is the figure every deliverable shows.

A report written before ``routed_confidence`` existed, or an engine with no routed
query, falls back to ``confidence_score``.

The ranking itself is ordered by workload share, owners first and the cache layer
after them, so its first entry is the main engine. :func:`main_engine` does not rely
on that order, so it also reads a legacy report that was ordered by weight.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def engine_confidence(entry: Mapping[str, Any]) -> float:
    """The confidence to show for a ranking entry: routed, else the analysis average."""
    routed = entry.get("routed_confidence")
    if routed is not None:
        return float(routed)
    return float(entry.get("confidence_score") or 0)


def is_cache_layer_entry(entry: Mapping[str, Any]) -> bool:
    """True for the cache layer entry (owns no query, #296)."""
    return bool(entry.get("role") == "cache_layer")


def main_engine(ranking: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    """The engine that owns the largest share of the workload, or None for no ranking.

    The cache layer is never the main engine. Without workload shares (no
    assignment) the first owner entry stands, as before.
    """
    entries = [r for r in (ranking or []) if isinstance(r, Mapping)]
    owners = [r for r in entries if not is_cache_layer_entry(r)]
    if not owners:
        return entries[0] if entries else None
    if any(r.get("workload_percent") is not None for r in owners):
        # max() keeps the first of equal shares, i.e. the ranking's own order
        return max(owners, key=lambda r: float(r.get("workload_percent") or 0))
    return owners[0]
