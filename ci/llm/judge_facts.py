"""Structured ground-truth facts for the rubric judge (``ci/llm/judge.py``).

The judge used to see the first 8,000 characters of ``report.json`` (about
4% of a wordpress-sized report), which cut off ``architecture_type``,
``table_mappings``, ``tco_analysis``, ``risk_assessment`` and
``reality_check``. ``build_facts`` instead pulls out exactly the fields the
rubric criteria are checked against, as one compact JSON-serialisable dict:

* ``totals`` -- headline counts (tables, queries, risks), architecture type,
  overall risk.
* ``engines`` -- per selected engine: workload, confidence (the routed fit the
  deliverables show, #152; ``analysis_confidence`` is the all-tables average), monthly cost,
  schema counts, assignment reasons, the tables its assigned queries touch
  (``tables_served``) and its ``primary_tables`` from ``table_mappings``.
* ``eliminated_engines`` -- engines dropped by reality-check, with the engine
  that absorbed their queries.
* ``reality_check`` -- before/after query distribution and each move.
* ``cache_overlay`` -- the cache layer (#296): ElastiCache owns no query, so it
  is never part of the workload distribution; this block gives the hot reads it
  fronts (count, share of calls, owner engines) and the safety-net notes.
* ``tco`` -- the full ``tco_analysis`` (small).
* ``risks`` -- one compact entry per risk, including the engine(s) its
  queries are actually assigned to.
* ``migration_waves`` -- from ``report.json`` when synthesis emits it, else an
  explicit "not in report.json" marker.

Domain rule (the reason ``tables_served`` exists): assignments map *queries*
to engines, and a table can be served by several engines.
``table_mappings``' single ``recommended_database`` per table is not
ownership, so "does engine X touch table T" must be answered from
``tables_served`` -- ``effective_architecture.engines[].tables`` in the
synthesis ``llm_input.json`` when present, otherwise derived the same way
(tables accessed by the engine's assigned in-scope queries) from
``assignment.json``, or as a last resort from ``report.json`` ``query_groups``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

# Long free-text fields in the risk list are capped so one verbose risk can't
# crowd out the others; the cut is always marked.
RISK_TEXT_CHAR_CAP = 300

MIGRATION_WAVES_ABSENT = (
    "not in report.json; read the waves from the executive summary PDF's "
    "Migration Sequencing slide"
)

_ENGINE_PREFIX_RE = re.compile(r"^\[([a-z0-9_]+)\]\s*")


def _cap(text: Any, limit: int = RISK_TEXT_CHAR_CAP) -> str | None:
    if text is None:
        return None
    value = str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + f" ...[truncated: {limit} of {len(value)} chars shown]"


def _strip_db(table: str, db: str | None) -> str:
    """``wordpress.wp_posts`` -> ``wp_posts``: report.json qualifies table
    names with the source database, effective_architecture doesn't. One
    spelling everywhere lets the judge compare lists directly."""
    if db and table.startswith(db + "."):
        return table[len(db) + 1 :]
    return table


def _assigned_queries(
    report: dict[str, Any], assignment: dict[str, Any] | None
) -> Iterator[tuple[str | None, list[str], set[str], bool]]:
    """Yield ``(query_id, tables, engines, in_scope)`` per assigned query.

    The assignment's ``query_assignments`` are authoritative (same source as
    ``synthesis_grounding.engine_table_scope``). Without one, fall back to
    ``report.json`` ``query_groups``: the engines of the access patterns each
    query is linked to, else the group's engines. ``query_groups`` only lists
    queries that belong to a design group, so the fallback can be partial.
    """
    qas = (assignment or {}).get("query_assignments") or []
    if qas:
        for qa in qas:
            # The cache layer serves the tables of the reads it fronts (#296)
            engines = {e for e in (qa.get("assigned_engine"), qa.get("cache_engine")) if e}
            yield (
                qa.get("query_id"),
                list(qa.get("source_tables") or []),
                engines,
                bool(qa.get("in_scope", True)),
            )
        return
    for group in report.get("query_groups") or []:
        pattern_engine = {
            ap.get("pattern_id"): ap.get("engine") for ap in group.get("access_patterns") or []
        }
        group_engines = {e for e in group.get("engines") or [] if e}
        for query in group.get("source_queries") or []:
            linked = {
                pattern_engine[p]
                for p in query.get("linked_patterns") or []
                if pattern_engine.get(p)
            }
            yield (
                query.get("query_id"),
                list(query.get("tables_accessed") or []),
                linked or group_engines,
                True,
            )


def query_engines(
    report: dict[str, Any], assignment: dict[str, Any] | None = None
) -> dict[str, set[str]]:
    """Map each query id to the engine(s) it's assigned to."""
    out: dict[str, set[str]] = {}
    for qid, _tables, engines, _in_scope in _assigned_queries(report, assignment):
        if qid:
            out.setdefault(qid, set()).update(engines)
    return out


