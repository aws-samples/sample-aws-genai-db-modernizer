# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Objects a collection script prints after the collection, merged into it (#2128).

``scripts/collect-mysql.sql`` measures each LOB column's longest value last, after it has
printed the whole collection, so that a timeout or a lock held by another session stops only
that step. The result is a second object, ``{"lob_sizes": [...]}``, with one entry per
measured column: ``table_name``, ``ordinal_position`` and ``max_value_bytes``. This module
puts each value on its column of ``columns``. A file without the second object, or with a
cut one, leaves every ``max_value_bytes`` as it was (None: not measured). Any other second
object is refused, as before: returning only the first would silently drop data.
"""

from __future__ import annotations

import json
from typing import Any

# The keys a second object may have.
ADDENDUM_KEYS = frozenset({"lob_sizes"})


def merge_addenda(data: dict[str, Any], bodies: list[str], *, source: str) -> dict[str, Any]:
    """``data`` with each later object in ``bodies`` merged in.

    Raises:
        ValueError: a later object is not valid JSON or is not one a script prints after the
            collection (its keys are not all in :data:`ADDENDUM_KEYS`).
    """
    for body in bodies:
        try:
            addendum = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}: a second JSON object is not valid JSON ({exc})") from exc
        if not isinstance(addendum, dict) or not addendum or not set(addendum) <= ADDENDUM_KEYS:
            raise ValueError(f"{source}: more than one JSON object found; expected exactly one")
        if "lob_sizes" in addendum:
            _merge_lob_sizes(data, addendum["lob_sizes"])
    return data


def _merge_lob_sizes(data: dict[str, Any], sizes: object) -> None:
    if not isinstance(sizes, list):
        return
    longest: dict[tuple[Any, Any], int] = {}
    for entry in sizes:
        if not isinstance(entry, dict):
            continue
        value = entry.get("max_value_bytes")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            longest[(entry.get("table_name"), entry.get("ordinal_position"))] = value
    for column in data.get("columns") or []:
        if isinstance(column, dict):
            key = (column.get("table_name"), column.get("ordinal_position"))
            if key in longest and column.get("max_value_bytes") is None:
                column["max_value_bytes"] = longest[key]
