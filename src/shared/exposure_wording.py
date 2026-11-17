# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive summary's exposures, grouped on what the summary says (#2324, ADR-032).

The summary names a risk by its rule's own phrase when the rule has one
(:data:`EXPOSURE_PHRASES`), else by its severity, type and engine. Two risks the summary
would word the same are one exposure, counted: :func:`group_exposures` groups on that
wording key, so the facts layer owns the count and the summary only words it. Synthesis
groups each open risk with it (``report_facts.executive_brief_facts``), and the report
reader regroups a block saved before exposures were counted (``src.report.facts``).

``src/shared`` imports nothing from ``src/agents`` or ``src/report``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Each migration-risk rule as the summary names it: (what it is, what to do about it).
# A risk with no rule here is named by its severity, type and engine.
EXPOSURE_PHRASES: dict[str, tuple[str, str]] = {
    "sync.wave": (
        "the tables kept current by change data capture (CDC)",
        "test the CDC replication",
    ),
    "routing.close_calls": (
        "the queries where two engines scored almost the same",
        "confirm where those queries run",
    ),
    "routing.tie_open": (
        "the queries tied between two engines",
        "choose their engine",
    ),
    "design.redesign": (
        "the queries whose plan relies on a schema redesign",
        "confirm the redesign in the schema design",
    ),
    "design.carried_writes": (
        "the writes the target engine serves poorly",
        "review those writes",
    ),
    "relational.gaps": (
        "the SQL features that need a rewrite or behave differently",
        "fix them",
    ),
}


def exposure_key(exposure: Mapping[str, Any]) -> tuple[Any, ...]:
    """What the summary says for an exposure: its rule's phrase, else its severity, risk
    type and engine. Two exposures with one key read the same."""
    rule = exposure.get("rule")
    if isinstance(rule, str) and rule in EXPOSURE_PHRASES:
        return ("phrase", EXPOSURE_PHRASES[rule][0])
    return (
        "risk",
        str(exposure.get("severity") or "").upper(),
        str(exposure.get("risk_type") or "") or None,
        exposure.get("engine") or None,
    )


def _count(exposure: Mapping[str, Any]) -> int:
    n = exposure.get("count")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1


def _ids(exposure: Mapping[str, Any]) -> list[str]:
    ids = exposure.get("risk_ids")
    if isinstance(ids, list) and ids:
        return [str(i) for i in ids if i]
    return [str(exposure["risk_id"])] if exposure.get("risk_id") else []


def _waves(exposure: Mapping[str, Any]) -> list[int]:
    waves = exposure.get("waves")
    if not isinstance(waves, list) or not waves:
        waves = [exposure.get("wave")]
    return [w for w in waves if isinstance(w, int) and not isinstance(w, bool)]


def group_exposures(
    exposures: Iterable[Mapping[str, Any]], limit: int | None = None
) -> list[dict[str, Any]]:
    """``exposures`` with the ones the summary words the same merged into one, in the
    order the first of each comes: its fields kept, ``count`` the risks it stands for,
    ``risk_ids`` theirs and ``waves`` every wave they act before, in order. An entry
    without a count (a single risk, or a block saved before #2324) counts one. Then the
    first ``limit`` exposures. Grouping a grouped list changes nothing."""
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for e in exposures:
        if not isinstance(e, Mapping):
            continue
        key = exposure_key(e)
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {**e, "count": _count(e), "risk_ids": _ids(e), "waves": _waves(e)}
            continue
        entry["count"] += _count(e)
        entry["risk_ids"] += [i for i in _ids(e) if i not in entry["risk_ids"]]
        entry["waves"] += [w for w in _waves(e) if w not in entry["waves"]]
    out = list(grouped.values())
    for entry in out:
        entry["waves"] = sorted(entry["waves"])
    return out if limit is None else out[:limit]
