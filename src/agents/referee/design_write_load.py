# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The average write load behind a DynamoDB design's write patterns (#805, #1753).

The design-time costs synthesis adds to the DynamoDB TCO row (the GSI cost, ``gsi_cost``,
and the derived copies' stream sync, ``derived_copy_sync``) price a write pattern on the
load the rest of the TCO is priced on: the average load, never ``design_rps``, which
carries the design's peak and growth factors (3x and 10x by default). One rule, here, so
both read the same rate:

- A pattern's queries are its ``query_ids`` that the collection has and, with an
  assignment, that the TCO sizes the engine for (its in-scope queries).
- A query writes ``calls_per_second`` times the rows each call changes, as collected
  (``rows_affected_avg``, else ``rows_returned_avg``, else one for a write with no row
  statistics). A fraction above zero stays a fraction: a call that usually changes no
  row (0.0156 rows on average) is not counted as changing one. An average of exactly
  zero is taken as missing data, like no row statistics, and counts as one row: a write
  that changes nothing on every call is far likelier a gap in the collection than real
  zero work, and counting it as zero would hide its cost. The analysis-phase estimate keeps its
  own floor of one item a call; this rule is the design-time costs' only.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


def collected_queries(
    collector: Mapping[str, Any] | None, query_ids: Iterable[str] | None = None
) -> dict[str, Mapping[str, Any]]:
    """The collected queries by id, only ``query_ids`` when given (None: every one)."""
    wanted = None if query_ids is None else {str(q) for q in query_ids}
    patterns = ((collector or {}).get("queries") or {}).get("query_patterns") or []
    return {
        str(q["query_id"]): q
        for q in patterns
        if isinstance(q, Mapping)
        and q.get("query_id")
        and (wanted is None or str(q["query_id"]) in wanted)
    }


def rows_per_call(query: Mapping[str, Any]) -> float:
    """The rows one call of a write changes, as collected (see the module docstring)."""
    for name in ("rows_affected_avg", "rows_returned_avg"):
        value = query.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if math.isfinite(value) and value > 0:  # exactly zero: missing data, as no value
            return float(value)
    return 1.0


def items_per_second(query: Mapping[str, Any]) -> float:
    """The items a write query changes a second, on average load."""
    cps = query.get("calls_per_second")
    if not isinstance(cps, (int, float)) or isinstance(cps, bool):
        return 0.0
    if cps <= 0 or not math.isfinite(cps):
        return 0.0
    return float(cps) * rows_per_call(query)


def pattern_queries(
    pattern: Mapping[str, Any], queries: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """The ids of ``pattern``'s queries that ``queries`` holds, in order, each once."""
    ids = [str(q) for q in pattern.get("query_ids") or [] if q is not None]
    return [q for q in dict.fromkeys(ids) if q in queries]
