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


# A routed confidence is "partly signal-based" when at least this share of the
# engine's routed queries has no table-level evidence (#152).
PARTIAL_EVIDENCE_MIN_SHARE = 0.25

SIGNAL_ONLY_NOTE = "signal only — no table-level evidence"
SIGNAL_ONLY_SHORT = "signal only"
PARTIAL_NOTE = "partly signal-based"


def is_signal_only(entry: Mapping[str, Any]) -> bool:
    """True when no source table the engine's analysis rated backs its routed fit."""
    return entry.get("routed_confidence") is not None and (
        entry.get("routed_confidence_evidence") == "signal_only"
    )


def evidence_note(entry: Mapping[str, Any], short: bool = False) -> str:
    """How much a routed confidence rests on signals alone, or "" when it does not.

    A signal-only fit is the basic baseline plus the signal bonus, never a
    measurement, so it is always labelled. A ``partial`` fit is labelled only when
    at least PARTIAL_EVIDENCE_MIN_SHARE of the engine's routed queries lack table
    evidence.
    """
    if entry.get("routed_confidence") is None:
        return ""
    evidence = entry.get("routed_confidence_evidence")
    if evidence == "signal_only":
        return SIGNAL_ONLY_SHORT if short else SIGNAL_ONLY_NOTE
    if evidence == "partial":
        n = int(entry.get("routed_queries") or 0)
        unbacked = int(entry.get("routed_queries_without_table_evidence") or 0)
        if n and unbacked / n >= PARTIAL_EVIDENCE_MIN_SHARE:
            return PARTIAL_NOTE
    return ""


def confidence_text(entry: Mapping[str, Any], short: bool = False) -> str:
    """``60% (signal only — no table-level evidence)``; ``93%`` when table-backed."""
    note = evidence_note(entry, short=short)
    return f"{engine_confidence(entry):.0f}%" + (f" ({note})" if note else "")
