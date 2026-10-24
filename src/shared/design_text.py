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
