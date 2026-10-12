"""Shared risk-severity counting (#536 round-1 review suggestion).

``src.report.renderers`` (the posture line), ``src.report.pptx_report`` (the Risk
Profile slide) and ``src.agents.referee.synthesis_grounding`` (the summary grounding
check) each counted ``risk_assessment`` risks by severity with their own small loop.
One counter here, imported by all three, so the #506 rule -- an unrecognised severity
string counts as unrated, never folded into LOW -- only has one implementation to get
right.
"""

from __future__ import annotations

from typing import Any

SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
# A risk whose severity is none of SEVERITY_ORDER is counted here, not dropped and not
# folded into LOW (#506: an unrated risk is not known to be low severity).
UNRATED = "UNRATED"
_KNOWN_SEVERITIES = frozenset(SEVERITY_ORDER)


def severity_of(risk: dict[str, Any]) -> str:
    """``risk``'s severity, upper-cased; an unrecognised value is ``UNRATED``."""
    level = str(risk.get("severity") or "").upper()
    return level if level in _KNOWN_SEVERITIES else UNRATED


def count_by_severity(risks: list[dict[str, Any]]) -> dict[str, int]:
    """Count ``risks`` per severity (``CRITICAL``/``HIGH``/``MEDIUM``/``LOW``/
    ``UNRATED``), case-insensitively. Every risk counts exactly once, so the values
    always sum to ``len(risks)``."""
    counts: dict[str, int] = {}
    for risk in risks:
        level = severity_of(risk)
        counts[level] = counts.get(level, 0) + 1
    return counts
