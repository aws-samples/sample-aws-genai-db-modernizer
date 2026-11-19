# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive summary's lead line: the decision, in one line (#2315, ADR-032).

The results page's summary header and the chat summary open with it, and so do the
executive report's cover and the engineering report (#2460). It is worded from
the facts' own executive brief only (``decision_wave`` and what each wave does), never
from a model's text:

- with a suggested wave, the wave to approve and what it does
  ("Approve wave 1: move wordpress to Aurora MySQL");
- with none suggested yet, the plan itself
  ("Move trailgear to Aurora PostgreSQL and DynamoDB, and add ElastiCache, in 3 waves"):
  the summary states the best solution, and each wave's readiness says what it still
  needs.

An engine whose wave moves no table (the cache layer, a read model) is added, never
moved to. The cache layer moves no data; a read model is synced from the tables'
owners. :func:`wave_adds_engines` is that one rule, and the Decision paragraph
(``src/shared/executive_brief.py``) uses it too, so the two never disagree.

It gives no cutover condition: the cutover is the customer's (``CUTOVER_MESSAGE``).

``src/shared`` imports nothing from ``src/agents`` or ``src/report``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.shared.engine_names import display_engine


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


# #2315 review: the brief's wave kinds whose engines are added and no table changes owner
# (``is_migrating_wave``: their ``moves_from`` is empty). The cache layer serves reads
# cache-aside and a read model is fed from the data that stays. A no_move wave names no
# engine at all, so it adds none and the line names only the waves that do.
ADDING_WAVE_KINDS = frozenset({"cache", "read_model"})


def wave_adds_engines(kind: Any) -> bool:
    """True when a brief wave of ``kind`` adds its engines and no table changes owner."""
    return kind in ADDING_WAVE_KINDS


def _wave_number(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("wave") or 0)
    except (TypeError, ValueError):
        return 0


def decision_headline(brief: Any, database_name: str | None) -> str | None:
    """The lead line for ``brief`` (``facts.executive_brief``); None with no waves."""
    if not isinstance(brief, Mapping):
        return None
    waves = [w for w in brief.get("waves") or [] if isinstance(w, Mapping)]
    if not waves:
        return None
    db = str(database_name or "").strip() or "the database"
    decision = brief.get("decision_wave")
    if isinstance(decision, int) and not isinstance(decision, bool):
        row = next((w for w in waves if _wave_number(w) == decision), None) or {}
        names = [display_engine(str(e)) for e in row.get("engines") or [] if e]
        kind = row.get("kind")
        head = f"Approve wave {decision}"
        if not names or kind in ("no_move", "heterogeneous"):
            return head
        if wave_adds_engines(kind):
            return f"{head}: add {_join(names)}"
        if kind in ("homogeneous", "cross_engine"):
            return f"{head}: move {db} to {names[0]}"
        return f"{head}: move queries to {_join(names)}"
    moved: list[str] = []
    added: list[str] = []
    for w in waves:
        bucket = added if wave_adds_engines(w.get("kind")) else moved
        for e in w.get("engines") or []:
            name = display_engine(str(e)) if e else ""
            if name and name not in moved and name not in added:
                bucket.append(name)
    count = len(waves)
    noun = "wave" if count == 1 else "waves"
    if not moved and not added:
        return None
    if not moved:
        return f"Add {_join(added)} to {db} in {count} {noun}"
    if not added:
        return f"Move {db} to {_join(moved)} in {count} {noun}"
    return f"Move {db} to {_join(moved)}, and add {_join(added)}, in {count} {noun}"


# #2460: each wave readiness as the results page's summary words it beside the headline
# (en.json ``analysis-results.waves.readiness-status.*``; a test keeps the two equal).
READINESS_LABELS: dict[str, str] = {
    "suggested": "Suggested",
    "held": "Not suggested yet",
    "not_designed": "Not designed",
    "not_reviewed": "Not reviewed",
}


def decision_readiness_label(facts: Any) -> str | None:
    """The readiness of the wave the headline is about, as the results page's summary
    states it beside the headline ("Wave 1: Suggested, 2 warnings to accept").

    The wave is the brief's decision wave, else its first wave; its readiness is the
    stored ``facts.waves[].readiness`` and ``open_warning_count``. None when the facts
    state no readiness for it (a report saved before the readiness check, #1992)."""
    if not isinstance(facts, Mapping):
        return None
    brief = facts.get("executive_brief")
    if not isinstance(brief, Mapping):
        return None
    waves = [w for w in brief.get("waves") or [] if isinstance(w, Mapping)]
    decision = brief.get("decision_wave")
    if isinstance(decision, int) and not isinstance(decision, bool):
        wave = decision
    elif waves:
        wave = _wave_number(waves[0])
    else:
        return None
    row = next(
        (r for r in facts.get("waves") or [] if isinstance(r, Mapping) and r.get("wave") == wave),
        None,
    )
    if row is None or not row.get("readiness_line"):
        return None
    label = READINESS_LABELS.get(str(row.get("readiness")))
    if label is None:
        return None
    warnings = row.get("open_warning_count")
    if row.get("readiness") == "suggested" and isinstance(warnings, int) and warnings > 0:
        noun = "warning" if warnings == 1 else "warnings"
        label = f"{label}, {warnings} {noun} to accept"
    return f"Wave {wave}: {label}"
