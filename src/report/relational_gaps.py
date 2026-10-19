"""A cross-engine wave 1's note and relational gap list (#1120, #471).

The facts layer states, for wave 1 of a heterogeneous source, that the wave is not yet
optimized for heterogeneous migrations (``facts.waves[].heterogeneous_note``) and
detects the relational gaps (``facts.relational_gaps``). Every deliverable prints the
note under wave 1 and lists the gaps after it; this module only reads them.
"""

from __future__ import annotations

from typing import Any

from src.shared.catalog.gaps import GAP_IMPACT_LABELS

from .facts import report_facts


def heterogeneous_wave(report: dict[str, Any]) -> dict[str, Any] | None:
    """The facts row of the wave carrying a heterogeneous note, or None."""
    return next(
        (
            w
            for w in report_facts(report).get("waves") or []
            if isinstance(w, dict) and w.get("heterogeneous_note")
        ),
        None,
    )


def wave_gaps(report: dict[str, Any], wave: int) -> list[dict[str, Any]]:
    """The relational gaps listed under ``wave``: every detected gap for the wave whose
    facts say the gap check is its own (``lists_relational_gaps``), none otherwise."""
    het = heterogeneous_wave(report)
    if het is None or int(het.get("wave") or 0) != wave or not het.get("lists_relational_gaps"):
        return []
    gaps = (report_facts(report).get("relational_gaps") or {}).get("gaps") or []
    return [g for g in gaps if isinstance(g, dict)]


_LABELS: dict[str, str] = {str(k): v for k, v in GAP_IMPACT_LABELS.items()}


def impact_label(impact: Any) -> str:
    """ "needs a rewrite" for ``needs_rewrite``."""
    return _LABELS.get(str(impact), str(impact).replace("_", " "))
