# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``facts.design_changes``: what each Aurora design changes from the source (#2289).

Synthesis compares every table of an Aurora design with its collected table and stores
the rows on the design's summary (``schema_designs[engine].tables[].source_changes``,
``src/tools/schema/aurora_common/source_changes.py``). This module reads them once for
every deliverable: per table whether it changes, the kinds of change it has and how many
rows of each; per engine the counts, the tables that could not be compared and the
executive report's count line, which names no table. The rows themselves stay on the
summary, stored once; the deliverables read them there. A summary written before #2289 has
no rows, so its engine is left out until synthesis runs again.

One definition of a change: a table changes when it has any kind of change other than a
type translated as the deterministic translation writes it (``translated``) or an
expression index step 1 recreates (``index_step1``). Those two are stated on their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.shared.engine_names import display_engine

#: Each table-level change kind and how the count line names it, in the order the
#: deliverables list them.
CHANGE_KINDS: tuple[tuple[str, str, str], ...] = (
    ("table_added", "table added", "tables added"),
    ("column_added", "column added", "columns added"),
    ("column_skipped", "column not created", "columns not created"),
    ("type_changed", "column type changed", "column types changed"),
    ("index_added", "index added", "indexes added"),
    ("index_renamed", "index renamed", "indexes renamed"),
    ("index_changed", "index changed", "indexes changed"),
    ("index_skipped", "index not created", "indexes not created"),
)
TRANSLATED = "translated"
INDEX_STEP1 = "index_step1"
#: The kinds that are no change: the target gets what the source has, as decided.
NOT_A_CHANGE = frozenset({TRANSLATED, INDEX_STEP1})
ORDER = (*(k for k, *_ in CHANGE_KINDS), TRANSLATED, INDEX_STEP1)
_COLUMN_KINDS = {"added": "column_added", "skipped": "column_skipped",
                 "type_changed": "type_changed", "translated": TRANSLATED}  # fmt: skip
_INDEX_KINDS = {"added": "index_added", "skipped": "index_skipped", "renamed": "index_renamed",
                "changed": "index_changed", "step1": INDEX_STEP1}  # fmt: skip
_UNCOMPARED_REASONS = {
    "ambiguous_source": "two source schemas hold a table of its name and the design names none",
    "no_name": "its table definition has no name",
    "no_draft": (
        "the deterministic translation of its source table could not be built, so its "
        "types cannot be told translated from changed"
    ),
}


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [r for r in value if isinstance(r, Mapping)] if isinstance(value, list) else []


def _table_row(table: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    kinds: list[str] = []
    if changes.get("source_table") is None:
        kinds.append("table_added")
    else:
        kinds += [
            _COLUMN_KINDS[str(c.get("change"))]
            for c in _rows(changes.get("columns"))
            if c.get("change") in _COLUMN_KINDS
        ]
        kinds += [
            _INDEX_KINDS[str(i.get("change"))]
            for i in _rows(changes.get("indexes"))
            if i.get("change") in _INDEX_KINDS
        ]
    counted = {k: kinds.count(k) for k in ORDER if k in kinds}
    return {
        "table_name": str(table.get("table_name") or ""),
        "schema_name": table.get("schema_name") or None,
        "source_table": changes.get("source_table"),
        # The one definition of a changed table every deliverable counts and filters on.
        "changed": any(k not in NOT_A_CHANGE for k in counted),
        # The kinds of change the table has, in ORDER; empty when it is the same.
        "change_kinds": list(counted),
        "change_counts": counted,
    }


def _count_text(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _joined(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _brief_line(
    engine: str, tables: list[dict[str, Any]], counts: dict[str, int], uncompared: int
) -> str:
    """The executive report's count line for one engine's design (#2289): how many tables
    change and how, counted by kind; it names no table, as the executive report never does."""
    name = display_engine(engine)
    changed = sum(1 for t in tables if t["changed"])
    parts = [_count_text(counts[k], one, many) for k, one, many in CHANGE_KINDS if counts.get(k)]
    total = _count_text(len(tables), "table", "tables")
    sentences: list[str] = []
    if parts:
        sentences.append(
            f"Compared with the source, the {name} design changes {changed} of {total}: "
            f"{_joined(parts)}."
        )
    elif tables:
        sentences.append(
            f"Compared with the source, the {name} design keeps all {total} as collected."
        )
    if counts.get(TRANSLATED):
        n = counts[TRANSLATED]
        sentences.append(
            f"It translates {_count_text(n, 'column type', 'column types')} as the "
            f"deterministic translation to {name} writes them."
        )
    if counts.get(INDEX_STEP1):
        n = counts[INDEX_STEP1]
        sentences.append(
            f"Step 1 recreates {_count_text(n, 'expression index', 'expression indexes')} "
            "the design script does not create, when it moves "
            f"{'its table' if n == 1 else 'their tables'}."
        )
    if uncompared:
        sentences.append(
            f"{_count_text(uncompared, 'table of the design is', 'tables of the design are')} "
            "not compared."
        )
    if parts or counts.get(TRANSLATED) or counts.get(INDEX_STEP1) or uncompared:
        sentences.append("The engineering report and the results page name each one.")
    return " ".join(sentences)


def design_change_facts(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per Aurora engine whose design summary was compared with the source (#2289): each
    table's changed flag, kinds and counts, the engine's counts by kind, the tables not
    compared with their reason, and the executive report's line, from the report's
    ``schema_designs``. A summary written before the comparison is left out; empty when
    none was compared."""
    designs = report.get("schema_designs")
    if not isinstance(designs, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for engine, summary in sorted(designs.items()):
        if not isinstance(summary, Mapping) or summary.get("source_compared") is not True:
            continue
        tables: list[dict[str, Any]] = []
        uncompared: list[dict[str, Any]] = []
        for t in _rows(summary.get("tables")):
            changes = t.get("source_changes")
            if not isinstance(changes, Mapping):
                continue
            if changes.get("uncompared"):
                code = str(changes["uncompared"])
                uncompared.append(
                    {
                        "table_name": str(t.get("table_name") or ""),
                        "schema_name": t.get("schema_name") or None,
                        "reason_code": code,
                        "reason": _UNCOMPARED_REASONS.get(code, code),
                    }
                )
                continue
            tables.append(_table_row(t, changes))
        counts: dict[str, int] = {}
        for t in tables:
            for kind, n in t["change_counts"].items():
                counts[kind] = counts.get(kind, 0) + n
        out.append(
            {
                "engine": engine,
                "table_count": len(tables),
                "changed_table_count": sum(1 for t in tables if t["changed"]),
                "change_counts": {k: counts[k] for k in ORDER if k in counts},
                "table_changes": tables,
                "uncompared_table_count": len(uncompared),
                "uncompared_tables": uncompared,
                "brief_line": _brief_line(engine, tables, counts, len(uncompared)),
            }
        )
    return out