def derive_tables_served(
    report: dict[str, Any], assignment: dict[str, Any] | None = None
) -> dict[str, list[str]]:
    """Per engine, the tables its assigned in-scope queries access (the
    fallback for ``effective_architecture.engines[].tables``). Pseudo-tables
    that aren't source tables (``unknown``, ``DUAL``, a column name a parser
    mistook for a table) are dropped when ``table_mappings`` lists the
    source tables."""
    db = report.get("database_name")
    known = {_strip_db(m.get("source_table", ""), db) for m in report.get("table_mappings") or []}
    out: dict[str, set[str]] = {}
    for _qid, tables, engines, in_scope in _assigned_queries(report, assignment):
        if not in_scope:
            continue
        names = {_strip_db(t, db) for t in tables}
        if known:
            names &= known
        for engine in engines:
            out.setdefault(engine, set()).update(names)
    return {engine: sorted(tables) for engine, tables in out.items()}


def _tables_served(
    tables: list[str] | None, assigned_queries: Any, partial_scope: bool
) -> list[str] | None:
    """``None`` (unknown) rather than ``[]`` when the scope came from the
    partial query_groups fallback and an engine with assigned queries got no
    tables from it: an empty list would claim the engine touches no table."""
    if tables:
        return tables
    if partial_scope and isinstance(assigned_queries, int) and assigned_queries > 0:
        return None
    return tables or []


