"""Expression index keys, as the collectors report them (#295).

Every catalog the collectors read reports one row per index key, and an
expression key has no column to name:

- PostgreSQL: ``pg_index.indkey`` holds 0 for an expression key, and 0 is never
  an ``attnum``, so the (left) join to ``pg_attribute`` gives a NULL column
  name. The queries also report a ``has_expressions`` flag and the expression
  text (:func:`expression_key_fields`).
- MySQL 8.0.13+: a functional key part has a NULL ``COLUMN_NAME`` in
  ``information_schema.statistics`` (and its text in ``EXPRESSION``).
- Oracle: a function-based key is stored as a hidden virtual column named
  ``SYS_NCnnnnn$``, which ``ALL_IND_COLUMNS`` reports in place of a column.
  Oracle stores a descending key the same way, so that reads as an expression
  key too.
- SQL Server and MariaDB index only real (possibly computed) columns, so every
  key row names a column.

A key list with the expression keys left out is a narrower index, and a
narrower UNIQUE index rejects rows the source accepts. So every path flags the
index instead (:func:`add_index_key`), and the draft notes it rather than
building it. This is the one place that reads those raw columns, so the live
paths and the offline path put the same thing on the contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.tools.database.index_prefix import prefix_length

# Both a JSON dump and a psql text dump reach us, so the boolean arrives
# either already parsed or as PostgreSQL's own 't'/'f'.
_TRUTHY = (True, 1, "t", "true", "1")

# The hidden virtual column Oracle creates for a function-based index key.
_ORACLE_EXPRESSION_COLUMN = re.compile(r"sys_nc\d+\$", re.IGNORECASE)


def expression_key_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """The ``Index`` fields describing one raw index row's expression keys.

    The two text fields ride along only for an index that actually has
    expression keys: ``pg_get_indexdef`` is never null, and carrying every
    plain index's CREATE statement into every schema designer's prompt would
    be a lot of text that says nothing the columns don't already.
    """
    if row.get("has_expressions") not in _TRUTHY:
        return {"has_expressions": False, "index_expressions": None, "index_def": None}
    return {
        "has_expressions": True,
        "index_expressions": row.get("index_expressions") or None,
        "index_def": row.get("index_def") or None,
    }


def is_expression_key(column_name: Any, source_engine: str | None = None) -> bool:
    """Whether a raw index key row stands for an expression, not a column.

    A row with no column name is one on every engine. On Oracle, so is a row
    naming the ``SYS_NCnnnnn$`` column a function-based key is stored as.
    """
    name = "" if column_name is None else str(column_name).strip()
    if not name:
        return True
    engine = (source_engine or "").strip().lower()
    return engine == "oracle" and _ORACLE_EXPRESSION_COLUMN.fullmatch(name) is not None


def add_index_key(
    index: dict[str, Any], row: Mapping[str, Any], source_engine: str | None = None
) -> None:
    """Add one raw key row to an index dict being built from catalog rows.

    A column key joins ``index["columns"]``. An expression key sets
    ``has_expressions`` instead, and its text, when the row carries one
    (MySQL's ``expression``), joins ``index_expressions``. An index dict that carries
    ``prefix_lengths`` gets each column key's prefix length (MySQL ``sub_part``) too.
    """
    column = row.get("column_name")
    if not is_expression_key(column, source_engine):
        index["columns"].append(column)
        if "prefix_lengths" in index:  # MySQL SUB_PART (#1623), aligned with ``columns``
            index["prefix_lengths"].append(prefix_length(row.get("sub_part")))
        return
    index["has_expressions"] = True
    text = str(row.get("expression") or "").strip()
    if text:
        known = index.get("index_expressions")
        index["index_expressions"] = f"{known}, {text}" if known else text
