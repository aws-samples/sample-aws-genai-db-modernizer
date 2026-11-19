# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The source objects an Aurora design leaves out, read from its app-layer notes (#2484).

The schema merge leaves an object out of the design when it cannot create it safely: a
table whose name collides with another once case and quote marks are folded
(``colliding_table_name``), a name SQL may not quote (``unsafe_name``), a foreign key
whose referenced table two other schemas have (``ambiguous_reference``), a second
index of one name (``duplicate_index_name``), or a table of a system schema such as
``pg_catalog`` (``system_schema``, #2478). Each one becomes an app-layer note whose
recommendation reads ``<Object> not created: <reason>.``
(``delta_merge._left_out_notes``).

This module is the one reader of those notes: the facts layer lists them
(``report_facts.design_left_out_facts``), and the merge and the saved-design check write
and check one ``-- Not created: <name>: <reason>`` comment per object at the end of the
generated script. It imports no ``src.agents`` or ``src.tools`` code (ADR-032 Decision 3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from src.shared.sql_identifiers import comment_text

#: The note features the merge writes for an object it leaves out of the design.
LEFT_OUT_FEATURES = frozenset(
    {
        "unsafe_name",
        "colliding_table_name",
        "ambiguous_reference",
        "duplicate_index_name",
        "system_schema",
    }
)

_OBJECTS = ("Table", "Column", "Primary key", "Index", "Foreign key")
_HEAD = re.compile(rf"^({'|'.join(_OBJECTS)}) not created: (\S.*)$", re.DOTALL)

#: How each comment the generated script carries for a left-out object starts.
COMMENT_PREFIX = "-- Not created: "


def _table_name(name: str) -> str:
    """A left-out table's name: a note written before #2484 named a table twice
    (``Sales.orders.Sales.orders``, the table then the object), so the first half. Only
    that doubled form of a qualified table is halved: ``sales.sales`` is a schema and a
    table of one name, and keeps both."""
    half = len(name) // 2
    if len(name) % 2 == 1 and name[half] == "." and name[:half] == name[half + 1 :]:
        if "." in name[:half]:
            return name[:half]
    return name


def label_of(feature: str) -> str:
    """A note feature as the deliverables name it: ``Colliding table name``."""
    return feature.replace("_", " ").capitalize()


def is_left_out_note(note: Any) -> bool:
    """Whether ``note`` is the merge's note for an object the design leaves out."""
    return left_out_object(note) is not None


def left_out_object(note: Any) -> dict[str, str] | None:
    """The left-out object a note states (``name``, ``feature``, its ``label``, the
    ``object`` kind in lower case and the one-sentence ``reason``), or None for any other
    note (a model-written one, a time zone, a column default, a key prefix)."""
    if not isinstance(note, dict) or note.get("feature") not in LEFT_OUT_FEATURES:
        return None
    name, text = note.get("source_object"), note.get("recommendation")
    if not isinstance(name, str) or not name.strip() or not isinstance(text, str):
        return None
    match = _HEAD.match(text.strip())
    if match is None:
        return None
    kind = match.group(1).lower()
    feature = str(note["feature"])
    return {
        "name": _table_name(name) if kind == "table" else name,
        "feature": feature,
        "label": label_of(feature),
        "object": kind,
        "reason": match.group(2),
    }


def left_out_objects(notes: Any) -> list[dict[str, str]]:
    """Every left-out object the notes state, in note order."""
    if not isinstance(notes, list):
        return []
    return [o for o in (left_out_object(n) for n in notes) if o is not None]


def not_created_comment(entry: dict[str, str]) -> str:
    """One object's comment line: ``-- Not created: <name>: <reason>``, both made one line
    (``sql_identifiers.comment_text``), so a hostile name cannot end the comment or start a statement.
    """
    return f"{COMMENT_PREFIX}{comment_text(entry['name'])}: {comment_text(entry['reason'])}"


def not_created_block(notes: Any) -> str:
    """The comment lines for every left-out object the notes state; empty when none."""
    return "\n".join(not_created_comment(o) for o in left_out_objects(notes))


def with_not_created_comments(script: str, notes: Iterable[Any] | None) -> str:
    """``script`` with the left-out objects' comments after its last statement. A design
    that leaves nothing out gets ``script`` back unchanged, byte for byte."""
    block = not_created_block(list(notes or []))
    if not block:
        return script
    return f"{script}\n\n{block}" if script else block


def script_has_not_created_comments(script: str, notes: Any) -> bool:
    """Whether ``script`` ends with exactly the comments :func:`with_not_created_comments`
    adds for ``notes``. False for a design that leaves nothing out and for one saved
    before #2484, whose script has none."""
    block = not_created_block(notes)
    return bool(block) and (script == block or script.endswith(f"\n\n{block}"))


def without_not_created_comments(script: str, notes: Any) -> str:
    """``script`` less the comments :func:`with_not_created_comments` adds for ``notes``,
    when it ends with exactly them; else ``script`` as it is (a design saved before #2484
    has none, and any other text is left for the saved-design check to refuse)."""
    block = not_created_block(notes)
    if not block:
        return script
    if script == block:
        return ""
    if script.endswith(f"\n\n{block}"):
        return script[: -len(block) - 2]
    return script
