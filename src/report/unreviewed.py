# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reality-check moves no model reviewed (#746): one plain line, when any.

The consolidation validator reviews moved queries in batches; a batch with no
usable verdict keeps its moves as the routing rules placed them. Every
deliverable states how many, from ``facts.unreviewed_move_count`` (ADR-032);
nothing here counts.
"""

from __future__ import annotations

from typing import Any

from .facts import report_facts
from .formatting import plural_noun, plural_verb


def unreviewed_moves_line(report: dict[str, Any]) -> str | None:
    """``3 reality-check moves were not reviewed by the model ...``, or None."""
    n = int(report_facts(report).get("unreviewed_move_count") or 0)
    if not n:
        return None
    return (
        f"{n} reality-check {plural_noun(n, 'move', 'moves')} "
        f"{plural_verb(n, 'was', 'were')} not reviewed by the model: its review of "
        f"{plural_noun(n, 'that move', 'those moves')} failed, so "
        f"{plural_noun(n, 'it stands', 'they stand')} as the routing rules placed "
        f"{plural_noun(n, 'it', 'them')}."
    )


def unreviewed_moves_md(report: dict[str, Any]) -> list[str]:
    """The line as its own paragraph (engineering report), or nothing."""
    line = unreviewed_moves_line(report)
    return [f"**Not reviewed:** {line}", ""] if line else []
