"""
Group Input — the compact per-group input a schema design group reads.

``--split`` writes one ``input_group_<G>.json`` per group. A group subagent
reads it with the Read tool (2000 lines per call), and the Bedrock path
validates its ``collector_output`` / ``analysis_output`` against the full
``CollectorOutputContract`` / ``AnalysisOutputContract``. So the file keeps the
contract shape but holds only what the group's design uses (issue #272):

- the group's queries, with the query fields the schema design projection
  (``AgentQueryPattern``) carries, plus the source ``queries`` header except
  ``total_queries_analyzed``: that counts the whole database, so the skill's
  coverage check (patterns / total_queries_analyzed) would flag every group
  as incomplete; a group's own count is ``_filtered_count``;
- the tables those queries touch, with the projected table, column, index and
  foreign-key fields (plus the native ``data_type``), the triggers on those
  tables (the skill writes ``migration_notes`` for them), and no procedures,
  views or duplicate top-level ``tables``;
- the analysis patterns and anti-patterns that touch the group's queries or
  tables, with their ``query_ids`` / ``table_ids`` trimmed to the group's, the
  aggregates and table recommendations for the group's tables;
- no null or empty optional fields.

``render_group_input`` writes it one record per line (a column, an index, a
query) instead of one field per line. A query text too long for one Read line
is also given as ``query_text_lines``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from src.contracts.analysis_output import (
    AggregateRecommendation,
    AntiPattern,
    Pattern,
    TableRecommendation,
)
from src.contracts.collector_output import (
    Column,
    ForeignKey,
    Index,
    QueryPattern,
    Table,
    Trigger,
)
from src.contracts.schema_design_input import (
    AgentAggregateRecommendation,
    AgentAntiPattern,
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentPattern,
    AgentQueryPattern,
    AgentTable,
)

# Read returns at most 2000 lines per call and cuts lines longer than 2000
# characters, so records are inlined only up to this width.
LINE_WIDTH = 1500
# A query text longer than this is also split into ``query_text_lines``.
QUERY_TEXT_LINE = 160
# Characters per Read page. Read refuses a call whose output it estimates
# over READ_TOKEN_LIMIT tokens, and it estimates about one token per two
# characters, so a page stays well under 2 * READ_TOKEN_LIMIT characters.
READ_TOKEN_LIMIT = 25_000
READ_CHARS_PER_TOKEN = 2
READ_PAGE_CHARS = 40_000
# Read returns at most this many lines per call.
READ_PAGE_LINES = 2000

# Fields a group design reads, per record: the schema design projection's
# fields plus the full contract's required ones (so the Bedrock path still
# validates the file).
# ``data_type`` is the native type the design reads when
# ``normalized_data_type`` is empty. ``ordinal_position`` stays: the list order
# is not always the ordinal (MySQL), and the skill sorts attributes by it.
_COLUMN_FIELDS = set(AgentColumn.model_fields) | {"data_type"}
_INDEX_FIELDS = set(AgentIndex.model_fields)
_FK_FIELDS = set(AgentForeignKey.model_fields)
_TABLE_FIELDS = set(AgentTable.model_fields)
_QUERY_FIELDS = set(AgentQueryPattern.model_fields)
_PATTERN_FIELDS = set(AgentPattern.model_fields)
_ANTI_PATTERN_FIELDS = set(AgentAntiPattern.model_fields)
_AGGREGATE_FIELDS = set(AgentAggregateRecommendation.model_fields) | set(
    AggregateRecommendation.model_fields
)
_TABLE_REC_FIELDS = set(TableRecommendation.model_fields)
_TRIGGER_FIELDS = set(Trigger.model_fields)


def _required(model: type[BaseModel]) -> set[str]:
    return {k for k, f in model.model_fields.items() if f.is_required()}


_REQUIRED = {
    "column": _required(Column),
    "index": _required(Index),
    "fk": _required(ForeignKey),
    "table": _required(Table),
    "query": _required(QueryPattern),
    "pattern": _required(Pattern),
    "anti_pattern": _required(AntiPattern),
    "aggregate": _required(AggregateRecommendation),
    "table_rec": _required(TableRecommendation),
    "trigger": _required(Trigger),
}


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, (list, dict, str)) and len(value) == 0)


def _slim(record: dict, fields: set[str], kind: str) -> dict:
    """Keep ``fields`` of ``record``, dropping null/empty optional values."""
    required = _REQUIRED[kind]
    return {k: v for k, v in record.items() if k in fields and (k in required or not _empty(v))}


def _slim_column(column: dict) -> dict:
    slim = _slim(column, _COLUMN_FIELDS, "column")
    if slim.get("is_auto_increment") is False:  # the contract default
        del slim["is_auto_increment"]
    return slim


def _slim_table(table: dict) -> dict:
    slim = _slim(table, _TABLE_FIELDS, "table")
    slim["columns"] = [_slim_column(c) for c in table.get("columns") or []]
    for key, fields, kind in (
        ("indexes", _INDEX_FIELDS, "index"),
        ("foreign_keys", _FK_FIELDS, "fk"),
    ):
        if slim.get(key):
            slim[key] = [_slim(x, fields, kind) for x in slim[key]]
    return slim


def _split_text(text: str, width: int = QUERY_TEXT_LINE) -> list[str]:
    """Break ``text`` into lines of at most ``width`` characters.

    Runs of whitespace (newlines, indentation) collapse to one space, so
    ``" ".join(lines)`` is the text with its whitespace normalised, not the
    exact ``query_text``. A word longer than ``width`` is hard-wrapped.
    """
    lines: list[str] = []
    current = ""
    words: list[str] = []
    for word in re.split(r"\s+", text.strip()):
        words.extend(word[i : i + width] for i in range(0, len(word), width))
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines


def _slim_query(query: dict) -> dict:
    slim = _slim(query, _QUERY_FIELDS, "query")
    text = slim.get("query_text") or ""
    if len(json.dumps(text, ensure_ascii=False)) > LINE_WIDTH:  # as rendered, escapes included
        slim["query_text_lines"] = _split_text(text)
    return slim


def _trim_signal(signal: dict, fields: set[str], kind: str, query_ids: set[str], tables: set[str]):
    """An analysis pattern trimmed to the group's queries and tables, or None if unrelated."""
    qids = [q for q in signal.get("query_ids") or [] if q in query_ids]
    tids = [t for t in signal.get("table_ids") or [] if t in tables]
    if not qids and not tids:
        return None
    return _slim({**signal, "query_ids": qids, "table_ids": tids}, fields, kind)


