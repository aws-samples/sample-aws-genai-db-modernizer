"""Drop table rows a collection carries, and count them (#387).

The assessment needs schema, query shapes and counters, never row data, so no
collector reads table rows. A collection or collector output written before #387
may still carry ``sample_data`` on its tables (the live collectors stored ten rows
per table by default). Every ingest path drops it here and states how many rows
it dropped; the collector output contract has no such field.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SAMPLE_ROWS_KEY = "sample_data"


def without_sample_rows(tables: object) -> tuple[list[Any], int]:
    """``tables`` with no ``sample_data`` key, and how many rows were dropped.

    Returns new table dicts; the input is not changed. A value that is not a list
    is dropped too and counts no rows; a non-dict entry is kept as is.
    """
    if not isinstance(tables, list):
        return [], 0
    kept: list[Any] = []
    dropped = 0
    for table in tables:
        if isinstance(table, Mapping) and SAMPLE_ROWS_KEY in table:
            rows = table[SAMPLE_ROWS_KEY]
            if isinstance(rows, list):
                dropped += len(rows)
            kept.append({k: v for k, v in table.items() if k != SAMPLE_ROWS_KEY})
        else:
            kept.append(table)
    return kept, dropped


def raw_collection_without_sample_rows(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    """A raw collection-script file with no table rows, and how many were dropped.

    The scripts call this before they keep their own copy of a raw collection
    (``uploads/collector-output.json``), so no copy the tool writes holds a row. A
    file a customer uploads through the API or AWS Transform is kept as sent; the
    parser drops its rows from everything derived from it. Returns a new dict.
    """
    out = dict(data)
    if "tables" not in out:
        return out, 0
    tables, dropped = without_sample_rows(out["tables"])
    out["tables"] = tables
    return out, dropped


def collector_output_without_sample_rows(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    """A collector output with no table rows, and how many rows were dropped.

    Returns a new dict (the input is not changed); one with no
    ``database_schema.tables`` list is returned as a plain copy.
    """
    out = dict(payload)
    schema = out.get("database_schema")
    if not isinstance(schema, Mapping) or not isinstance(schema.get("tables"), list):
        return out, 0
    tables, dropped = without_sample_rows(schema["tables"])
    out["database_schema"] = {**schema, "tables": tables}
    return out, dropped
