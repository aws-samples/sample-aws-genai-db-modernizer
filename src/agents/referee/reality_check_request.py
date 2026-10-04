"""Reality Check request — the compact, bounded brief the consolidation reviewer reads.

Both LLM paths review the same thing: for each consolidation, the queries it
moved, each with its SQL (truncated), type, tables, triage signals and calls per
second. The Bedrock validator sends those records in its prompt
(``consolidation_validator.validate_consolidations``); ``--llm-mode external``
writes them to ``reality-check/llm_input.json`` for the ``/reality-check``
command, together with the context its executive summary needs.

The external request used to embed the full collector output and every engine's
analysis output: about 7.5 MB on the discourse sample, which a headless subagent
cannot read, and still without the list of moved queries (#285). It now holds:

- ``read_pages``: Read tool pages (``offset``/``limit``) covering the file;
- ``consolidation_validation.consolidations``: the consolidation records, each
  with ``moved_queries`` (one record per line);
- ``executive_summary``: distributions, consolidation records, a per-engine
  unique-value summary (counts, not query id lists), patterns,
  recommendations, absorption candidates and the assessment scope.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from src.agents.schema_design.group_input import read_pages, render_group_input

# SQL characters kept per moved query (the Bedrock validator's long-standing cut).
SQL_CHARS = 500
# Most characters the kept SQL may take as a JSON string: quotes and control
# characters are escaped, and Read truncates a line past 2000 characters.
SQL_JSON_CHARS = 1000

# Keys of ``unique_value_assessment`` entries that hold query id lists; the
# request carries their counts instead.
_QUERY_LIST_KEYS = ("unique_queries", "redundant_queries")


def query_signal_map(triage: dict) -> dict[str, list[str]]:
    """``{query_id: [signal names]}`` from the triage signals."""
    signals: dict[str, list[str]] = defaultdict(list)
    for signal in triage.get("signals", []):
        for qid in signal.get("query_ids", []):
            signals[qid].append(signal.get("signal", ""))
    return dict(signals)


def moved_queries(
    consolidation: dict,
    revised_assignments: list[dict],
    original_assignments: list[dict] | None = None,
) -> list[dict]:
    """The revised assignments ``consolidation`` moved from its source to its target.

    With ``original_assignments`` a query counts as moved when it sat on
    ``from_engine`` and now sits on ``to_engine`` (this covers every kind of move,
    Aurora absorption included). Without them, fall back to the assignment
    reason the consolidation step writes.
    """
    from_engine = consolidation["from_engine"]
    to_engine = consolidation["to_engine"]
    on_target = [qa for qa in revised_assignments if qa.get("assigned_engine") == to_engine]
    if original_assignments is None:
        marker = f"consolidated from {from_engine}"
        return [qa for qa in on_target if marker in qa.get("assignment_reason", "")]
    was_on = {qa["query_id"]: qa.get("assigned_engine") for qa in original_assignments}
    return [qa for qa in on_target if was_on.get(qa["query_id"]) == from_engine]


def moved_query_record(
    query_id: str, query_map: dict[str, dict], query_signals: dict[str, list[str]]
) -> dict[str, Any]:
    """What a reviewer needs to judge one moved query."""
    q = query_map.get(query_id, {})
    text = q.get("query_text", "") or ""
    sql = text[:SQL_CHARS]
    while len(json.dumps(sql, ensure_ascii=False)) > SQL_JSON_CHARS:
        sql = sql[: len(sql) * 3 // 4]
    record: dict[str, Any] = {
        "query_id": query_id,
        "type": q.get("query_type", ""),
        "cps": round(float(q.get("calls_per_second") or 0), 3),
        "tables": q.get("tables_accessed", []) or [],
        "signals": query_signals.get(query_id, []),
        "sql": sql,
    }
    if len(sql) < len(text):
        record["sql_chars"] = len(text)  # the SQL above is truncated
    return record


def build_reality_check_request(det: dict, absorption_candidates: list[str]) -> dict:
    """The external Reality Check request for a deterministic result (no pages yet)."""
    collector = det.get("collector_output", {})
    queries = collector.get("queries", {}).get("query_patterns", [])
    query_map = {q["query_id"]: q for q in queries}
    signals = query_signal_map(det.get("triage", {}))
    original = det.get("assignment", {}).get("query_assignments")

    # A query listed under an earlier record of the same move is not listed again
    listed: dict[tuple[str, str], set[str]] = defaultdict(set)
    reviewed = []
    for c in det["consolidations"]:
        seen = listed[(c["from_engine"], c["to_engine"])]
        ids = [
            qa["query_id"]
            for qa in moved_queries(c, det["revised_assignments"], original)
            if qa["query_id"] not in seen
        ]
        seen.update(ids)
        reviewed.append(
            {**c, "moved_queries": [moved_query_record(q, query_map, signals) for q in ids]}
        )
    return {
        "consolidation_validation": {"consolidations": reviewed},
        "executive_summary": {
            "scope": {
                "source_tables": len(collector.get("database_schema", {}).get("tables", [])),
                "query_patterns": len(queries),
                "engines_evaluated": sorted(det["before_distribution"]),
            },
            "before_distribution": det["before_distribution"],
            "after_distribution": det["after_distribution"],
            "consolidations": det["consolidations"],
            "unique_value_assessment": _compact_unique_value(det["unique_value_assessment"]),
            "architectural_patterns": det["architectural_patterns"],
            "recommendations": det["recommendations"],
            "absorption_candidates": absorption_candidates,
        },
    }


def with_read_pages(request: dict) -> dict:
    """``request`` with a leading ``read_pages`` that covers its own rendering."""
    pages: list[dict[str, int]] = []
    for _ in range(10):  # the page list only changes line 2; it settles at once
        paged = {"read_pages": pages, **request}
        new_pages = read_pages(render_group_input(paged))
        if new_pages == pages:
            return paged
        pages = new_pages
    return {"read_pages": pages, **request}


def render_reality_check_request(request: dict) -> str:
    """JSON text, one record per line (the same renderer as schema-design groups)."""
    return render_group_input(request)


def _compact_unique_value(assessment: dict) -> dict:
    compact: dict[str, dict] = {}
    for engine, entry in assessment.items():
        slim = {k: v for k, v in entry.items() if k not in _QUERY_LIST_KEYS}
        for key in _QUERY_LIST_KEYS:
            if isinstance(entry.get(key), list):
                slim[f"{key}_count"] = len(entry[key])
        if isinstance(slim.get("unique_ratio"), float):
            slim["unique_ratio"] = round(slim["unique_ratio"], 3)
        compact[engine] = slim
    return compact