def build_group_input(
    *,
    job_id: str,
    database_name: str,
    engine: str,
    group_index: int,
    group_name: str,
    primary_tables: list[str],
    group_queries: list[dict],
    group_tables: list[dict],
    collector_output: dict,
    analysis_output: dict,
    total_queries: int,
) -> dict:
    """The compact input for one group (see the module docstring)."""
    table_ids = {t["table_id"] for t in group_tables if t.get("table_id")}
    table_keys = table_ids | {t["table_name"] for t in group_tables if t.get("table_name")}
    query_ids = {q.get("query_id", "") for q in group_queries}

    source_queries = collector_output.get("queries", {})
    queries_header = {
        k: v
        for k, v in source_queries.items()
        if k not in ("query_patterns", "total_queries_analyzed")
        and not k.startswith("_")
        and not _empty(v)
    }
    triggers = [
        _slim(t, _TRIGGER_FIELDS, "trigger")
        for t in collector_output.get("database_schema", {}).get("triggers") or []
        if t.get("table_id") in table_keys  # schema-qualified id or bare name
    ]
    group_schema: dict[str, Any] = {"tables": [_slim_table(t) for t in group_tables]}
    if triggers:
        group_schema["triggers"] = triggers
    group_collector = {
        "contract_version": collector_output.get("contract_version"),
        "job_id": collector_output.get("job_id"),
        "metadata": collector_output.get("metadata"),
        "database_schema": group_schema,
        "queries": {
            **queries_header,
            "query_patterns": [_slim_query(q) for q in group_queries],
            "_filtered": True,
            "_filter_engine": engine,
            "_group_index": group_index,
            "_group_name": group_name,
            "_original_count": total_queries,
            "_filtered_count": len(group_queries),
        },
        "metrics": collector_output.get("metrics"),
    }

    workload = analysis_output.get("workload_analysis") or {}
    patterns = [
        p
        for p in (
            _trim_signal(x, _PATTERN_FIELDS, "pattern", query_ids, table_ids)
            for x in workload.get("patterns_detected") or []
        )
        if p
    ]
    anti_patterns = [
        p
        for p in (
            _trim_signal(x, _ANTI_PATTERN_FIELDS, "anti_pattern", query_ids, table_ids)
            for x in workload.get("anti_patterns_detected") or []
        )
        if p
    ]
    group_workload: dict[str, Any] = {"patterns_detected": patterns}
    if anti_patterns:
        group_workload["anti_patterns_detected"] = anti_patterns

    group_analysis: dict[str, Any] = {
        k: v
        for k, v in analysis_output.items()
        if k not in ("table_recommendations", "workload_analysis", "aggregate_recommendations")
        and not _empty(v)
    }
    group_analysis["table_recommendations"] = [
        _slim(r, _TABLE_REC_FIELDS, "table_rec")
        for r in analysis_output.get("table_recommendations") or []
        if r.get("table_id") in table_keys or r.get("table_name") in table_keys
    ]
    group_analysis["workload_analysis"] = group_workload
    aggregates = [
        _slim(a, _AGGREGATE_FIELDS, "aggregate")
        for a in analysis_output.get("aggregate_recommendations") or []
        if set(a.get("member_tables") or []) & table_ids
    ]
    if aggregates:
        group_analysis["aggregate_recommendations"] = aggregates

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_engine": engine,
        "group_index": group_index,
        "group_name": group_name,
        "group_primary_tables": primary_tables,
        "collector_output": group_collector,
        "analysis_output": group_analysis,
    }


