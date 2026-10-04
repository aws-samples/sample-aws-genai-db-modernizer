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

from collections import defaultdict

from src.contracts.schema_design_input import AgentAnalysisInput, AgentCollectorInput
from src.tools.schema.aurora_common.ddl_generator import DdlResult
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


def _hot_queries(collector: AgentCollectorInput, limit: int) -> list[dict]:
    ranked = sorted(collector.queries.query_patterns, key=_qps, reverse=True)[:limit]
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
        for name in (
            "filter_columns",
            "sort_columns",
            "rows_returned_avg",
            "execution_time_ms_avg",
            "full_table_scans",
            "queries_without_index",
            "has_joins",
            "has_aggregations",
            "has_text_search",
        ):
            value = getattr(q, name)
            if value not in (None, [], 0, False):
                entry[name] = value
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


def build_design_view(
    base: AuroraDesignBase,
    collector: AgentCollectorInput,
    analysis: AgentAnalysisInput | None = None,
    raw_collector: dict | None = None,
    hot_query_limit: int = HOT_QUERY_LIMIT,
) -> dict:
    """The compact view the model designs the delta from."""
    draft = base.generate()
    rates = _table_rates(collector)
    residual_cols = {(r["table"], r["column"]) for r in draft.residuals}
    tables = []
    for src, ddl in zip(base.tables, draft.tables, strict=True):
        rate = rates.get(_key(src.table_name), {"read": 0.0, "write": 0.0, "n": 0})
        tables.append(
            {
                "table_name": src.table_name,
                "row_count": src.row_count,
                "size_mb": src.size_mb,
                "read_qps": round(rate["read"], 4),
                "write_qps": round(rate["write"], 4),
                "query_count": int(rate["n"]),
                "primary_key": src.primary_key or [],
                "columns": [
                    f"{c.name} {c.aurora_type}"
                    + (
                        f" (residual; source {base.source_data_types.get((src.table_name, c.name)) or '?'})"
                        if (src.table_name, c.name) in residual_cols
                        else ""
                    )
                    for c in ddl.columns
                ],
                "indexes": list(ddl.index_sql),
                "foreign_key_count": len(ddl.fk_sql),
            }
        )
    return {
        "migration_strategy": base.migration_strategy,
        "source_engine": base.source_engine,
        "table_count": len(tables),
        "column_count": sum(len(t.columns) for t in draft.tables),
        "in_scope_query_count": len(collector.queries.query_patterns),
        "tables": tables,
        "residual_types": _residual_types(base, draft),
        "hot_queries": _hot_queries(collector, hot_query_limit),
        "analysis": _analysis_summary(analysis),
        "source_features": _source_features(
            raw_collector or {}, {_key(t.table_name) for t in base.tables}
        ),
    }
