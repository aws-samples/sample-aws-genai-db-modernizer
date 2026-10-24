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
