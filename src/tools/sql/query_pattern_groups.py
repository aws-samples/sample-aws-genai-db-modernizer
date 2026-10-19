"""Collected queries grouped into distinct query patterns (#1146).

Two collected statements are one query pattern when they read or write the same rows
the same way (``src.tools.sql.access_signature.query_signature``, maintainer rule on
#1104 and #1146): the same tables, the same predicate shape with the literals removed
and every IN-list length one, the same ``ORDER BY``, the same limit class, the same
operation (an INSERT, an UPDATE, a DELETE and an upsert are four) and, for an
aggregate, the same ``GROUP BY``, ``HAVING`` and aggregate functions. The columns they
return (and ``DISTINCT``) never separate them. A statement with no signature (a fallback
parse, a digest cut short, a ``SET``) is a pattern of its own. "Query pattern", not "access pattern": in the deliverables an access pattern is
what a schema designer designs.

:func:`group_by_query_pattern` is the one grouping. It reads the facts through
``src.tools.sql.query_facts`` (the collector's stored readings when it has them) in the
source engine's dialect, and keeps every query underneath its pattern: its id and
frequency stay as collected, and the pattern adds them up. Routing never reads it: the
query stays the unit (ADR-031). Its readers are the facts layer (the deliverables'
"N queries in M distinct query patterns", ``src.shared.report_facts``), the schema-design
inputs (:func:`repeated_query_patterns`) and the design-group splitter, which keeps a
pattern's queries in one group, so a designer sees each pattern once.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.tools.sql.access_signature import QuerySignature, query_signature, returned_columns
from src.tools.sql.query_facts import query_facts

# The prefix of a pattern id: ``PAT-1``, ``PAT-2``, ... in the order of each pattern's
# first query in the input.
PATTERN_ID_PREFIX = "PAT-"


@dataclass(frozen=True)
class QueryPatternGroup:
    """One query pattern and the collected queries that make it."""

    pattern_id: str
    query_ids: tuple[str, ...]
    frequency_per_hour: float
    calls_per_second: float | None
    operation: str
    tables: tuple[str, ...]
    returned_columns: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "query_ids": list(self.query_ids),
            "frequency_per_hour": self.frequency_per_hour,
            "calls_per_second": self.calls_per_second,
            "operation": self.operation,
            "tables": list(self.tables),
            "returned_columns": list(self.returned_columns),
        }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def group_by_query_pattern(
    queries: Iterable[Mapping[str, Any]], source_engine: str
) -> list[QueryPatternGroup]:
    """The query patterns of ``queries`` (collected query patterns: ``query_id``,
    ``query_text``, ``frequency_per_hour``, ``calls_per_second``), one group per pattern,
    in the order of each pattern's first query. A query id seen twice counts once."""
    order: list[QuerySignature | str] = []
    members: dict[QuerySignature | str, list[Mapping[str, Any]]] = {}
    shape: dict[QuerySignature | str, tuple[str, tuple[str, ...]]] = {}
    columns: dict[QuerySignature | str, set[str]] = {}
    seen: set[str] = set()
    for query in queries:
        qid = str(query.get("query_id") or "")
        if not qid or qid in seen:
            continue
        seen.add(qid)
        facts = query_facts(str(query.get("query_text") or ""), source_engine)
        access = facts.access_facts()
        signature = query_signature(access, facts.write_targets, facts.upsert)
        key: QuerySignature | str = signature if signature is not None else f"query:{qid}"
        if key not in members:
            order.append(key)
            members[key] = []
            columns[key] = set()
            if signature is not None:
                # Every table it reads, a CTE's or a subquery's too, and every one it writes.
                read = {t for scope in signature.scopes for t in scope.tables}
                tables = tuple(sorted({*signature.writes, *read}))
                # A write is named by its item operation (insert, update, delete, upsert).
                shape[key] = (signature.write_kind or signature.scopes[0].operation, tables)
            else:
                shape[key] = ("unread", ())
        members[key].append(query)
        columns[key].update(returned_columns(access))
    groups = []
    for n, key in enumerate(order, start=1):
        rows = members[key]
        calls = [c for c in (_number(q.get("calls_per_second")) for q in rows) if c is not None]
        op, tables = shape[key]
        groups.append(
            QueryPatternGroup(
                pattern_id=f"{PATTERN_ID_PREFIX}{n}",
                query_ids=tuple(str(q.get("query_id")) for q in rows),
                frequency_per_hour=round(
                    sum(_number(q.get("frequency_per_hour")) or 0.0 for q in rows), 6
                ),
                calls_per_second=round(sum(calls), 6) if calls else None,
                operation=op,
                tables=tables,
                returned_columns=tuple(sorted(columns[key], key=lambda c: (c != "*", c))),
            )
        )
    return groups


def repeated_query_patterns(
    queries: Iterable[Mapping[str, Any]], source_engine: str
) -> list[dict[str, Any]]:
    """The query patterns of ``queries`` that more than one query makes, as the
    schema-design inputs list them (``queries.query_pattern_groups``): each once, with
    its member query ids, combined frequency and the union of the columns they return.
    Every query not listed is a pattern of its own."""
    return [
        g.to_dict() for g in group_by_query_pattern(queries, source_engine) if len(g.query_ids) > 1
    ]


# Where the schema-design inputs carry them: the filtered collection's ``queries``.
INPUT_KEY = "query_pattern_groups"


def listed_query_patterns(collector_output: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The ``queries.query_pattern_groups`` a schema-design input file carries, for the
    designer's projection (the collector contract does not hold them), or None."""
    queries = collector_output.get("queries")
    listed = queries.get(INPUT_KEY) if isinstance(queries, Mapping) else None
    return [g for g in listed if isinstance(g, dict)] if isinstance(listed, list) else None