def _eliminated_engines(
    report: dict[str, Any], effective: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if effective and isinstance(effective.get("eliminated_engines"), list):
        return list(effective["eliminated_engines"])
    # Fallback: a full reality-check consolidation whose source engine no
    # longer carries any query.
    rc = report.get("reality_check") or {}
    after = rc.get("after_distribution") or {}
    out = []
    for move in rc.get("consolidations") or []:
        source = move.get("from_engine")
        if move.get("action") == "full" and source and not after.get(source):
            out.append({"engine": source, "absorbed_by": move.get("to_engine")})
    return out


def build_facts(
    report: dict[str, Any],
    llm_input: dict[str, Any] | None = None,
    assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the structured facts block for one job (see module docstring).

    ``llm_input`` is the synthesis ``llm_input.json`` (its
    ``effective_architecture`` is the preferred per-engine table scope) and
    ``assignment`` the matching ``assignment/v{N}/assignment.json`` (the
    query-to-engine map); both optional."""
    db = report.get("database_name")
    effective = (llm_input or {}).get("effective_architecture")
    if not isinstance(effective, dict):
        effective = None

    ranking = [r for r in report.get("ranking") or [] if isinstance(r, dict)]
    tco = report.get("tco_analysis") or {}
    cost_by_engine = {
        c.get("database"): c.get("monthly_cost_usd") for c in tco.get("cost_breakdown") or []
    }
    table_mappings = report.get("table_mappings") or []
    rec_arch = report.get("recommended_architecture") or {}
    rec_tables = {d.get("service"): d.get("table_count") for d in rec_arch.get("databases") or []}

    if effective:
        served_source = "effective_architecture (synthesis llm_input.json)"
        served = {
            e.get("engine"): sorted(e.get("tables") or []) for e in effective.get("engines") or []
        }
    elif (assignment or {}).get("query_assignments"):
        served_source = "derived from assignment.json query_assignments (no llm_input.json)"
        served = derive_tables_served(report, assignment)
    else:
        served_source = (
            "derived from report.json query_groups (no llm_input.json or assignment.json; "
            "covers only queries in a design group, so may be partial; null = unknown, "
            "not 'no tables')"
        )
        served = derive_tables_served(report)
    partial_scope = not effective and not (assignment or {}).get("query_assignments")

    # Names that aren't source tables ("unknown", "DUAL", a column a parser
    # took for a table) are dropped from risk table lists when the source
    # tables are known.
    known_tables = {_strip_db(m.get("source_table", ""), db) for m in table_mappings}
    for tables in served.values():
        known_tables.update(tables)

    engines = []
    for entry in ranking:
        engine = entry.get("target") or entry.get("engine")
        is_cache = entry.get("role") == "cache_layer"
        engines.append(
            {
                "engine": engine,
                "assigned_queries": entry.get("assigned_queries"),
                "workload_percent": None if is_cache else entry.get("workload_percent"),
                # The cache layer owns no query: workload_percent is null and the
                # cached_* fields describe it instead (#296)
                **(
                    {
                        "role": "cache_layer",
                        "cached_queries": entry.get("cache_overlay_queries"),
                        "cached_call_share_percent": entry.get("cache_call_share_percent"),
                    }
                    if is_cache
                    else {}
                ),
                # The deliverables show the fit of the routed queries (#152); the
                # all-analyzed-tables average is kept apart for audit
                "confidence": (
                    entry["routed_confidence"]
                    if entry.get("routed_confidence") is not None
                    else entry.get("confidence_score")
                ),
                "analysis_confidence": entry.get("confidence_score"),
                "monthly_cost_usd": cost_by_engine.get(engine, entry.get("monthly_cost_usd")),
                "tables_served": _tables_served(
                    served.get(engine), entry.get("assigned_queries"), partial_scope
                ),
                "primary_tables": [
                    _strip_db(m.get("source_table", ""), db)
                    for m in table_mappings
                    if m.get("recommended_database") == engine
                ],
                "recommended_architecture_table_count": rec_tables.get(engine),
                "schema": {
                    "target_tables": entry.get("target_tables"),
                    "access_patterns": entry.get("access_patterns"),
                    "pattern_groups": entry.get("pattern_groups"),
                },
                "assignment_reasons": [
                    _cap(r) for r in entry.get("assignment_reason_summary") or []
                ],
            }
        )

    qeng = query_engines(report, assignment)
    risk_assessment = report.get("risk_assessment") or {}
    risks = []
    severity_counts: Counter[str] = Counter()
    for risk in risk_assessment.get("risks") or []:
        description = risk.get("description") or ""
        prefix = _ENGINE_PREFIX_RE.match(description)
        query_ids = risk.get("query_ids") or []
        on_engines: Counter[str] = Counter()
        for qid in query_ids:
            on_engines.update(qeng.get(qid) or {"not_in_assignment_data"})
        severity_counts[str(risk.get("severity"))] += 1
        risks.append(
            {
                "id": risk.get("risk_id"),
                "severity": risk.get("severity"),
                "engine": risk.get("engine") or (prefix.group(1) if prefix else None),
                "type": risk.get("risk_type"),
                "description": _cap(description),
                "mitigation": _cap(risk.get("mitigation")),
                "affected_tables": [
                    name
                    for name in (_strip_db(t, db) for t in risk.get("affected_tables") or [])
                    if not known_tables or name in known_tables
                ],
                "query_count": len(query_ids),
                "queries_assigned_to": dict(on_engines),
            }
        )

    summary = report.get("assignment_summary") or {}
    tables_analyzed = next(
        (r.get("tables_analyzed") for r in ranking if r.get("tables_analyzed") is not None), None
    )
    rc = report.get("reality_check") or {}
    waves = report.get("migration_waves")
    overlay = report.get("cache_overlay") or {}

    return {
        "source": {
            "report_json": "synthesis report.json",
            "tables_served": served_source,
            "risk_query_engines": (
                "assignment.json query_assignments"
                if (assignment or {}).get("query_assignments")
                else "report.json query_groups (partial: only queries in a design group)"
            ),
        },
        "totals": {
            "database": db,
            "architecture_type": rec_arch.get("architecture_type"),
            "architecture_rationale": _cap(rec_arch.get("rationale")),
            "engines_selected": len(engines),
            "tables_analyzed": tables_analyzed,
            "tables_mapped": len(table_mappings),
            "queries": summary.get("query_count"),
            "queries_in_scope": summary.get("in_scope_count"),
            "co_dependency_groups": summary.get("co_dependency_groups"),
            "overall_risk": risk_assessment.get("overall_risk_level"),
            "risks_by_severity": dict(severity_counts),
            "projected_monthly_cost_usd": tco.get("projected_monthly_cost"),
        },
        "engines": engines,
        "eliminated_engines": _eliminated_engines(report, effective),
        "reality_check": {
            "before_distribution": rc.get("before_distribution"),
            "after_distribution": rc.get("after_distribution"),
            "moves": [
                {
                    "from_engine": m.get("from_engine"),
                    "to_engine": m.get("to_engine"),
                    "action": m.get("action"),
                    "query_count": m.get("query_count"),
                    "queries_retained": len(m.get("queries_retained") or []),
                    "reason": _cap(m.get("reason")),
                    "retention_reason": _cap(m.get("retention_reason")),
                }
                for m in rc.get("consolidations") or []
            ],
        },
        **(
            {
                "cache_overlay": {
                    "engine": overlay.get("engine"),
                    "rule": (
                        "the cache owns no query; it fronts hot reads cache-aside and is "
                        "not part of the workload distribution"
                    ),
                    "cached_queries": overlay.get("query_count"),
                    "call_share_percent": overlay.get("call_share_percent"),
                    "owners": overlay.get("owners"),
                    "min_calls_per_second": overlay.get("min_calls_per_second"),
                    "max_rows_avg": overlay.get("max_rows_avg"),
                    "notes": [_cap(n) for n in overlay.get("notes") or []],
                }
            }
            if overlay
            else {}
        ),
        "tco": {
            **tco,
            "assumptions": [_cap(a) for a in tco.get("assumptions") or []],
        },
        "risks": risks,
        "mitigation_strategies": [
            _cap(m) for m in risk_assessment.get("mitigation_strategies") or []
        ],
        "migration_waves": waves if waves else MIGRATION_WAVES_ABSENT,
    }
