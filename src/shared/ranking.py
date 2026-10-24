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

from src.shared.engine_capabilities import PATTERN_ONLY_ENGINES
from src.shared.workload_fit_labels import PINNED_UTILITY_LABEL

#: Said once by every view of a report written before utility pins left the mean fit
#: (#1373, #1389): its stored figures and traces cannot be recounted by a renderer
#: (ADR-032), only by running synthesis again.
PIN_RULE_PREDATES_NOTE = (
    "This report was written before utility-pinned queries were left out of an "
    "engine's mean fit, so its fit figures and query traces may still count them. "
    "Run synthesis again to update them."
)


def report_predates_pin_rule(report: Mapping[str, Any]) -> bool:
    """Whether ``report`` was written before ``routed_pinned_queries`` existed (#1389).

    A routed ranking entry of a current report always carries the count (0 when it
    holds no pin); an entry with a routed figure and no count was written before.
    Reads what the file holds; counts nothing.
    """
    return any(
        isinstance(e, Mapping)
        and e.get("routed_queries") is not None
        and e.get("routed_pinned_queries") is None
        for e in report.get("ranking") or []
    )


def engine_confidence(entry: Mapping[str, Any]) -> float:
    """The confidence to show for a ranking entry: routed, else the analysis average.

    An engine whose routed queries are all utility pins has no routed figure and no
    average to stand in for one (#1373): show it with :func:`fit_text`, which says so.
    """
    routed = entry.get("routed_confidence")
    if routed is not None:
        return float(routed)
    return float(entry.get("confidence_score") or 0)


def pinned_only(entry: Mapping[str, Any]) -> int:
    """How many queries are routed to the entry's engine when every one of them is a
    utility pin (no routed figure, #1373); 0 otherwise."""
    if entry.get("routed_confidence") is not None:
        return 0
    return int(entry.get("routed_pinned_queries") or 0)


def fit_text(entry: Mapping[str, Any], word: str = "fit") -> str:
    """``73% fit`` for a ranking entry, or ``all 3 routed queries pinned (utility)``
    for an engine that holds only utility pins (never the analysis average, #1373)."""
    pinned = pinned_only(entry)
    if pinned:
        noun = "query" if pinned == 1 else "queries"
        return f"all {pinned} routed {noun} {PINNED_UTILITY_LABEL}"
    return f"{engine_confidence(entry):.0f}% {word}"


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

SIGNAL_ONLY_NOTE = "signal only, no table-level evidence"
SIGNAL_ONLY_SHORT = "signal only"
PARTIAL_NOTE = "partly signal-based"


def is_signal_only(entry: Mapping[str, Any]) -> bool:
    """True when no source table the engine's analysis rated backs its routed fit."""
    return entry.get("routed_confidence") is not None and (
        entry.get("routed_confidence_evidence") == "signal_only"
    )


def evidence_note(entry: Mapping[str, Any], short: bool = False) -> str:
    """How much a routed confidence rests on signals alone, or "" when it does not.

    A signal-only fit is always labelled. Pattern-only engines use query pattern
    evidence, so their note does not refer to tables. A ``partial`` fit is labelled
    only when at least PARTIAL_EVIDENCE_MIN_SHARE of the engine's routed queries
    lack evidence (matched patterns for pattern-only engines, rated tables otherwise).
    """
    if entry.get("routed_confidence") is None:
        return ""
    evidence = entry.get("routed_confidence_evidence")
    if evidence == "signal_only":
        return (
            SIGNAL_ONLY_SHORT
            if short or entry.get("target") in PATTERN_ONLY_ENGINES
            else SIGNAL_ONLY_NOTE
        )
    if evidence == "partial":
        n = int(entry.get("routed_queries") or 0)
        unbacked = int(entry.get("routed_queries_without_table_evidence") or 0)
        if n and unbacked / n >= PARTIAL_EVIDENCE_MIN_SHARE:
            return PARTIAL_NOTE
    return ""


def confidence_text(entry: Mapping[str, Any], short: bool = False) -> str:
    """``60% (signal only, no table-level evidence)``; ``93%`` when table-backed."""
    if pinned_only(entry):
        return fit_text(entry)
    note = evidence_note(entry, short=short)
    return f"{engine_confidence(entry):.0f}%" + (f" ({note})" if note else "")
