# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Table names that stand for no table, and the one reader of saved write groups (#1817).

``PLACEHOLDER_TABLE_NAMES`` is the one list: the collector's placeholder spellings
(``unknown`` and the ``UNKNOWN`` / ``None`` / ``null`` some collections carry) and the
dummy tables of a statement with no ``FROM`` (``DUAL``, the ``dual`` the offline parser
keeps in the statement's own case, DB2's ``SYSIBM.SYSDUMMY1``). Matching is exact, never
case-folded: a real table named ``none``, ``Null`` or ``Unknown`` is still a table. The
empty string (and a missing name) is a placeholder too.

Routing, table ownership, the routed confidence and every report read the list through
:func:`is_placeholder_table`; ``report_facts`` re-exports both names. A write group
keyed on a placeholder is no write group (ADR-031 Amendment 9): ``write_groups``
never builds one, and :func:`saved_write_groups`, the only reader of a saved
assignment's ``write_groups`` (``tests/unit/agents/referee/test_saved_write_groups_reader.py``),
drops one an assignment saved before #1817 carries.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Mirrored by ``PLACEHOLDER_TABLES`` in src/ui/src/utils/tableNames.js.
PLACEHOLDER_TABLE_NAMES = frozenset(
    {"unknown", "UNKNOWN", "None", "null", "DUAL", "dual", "SYSIBM.SYSDUMMY1"}
)


def is_placeholder_table(name: object) -> bool:
    """Whether ``name`` stands for no table: missing, blank, or exactly one of
    ``PLACEHOLDER_TABLE_NAMES``."""
    text = "" if name is None else str(name).strip()
    return not text or text in PLACEHOLDER_TABLE_NAMES


def real_write_groups(
    write_groups: Mapping[str, Iterable[str]] | None,
) -> dict[str, list[str]]:
    """``write_groups`` without any group keyed on a placeholder table."""
    return {
        str(table): list(members)
        for table, members in (write_groups or {}).items()
        if not is_placeholder_table(table)
    }


def saved_write_groups(assignment: Any) -> dict[str, list[str]]:
    """A saved assignment's ``write_groups`` (an artifact dict or an ``Assignment``),
    without a group keyed on a placeholder table; empty for one written before write
    groups existed, or for none."""
    if assignment is None:
        return {}
    if isinstance(assignment, Mapping):
        raw = assignment.get("write_groups")
    else:
        raw = getattr(assignment, "write_groups", None)
    return real_write_groups(raw)


# A pending engine choice for a statement that reads no table (#2147): named for the
# query (``query:<query_id>``), its ``table`` empty, and shown under this label wherever
# a choice's table is shown (the reports, the approval gate, the UI).
NO_SOURCE_TABLE_LABEL = "No source table"
NO_TABLE_CHOICE_PREFIX = "query:"


def no_table_choice_id(query_id: str) -> str:
    """The id of the engine choice for one statement that reads no table."""
    return f"{NO_TABLE_CHOICE_PREFIX}{query_id}"


def choice_table_label(table: object) -> str:
    """A choice's table as the deliverables show it: its bare name
    (``app.theme_fields`` -> ``theme_fields``), or :data:`NO_SOURCE_TABLE_LABEL` for a
    choice that reads no table (an empty ``table``, or a placeholder an assignment
    saved before #2147 named)."""
    if is_placeholder_table(table):
        return NO_SOURCE_TABLE_LABEL
    return str(table).strip().rsplit(".", 1)[-1]
