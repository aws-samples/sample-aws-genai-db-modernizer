"""Design texts as a risk's ``details`` list them (#950, #1410).

Synthesis (``src.agents.referee.migration_risks``) writes each design trade-off's
title and each unsupported cache pattern into a risk's ``details`` with these
functions; the engineering report uses the same ones to tell which details it
already lists in their own section, by their text, so the risk register does not
print them a second time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.shared.unsupported_pattern import unsupported_pattern_label

# A leading review marker a designer puts on a line: "[PE note] ...", "PE note: ...".
_MARKER_RE = re.compile(r"^(?:\[[^\]]{1,30}\]\s*|(?:PE|review) note:\s*)+", re.IGNORECASE)


def design_text_item(text: str) -> str:
    """One detail line in full: one line, its review marker and final full stop gone."""
    return _MARKER_RE.sub("", " ".join(str(text or "").split())).rstrip(" .;")


def unsupported_pattern_detail(up: Mapping[str, Any]) -> str:
    """One unsupported pattern in full: its label, reason and workaround."""
    label = unsupported_pattern_label(dict(up))
    reason = design_text_item(str(up.get("reason") or "")) or "not served by the cache design"
    fix = design_text_item(str(up.get("workaround") or up.get("recommendation") or ""))
    return f"{label}: {reason}" + (f". Workaround: {fix}" if fix else "")


_CARRIED_WRITE_RE = re.compile(r"^(?P<lead>.+? \(score [^)]*\)): (?P<approach>.+)$")


def carried_write_detail(
    statement: str, table: str, intent: str, score: str | float, approach: str
) -> str:
    """One carried write in full (#1362): ``DELETE of t, retention delete (score 20):
    <approach>``. ``score`` is the written figure (an int-valued float reads ``20``).
    The engineering report (#1454) reads it back with ``split_carried_write_detail``
    to list the statements that share an approach under it once."""
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    return (
        f"{(statement or 'Statement').upper()} of {str(table).rsplit('.', 1)[-1]}, "
        f"{str(intent).lower()} (score {score}): {design_text_item(approach)}"
    )


def split_carried_write_detail(text: str) -> tuple[str, str] | None:
    """``(statement lead, approach)`` for a detail ``carried_write_detail`` wrote, or
    ``None`` for any other detail text."""
    m = _CARRIED_WRITE_RE.match(text)
    return (m.group("lead"), m.group("approach")) if m else None


# A close-call risk's description (#1220): "<wave and counts>: <engine pairs>. A small
# change in the scores moves them." The engine pairs are the rows of the engineering
# report's Close calls table, so the register names that table instead of listing them.
_CLOSE_CALL_TAIL = "A small change in the scores moves "
_CLOSE_CALL_RE = re.compile(
    rf"^(?P<lead>.+?): (?P<pairs>[^:]+? (?:keeps|takes) [^:]+?)\. "
    rf"(?P<tail>{_CLOSE_CALL_TAIL}(?:it|them)\.)$"
)


def close_call_description(lead: str, pairs: list[str], moved: str) -> str:
    """A close-call risk's description: its counts (``lead``), each engine pair
    (``"Aurora MySQL keeps 14 close calls over DynamoDB"``) and ``moved`` ("it" or
    "them"). The engineering report reads it back with ``split_close_call_description``."""
    return f"{lead}: {'; '.join(pairs)}. {_CLOSE_CALL_TAIL}{moved}."


def split_close_call_description(text: str) -> tuple[str, str, str] | None:
    """``(lead, engine pairs, last sentence)`` for a description
    ``close_call_description`` wrote, or ``None`` for any other text."""
    m = _CLOSE_CALL_RE.match(text)
    return (m.group("lead"), m.group("pairs"), m.group("tail")) if m else None


def severe_risk_strategy(risk_id: str, label: str, scope: str, mitigation: str) -> str:
    """A CRITICAL or HIGH risk's line in ``mitigation_strategies``: its id, severity and
    engine (``label``), scope and own mitigation (one sentence). The engineering report
    reads it back with ``split_severe_risk_strategy``: its Risk register prints that
    mitigation already, so the strategy refers to it (#1220)."""
    return f"{risk_id} ({label}; {scope}): {mitigation}"


_SEVERE_STRATEGY_RE = re.compile(r"^(?P<head>(?P<rid>RISK-\d+) \([^:]*\)): (?P<body>.+)$")


def split_severe_risk_strategy(text: str) -> tuple[str, str, str] | None:
    """``(risk id, head, mitigation)`` for a line ``severe_risk_strategy`` wrote, or
    ``None`` for any other strategy."""
    m = _SEVERE_STRATEGY_RE.match(text)
    return (m.group("rid"), m.group("head"), m.group("body")) if m else None