def render_group_input(data: Any) -> str:
    """JSON with one record per line: a container is inlined when it fits ``LINE_WIDTH``."""
    return _render(data, 0) + "\n"


def _render(value: Any, indent: int) -> str:
    flat = json.dumps(value, default=str, ensure_ascii=False)
    if not isinstance(value, (dict, list)) or not value or len(flat) + 2 * indent <= LINE_WIDTH:
        return flat
    pad = "  " * (indent + 1)
    if isinstance(value, dict):
        items = [
            f"{pad}{json.dumps(k, ensure_ascii=False)}: {_render(v, indent + 1)}"
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + "  " * indent + "}"
    items = [f"{pad}{_render(v, indent + 1)}" for v in value]
    return "[\n" + ",\n".join(items) + "\n" + "  " * indent + "]"


def read_pages(text: str) -> list[dict[str, int]]:
    """Read tool pages (``offset``/``limit``, 1-based lines) covering ``text``.

    Each page holds whole lines, at most ``READ_PAGE_CHARS`` characters (a
    longer single line gets a page of its own) and ``READ_PAGE_LINES`` lines.
    """
    pages: list[dict[str, int]] = []
    start, size, count = 1, 0, 0
    for n, line in enumerate(text.splitlines(), start=1):
        if count and (size + len(line) + 1 > READ_PAGE_CHARS or count >= READ_PAGE_LINES):
            pages.append({"offset": start, "limit": count})
            start, size, count = n, 0, 0
        size += len(line) + 1
        count += 1
    if count:
        pages.append({"offset": start, "limit": count})
    return pages
