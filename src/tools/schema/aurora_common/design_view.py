"""Compact view of an Aurora design for the model (issue #273).

The model decides only Aurora-level changes on top of the deterministic draft,
so it does not need the draft itself, the full collector output or the full
analysis. This view carries what those decisions need: each table's columns
(name and draft type), primary key, indexes, row count and read/write rate;
residual columns grouped by source data type; the hottest queries; and the
source features (triggers, procedures, views) that may need app-layer notes.

Its size grows with the table and column count only through one short line
per column, and is independent of the query count beyond ``hot_query_limit``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from src.contracts.schema_design_input import (
    AgentAnalysisInput,
    AgentCollectorInput,
    AgentQueryPattern,
    AgentTable,
)
from src.tools.schema.aurora_common.ddl_generator import DdlResult, secondary_indexes
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase

HOT_QUERY_LIMIT = 40
_QUERY_TEXT_LIMIT = 240
_EXAMPLES_PER_TYPE = 5
_FEATURE_LIMIT = 200
_WRITE_TYPES = {"INSERT", "UPDATE", "DELETE", "MERGE"}


def _key(name: str) -> str:
    cleaned = "".join(c for c in name if c not in '`"[]').strip().lower()
    return cleaned.rsplit(".", 1)[-1].strip()


def _qps(q) -> float:
    if q.calls_per_second is not None:
        return float(q.calls_per_second)
    return float(q.frequency_per_hour) / 3600.0


def _table_rates(collector: AgentCollectorInput) -> dict[str, dict[str, float]]:
    rates: dict[str, dict[str, float]] = defaultdict(lambda: {"read": 0.0, "write": 0.0, "n": 0})
    for q in collector.queries.query_patterns:
        kind = "write" if (q.query_type and q.query_type.value in _WRITE_TYPES) else "read"
        for t in q.tables_accessed:
            rates[_key(t)][kind] += _qps(q)
            rates[_key(t)]["n"] += 1
    return rates


def _residual_types(base: AuroraDesignBase, draft: DdlResult) -> list[dict]:
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for r in draft.residuals:
        raw = base.source_data_types.get((r["table"], r["column"]), "")
        groups[(raw, r["fallback_type"], r["reason"])].append(f"{r['table']}.{r['column']}")
    return [
        {
            "source_data_type": raw or None,
            "draft_fallback_type": fallback,
            "reason": reason,
            "count": len(cols),
            "examples": cols[:_EXAMPLES_PER_TYPE],
        }
        for (raw, fallback, reason), cols in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]


def _load(q) -> float:
    """Share of database time: load contribution when collected, else total time."""
    if q.db_load_contribution_percent is not None:
        return float(q.db_load_contribution_percent)
    if q.total_time_ms is not None:
        return float(q.total_time_ms)
    return _qps(q) * float(q.execution_time_ms_avg or 0.0)


_SYSTEM_SCHEMAS = ("pg_catalog.", "information_schema.", "mysql.", "performance_schema.", "sys.")
_NON_DESIGN_STATEMENT = re.compile(
    r"^\s*(?:BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION|SAVEPOINT|RELEASE|END|SET|SHOW|RESET"
    r"|DISCARD|DEALLOCATE|VACUUM|ANALYZE|CHECKPOINT|LISTEN|NOTIFY|UNLISTEN|LOCK"
    r"|REFRESH\s+MATERIALIZED\s+VIEW)\b",
    re.IGNORECASE,
)


def _is_system_table(name: str) -> bool:
    lowered = name.strip().strip('"`').lower()
    if lowered.startswith(_SYSTEM_SCHEMAS):
        return True
    bare = lowered.rsplit(".", 1)[-1]
    return bare.startswith("pg_") or lowered in ("unknown", "")


def _designable(q: AgentQueryPattern) -> bool:
    """Whether a query can inform a schema decision.

    Dropped: transaction control and maintenance statements (BEGIN, COMMIT,
    SET, SHOW, VACUUM, REFRESH MATERIALIZED VIEW, ...), and queries that touch
    only system catalogs (pg_catalog, information_schema, mysql.*, pg_*) or
    an unknown table.
    """
    if _NON_DESIGN_STATEMENT.match(q.query_text or ""):
        return False
    tables = q.tables_accessed or []
    return bool(tables) and not all(_is_system_table(t) for t in tables)


def _bad_index_counts(raw_collector: dict) -> dict[str, int]:
    """``queries_with_bad_index`` is not in the schema-design projection; read it raw."""
    counts: dict[str, int] = {}
    for q in (raw_collector.get("queries") or {}).get("query_patterns") or []:
        value = q.get("queries_with_bad_index")
        if value:
            counts[str(q.get("query_id"))] = int(value)
    return counts


_HOT_QUERY_FIELDS = (
    "filter_columns",
    "sort_columns",
    "rows_returned_avg",
    "rows_examined_avg",
    "execution_time_ms_avg",
    "execution_time_ms_p95",
    "total_time_ms",
    "db_load_contribution_percent",
    "full_table_scans",
    "queries_without_index",
    "has_joins",
    "has_aggregations",
    "has_text_search",
)


def _hot_queries(collector: AgentCollectorInput, limit: int, raw_collector: dict) -> list[dict]:
    """The busiest queries: top by database load (time) together with top by call rate.

    A slow query called rarely and a fast query called constantly can both need
    an index, so neither ranking alone is enough. Ordered by load.
    """
    patterns = [q for q in collector.queries.query_patterns if _designable(q)]
    by_load = sorted(patterns, key=_load, reverse=True)
    by_qps = sorted(patterns, key=_qps, reverse=True)
    chosen: dict[str, AgentQueryPattern] = {}
    for q in [x for pair in zip(by_load, by_qps, strict=True) for x in pair]:
        if len(chosen) >= limit:
            break
        chosen.setdefault(q.query_id, q)
    ranked = sorted(chosen.values(), key=_load, reverse=True)
    bad_index = _bad_index_counts(raw_collector)
    out = []
    for q in ranked:
        text = q.query_text or ""
        entry = {
            "query_id": q.query_id,
            "query_type": q.query_type.value if q.query_type else None,
            "calls_per_second": round(_qps(q), 4),
            "tables_accessed": q.tables_accessed,
            "query_text": (
                text if len(text) <= _QUERY_TEXT_LIMIT else text[:_QUERY_TEXT_LIMIT] + "..."
            ),
        }
        for name in _HOT_QUERY_FIELDS:
            value = getattr(q, name)
            if value not in (None, [], 0, False):
                entry[name] = round(value, 3) if isinstance(value, float) else value
        if bad_index.get(q.query_id):
            entry["queries_with_bad_index"] = bad_index[q.query_id]
        out.append(entry)
    return out


def _source_features(raw_collector: dict, table_keys: set[str]) -> dict:
    schema = raw_collector.get("database_schema") or {}
    triggers = [
        {
            "trigger_name": t.get("trigger_name"),
            "table": t.get("table_id"),
            "timing": t.get("timing"),
            "event_type": t.get("event_type"),
            "definition": (t.get("definition") or "")[:_QUERY_TEXT_LIMIT],
        }
        for t in schema.get("triggers") or []
        if _key(str(t.get("table_id") or "")) in table_keys
    ]
    procedures = schema.get("procedures") or []
    views = schema.get("views") or []
    return {
        "triggers": triggers[:_FEATURE_LIMIT],
        "procedures": {
            "count": len(procedures),
            "names": [
                f"{p.get('procedure_name')} ({p.get('procedure_type') or '?'})"
                for p in procedures[:_FEATURE_LIMIT]
            ],
        },
        "views": [v.get("view_name") for v in views[:_FEATURE_LIMIT]],
    }


def _analysis_summary(analysis: AgentAnalysisInput | None) -> dict:
    if analysis is None:
        return {}
    return {
        "patterns": [
            {
                "pattern_type": p.pattern_type,
                "confidence": getattr(p.confidence, "value", p.confidence),
                "query_count": len(p.query_ids or []),
                "table_ids": (p.table_ids or [])[:10],
            }
            for p in analysis.patterns_detected
        ],
        "anti_patterns": [
            {
                "anti_pattern_type": a.anti_pattern_type,
                "query_count": len(a.query_ids or []),
                "table_ids": (a.table_ids or [])[:10],
                "recommendation": a.recommendation,
            }
            for a in analysis.anti_patterns_detected or []
        ],
    }


_DEFAULT_LIMIT = 40


def _column_line(base: AuroraDesignBase, table: AgentTable, col, ddl_col, residual: bool) -> str:
    """``name TYPE [NOT NULL] [AI] [DEFAULT x] [(source t) | (residual; source t)]``."""
    parts = [f"{ddl_col.name} {ddl_col.aurora_type}"]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.is_auto_increment:
        parts.append("AI")
    if col.default_value is not None and not col.is_auto_increment:
        default = str(col.default_value)
        if len(default) > _DEFAULT_LIMIT:
            default = default[:_DEFAULT_LIMIT] + "..."
        parts.append(f"DEFAULT {default}")
    raw = base.source_data_types.get((table.table_name, col.column_name))
    if residual:
        parts.append(f"(residual; source {raw or '?'})")
    elif raw and " ".join(raw.lower().split()) != ddl_col.aurora_type.lower():
        parts.append(f"(source {raw})")
    return " ".join(parts)


def _index_line(index) -> str:
    line = f"{index.index_name}{' UNIQUE' if index.is_unique else ''} ({', '.join(index.columns)})"
    return f"{line} WHERE {index.predicate}" if index.predicate else line


def _fk_line(fk) -> str:
    return f"{', '.join(fk.columns)} -> {fk.referenced_table}({', '.join(fk.referenced_columns)})"


def build_design_view(
    base: AuroraDesignBase,
    collector: AgentCollectorInput,
    analysis: AgentAnalysisInput | None = None,
    raw_collector: dict | None = None,
    hot_query_limit: int = HOT_QUERY_LIMIT,
) -> dict:
    """The compact view the model designs the delta from.

    Key order is the reading order: summary, residual types and hot queries
    first, the per-table list last (it is the long part).
    """
    raw_collector = raw_collector or {}
    draft = base.generate()
    rates = _table_rates(collector)
    residual_cols = {(r["table"], r["column"]) for r in draft.residuals}
    tables = []
    for src, ddl in zip(base.tables, draft.tables, strict=True):
        rate = rates.get(_key(src.table_name), {"read": 0.0, "write": 0.0, "n": 0})
        entry: dict = {
            "table_name": src.table_name,
            "row_count": src.row_count,
            "size_mb": src.size_mb,
            "read_qps": round(rate["read"], 4),
            "write_qps": round(rate["write"], 4),
            "query_count": int(rate["n"]),
            "primary_key": src.primary_key or [],
            "columns": [
                _column_line(
                    base, src, col, ddl_col, (src.table_name, col.column_name) in residual_cols
                )
                for col, ddl_col in zip(src.columns, ddl.columns, strict=True)
            ],
            "indexes": [_index_line(i) for i in secondary_indexes(src)],
        }
        if src.foreign_keys:
            entry["foreign_keys"] = [_fk_line(fk) for fk in src.foreign_keys]
        tables.append(entry)
    return {
        "migration_strategy": base.migration_strategy,
        "source_engine": base.source_engine,
        "table_count": len(tables),
        "column_count": sum(len(t.columns) for t in draft.tables),
        "in_scope_query_count": len(collector.queries.query_patterns),
        "residual_types": _residual_types(base, draft),
        **({"index_notes": draft.index_notes} if draft.index_notes else {}),
        "hot_queries": _hot_queries(collector, hot_query_limit, raw_collector),
        "analysis": _analysis_summary(analysis),
        "source_features": _source_features(
            raw_collector, {_key(t.table_name) for t in base.tables}
        ),
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# Request file rendering: one table per line (issue #273)
# ---------------------------------------------------------------------------

MAX_LINE = 1800  # the Read tool truncates longer lines
_SENTINEL = "__AURORA_DESIGN_TABLES__"


def _compact(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _table_lines(table: dict, indent: str) -> list[str]:
    """One line per table; a table too long for one line gets one line per key
    (and one line per list item when a key's list is still too long)."""
    line = _compact(table)
    if len(indent) + len(line) <= MAX_LINE:
        return [indent + line]
    lines = [indent + "{"]
    items = list(table.items())
    for n, (key, value) in enumerate(items):
        comma = "," if n < len(items) - 1 else ""
        kv = f"{indent}  {_compact(key)}: {_compact(value)}{comma}"
        if len(kv) <= MAX_LINE or not isinstance(value, list):
            lines.append(kv)
            continue
        lines.append(f"{indent}  {_compact(key)}: [")
        for m, item in enumerate(value):
            lines.append(f"{indent}    {_compact(item)}{',' if m < len(value) - 1 else ''}")
        lines.append(f"{indent}  ]{comma}")
    lines.append(indent + "}")
    return lines


def _render(request: dict) -> str:
    """Pretty JSON with ``design_view.tables`` one per line."""
    view = request.get("design_view") or {}
    tables = view.get("tables")
    if not isinstance(tables, list):
        return json.dumps(request, indent=2, ensure_ascii=False) + "\n"
    shell = {**request, "design_view": {**view, "tables": _SENTINEL}}
    text = json.dumps(shell, indent=2, ensure_ascii=False)
    marker = f'"tables": "{_SENTINEL}"'
    head, tail = text.split(marker, 1)
    indent = head[head.rfind("\n") + 1 :] + "  "
    body: list[str] = []
    for n, table in enumerate(tables):
        lines = _table_lines(table, indent)
        if n < len(tables) - 1:
            lines[-1] += ","
        body.extend(lines)
    closing = indent[:-2]
    rendered = '"tables": [\n' + "\n".join(body) + ("\n" + closing if body else "") + "]"
    return "".join((head, rendered, tail, "\n"))


PAGE_CHARS = 40_000  # Read refuses pages much above ~48k characters
# Read prefixes each line with its number and a tab; count that against the page.
_READ_LINE_OVERHEAD = 8
_TOP_KEY = re.compile(r'^  "([A-Za-z_]+)":')
_VIEW_KEY = re.compile(r'^    "([A-Za-z_]+)":')
_TABLE_LINE = re.compile(r'^\s*\{"table_name": "((?:[^"\\]|\\.)*)"')


def _line_sections(lines: list[str]) -> list[str]:
    """The section each line belongs to (``output_schema``, ``design_view.tables``, ...)."""
    sections: list[str] = []
    top, current = "header", "header"
    for line in lines:
        if m := _TOP_KEY.match(line):
            top = m.group(1)
            current = top
        elif top == "design_view" and (m := _VIEW_KEY.match(line)):
            current = f"design_view.{m.group(1)}"
        sections.append(current)
    return sections


def compute_pages(text: str, page_chars: int = PAGE_CHARS) -> list[dict]:
    """Split ``text`` into consecutive Read pages of at most ``page_chars`` characters.

    Pages are line-aligned, 1-based (``offset`` is the first line, ``limit`` the
    line count) and together cover every line exactly once. ``section`` names
    what the page holds; table pages also name their first and last table.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    sections = _line_sections(lines)
    pages: list[dict] = []
    start, size = 0, 0
    for n, line in enumerate(lines):
        cost = len(line) + 1 + _READ_LINE_OVERHEAD
        if n > start and size + cost > page_chars:
            pages.append(_page(lines, sections, start, n))
            start, size = n, 0
        size += cost
    if start < len(lines):
        pages.append(_page(lines, sections, start, len(lines)))
    return pages


def _page(lines: list[str], sections: list[str], start: int, end: int) -> dict:
    first, last = sections[start], sections[end - 1]
    label = first if first == last else f"{first} .. {last}"
    names = [m.group(1) for line in lines[start:end] if (m := _TABLE_LINE.match(line))]
    if names:
        label += f" ({names[0]} .. {names[-1]})" if len(names) > 1 else f" ({names[0]})"
    return {"offset": start + 1, "limit": end - start, "section": label}


def _compact_pages(text: str, pages: list[dict]) -> str:
    """Write ``input_pages`` one page per line, so the header stays short."""
    if not pages:
        return text
    start = text.index('  "input_pages": [\n')
    end = text.index("\n  ],\n", start) + len("\n  ],")
    body = ",\n".join(f"    {_compact(page)}" for page in pages)
    return text[:start] + '  "input_pages": [\n' + body + "\n  ]," + text[end:]


def render_request(request: dict, *, paged: bool = True) -> str:
    """The external request file: pretty JSON, ``design_view.tables`` one per line.

    With ``paged`` (and a ``draft_fingerprint`` to anchor it), the header also
    lists ``input_pages`` right after ``draft_fingerprint``: the exact Read
    pages (``offset``/``limit``, each at most ``PAGE_CHARS`` characters) that
    cover the file, so the model never has to guess a page size. The pages
    describe the file they are written in, so they are recomputed until stable.
    """
    if not paged or "draft_fingerprint" not in request:
        return _render(request)
    pages: list[dict] = []
    for _ in range(10):
        ordered: dict = {}
        for key, value in request.items():
            if key == "input_pages":
                continue
            ordered[key] = value
            if key == "draft_fingerprint":
                ordered["input_pages"] = pages
        text = _compact_pages(_render(ordered), pages)
        new_pages = compute_pages(text)
        if new_pages == pages:
            return text
        pages = new_pages
    raise RuntimeError("input_pages did not stabilize")  # pragma: no cover
