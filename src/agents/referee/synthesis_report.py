"""
Synthesis report builder — transforms pipeline artifacts into RefereeOutputContract.

Deterministic logic for:
- Architecture recommendation (single/multi/hybrid based on engine selection)
- Table mappings (from schema design source_tables → target engine)
- TCO analysis (aggregate cost estimates from analysis outputs)
- Risk assessment (from anti-patterns, migration notes, unsupported patterns)
- Query group summary (from schema design pattern_groups)

The executive summary narrative is generated separately (LLM or template).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from src.agents.prompt_framing import SYSTEM_PROMPT_DATA_DIRECTIVE, frame_untrusted
from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.cache_overlay import (
    CACHE_OVERLAY_ENGINES,
    PATTERN_LABELS,
    overlay_summary,
)
from src.agents.referee.routed_confidence import routed_fits
from src.agents.referee.synthesis_grounding import (
    SUMMARY_GROUNDING_RULE,
    display_name,
    engine_mentions,
    ground_risks,
    recommends_engine,
)
from src.agents.referee.table_resolution import TableNameResolver
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.shared.migration_wave_engines import cache_front_description
from src.shared.ranking import (
    PARTIAL_NOTE,
    SIGNAL_ONLY_NOTE,
    engine_confidence,
    evidence_note,
)
from src.shared.signal_labels import signal_noun
from src.shared.unsupported_pattern import (
    is_dedup_only_group_by,
    unsupported_pattern_ids,
    unsupported_pattern_label,
    unsupported_pattern_mitigation,
    unsupported_pattern_text,
)

if TYPE_CHECKING:
    from src.agents.referee.synthesis_data import SynthesisData

logger = logging.getLogger(__name__)

AURORA_ENGINES = frozenset({"aurora_mysql", "aurora_postgresql"})


def schema_table_defs(engine: str, schema: dict) -> list[dict]:
    """Return an engine's schema output as ``table_definitions``-shaped dicts.

    Each entry has at least ``table_name``, ``source_tables`` and
    ``aggregate_pattern``. DynamoDB already emits this shape. The other engines
    use their own containers:

    - OpenSearch: ``index_designs`` + ``data_stream_designs``
    - DocumentDB: ``collections``
    - ElastiCache: ``key_designs`` (one entry per key pattern)
    - Aurora: ``table_definitions`` without ``source_tables``, because tables
      carry over 1:1. Each maps back to ``<source_database>.<table_name>``, the
      ``table_id`` form analysis uses.

    Never mutates ``schema``.
    """
    table_defs = list(schema.get("table_definitions", []))

    if engine in AURORA_ENGINES:
        source_db = schema.get("source_database", "")
        return [
            {
                **t,
                "source_tables": t.get("source_tables")
                or [f"{source_db}.{t['table_name']}" if source_db else t["table_name"]],
                "aggregate_pattern": "relational_table",
            }
            for t in table_defs
        ]
    if table_defs:
        return table_defs

    if engine == "opensearch":
        return [
            {
                "table_name": idx.get("index_name", ""),
                "source_tables": idx.get("source_tables", []),
                "aggregate_pattern": "search_index",
            }
            for idx in schema.get("index_designs", [])
        ] + [
            {
                "table_name": ds.get("data_stream_name", ""),
                "source_tables": ds.get("source_tables", []),
                "aggregate_pattern": "data_stream",
            }
            for ds in schema.get("data_stream_designs", [])
        ]
    if engine == "documentdb":
        return [
            {
                "table_name": coll.get("collection_name", ""),
                "source_tables": coll.get("source_tables", []),
                "aggregate_pattern": "document_collection",
            }
            for coll in schema.get("collections", [])
        ]
    if engine == "elasticache":
        return [
            {
                "table_name": kd.get("key_pattern", ""),
                "source_tables": kd.get("source_tables", []),
                "aggregate_pattern": kd.get("data_type", "unknown"),
            }
            for kd in schema.get("key_designs", [])
        ]
    return []


def _compute_assignment_distribution(data: SynthesisData) -> dict:
    """Compute per-engine workload distribution from assignment data.

    Returns a dict keyed by engine name with:
      query_count, in_scope_count, workload_percent, primary_table_count, top_reasons
    Returns empty dict if no assignment exists.
    """
    if not data.assignment:
        return {}

    query_assignments = data.assignment.get("query_assignments", [])
    total_queries = len(query_assignments)
    if total_queries == 0:
        return {}

    table_assignments = data.assignment.get("table_assignments", [])

    # Build per-engine stats
    engine_stats: dict[str, dict] = {}
    for qa in query_assignments:
        eng = qa.get("assigned_engine", "")
        if eng not in engine_stats:
            engine_stats[eng] = {
                "query_count": 0,
                "in_scope_count": 0,
                "reasons": [],
            }
        engine_stats[eng]["query_count"] += 1
        if qa.get("in_scope", True):
            engine_stats[eng]["in_scope_count"] += 1
        reason = qa.get("assignment_reason", "")
        if reason:
            engine_stats[eng]["reasons"].append(reason)

    # Count primary tables per engine
    engine_primary_tables: dict[str, int] = {}
    for ta in table_assignments:
        if isinstance(ta, dict):
            primary = ta.get("primary_engine", "")
            if primary:
                engine_primary_tables[primary] = engine_primary_tables.get(primary, 0) + 1

    result = {}
    for eng, stats in engine_stats.items():
        # Top reasons by frequency
        reason_counts: dict[str, int] = {}
        for r in stats["reasons"]:
            reason_counts[r] = reason_counts.get(r, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:3]

        result[eng] = {
            "query_count": stats["query_count"],
            "in_scope_count": stats["in_scope_count"],
            "workload_percent": round(stats["query_count"] / total_queries * 100, 1),
            "primary_table_count": engine_primary_tables.get(eng, 0),
            "top_reasons": [r[0] for r in top_reasons],
        }

    return result


def build_cache_overlay(data: SynthesisData) -> dict | None:
    """The cache layer's view of the workload (#296), or None when nothing is cached.

    ElastiCache owns no query, so the owner distribution (``workload_percent``) never
    counts it. This is the separate view: the in-scope queries it fronts, their share
    of calls, their owner engines, and the notes of the post-schema safety net.
    """
    if not data.assignment:
        return None
    qas = data.assignment.get("query_assignments", [])
    summary = overlay_summary(qas, data.source_queries)
    # Drops and notes persist on the assignment (cache_dropped, cache_notes), so a
    # re-run of synthesis reports them too, not only the run that dropped them.
    dropped = list(
        dict.fromkeys(
            [
                *data.cache_overlay_dropped,
                *(qa["query_id"] for qa in qas if qa.get("cache_dropped")),
            ]
        )
    )
    notes = list(
        dict.fromkeys([*(data.assignment.get("cache_notes") or []), *data.cache_overlay_notes])
    )
    if summary is None and not notes and not dropped:
        return None
    out = dict(summary or {})
    out["dropped_query_ids"] = dropped
    out["notes"] = notes
    return out


def is_cache_layer(entry: dict) -> bool:
    """True for a ranking entry that is the cache layer (owns no query, #296)."""
    return bool(entry.get("role") == "cache_layer")


def build_ranking(data: SynthesisData) -> list[dict]:
    """Rank target engines by the share of the workload routed to them (#152).

    Each entry keeps ``analysis_confidence`` (suitability averaged over every table
    the engine analyzed, also as ``confidence_score``) and ``weight`` for audit, and
    carries ``routed_confidence``: the mean fit of the queries the effective
    assignment routes to it. Owners come first, largest
    workload share first (weight, then name, break ties); without an assignment the
    weight order stands.

    The cache layer (#296) owns no query: its ``workload_percent`` is 0, it carries
    ``cache_overlay_queries`` / ``cache_call_share_percent`` instead, its routed
    confidence is measured on the reads it fronts, and it ranks after every owner
    engine so it is never read as the main recommendation.
    """
    ranking = []
    assignment_dist = _compute_assignment_distribution(data)
    overlay = build_cache_overlay(data) or {}
    fits = routed_fits(
        data.assignment,
        data.triage,
        data.source_queries,
        {e: a.analysis or {} for e, a in data.engines.items()},
        source_tables=[str(t["table_id"]) for t in data.source_tables if t.get("table_id")],
    )

    for engine, artifacts in data.engines.items():
        analysis = artifacts.analysis or {}
        table_recs = analysis.get("table_recommendations") or []
        workload = analysis.get("workload_analysis", {})
        cost = analysis.get("cost_estimate", {})
        aggregates = analysis.get("aggregate_recommendations") or []

        avg_confidence = (
            sum(t.get("confidence_score", 0) for t in table_recs) / len(table_recs)
            if table_recs
            else 0
        )

        patterns = workload.get("patterns_detected") or []
        anti_patterns = workload.get("anti_patterns_detected") or []

        complexities = [t.get("migration_complexity", "MEDIUM") for t in table_recs]
        complexity_counts = {c: complexities.count(c) for c in set(complexities)}
        most_common = (
            max(complexity_counts, key=lambda k: complexity_counts[k])
            if complexity_counts
            else "MEDIUM"
        )

        monthly_cost = cost.get("monthly_cost_usd", 0)
        pattern_score = min(len(patterns) / 5, 1.0)
        anti_penalty = min(len(anti_patterns) * 0.1, 0.3)
        weight = round(
            (avg_confidence / 100) * 0.6 + pattern_score * 0.25 - anti_penalty + 0.15,
            3,
        )
        weight = max(0.0, min(1.0, weight))

        # Schema design stats — handle engine-specific formats
        schema = artifacts.schema_design or {}
        schema_tables = schema_table_defs(engine, schema)
        access_patterns = schema.get("access_patterns", [])
        pattern_groups: dict[str, list] = {}
        for ap in access_patterns:
            g = ap.get("pattern_group", "ungrouped")
            pattern_groups.setdefault(g, []).append(ap)

        fit = fits.get(engine)
        entry = {
            "target": engine,
            "confidence_score": round(avg_confidence),  # backward compat
            "analysis_confidence": round(avg_confidence),
            "routed_confidence": fit.confidence if fit else None,
            "weight": weight,
            "monthly_cost_usd": monthly_cost,
            "tables_analyzed": len(table_recs),
            "tables_highly_suitable": sum(
                1 for t in table_recs if t.get("confidence_score", 0) >= 80
            ),
            "tables_suitable": sum(
                1 for t in table_recs if 60 <= t.get("confidence_score", 0) < 80
            ),
            "tables_marginal": sum(
                1 for t in table_recs if 40 <= t.get("confidence_score", 0) < 60
            ),
            "tables_not_suitable": sum(1 for t in table_recs if t.get("confidence_score", 0) < 40),
            "patterns_detected": len(patterns),
            "anti_patterns_detected": len(anti_patterns),
            "migration_complexity_avg": most_common,
            "aggregate_count": len(aggregates),
            "schema_design_available": bool(schema_tables),
            "target_tables": len(schema_tables),
            "access_patterns": len(access_patterns),
            "pattern_groups": len(pattern_groups),
        }
        if fit is not None:
            entry["routed_confidence_basis"] = fit.basis
            entry["routed_queries"] = fit.queries
            entry["routed_tables"] = fit.tables
            entry["routed_lead"] = fit.lead_signal
            entry["routed_lead_count"] = fit.lead_count
            entry["routed_confidence_evidence"] = fit.evidence
            entry["routed_queries_without_table_evidence"] = fit.unbacked_queries

        # Enrich with assignment distribution when available
        if assignment_dist and engine in assignment_dist:
            dist = assignment_dist[engine]
            entry["assigned_queries"] = dist["query_count"]
            entry["assigned_queries_in_scope"] = dist["in_scope_count"]
            entry["workload_percent"] = dist["workload_percent"]
            entry["primary_tables"] = dist["primary_table_count"]
            entry["assignment_reason_summary"] = dist["top_reasons"]
        elif assignment_dist:
            # Engine exists but got no assignments
            entry["assigned_queries"] = 0
            entry["assigned_queries_in_scope"] = 0
            entry["workload_percent"] = 0.0
            entry["primary_tables"] = 0
            entry["assignment_reason_summary"] = []

        if engine in CACHE_OVERLAY_ENGINES and overlay.get("engine") == engine:
            entry["role"] = "cache_layer"
            entry["cache_overlay_queries"] = overlay.get("query_count", 0)
            entry["cache_call_share_percent"] = overlay.get("call_share_percent", 0.0)
            entry["cache_overlay_owners"] = overlay.get("owners", {})
            # The overlay's own eligibility floor (#304/#296) and the combined
            # traffic that cleared it, carried through so the "Why" cell can
            # justify the cache's cost from these facts (#375 review) rather
            # than stating a dollar figure with no explanation beside it.
            entry["cache_calls_per_second"] = overlay.get("calls_per_second", 0.0)
            entry["cache_min_calls_per_second"] = overlay.get("min_calls_per_second", 0.0)

        ranking.append(entry)

    ranking.sort(
        key=lambda r: (
            is_cache_layer(r),
            -float(r.get("workload_percent") or 0),
            -r["weight"],
            r["target"],
        )
    )
    # Every engine carries its rationale, so the reports can show it for an engine
    # recommended_architecture.databases leaves out (the retained engine, #152)
    for entry in ranking:
        entry["rationale"] = _engine_rationale(data, entry)
    return ranking


def build_table_mappings(data: SynthesisData) -> list[dict]:
    """Build source table → target engine mappings from schema design outputs.

    Each source table gets mapped to the engine(s) whose schema design
    includes it. A table can map to multiple engines (e.g., payment stays
    in Aurora for writes but gets a DynamoDB read replica).

    A schema design's own ``source_tables`` is free text a model wrote (#380):
    it has named a Postgres sequence (``public.badge_groupings_id_seq``) as the
    "source" for a DynamoDB id-generator table, with no real analysis behind
    it (``table_confidence`` then defaults to 0), which inflated the migration
    map with objects nothing actually collected. Every ``source_table`` is
    resolved against the collector's own schema (:class:`TableNameResolver`,
    the same canonical check ``migration_waves`` and assignment resolution
    use, #225/#316) before it is kept: a name that does not resolve to a real
    table or view the collector saw is dropped, never counted as a "mapped"
    table. A name that resolves under a different spelling (a bare name the
    collector recorded schema-qualified) is carried under the canonical id,
    so it is not double-counted under two spellings.
    """
    resolver = TableNameResolver.from_collector(data.collector)

    def _canonical(source_table: str) -> str | None:
        if resolver is None:
            # No collector schema to check against (#380's guard only has teeth
            # when there is one): fail open and keep every name, the pre-#380
            # behavior, rather than treat a known-good caller as all noise.
            return source_table
        return resolver.resolve(str(source_table))

    # Collect all source_table → engine mappings from schema designs
    table_to_engines: dict[str, list[dict]] = {}

    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        analysis = artifacts.analysis or {}

        # Get per-table confidence from analysis
        table_confidence = {
            t["table_id"]: t.get("confidence_score", 0)
            for t in (analysis.get("table_recommendations") or [])
        }

        table_defs = schema_table_defs(engine, schema)

        for table_def in table_defs:
            target_table_name = table_def.get("table_name", "")
            aggregate_pattern = table_def.get("aggregate_pattern", "separate")

            for raw_source_table in table_def.get("source_tables", []):
                source_table = _canonical(raw_source_table)
                if source_table is None:
                    continue
                if source_table not in table_to_engines:
                    table_to_engines[source_table] = []

                table_to_engines[source_table].append(
                    {
                        "engine": engine,
                        "target_table": target_table_name,
                        "aggregate_pattern": aggregate_pattern,
                        "confidence_score": table_confidence.get(
                            source_table, table_confidence.get(raw_source_table, 0)
                        ),
                    }
                )

    # Build mappings — primary = highest confidence, others = alternatives
    mappings = []
    for source_table, targets in sorted(table_to_engines.items()):
        targets.sort(key=lambda t: t["confidence_score"], reverse=True)
        primary = targets[0]
        alternatives = targets[1:] if len(targets) > 1 else []

        mappings.append(
            {
                "source_table": source_table,
                "recommended_database": primary["engine"],
                "target_table": primary["target_table"],
                "aggregate_pattern": primary["aggregate_pattern"],
                "confidence_score": primary["confidence_score"],
                "alternatives": [
                    {
                        "database": alt["engine"],
                        "target_table": alt["target_table"],
                        "confidence_score": alt["confidence_score"],
                    }
                    for alt in alternatives
                ],
            }
        )

    return mappings


def build_query_groups(data: SynthesisData) -> list[dict]:
    """Build unified query group view across all engines.

    Groups access patterns by pattern_group from schema design outputs,
    enriched with the original source query data from the collector.
    This is the primary organizing structure for the UI — similar to
    Leo's query classification approach.
    """
    source_queries = {q["query_id"]: q for q in data.source_queries}
    groups: dict[str, dict] = {}

    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        for ap in schema.get("access_patterns", []):
            group_name = ap.get("pattern_group", "ungrouped")

            if group_name not in groups:
                groups[group_name] = {
                    "group_name": group_name,
                    "engines": [],
                    "access_patterns": [],
                    "source_queries": [],
                    "total_design_rps": 0,
                }

            if engine not in groups[group_name]["engines"]:
                groups[group_name]["engines"].append(engine)

            groups[group_name]["access_patterns"].append(
                {
                    "pattern_id": ap.get("pattern_id"),
                    "engine": engine,
                    "operation": ap.get("operation"),
                    "table_name": ap.get("table_name"),
                    "key_condition": ap.get("key_condition"),
                    "design_rps": ap.get("design_rps", 0),
                    "description": ap.get("description"),
                    "in_scope": ap.get("in_scope", True),
                    "query_ids": ap.get("query_ids", []),
                }
            )

            groups[group_name]["total_design_rps"] += ap.get("design_rps", 0)

            # Link back to source queries
            for qid in ap.get("query_ids", []):
                if qid in source_queries:
                    # Check if already added
                    existing = next(
                        (
                            sq
                            for sq in groups[group_name]["source_queries"]
                            if sq["query_id"] == qid
                        ),
                        None,
                    )
                    if existing:
                        # Add this pattern to the existing query's linked patterns
                        if ap.get("pattern_id") not in existing["linked_patterns"]:
                            existing["linked_patterns"].append(ap.get("pattern_id"))
                    else:
                        sq = source_queries[qid]
                        groups[group_name]["source_queries"].append(
                            {
                                "query_id": qid,
                                "query_text": sq.get("query_text", "")[:200],
                                "query_type": sq.get("query_type"),
                                "frequency_per_hour": sq.get("frequency_per_hour", 0),
                                "execution_time_ms_avg": sq.get("execution_time_ms_avg"),
                                "tables_accessed": sq.get("tables_accessed", []),
                                "linked_patterns": [ap.get("pattern_id")],
                            }
                        )

    # Sort by total RPS descending
    result = sorted(groups.values(), key=lambda g: g["total_design_rps"], reverse=True)
    return result


def build_tco_analysis(
    data: SynthesisData, eliminated_costs: dict[str, float] | None = None
) -> dict:
    """Aggregate cost estimates from all analysis outputs.

    ``eliminated_costs`` (#380) is each engine the reality check eliminated,
    mapped to its own analysed ``cost_estimate.monthly_cost_usd`` -- the
    figure a recommendation sentence names as a saving (e.g. "$271.80/mo")
    when it recommends removing that engine's dedicated cluster. It is
    carried into ``eliminated_engine_costs`` here so that figure is
    traceable in the TCO facts the report publishes, not only in prose
    elsewhere; omitted entirely (not an empty list) when there is nothing to
    report, so a reader never sees a bare "[]" with no eliminated engine.
    """
    target_costs = []
    total_projected = 0.0

    for engine, artifacts in data.engines.items():
        analysis = artifacts.analysis or {}
        cost = analysis.get("cost_estimate", {})
        monthly = cost.get("monthly_cost_usd", 0)
        target_costs.append(
            {
                "database": engine,
                "monthly_cost_usd": monthly,
                "pricing_mode": cost.get("cost_components", {}).get("pricing_mode", "on-demand"),
            }
        )
        total_projected += monthly

    # Source cost estimate from collector metrics (if available)
    rds_meta = (
        data.collector.get("metadata", {})
        .get("source_database", {})
        .get("rds_instance_metadata", {})
    )
    # #380: whether there is a real source baseline to compare against at all,
    # not just whether the rough estimate happened to come out to zero -- a
    # customer reading "$0.00 current / 0% savings" cannot tell "no instance
    # metadata was collected" from "the source genuinely costs nothing", and
    # the second never happens. Renderers must show "source cost not
    # provided" instead of the figure when this is ``False``.
    current_cost_known = bool(rds_meta)
    # Rough RDS cost estimate based on instance class
    current_monthly = _estimate_rds_cost(rds_meta) if rds_meta else 0

    savings_pct = (
        round((1 - total_projected / current_monthly) * 100, 1) if current_monthly > 0 else 0
    )

    result = {
        "current_monthly_cost": current_monthly,
        "current_cost_known": current_cost_known,
        "projected_monthly_cost": round(total_projected, 2),
        "savings_percent": savings_pct,
        "cost_breakdown": target_costs,
        "assumptions": [
            "On-demand capacity mode for all target databases",
            "Region: us-east-1",
            "Current RDS cost estimated from instance class (actual billing may differ)",
            "Does not include data transfer, backups, or global tables",
        ],
    }
    if eliminated_costs:
        result["eliminated_engine_costs"] = [
            {"database": engine, "monthly_cost_usd": cost}
            for engine, cost in sorted(eliminated_costs.items())
        ]
    return result


def _estimate_rds_cost(rds_meta: dict) -> float:
    """Rough monthly cost estimate for an RDS instance based on class."""
    # Simplified pricing — actual costs vary by region, reserved vs on-demand
    instance_costs = {
        "db.t3.micro": 15,
        "db.t3.small": 30,
        "db.t3.medium": 65,
        "db.t3.large": 130,
        "db.t3.xlarge": 260,
        "db.t3.2xlarge": 520,
        "db.r5.large": 175,
        "db.r5.xlarge": 350,
        "db.r5.2xlarge": 700,
        "db.r5.4xlarge": 1400,
        "db.r5.8xlarge": 2800,
        "db.r6g.large": 160,
        "db.r6g.xlarge": 320,
        "db.r6g.2xlarge": 640,
    }
    instance_class = rds_meta.get("instance_class", "")
    base = instance_costs.get(instance_class, 200)  # Default $200/mo
    if rds_meta.get("multi_az"):
        base *= 2
    return base


def _engines_with_assigned_queries(data: SynthesisData) -> set[str]:
    """Engines the assignment actually routed in-scope queries to.

    ``data.engines`` is keyed on *triage's* selections and is never narrowed by the
    assignment, so it still holds engines the assignment eliminated outright. Risks are
    generated per engine during analysis, while every triage-selected engine is still a
    candidate, so carrying a dropped engine's risks forward inflates the counts -- and
    because ``overall_risk_level`` is the highest open severity, one HIGH risk on an
    engine that carries no workload would raise the headline rating to HIGH.

    Fail-open: an empty set means "cannot tell". The caller keeps every risk in that case
    rather than silently emptying the register, which would be far worse than the defect.
    """
    if not data.assignment:
        return set()
    return {
        qa["assigned_engine"]
        for qa in data.assignment.get("query_assignments", [])
        if qa.get("in_scope", True) and qa.get("assigned_engine")
    }


def build_risk_assessment(
    data: SynthesisData,
    eliminated: dict[str, str | None] | None = None,
) -> dict:
    """Compile risks from anti-patterns, migration notes, and unsupported patterns.

    Anti-patterns are cross-referenced against schema design access patterns:
    if all of an anti-pattern's query_ids are covered by in-scope access
    patterns, the risk is considered resolved and downgraded to a note.

    Anti-pattern risks follow their queries (#221): the ``[engine]`` risk covers only the
    queries the effective assignment keeps on that engine. Queries moved to another
    engine are resolved by the move only when that engine's design serves them with an
    in-scope access pattern (an unsupported pattern there, itself a MEDIUM risk, counts
    only for risks no more severe than MEDIUM), or when they moved to Aurora, which runs
    the source SQL. Otherwise the risk is re-attributed to the new engine with its
    original severity. When the anti-pattern's own advice was to move the queries to the
    engine they landed on, the anti-pattern is resolved and the queries that engine's
    design does not serve become one coverage-gap risk per engine. Every resolved risk is
    recorded in ``resolved_risks`` (grounded like the risks).

    Only engines the assignment routed queries to contribute risks. An engine triage
    selected but the assignment then dropped is not part of the target architecture, so its
    anti-patterns describe a design that will never be built.

    ``eliminated`` maps engines the reality check removed to the engine that absorbed
    them (see ``synthesis_grounding.eliminated_engines``). Surviving engines' risk text
    written while those engines were still candidates is rewritten so no risk or
    mitigation recommends an eliminated engine, and the mitigation strategies only name
    engines in the effective architecture (#202).
    """
    risks = []
    resolved: list[dict] = []
    risk_id = 0
    assigned = _engines_with_assigned_queries(data)
    # Effective engine of every in-scope query (built first: anti-pattern risks follow
    # their queries to this engine, #221).
    query_engine = {
        qa["query_id"]: qa["assigned_engine"]
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("in_scope", True) and qa.get("assigned_engine") and qa.get("query_id")
    }
    # Every in-scope query's own assignment_reason (#375 review): the capability
    # gate and the utility pin already explain a hard-pinned query there, reused
    # by _capability_pin_reason so a DynamoDB-alternative risk on the same table
    # can name the reason instead of contradicting the routing outright.
    assignment_reason = {
        qa["query_id"]: qa.get("assignment_reason", "")
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("in_scope", True) and qa.get("query_id")
    }
    assignment_ids = {
        qa.get("query_id") for qa in (data.assignment or {}).get("query_assignments", [])
    }
    query_tables = {
        q["query_id"]: set(q.get("tables_accessed") or [])
        for q in data.source_queries
        if q.get("query_id")
    }
    # Normalised tables per query for risks built from query ids (unsupported patterns,
    # coverage gaps): ``tables_accessed``, else the assignment's ``source_tables``.
    normalise = _table_normaliser(data)
    assigned_tables = {
        qa["query_id"]: qa.get("source_tables") or []
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("query_id")
    }
    risk_tables = {
        q: normalise(query_tables.get(q) or assigned_tables.get(q) or [])
        for q in set(query_tables) | set(assigned_tables)
    }
    query_text = {
        q["query_id"]: str(q.get("query_text") or "")
        for q in data.source_queries
        if q.get("query_id")
    }
    covered_by = {
        engine: _covered_query_ids(artifacts.schema_design or {})
        for engine, artifacts in data.engines.items()
    }
    unsupported_by = {
        engine: _unsupported_query_ids(artifacts.schema_design or {})
        for engine, artifacts in data.engines.items()
    }
    # target engine -> {query id: severity} for queries an anti-pattern's advice moved to
    # that engine but its design does not serve (reported as one gap risk per engine).
    coverage_gaps: dict[str, dict[str, str]] = {}

    for engine, artifacts in data.engines.items():
        # ``assigned`` empty => no readable assignment => keep every risk (fail-open).
        if assigned and engine not in assigned:
            logger.info(
                "Risk assessment: skipping %s (assignment routed it no in-scope queries)",
                engine,
            )
            continue
        analysis = artifacts.analysis or {}
        schema = artifacts.schema_design or {}

        # Every table's in-scope queries on THIS engine (#375 review): a
        # DynamoDB-alternative anti-pattern's own flagged queries can be simple
        # key-value reads with no capability need of their own, while a
        # *different* query on the same table is why the table stays here --
        # _capability_pin_reason below looks at every one of them, not just
        # the flagged queries, to find that reason.
        table_to_qids: dict[str, set[str]] = {}
        for qid, qid_engine in query_engine.items():
            if qid_engine != engine:
                continue
            for table in normalise(query_tables.get(qid) or assigned_tables.get(qid) or []):
                table_to_qids.setdefault(table, set()).add(qid)

        # Anti-patterns from analysis — only include if NOT resolved by schema design
        # Source database anti-patterns (full scans, slow queries) are migration
        # motivation, not destination risks. Skip them entirely — the schema design
        # already addresses them with proper access patterns.
        # Only include anti-patterns that affect the TARGET database design.
        for ap in analysis.get("workload_analysis", {}).get("anti_patterns_detected") or []:
            ap_type = ap.get("anti_pattern_type", "")

            # Skip source-database-only anti-patterns
            if ap_type in ("frequent-full-scan", "queries-without-index"):
                continue

            ap_query_ids = set(ap.get("query_ids", []))
            severity = "HIGH" if ap.get("severity_weight", 0) >= 0.7 else "MEDIUM"
            description = ap.get("description", ap.get("anti_pattern_type", "Unknown"))
            ap_tables = [t for t in ap.get("table_ids", []) if t and t not in _PLACEHOLDER_TABLES]

            # Split the flagged queries by the engine the assignment routed them to
            # (#221). Fail-open: when none of them is in the assignment, keep the
            # analysis attribution.
            if query_engine and ap_query_ids & assignment_ids:
                on_engine: dict[str, set[str]] = {}
                for q in ap_query_ids:
                    if q in query_engine:
                        on_engine.setdefault(query_engine[q], set()).add(q)
            else:
                on_engine = {engine: ap_query_ids}

            if not on_engine:
                resolved.append(
                    _resolved_risk(
                        engine,
                        severity,
                        description,
                        ap_tables,
                        ap_query_ids,
                        None,
                        "every flagged query is out of scope in the assignment",
                    )
                )
                continue

            for target in sorted(on_engine, key=lambda e: (e != engine, e)):
                ids = on_engine[target]
                tables = _tables_for(ids, ap_tables, query_tables)
                if target == engine:
                    covered = covered_by.get(engine, set())
                    # If all flagged queries are covered by the schema design, it's resolved
                    if ids and ids.issubset(covered):
                        resolved.append(
                            _resolved_risk(
                                engine,
                                severity,
                                description,
                                tables,
                                ids,
                                engine,
                                "the schema design covers every flagged query",
                            )
                        )
                        continue
                    # Partially resolved — note which queries are still uncovered
                    uncovered = ids - covered
                    text = description
                    if uncovered and ids:
                        pct_covered = round((1 - len(uncovered) / len(ids)) * 100)
                        text += (
                            f" ({pct_covered}% of queries resolved by schema design, "
                            f"{len(uncovered)} remaining)"
                        )
                    mitigation = ap.get("recommendation")
                    if ap_type in _DYNAMODB_ALTERNATIVE_ANTI_PATTERNS:
                        sibling_qids: set[str] = set()
                        for t in tables:
                            sibling_qids |= table_to_qids.get(t, set())
                        pin_reason = _capability_pin_reason(
                            assignment_reason.get(q, "") for q in sibling_qids
                        )
                        if pin_reason:
                            # #380 review: this anti-pattern's own flagged queries
                            # look like simple key-value access, but a different
                            # query on the same table is why it stays on this
                            # engine -- the routing is deliberate, not an open
                            # risk, so it is resolved (not raised) rather than
                            # kept as a MEDIUM risk whose own text says the table
                            # "could run on a simpler engine" and whose
                            # "mitigation" is "no action needed" (not a
                            # mitigation at all, and a restatement of a decision
                            # the report already made elsewhere). This is also
                            # how two anti-patterns flagging the same
                            # already-pinned table (e.g. "no foreign keys" and
                            # "at most 2 patterns") stop duplicating each other
                            # as separate open risks over largely the same
                            # tables: both resolve here instead.
                            engine_name = display_name(engine)
                            resolved.append(
                                _resolved_risk(
                                    engine,
                                    severity,
                                    text,
                                    tables,
                                    ids,
                                    engine,
                                    f"{engine_name} is also required for {pin_reason} on "
                                    "this table, so keeping it there is a deliberate "
                                    "routing decision, not an open risk; revisit "
                                    "DynamoDB for this table's simple key-value reads "
                                    "in a later wave if those other queries are "
                                    "retired or move too",
                                )
                            )
                            continue
                    risk_id += 1
                    risks.append(
                        {
                            "risk_id": f"RISK-{risk_id:03d}",
                            "risk_type": "PERFORMANCE_DEGRADATION",
                            "severity": severity,
                            "description": f"[{engine}] {text}",
                            "affected_tables": tables,
                            "mitigation": mitigation,
                            "query_ids": sorted(ids),
                        }
                    )
                    continue

                # Queries moved to another engine. Aurora runs the source SQL as-is, so a
                # non-relational engine's limitation does not follow them there.
                if target in AURORA_ENGINES and engine not in AURORA_ENGINES:
                    resolved.append(
                        _resolved_risk(
                            engine,
                            severity,
                            description,
                            tables,
                            ids,
                            target,
                            f"the queries run as SQL on {display_name(target)}",
                        )
                    )
                    continue
                # Only an in-scope access pattern on the new engine resolves a query. An
                # unsupported pattern there is its own (MEDIUM) risk, so it stands in only
                # for risks no more severe than that.
                addressed = set(covered_by.get(target, set()))
                if _SEVERITY_ORDER.get(severity, 9) >= _SEVERITY_ORDER["MEDIUM"]:
                    addressed |= unsupported_by.get(target, set())
                remaining = ids - addressed
                recommendation = ap.get("recommendation") or ""
                if recommends_engine(f"{description} {recommendation}", target):
                    # The analysis advised moving these queries to ``target`` and the
                    # assignment did: the anti-pattern itself is resolved. Queries the new
                    # design does not serve are a coverage gap on ``target``, reported once
                    # per engine in their own words below.
                    resolved.append(
                        _resolved_risk(
                            engine,
                            severity,
                            description,
                            tables,
                            ids,
                            target,
                            f"the {display_name(engine)} analysis recommended "
                            f"{display_name(target)} for these queries and the assignment "
                            "moved them there",
                        )
                    )
                    gap = coverage_gaps.setdefault(target, {})
                    for q in remaining:
                        if _SEVERITY_ORDER.get(severity, 9) < _SEVERITY_ORDER.get(
                            gap.get(q, "LOW"), 9
                        ):
                            gap[q] = severity
                        else:
                            gap.setdefault(q, severity)
                    continue
                if not remaining:
                    resolved.append(
                        _resolved_risk(
                            engine,
                            severity,
                            description,
                            tables,
                            ids,
                            target,
                            f"the queries moved to {display_name(target)}, whose schema "
                            "design serves all of them",
                        )
                    )
                    continue
                risk_id += 1
                pct_covered = round((1 - len(remaining) / len(ids)) * 100)
                n_rem = len(remaining)
                mitigation = (
                    f"Cover the {n_rem} remaining {'query' if n_rem == 1 else 'queries'} in "
                    f"the {display_name(target)} schema design or route "
                    f"{'it' if n_rem == 1 else 'them'} to an engine that serves "
                    f"{'it' if n_rem == 1 else 'them'}."
                )
                # The old engine's advice is kept only as background, and dropped when it
                # leans on that engine's own features (e.g. DynamoDB Streams for queries
                # now on ElastiCache).
                if recommendation and engine not in {
                    e for e, _, _ in engine_mentions(recommendation)
                }:
                    mitigation += f" Background ({display_name(engine)} analysis): {recommendation}"
                n_ids = len(ids)
                risks.append(
                    {
                        "risk_id": f"RISK-{risk_id:03d}",
                        "risk_type": "PERFORMANCE_DEGRADATION",
                        "severity": severity,
                        # Attribution first; the standard "(N% ..., M remaining)"
                        # parenthetical last, which the deck parses for the count.
                        "description": (
                            f"[{target}] Flagged by the {display_name(engine)} analysis for "
                            f"{n_ids} {'query' if n_ids == 1 else 'queries'} now on "
                            f"{display_name(target)}: {description} ({pct_covered}% of "
                            f"queries resolved by schema design, {n_rem} remaining)"
                        ),
                        "affected_tables": tables,
                        "mitigation": mitigation,
                        "query_ids": sorted(ids),
                        "reattributed_from": engine,
                    }
                )

        # Unsupported patterns from schema design. The four schema-design
        # contracts disagree on field names (dynamodb/opensearch carry
        # pattern_type/recommendation; documentdb/elasticache carry
        # reason/workaround instead) -- read via the shared helper so every
        # engine's unsupported patterns become risks with real text, not
        # "[engine] unknown: " for the engines whose contract this code used
        # to not read (#210).
        for up in schema.get("unsupported_patterns", []):
            pattern_ids = unsupported_pattern_ids(up)
            tables = sorted({t for q in pattern_ids for t in risk_tables.get(q, ())})
            risk_id += 1
            # Dedup-only GROUP BY (#336): every one of this *aggregation* entry's
            # queries is a GROUP BY used only to de-duplicate rows (no aggregate
            # function, no HAVING, no window function, no ROLLUP/CUBE/GROUPING
            # SETS) -- an application-code fix, not a real blocking aggregation.
            # Gated on the entry's own category (``_is_aggregation_unsupported_
            # pattern``), not just the SQL shape: a non-aggregation entry (a join
            # or LIKE-pattern limitation, say) is never reclassified just because
            # its query also happens to have a dedup-only GROUP BY. Kept as an
            # open LOW risk, worded as the application-code fix it is -- the
            # issue's own alternative -- not resolved and not counted as covered:
            # no in-scope access pattern actually serves these queries.
            if (
                pattern_ids
                and _is_aggregation_unsupported_pattern(up)
                and all(is_dedup_only_group_by(query_text.get(q, "")) for q in pattern_ids)
            ):
                risks.append(
                    {
                        "risk_id": f"RISK-{risk_id:03d}",
                        "risk_type": "MIGRATION_COMPLEXITY",
                        "severity": "LOW",
                        "description": f"[{engine}] {unsupported_pattern_label(up)}: "
                        f"{_unsupported_pattern_problem(engine, up, query_text)}",
                        "affected_tables": tables,
                        "mitigation": (
                            "Needs an application change: drop the GROUP BY and serve "
                            "it as a Query."
                        ),
                        "query_ids": sorted(pattern_ids),
                    }
                )
                continue
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "MIGRATION_COMPLEXITY",
                    "severity": "MEDIUM",
                    "description": f"[{engine}] {unsupported_pattern_label(up)}: "
                    f"{_unsupported_pattern_problem(engine, up, query_text)}",
                    "affected_tables": tables,
                    "mitigation": unsupported_pattern_mitigation(up),
                    "query_ids": sorted(pattern_ids),
                }
            )

        # Queries --split left out of every design group because they touch no
        # source table (catalog/utility statements, #276/#369): a deterministic
        # out-of-scope note, same as an unsupported pattern, so they are visible
        # in the report rather than silently missing from both coverage and risks.
        excluded_queries = schema.get("excluded_queries") or []
        if excluded_queries:
            risk_id += 1
            excluded_ids = sorted(
                {str(e.get("query_id")) for e in excluded_queries if e.get("query_id")}
            )
            n_excluded = len(excluded_ids)
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "MIGRATION_COMPLEXITY",
                    "severity": "LOW",
                    "description": (
                        f"[{engine}] Not designed: {n_excluded} "
                        f"{'query' if n_excluded == 1 else 'queries'} touch no source "
                        "table (catalog/utility statements) and were left out of "
                        "schema design."
                    ),
                    "affected_tables": [],
                    "mitigation": (
                        "Review whether these statements should be routed to this " "engine at all."
                    ),
                    "query_ids": excluded_ids,
                }
            )

        # Migration notes from schema design
        # The description names the object; the logic to build is the mitigation (#252).
        for mn in schema.get("migration_notes", []):
            risk_id += 1
            object_label = str(mn.get("object_name") or "").strip()
            logic = str(mn.get("application_logic_required") or "").strip()
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "OPERATIONAL_RISK",
                    "severity": "MEDIUM",
                    "description": (
                        f"[{engine}] {mn.get('object_type') or 'migration note'}: "
                        f"{object_label or 'this object'} needs application-side logic on "
                        f"{display_name(engine)}."
                    ),
                    "affected_tables": sorted(normalise([mn.get("source_table") or ""])),
                    # #380: a MEDIUM+ risk always carries a concrete mitigation --
                    # when the schema design gave no logic to build, fall back to
                    # a reviewable action instead of leaving it ``None`` (silently
                    # no mitigation at all for a risk this severe).
                    "mitigation": (
                        f"Implement as application logic: {logic}"
                        if logic
                        else (
                            f"Review {object_label or 'this object'} "
                            f"({mn.get('object_type') or 'migration note'}) and implement its "
                            f"logic in the application before cutover to {display_name(engine)}."
                        )
                    ),
                    "object_type": str(mn.get("object_type") or ""),
                    "object_name": str(mn.get("object_name") or ""),
                }
            )

    for target in sorted(coverage_gaps):
        gap = coverage_gaps[target]
        if not gap:
            continue
        risk_id += 1
        risks.append(
            _coverage_gap_risk(f"RISK-{risk_id:03d}", target, gap, query_text, risk_tables)
        )

    # Guard (#335): a query reality check moved onto an engine whose own schema
    # design lists it as unsupported. #338 fixes #335's root cause (a missing
    # aggregation/complex-joins capability check in the reality check's
    # serviceability gate) at the source; this is a narrow invariant guard in
    # case a gap like it slips through again, not the fix itself. Every such id
    # already has an open risk by construction (the per-pattern unsupported-
    # pattern risk above, at minimum), so this never adds a parallel risk: it
    # raises/annotates the existing one, and only falls back to a new one for
    # the (should not happen) case where none names the id.
    risk_id = _raise_moved_onto_unsupported_risks(
        risk_id, data, unsupported_by, risks, risk_tables, query_text
    )

    risks = [_without_repeated_mitigation(r) for r in risks]
    risks = ground_risks(risks, eliminated or {}, query_engine)
    resolved = ground_risks(resolved, eliminated or {}, query_engine)
    for r in resolved:
        logger.info(
            "Risk assessment: %s risk from %s resolved on %s (%s)",
            r["severity"],
            r["engine"],
            r["resolved_on"],
            r["reason"],
        )

    return {
        "overall_risk_level": overall_risk_level(risks),
        "risks": risks,
        "mitigation_strategies": _build_mitigation_strategies(risks, assigned or set(data.engines)),
        # Anti-pattern risks the effective assignment resolved (#221): kept for the
        # audit trail so no risk, HIGH or otherwise, disappears without a record.
        "resolved_risks": resolved,
    }


def overall_risk_level(risks: list[dict]) -> str:
    """Overall risk level: the highest severity among the open ``risks`` (#248).

    Any CRITICAL risk gives CRITICAL, any HIGH gives HIGH, any MEDIUM gives MEDIUM,
    otherwise LOW (including no risks). ``risks`` are the open risks only;
    ``resolved_risks`` never count.
    """
    severities = {str(r.get("severity") or "").upper() for r in risks}
    for level in ("CRITICAL", "HIGH", "MEDIUM"):
        if level in severities:
            return level
    return "LOW"


_SQL_EXCERPT_CHARS = 120


def _sql_excerpt(sql: str) -> str:
    """``sql`` on one line without backticks, clipped to ``_SQL_EXCERPT_CHARS``.

    Backticks (MySQL identifier quotes) would open code spans in the Markdown engineering
    report, whose text escaping leaves them alone.
    """
    one_line = " ".join(sql.replace("`", "").split())
    if len(one_line) <= _SQL_EXCERPT_CHARS:
        return one_line
    return one_line[: _SQL_EXCERPT_CHARS - 1].rstrip() + "…"


def _unsupported_pattern_problem(engine: str, up: dict, query_text: dict[str, str]) -> str:
    """What an unsupported pattern's risk is about, without its fix (#252).

    The ``reason`` when the contract has one (documentdb, elasticache, opensearch). The
    dynamodb contract carries only ``pattern_type`` and ``recommendation`` -- the fix,
    which is the risk's mitigation -- so the problem is stated from the queries instead,
    quoting the first one's SQL.
    """
    reason = str(up.get("reason") or "").strip()
    if reason:
        return reason
    ids = unsupported_pattern_ids(up)
    # OpenSearch patterns carry the SQL themselves (``source_query``).
    sql = next((query_text[q] for q in ids if query_text.get(q, "").strip()), "") or str(
        up.get("source_query") or ""
    )
    head = f"{display_name(engine)} has no native equivalent for"
    if len(ids) > 1:
        return f"{head} these {len(ids)} queries" + (f", e.g. {_sql_excerpt(sql)}" if sql else ".")
    subject = "this query" if ids else "this pattern"
    return f"{head} {subject}" + (f": {_sql_excerpt(sql)}" if sql else ".")


def _without_repeated_mitigation(risk: dict) -> dict:
    """``risk`` with ``mitigation`` set to None when the description already contains it.

    A mitigation is never a copy of the description (#252); renderers omit an empty one.
    Compared case-insensitively with whitespace collapsed.
    """
    mitigation = " ".join(str(risk.get("mitigation") or "").split()).casefold()
    description = " ".join(str(risk.get("description") or "").split()).casefold()
    if mitigation and mitigation in description:
        return {**risk, "mitigation": None}
    return risk


# Table ids analysis emits when it cannot attribute a query to a table
# (e.g. ``SELECT FOUND_ROWS()``).
_PLACEHOLDER_TABLES = frozenset({"unknown", "UNKNOWN", "None", "null"})
# Names that are never a source table in a risk's ``affected_tables``.
_PSEUDO_TABLES = _PLACEHOLDER_TABLES | {"", "DUAL", "dual"}
_PSEUDO_TABLES_LOWER = frozenset(t.lower() for t in _PSEUDO_TABLES)


def _table_normaliser(data: SynthesisData) -> Callable[[Iterable[str]], set[str]]:
    """Map table names to the collector's source-table ids.

    Source-table ids are qualified (``<db>.table`` for MySQL, ``<schema>.table`` for
    PostgreSQL, SQL Server and Oracle) while query ``tables_accessed`` may be bare or
    differently cased. A name that is a known id is kept; otherwise its last dotted
    segment is matched case-insensitively against the known ids' last segments and a
    unique match gives the known id. A name that matches nothing (or several) is kept as
    given (fail-open). Only pseudo-tables (``unknown``, ``DUAL``) are dropped.
    """
    known = {str(t["table_id"]) for t in data.source_tables if t.get("table_id")}
    by_lower = {k.lower(): k for k in known}
    by_segment: dict[str, set[str]] = {}
    for k in known:
        by_segment.setdefault(k.rsplit(".", 1)[-1].lower(), set()).add(k)

    def resolve(name: str) -> str:
        if name in known:
            return name
        if name.lower() in by_lower:
            return by_lower[name.lower()]
        matches = by_segment.get(name.rsplit(".", 1)[-1].lower(), set())
        return next(iter(matches)) if len(matches) == 1 else name

    def normalise(names: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for raw in names:
            name = str(raw or "").strip()
            if name.lower() in _PSEUDO_TABLES_LOWER:
                continue
            out.add(resolve(name))
        return out

    return normalise


def _access_pattern_query_ids(ap: dict) -> list[str]:
    """Source query ids of an access pattern.

    DynamoDB/OpenSearch key them ``query_ids``; DocumentDB/ElastiCache use
    ``source_query_ids`` (the same split as their unsupported patterns, #210).
    """
    return list(ap.get("query_ids") or ap.get("source_query_ids") or [])


def _covered_query_ids(schema: dict) -> set[str]:
    """Query ids served by the schema design's in-scope access patterns."""
    covered: set[str] = set()
    for ap in schema.get("access_patterns", []):
        if ap.get("in_scope", True):
            covered.update(_access_pattern_query_ids(ap))
    return covered


def _unsupported_query_ids(schema: dict) -> set[str]:
    """Query ids the schema design lists as unsupported (each is its own MEDIUM risk)."""
    ids: set[str] = set()
    for up in schema.get("unsupported_patterns", []):
        ids.update(unsupported_pattern_ids(up))
    return ids


def _is_aggregation_unsupported_pattern(up: dict) -> bool:
    """True when ``up``'s own category is aggregation, however its contract spells it (#336).

    Only DynamoDB's contract carries a real ``pattern_type`` (one of
    ``UNSUPPORTED_PATTERN_TYPES``); DocumentDB, ElastiCache and OpenSearch have
    no equivalent structured field, only free-text ``reason``/``workaround``
    (``unsupported_pattern_label`` falls back to the generic "unsupported
    pattern" for all three). Gating the dedup-only GROUP BY check (#336) on
    "this entry's own category is aggregation" -- not "this entry's queries
    happen to have a GROUP BY somewhere in their SQL" -- matters: a DynamoDB
    entry whose actual pattern_type is a multi-table join, or an ElastiCache
    entry about LIKE pattern matching, must not be reclassified just because
    one of its flagged queries also has a dedup-only GROUP BY elsewhere in its
    text.
    """
    pattern_type = str(up.get("pattern_type") or "").strip().strip("*").lower()
    if pattern_type:
        return pattern_type == "aggregation"
    return "aggregat" in unsupported_pattern_text(up).lower()


def _moved_query_ids(data: SynthesisData) -> dict[str, set[str]]:
    """Query ids Reality Check actually moved to a different engine, keyed by
    the CURRENT (post-move) engine (#335).

    Compares the effective assignment against the one Reality Check started
    from (``data.pre_reality_check_assignment``, loaded via
    ``resolve_reality_check_input_version`` -- see ``synthesis_data.py``): a
    query whose ``assigned_engine`` differs between the two was moved. This is
    not the same as "an anti-pattern on a different engine also names this
    query": an anti-pattern's own query_ids are routinely served by whichever
    engine the *initial* assignment resolver already picked -- most queries
    aurora_mysql's analysis flags, say, are assigned to dynamodb from the
    start, never "moved" there by anything. ``Consolidation``
    (``src/contracts/reality_check_output.py``) itself carries no per-query
    ids (only a per-engine-pair ``query_count``), so the only precise signal is
    this before/after diff.

    ``None`` for either assignment (no Reality Check run on record for this
    lineage, or the pre-run version could not be read) means nothing was
    moved -- fail safe, not "everything was moved".
    """
    if not data.assignment or not data.pre_reality_check_assignment:
        return {}
    before = {
        qa["query_id"]: qa.get("assigned_engine")
        for qa in data.pre_reality_check_assignment.get("query_assignments", [])
        if qa.get("query_id")
    }
    moved: dict[str, set[str]] = {}
    for qa in data.assignment.get("query_assignments", []):
        query_id = qa.get("query_id")
        after = qa.get("assigned_engine")
        if not query_id or not after:
            continue
        before_engine = before.get(query_id)
        if before_engine and before_engine != after:
            moved.setdefault(after, set()).add(query_id)
    return moved


def _source_compatible_aurora_engine(data: SynthesisData) -> str:
    """The Aurora engine matching the source database's dialect (#335's guard
    mitigation: Aurora always runs the source SQL as-is, regardless of what any
    other engine's assignment or schema design says).

    Prefers whichever Aurora engine is already part of this architecture
    (``data.engines``); otherwise maps the collector's source database engine
    (mysql/mariadb -> aurora_mysql, postgresql/postgres -> aurora_postgresql),
    defaulting to aurora_mysql when even that is unknown -- never wrong about
    there being an Aurora safety net, only about which flavour's name to print.
    """
    for engine in sorted(AURORA_ENGINES):
        if engine in data.engines:
            return engine
    source_engine = source_database_engine(data.collector)
    return SOURCE_ENGINE_TO_AURORA.get(source_engine, "aurora_mysql")


def _raise_moved_onto_unsupported_risks(
    risk_id: int,
    data: SynthesisData,
    unsupported_by: dict[str, set[str]],
    risks: list[dict],
    risk_tables: dict[str, set[str]],
    query_text: dict[str, str],
) -> int:
    """Raise to HIGH, and annotate, the risk(s) for a query reality check moved
    onto an engine whose own schema design lists it as unsupported (#335's
    actual invariant, narrowed by review).

    #338 (``feat/engines-earn-their-place``) fixes #335's root cause -- a
    missing aggregation/complex-joins capability check in the reality check's
    serviceability gate, ``src/agents/referee/capability_registry.py`` -- at
    the source. This module does not decide routing, so it stays a narrow
    invariant guard rather than the fix: it reports a query that still ends up
    moved onto an engine that cannot serve it, in case a capability gap like
    #335's slips through again (#338's own gate has at least one known gap:
    it does not block a single-table ``COUNT(*)`` move).

    Never adds a risk in parallel with an existing one for the same id: every
    unsupported id already has an open risk by construction (at minimum, the
    per-pattern "unsupported_patterns" risk every such id gets above, whatever
    its severity -- MEDIUM, or LOW for a #336 dedup-only ``GROUP BY``). This
    raises that risk's severity to HIGH (never down -- a query already flagged
    CRITICAL or already HIGH for an unrelated reason keeps that) and appends
    one sentence naming the Aurora fallback, rather than creating a second
    entry for the same gap. Only when (by construction, should not happen) no
    existing risk names the id does this add a new dedicated one, so the gap
    is never silently dropped.

    Not CRITICAL: Aurora always runs the source SQL as-is (#221), so this is
    never "no engine serves it" -- it is "stuck on the wrong engine rather than
    Aurora until that engine's schema design (or an application change) catches
    up", the same severity register as every other reattribution risk above.
    The note is phrased as advice ("keep it on Aurora until ..."), not as a
    statement of where the query is ("stays on Aurora"): the assignment still
    names the engine it was moved to, not Aurora.
    """
    moved = _moved_query_ids(data)
    aurora_name = display_name(_source_compatible_aurora_engine(data))

    ids_by_engine = {
        engine: ids
        for engine in sorted(moved)
        if engine not in AURORA_ENGINES
        for ids in [moved[engine] & unsupported_by.get(engine, set())]
        if ids
    }
    if not ids_by_engine:
        return risk_id

    mitigation_addendum = f"{aurora_name} runs the source SQL as-is."
    for engine, ids in ids_by_engine.items():
        # Grouped by the risk object itself (by risk_id, not by query id): several
        # of this engine's moved-and-unsupported ids can share one pre-existing
        # risk (e.g. one unsupported_patterns entry naming all of them), and each
        # such risk is annotated once, with its own it/them wording.
        claimed: dict[str, dict] = {}
        unclaimed: set[str] = set()
        for q in sorted(ids):
            target_risk = next((r for r in risks if q in (r.get("query_ids") or [])), None)
            if target_risk is None:
                unclaimed.add(q)
            else:
                claimed[target_risk["risk_id"]] = target_risk

        for target_risk in claimed.values():
            # This risk's own share of the moved-and-unsupported ids -- not every
            # id moved onto this engine necessarily sits in this particular risk.
            overlap = ids & set(target_risk.get("query_ids") or [])
            pronoun = "it" if len(overlap) == 1 else "them"
            note = (
                f" Moved onto {display_name(engine)}, which lists {pronoun} as "
                f"unsupported: keep {pronoun} on {aurora_name} until "
                f"{display_name(engine)}'s schema design (or an application "
                f"change) can serve {pronoun}."
            )
            if _SEVERITY_ORDER.get(target_risk.get("severity", "LOW"), 9) > _SEVERITY_ORDER["HIGH"]:
                target_risk["severity"] = "HIGH"
            if note not in target_risk["description"]:
                target_risk["description"] += note
            mitigation = target_risk.get("mitigation") or ""
            if aurora_name not in mitigation:
                target_risk["mitigation"] = (
                    f"{mitigation} {mitigation_addendum}".strip()
                    if mitigation
                    else mitigation_addendum
                )

        if not unclaimed:
            continue
        # Should not happen (every unsupported id has an unsupported_patterns risk
        # above), but a gap is never silently dropped if it ever does.
        ids_sorted = sorted(unclaimed)
        n = len(ids_sorted)
        pronoun = "it" if n == 1 else "them"
        tables = sorted({t for q in ids_sorted for t in risk_tables.get(q, ())})
        texts = [_sql_excerpt(query_text.get(q) or q[:12]) for q in ids_sorted[:3]]
        listed = "; ".join(texts) + (f"; and {n - 3} more" if n > 3 else "")
        risk_id += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_id:03d}",
                "risk_type": "MIGRATION_COMPLEXITY",
                "severity": "HIGH",
                "description": (
                    f"[{engine}] {n} {'query' if n == 1 else 'queries'} moved onto "
                    f"{display_name(engine)}, whose schema design lists {pronoun} as "
                    f"unsupported: {listed}. Keep {pronoun} on {aurora_name} until "
                    f"{display_name(engine)}'s schema design (or an application "
                    f"change) can serve {pronoun}."
                ),
                "affected_tables": tables,
                "mitigation": mitigation_addendum,
                "query_ids": ids_sorted,
            }
        )
    return risk_id


def _coverage_gap_risk(
    risk_id: str,
    engine: str,
    gap: dict[str, str],
    query_text: dict[str, str],
    query_tables: dict[str, set[str]],
) -> dict:
    """One risk for the queries assigned to ``engine`` that its design does not serve."""
    ids = sorted(gap)
    n = len(ids)
    severity = min(gap.values(), key=lambda s: _SEVERITY_ORDER.get(s, 9))
    texts = []
    for q in ids[:3]:
        t = " ".join(query_text.get(q, "").split()) or q[:12]
        texts.append(t if len(t) <= 80 else t[:77] + "...")
    listed = "; ".join(texts) + (f"; and {n - 3} more" if n > 3 else "")
    return {
        "risk_id": risk_id,
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": (
            f"[{engine}] Schema design gap: {n} {'query' if n == 1 else 'queries'} assigned "
            f"to {display_name(engine)} {'has' if n == 1 else 'have'} no in-scope access "
            f"pattern: {listed} (0% of queries resolved by schema design, {n} remaining)"
        ),
        "affected_tables": sorted(
            {t for q in ids for t in query_tables.get(q, set())} - _PLACEHOLDER_TABLES
        ),
        "mitigation": (
            f"Add in-scope access patterns for {'this query' if n == 1 else 'these queries'} "
            f"to the {display_name(engine)} schema design, or route "
            f"{'it' if n == 1 else 'them'} to an engine that serves "
            f"{'it' if n == 1 else 'them'}."
        ),
        "query_ids": ids,
        "coverage_gap": True,
    }


# aurora-anti-05 ("no-relational-need") and aurora-anti-06
# ("single-access-pattern-table"), aurora_common_pattern_catalog.py: both
# recommend DynamoDB for a table whose OWN flagged queries look like simple
# key-value access. That table can still have *other* in-scope queries that
# need a hard capability only Aurora offers (#338's capability gate) or are a
# utility/DDL statement (#327) -- the table then stays on Aurora for those
# other queries' sake, not because the flagged ones need it. A review of
# #375 found the risk reading as a flat contradiction of the routing in that
# case ("Consider DynamoDB" right next to an architecture that keeps the
# table on Aurora, with no reason given); it must instead name the reason
# the table stays, and offer DynamoDB as a later-wave opportunity for the
# flagged queries specifically, not a blanket recommendation.
_DYNAMODB_ALTERNATIVE_ANTI_PATTERNS = frozenset(
    {"no-relational-need", "single-access-pattern-table"}
)

# The capability gate (assignment_resolver.py, reality_check.py) and the
# utility pin (#327) already explain themselves in a query's own
# assignment_reason text ("[capability] dynamodb lacks required capability:
# aggregation, complex_joins", "utility/metadata statement -- kept on
# source-compatible relational engine") -- reused here rather than
# re-deriving the same decision from the query text a second time.
_CAPABILITY_PIN_RE = re.compile(r"\[capability\]\s+\S+\s+lacks required capability:\s*([^;]+)")
_UTILITY_PIN_RE = re.compile(r"utility/metadata statement")
_CAPABILITY_PIN_DISPLAY = {
    "aggregation": "aggregation",
    "complex_joins": "multi-table joins",
    "computed_join": "a join on a computed expression",
    "sql_admin": "a utility or DDL statement",
}


def _capability_pin_reason(reasons: Iterable[str]) -> str | None:
    """The named reason (joins, aggregation, a utility statement, ...) that some
    query sharing ``reasons``' table is pinned to its engine by a hard
    capability, or ``None`` when none of ``reasons`` names one.
    """
    found: set[str] = set()
    for reason in reasons:
        reason = reason or ""
        if _UTILITY_PIN_RE.search(reason):
            found.add(_CAPABILITY_PIN_DISPLAY["sql_admin"])
        match = _CAPABILITY_PIN_RE.search(reason)
        if match:
            for cap in match.group(1).split(","):
                cap = cap.strip()
                if cap:
                    found.add(_CAPABILITY_PIN_DISPLAY.get(cap, cap))
    if not found:
        return None
    names = sorted(found)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _tables_for(
    query_ids: set[str], ap_tables: list[str], query_tables: dict[str, set[str]]
) -> list[str]:
    """The anti-pattern's tables that the given queries touch (all of them if unknown)."""
    touched: set[str] = set()
    for q in query_ids:
        touched |= query_tables.get(q, set())
    narrowed = sorted(t for t in ap_tables if t in touched)
    return narrowed or sorted(ap_tables)


def _resolved_risk(
    engine: str,
    severity: str,
    description: str,
    tables: list[str],
    query_ids: set[str],
    resolved_on: str | None,
    reason: str,
) -> dict:
    return {
        "engine": engine,
        "severity": severity,
        "description": f"[{engine}] {description}",
        "affected_tables": tables,
        "query_ids": sorted(query_ids),
        "resolved_on": resolved_on,
        "reason": reason,
    }


def _complementary_services(effective_engines: set[str]) -> list[str]:
    """Where unsupported patterns can go: services in the effective architecture that
    take text search / aggregations, then application code."""
    services = []
    if "opensearch" in effective_engines:
        services.append(display_name("opensearch"))
    for engine in sorted(effective_engines & AURORA_ENGINES):
        services.append(f"{display_name(engine)} (full-text indexes, SQL aggregation)")
    services.append("application code")
    return services


_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
# Migration-note object types that are database-side code the application must absorb.
_DB_CODE_OBJECTS = ("procedure", "trigger", "view")


def _join_and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _join_or(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} or {items[-1]}"


def _tagged_engine(risk: dict) -> str | None:
    text = str(risk.get("description") or "")
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")]
    return None


def _engines_of(risks: list[dict]) -> list[str]:
    """Display names of the engines the risks are tagged with, in first-seen order."""
    seen: list[str] = []
    for r in risks:
        engine = _tagged_engine(r)
        if engine and engine not in seen:
            seen.append(engine)
    return [display_name(e) for e in seen]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _risk_scope(risk: dict) -> str:
    tables = [t for t in risk.get("affected_tables") or [] if t]
    if tables:
        shown = ", ".join(tables[:3])
        return shown + (f" and {len(tables) - 3} more" if len(tables) > 3 else "")
    n = len(risk.get("query_ids") or [])
    return f"{n} {'query' if n == 1 else 'queries'}" if n else "no table recorded"


def _build_mitigation_strategies(risks: list[dict], effective_engines: set[str]) -> list[str]:
    """Mitigation strategies derived from the risks actually present (#222).

    The CRITICAL/HIGH risks come first, each with its engine, scope and own
    mitigation; every other line names the risks, engines or objects it is about.
    Only engines in ``effective_engines`` (the post-reality-check architecture) are
    suggested as homes for unsupported patterns.
    """
    strategies: list[str] = []
    severe = sorted(
        (r for r in risks if r.get("severity") in ("CRITICAL", "HIGH")),
        key=lambda r: (_SEVERITY_ORDER.get(r["severity"], 9), r.get("risk_id", "")),
    )
    for r in severe:
        engine = _tagged_engine(r)
        head = r.get("risk_id", "Risk")
        label = f"{r['severity']}, {display_name(engine)}" if engine else r["severity"]
        mitigation = r.get("mitigation")
        body = (
            _sentence(mitigation)
            if mitigation
            else "no mitigation recorded; define one before cutover."
        )
        strategies.append(f"{head} ({label}; {_risk_scope(r)}): {body}")

    by_type: dict[str, list[dict]] = {}
    for r in risks:
        by_type.setdefault(r.get("risk_type", ""), []).append(r)

    perf = by_type.get("PERFORMANCE_DEGRADATION", [])
    if perf:
        strategies.append(
            f"Load-test the queries behind {_join_and([r['risk_id'] for r in perf])} on "
            f"{_join_and(_engines_of(perf)) or 'the target databases'} with "
            "production-scale data before cutover."
        )

    complexity = by_type.get("MIGRATION_COMPLEXITY", [])
    unsupported = [r for r in complexity if not r.get("coverage_gap")]
    gaps = [r for r in complexity if r.get("coverage_gap")]
    if gaps:
        n = sum(len(r.get("query_ids") or []) for r in gaps)
        strategies.append(
            f"Add in-scope access patterns for the {n} "
            f"{'query' if n == 1 else 'queries'} the {_join_and(_engines_of(gaps))} schema "
            f"{'design does' if len(gaps) == 1 else 'designs do'} not serve "
            f"({', '.join(r['risk_id'] for r in gaps)}), or route "
            f"{'it' if n == 1 else 'them'} to an engine that serves "
            f"{'it' if n == 1 else 'them'}."
        )
    if unsupported:
        n = len(unsupported)
        engines = _join_and(_engines_of(unsupported))
        strategies.append(
            f"Serve the {n} unsupported query {'pattern' if n == 1 else 'patterns'}"
            + (f" on {engines}" if engines else "")
            + f" ({', '.join(r['risk_id'] for r in unsupported)}) "
            + _join_or([f"in {s}" for s in _complementary_services(effective_engines)])
            + "."
        )

    notes = by_type.get("OPERATIONAL_RISK", [])
    db_code = [r for r in notes if str(r.get("object_type", "")).lower() in _DB_CODE_OBJECTS]
    other_notes = [r for r in notes if r not in db_code]
    if db_code:
        objects = _join_and(
            [f"{r['object_type'].lower()} '{r.get('object_name', '')}'" for r in db_code]
        )
        strategies.append(
            f"Re-implement the {objects} as application logic before migration "
            f"({', '.join(r['risk_id'] for r in db_code)})."
        )
    if other_notes:
        items = _join_and(
            [
                f"{r.get('object_name') or r.get('object_type') or 'migration note'} "
                f"({r['risk_id']})"
                for r in other_notes
            ]
        )
        strategies.append(
            f"Build the application-side {'replacement' if len(other_notes) == 1 else 'replacements'} "
            f"for {items} before cutover."
        )

    if severe:
        tables = sorted({t for r in severe for t in (r.get("affected_tables") or []) if t})
        strategies.append(
            "Use blue-green deployment with rollback for the tables behind the HIGH risks: "
            + ", ".join(tables)
            + "."
            if tables
            else "Use blue-green deployment with rollback for the migrations behind "
            + _join_and([r["risk_id"] for r in severe])
            + "."
        )

    # Every engine in the target, the ones carrying risks first.
    watched = _engines_of(risks)
    watched += [
        display_name(e) for e in sorted(effective_engines) if display_name(e) not in watched
    ]
    strategies.append(
        f"Monitor {_join_and(watched) or 'the target databases'} closely during the first "
        "2 weeks post-migration."
    )
    return strategies


def designed_and_not_designed_engines(ranking: list[dict]) -> tuple[list[str], list[str]]:
    """Engines in the effective architecture, split by whether a schema design
    exists for them (#132 review, 370-1/370-2).

    A *versioned* entry (an assignment ran, so ``assigned_queries`` is present,
    even if ``0``) is limited to engines with workload (``assigned_queries``)
    or a cache overlay (``cache_overlay_queries``): an engine with neither was
    not part of the effective assignment at all (eliminated, or never routed
    anything), so it belongs in neither list -- there is nothing to say about
    it either way.

    An *unversioned* entry (no assignment artifact at all, so
    ``assigned_queries`` was never added) has no workload signal to gate on in
    the first place, so it is classified by ``schema_design_available`` alone
    (#370 recheck): gating it the same way an assignment-aware entry is gated
    treated "no assignment yet" the same as "assignment ran and routed
    nothing here", which emptied both lists even when an engine clearly had a
    design and made the executive-summary prompt fall back to the fully
    assertive branch with nothing to back it.

    The single source of truth for "has a design" everywhere this question is
    asked: the architecture rationale, the deterministic summary, and both the
    Bedrock and Claude Code (external) executive-summary prompts
    (``prepare_synthesis_llm_input``'s ``schema_design_status``), so they
    can't drift out of step (370-4).
    """
    designed: list[str] = []
    not_designed: list[str] = []
    for r in ranking:
        if "assigned_queries" in r:
            has_workload = r.get("assigned_queries", 0) > 0 or r.get("cache_overlay_queries", 0) > 0
            if not has_workload:
                continue
        if r.get("schema_design_available"):
            designed.append(r["target"])
        else:
            not_designed.append(r["target"])
    return designed, not_designed


def build_architecture_recommendation(
    data: SynthesisData,
    ranking: list[dict],
    table_mappings: list[dict],
) -> dict:
    """Determine the recommended architecture type and database allocations."""
    # Count engines that have actual query assignments or schema designs
    engines_with_workload = [
        r["target"]
        for r in ranking
        if r.get("assigned_queries", 0) > 0
        or r.get("cache_overlay_queries", 0) > 0
        or r.get("schema_design_available")
    ]

    # Architecture type
    if len(engines_with_workload) <= 1:
        arch_type = "SINGLE_DATABASE"
    else:
        # Check if any engine is a cache (elasticache)
        has_cache = any("cache" in e.lower() for e in engines_with_workload)
        arch_type = "HYBRID_WITH_CACHE" if has_cache else "MULTI_DATABASE"

    # Database allocations
    engine_tables: dict[str, list[str]] = {}
    for mapping in table_mappings:
        engine = mapping["recommended_database"]
        engine_tables.setdefault(engine, []).append(mapping["source_table"])

    # When NO engine has a schema design (llm_mode=none, #281), table_mappings
    # are empty, so every engine with workload is listed with no tables; leaving
    # them all out made the rationale read "Hybrid architecture: .". In a run
    # where some engine was designed, an engine with workload but no design is
    # the retained engine: it stays out of databases, so the report renderers
    # call it "Retained" and the deck puts it in the no-migration Wave 1.
    designed_engines, _not_designed_engines = designed_and_not_designed_engines(ranking)
    no_schema_design = not designed_engines
    databases = []
    for r in ranking:
        engine = r["target"]
        tables = engine_tables.get(engine, [])
        if not tables and not r.get("schema_design_available"):
            if not (no_schema_design and engine in engines_with_workload):
                continue

        databases.append(
            {
                "service": engine,
                "table_count": len(tables),
                "rationale": r.get("rationale") or _engine_rationale(data, r),
                "tables": tables,
                "confidence_score": r["confidence_score"],
                "routed_confidence": r.get("routed_confidence"),
            }
        )

    rationale = _architecture_rationale(arch_type, databases, ranking)
    if databases and no_schema_design:
        rationale += " Schema design was not run, so no tables are allocated yet."
    return {
        "databases": databases,
        "architecture_type": arch_type,
        "rationale": rationale,
    }


def _routed_phrase(r: dict, noun: str, what: tuple[str, str]) -> str:
    """``90% mean fit across 98 queries (22 rated tables)`` (#152); empty without one.

    "Rated" because ``routed_tables`` counts only the source tables the engine's
    *analysis* rated (#312 review) -- not every table the routed queries touch,
    which the schema design's own table count already states elsewhere.

    Says "signal only — no table-level evidence" when no routed query touches a
    source table the engine's analysis rated: the fit is then the basic baseline
    plus the signal bonus, not a measurement. A partial fit adds "partly
    signal-based" once at least a quarter of the queries lack table evidence (the
    labels and threshold of ``src.shared.ranking``, which the deck uses too).
    """
    fit = r.get("routed_confidence")
    if fit is None:
        return ""
    n = int(r.get("routed_queries") or 0)
    n_t = int(r.get("routed_tables") or 0)
    unbacked = int(r.get("routed_queries_without_table_evidence") or 0)
    evidence = r.get("routed_confidence_evidence")
    if evidence == "signal_only" or (evidence is None and not n_t):
        detail = SIGNAL_ONLY_NOTE
    else:
        detail = _count(n_t, "rated table", "rated tables")
        if unbacked:
            detail += f"; {_count(unbacked, *what)} without table-level evidence"
            if evidence_note(r) == PARTIAL_NOTE:
                detail += f", {PARTIAL_NOTE}"
    return f"{fit}% {noun} across {_count(n, *what)} ({detail})"


def _engine_rationale(data: SynthesisData, r: dict) -> str:
    """Generate a rationale string for an engine recommendation.

    It names the workload routed to the engine and how well the engine fits it
    (#152), e.g. "90% mean fit across 98 queries (22 rated tables), led by key-value
    lookups (36 of 98)". The analysis average over every analyzed table is the
    ``analysis_confidence`` field, not the rationale: it describes the tables the
    engine looked at, not the work it was given. A report without an assignment
    keeps that average, since nothing is routed yet.

    Access patterns are counted in scope, as in the summary (#255). The cache layer
    is described by the reads it fronts, not by an owner share (#296) -- via
    ``cache_front_description``, the one place every deliverable's
    cache-fronting sentence is built (#375 review), so this "Why" cell and the
    roadmap's own wave 2 card cannot name different engines.
    """
    if is_cache_layer(r):
        retained_engine = SOURCE_ENGINE_TO_AURORA.get(source_database_engine(data.collector))
        owners = cache_front_description(retained_engine, r.get("cache_overlay_owners"))
        n = r.get("cache_overlay_queries", 0)
        lead = r.get("routed_lead")
        shape = f", mostly {PATTERN_LABELS.get(lead, lead.replace('_', ' '))}s" if lead else ""
        head = (
            f"Cache layer for {n} hot {'read' if n == 1 else 'reads'} "
            f"({r.get('cache_call_share_percent', 0)}% of calls){shape}, cache-aside in front "
            f"of {owners}; it owns no queries"
        )
        fit = _routed_phrase(r, "mean cache fit", ("cached read", "cached reads"))
        parts = [head + (f"; {fit}" if fit else "")]
        if r.get("target_tables", 0) > 0:
            parts.append(
                f"schema design: {_count(r['target_tables'], 'key design', 'key designs')}, "
                + _in_scope_phrase(*_access_pattern_scope(data, r))
            )
        if r["monthly_cost_usd"] > 0:
            cps = r.get("cache_calls_per_second", 0.0)
            floor = r.get("cache_min_calls_per_second", 0.0)
            # Review of #375: a bare dollar figure for a cache serving only a
            # handful of reads reads as an unjustified cost -- state the
            # overlay's own eligibility floor (#304/#296) and the combined
            # traffic that cleared it, so the figure is grounded in the same
            # facts the cache_overlay section already carries, not asserted
            # on its own.
            justification = (
                f" ({cps:g} calls/s combined across these reads clears the {floor:g} "
                "calls/s hot-read floor)"
                if floor
                else ""
            )
            parts.append(f"estimated ${r['monthly_cost_usd']:.2f}/month{justification}")
        return ". ".join(parts) + "."
    if r.get("routed_confidence") is not None:
        n = int(r.get("routed_queries") or 0)
        lead = r.get("routed_lead")
        parts = [
            _routed_phrase(r, "mean fit", ("query", "queries"))
            + (
                f", led by {signal_noun(lead)} ({r.get('routed_lead_count', 0)} of {n})"
                if lead
                else ""
            )
        ]
    elif "assigned_queries" in r:
        parts = [
            f"No queries routed; {r['confidence_score']}% average confidence across "
            f"{r['tables_analyzed']} analyzed tables"
        ]
    else:
        parts = [
            f"{r['confidence_score']}% average confidence across {r['tables_analyzed']} tables"
        ]
        if r["patterns_detected"] > 0:
            parts.append(f"{r['patterns_detected']} matching workload patterns")
    if r.get("target_tables", 0) > 0:
        parts.append(
            f"schema design: {r['target_tables']} target tables, "
            + _in_scope_phrase(*_access_pattern_scope(data, r))
        )
    if r["monthly_cost_usd"] > 0:
        parts.append(f"estimated ${r['monthly_cost_usd']:.2f}/month")
    return ". ".join(parts) + "."


def _architecture_rationale(
    arch_type: str,
    databases: list[dict],
    ranking: list[dict],
) -> str:
    """Generate architecture-level rationale."""
    if arch_type == "SINGLE_DATABASE":
        if databases:
            return (
                f"Workload analysis indicates {databases[0]['service']} as the primary "
                f"target with {engine_confidence(databases[0]):.0f}% confidence."
            )
        return "Insufficient data to recommend a specific architecture."
    elif arch_type == "HYBRID_WITH_CACHE":
        primary = [d for d in databases if "cache" not in d["service"].lower()]
        cache = [d for d in databases if "cache" in d["service"].lower()]
        parts = []
        if primary:
            parts.append(f"{primary[0]['service']} for primary data storage")
        if cache:
            parts.append(f"{cache[0]['service']} for caching hot data")
        return f"Hybrid architecture: {' and '.join(parts)}."
    else:
        services = [d["service"] for d in databases[:3]]
        return f"Multi-database architecture using {', '.join(services)} based on workload pattern analysis."


# What one schema-design object is called per engine (for the summary, #219).
# Relational designs say "target table": the deck and Decision Report count the
# *source* tables mapped to each engine on the same page.
_OBJECT_NOUNS = {
    "elasticache": ("key design", "key designs"),
    "documentdb": ("collection", "collections"),
    "opensearch": ("index", "indexes"),
}


def _object_noun(engine: str) -> tuple[str, str]:
    return _OBJECT_NOUNS.get(engine, ("target table", "target tables"))


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _access_pattern_scope(data: SynthesisData, rank: dict) -> tuple[int, int]:
    """``(in_scope, out_of_scope)`` access patterns of an engine's schema design.

    The ranking counts every pattern; without the design itself all of them are
    taken as in scope (nothing is known to be out of scope).
    """
    artifacts = data.engines.get(rank["target"])
    if artifacts is None or artifacts.schema_design is None:
        return int(rank.get("access_patterns", 0)), 0
    patterns = artifacts.schema_design.get("access_patterns", [])
    n_in = sum(1 for ap in patterns if ap.get("in_scope", True))
    return n_in, len(patterns) - n_in


def _in_scope_phrase(n_in: int, n_out: int) -> str:
    """``47 in-scope access patterns (plus 4 out of scope)`` (#255).

    The Engineering Report heading counts every pattern, so the summary says that
    it counts in-scope ones and how many it leaves out.
    """
    text = _count(n_in, "in-scope access pattern", "in-scope access patterns")
    return text + (f" (plus {n_out} out of scope)" if n_out else "")


def _risk_sentence(n_open: int, level: str, n_resolved: int) -> str:
    """``8 open migration risks (overall: LOW); 4 more were resolved by the assignment.``

    The resolved risks are not part of the open ones; the Engineering Report lists
    them apart (#258).
    """
    text = f"{_count(n_open, 'open migration risk', 'open migration risks')} (overall: {level})"
    if n_resolved:
        verb = "was" if n_resolved == 1 else "were"
        text += f"; {n_resolved} more {verb} resolved by the assignment"
    return text + "."


def build_summary(
    data: SynthesisData,
    ranking: list[dict],
    table_mappings: list[dict],
    tco: dict,
    risks: dict,
    query_groups: list[dict],
    eliminated: dict[str, str | None] | None = None,
) -> str:
    """Build a comprehensive executive summary.

    With assignment data the summary describes the whole workload split: schema-design
    totals over every engine that carries queries, and "other targets" are only the
    engines that carry none (left empty by the assignment, or eliminated by the
    reality check, see ``synthesis_grounding.eliminated_engines``). With an
    assignment the ranking is ordered by workload share (#152); without one
    ``ranking[0]`` is the analysis-weight leader.
    """
    if not ranking:
        return "No analysis results available."

    top = ranking[0]
    total_source_tables = len(data.source_tables)
    total_queries = len(data.source_queries)

    parts = []

    # Opening
    parts.append(
        f"Analyzed {total_source_tables} source tables and {total_queries} query patterns "
        f"across {len(ranking)} target database(s)."
    )

    has_assignment = any("assigned_queries" in r for r in ranking)
    with_workload = sorted(
        (r for r in ranking if r.get("assigned_queries", 0) > 0),
        key=lambda r: r.get("assigned_queries", 0),
        reverse=True,
    )
    if has_assignment:
        # Workload split view, largest share first
        if with_workload:
            engine_parts = [
                f"{r['target']} handles {r['assigned_queries']} queries "
                f"({r.get('workload_percent', 0)}%)"
                for r in with_workload
            ]
            parts.append(f"Workload split: {', '.join(engine_parts)}.")
        cache = [r for r in ranking if is_cache_layer(r) and r.get("cache_overlay_queries")]
        for r in cache:
            n = r["cache_overlay_queries"]
            parts.append(
                f"Cache layer: {r['target']} fronts {n} hot "
                f"{'read' if n == 1 else 'reads'} ({r.get('cache_call_share_percent', 0)}% of "
                "calls) cache-aside and owns no queries."
            )
        designed_engines, _ = designed_and_not_designed_engines(ranking)
        designed = [r for r in with_workload + cache if r["target"] in designed_engines]
        if designed:
            engines = {r["target"] for r in designed}
            groups = sum(1 for g in query_groups if engines & set(g.get("engines") or []))
            scope = {r["target"]: _access_pattern_scope(data, r) for r in designed}
            per_engine = "; ".join(
                f"{r['target']}: {_count(r.get('target_tables', 0), *_object_noun(r['target']))}"
                + (
                    f", {_count(scope[r['target']][0], 'in-scope access pattern', 'in-scope access patterns')}"
                    if scope[r["target"]][0]
                    else ""
                )
                for r in designed
            )
            parts.append(
                f"Schema design produced "
                f"{sum(r.get('target_tables', 0) for r in designed)} target objects and "
                + _in_scope_phrase(
                    sum(n for n, _ in scope.values()), sum(n for _, n in scope.values())
                )
                + f" across {groups} query groups ({per_engine})."
            )
    else:
        parts.append(
            f"Top recommendation: {top['target']} with {top['confidence_score']}% "
            f"average confidence."
        )
        # Schema design summary
        if top.get("schema_design_available"):
            parts.append(
                f"Schema design produced {top['target_tables']} target tables with "
                f"{_in_scope_phrase(*_access_pattern_scope(data, top))} across "
                f"{top['pattern_groups']} query groups."
            )

    # Table mapping summary
    if table_mappings:
        engines_used = sorted({m["recommended_database"] for m in table_mappings})
        parts.append(f"{len(table_mappings)} source tables mapped to {', '.join(engines_used)}.")

    # Cost
    if tco["projected_monthly_cost"] > 0:
        parts.append(
            f"Estimated monthly cost: ${tco['projected_monthly_cost']:.2f}"
            + (
                f" ({tco['savings_percent']}% savings vs current)."
                if tco["savings_percent"]
                else "."
            )
        )

    # Risks
    risk_count = len(risks.get("risks", []))
    if risk_count > 0:
        parts.append(
            _risk_sentence(
                risk_count,
                risks["overall_risk_level"],
                len(risks.get("resolved_risks") or []),
            )
        )

    # Query groups
    if query_groups:
        top_groups = [g["group_name"] for g in query_groups[:3]]
        parts.append(f"Top query groups by throughput: {', '.join(top_groups)}.")

    # Other engines: evaluated but carrying no workload in the target
    if has_assignment:
        others = [
            f"{r['target']} (no queries assigned)"
            for r in ranking
            if r.get("assigned_queries", 0) == 0 and not is_cache_layer(r)
        ]
        listed = {r["target"] for r in ranking}
        for engine, absorber in (eliminated or {}).items():
            if engine in listed:
                continue
            others.append(
                f"{engine} (consolidated into {absorber} by the reality check)"
                if absorber
                else f"{engine} (eliminated by the reality check)"
            )
        if others:
            parts.append(f"Other targets evaluated: {', '.join(others)}.")
    elif len(ranking) > 1:
        others_txt = ", ".join(f"{r['target']} ({r['confidence_score']}%)" for r in ranking[1:])
        parts.append(f"Other targets evaluated: {others_txt}.")

    return " ".join(parts)


def _executive_summary_prompt_fragments(
    designed: list[str], not_designed: list[str]
) -> dict[str, str]:
    """Prompt text for :func:`generate_executive_summary`, keyed by whether
    every, some, or no in-scope engine has a schema design (#132 review,
    370-1/370-3).

    All designed (``designed`` non-empty, ``not_designed`` empty): the
    original, fully assertive text. Some designed, some not: the premise,
    capability-gaps bullet and table/index-count instruction name only the
    designed engines, and separately tell the model schema design has not
    run for the rest -- never that it has. None designed (including the
    degenerate case where both lists are empty, #370 recheck: an empty
    ``not_designed`` is NOT by itself proof everything is designed): the
    routing-only premise, with no table/index count or "built" framing at
    all. ``tone_line`` and ``authority_claim`` (370-3) are conditional on the
    same flag, so they never sit next to "schema design has not run" while
    still claiming "here is what we built".
    """
    if designed and not not_designed:
        return {
            "premise": (
                "You just completed a full database modernization assessment. You "
                "designed the target schemas, mapped every access pattern, and "
                "validated everything. "
            ),
            "capability_gaps_bullet": (
                "- Capability gaps between source and target (e.g., JOINs, GROUP BY, "
                "recursive queries) are SOLVED by the schema design you produced. You "
                "already designed the access patterns that replace them. Present the "
                "solution, not the gap.\n"
            ),
            "sentence_1_2_detail": (
                "- Which engines, how many target tables/indexes were designed, and "
                "what role each engine plays in the workload.\n"
            ),
            "tone_line": (
                "- Your tone is: 'We analyzed this, here is what we built, here is "
                "how it works.' Not: 'There are concerns, risks, and unknowns.'\n\n"
            ),
            "authority_claim": (
                "you have already done the work: analyzed every query, designed "
                "every target schema, and mapped every access pattern. "
            ),
        }

    not_designed_names = ", ".join(display_name(e) for e in not_designed)
    not_designed_it = "it" if len(not_designed) == 1 else "them"

    if designed:
        designed_names = ", ".join(display_name(e) for e in designed)
        premise = (
            "You just completed a database modernization assessment. You designed "
            f"the target schema for {designed_names} and mapped the access patterns "
            f"it serves. Schema design has not run yet for {not_designed_names}: "
            f"there is no target schema for {not_designed_it} to report. Say that "
            f"plainly for {not_designed_it}; never describe a schema for "
            f"{not_designed_it} that does not exist. "
        )
        capability_gaps_bullet = (
            "- Capability gaps between source and target (e.g., JOINs, GROUP BY, "
            f"recursive queries) are SOLVED by the schema design for {designed_names}. "
            f"For {not_designed_names}, say schema design has not run and is what "
            "will address them; never say it already has.\n"
        )
        sentence_1_2_detail = (
            f"- Which engines the workload is routed to. Name a designed target "
            f"table/index count only for {designed_names}. For {not_designed_names}, "
            "say schema design is the next step: do not state a target table or "
            "index count for it, and do not imply one exists.\n"
        )
        authority_claim = (
            "you have already done the work: analyzed every query and routed it to "
            f"the engine that fits it, and designed the target schema for "
            f"{designed_names}. Schema design has not run yet for "
            f"{not_designed_names}, and you say so plainly rather than describing a "
            "designed schema for it that does not exist. "
        )
    else:
        premise = (
            "You just completed the routing phase of a database modernization "
            "assessment: every query was analyzed and routed to the engine that "
            "fits it. Schema design has not run yet, so there is no target schema, "
            "table count, or access pattern to report. Say that plainly; never "
            "describe design work that has not happened. "
        )
        capability_gaps_bullet = (
            "- Capability gaps between source and target (e.g., JOINs, GROUP BY, "
            "recursive queries) are resolved by the schema design still to come. "
            "Say that schema design, which has not run yet, is what addresses "
            "them; never say it already has.\n"
        )
        sentence_1_2_detail = (
            "- Which engines the workload was routed to and what role each will "
            "play. Schema design is the next step, since none has run: do not "
            "state a target table or index count, and do not imply one exists.\n"
        )
        authority_claim = (
            "you have already done the work: analyzed every query and routed it "
            "to the engine that fits it. Schema design has not run yet, and you "
            "say so plainly rather than describing designed schemas that do not "
            "exist. "
        )

    return {
        "premise": premise,
        "capability_gaps_bullet": capability_gaps_bullet,
        "sentence_1_2_detail": sentence_1_2_detail,
        "tone_line": (
            "- Your tone is: 'We analyzed this, here is how the workload is "
            "routed, here is what happens next.' Not: 'There are concerns, risks, "
            "and unknowns.'\n\n"
        ),
        "authority_claim": authority_claim,
    }


def generate_executive_summary(
    deterministic_summary: str,
    ranking: list[dict],
    query_groups: list[dict],
    tco: dict,
    risks: dict,
    table_mappings: list[dict],
    trade_offs: list[dict | str],
    effective_architecture: dict | None = None,
) -> str:
    """Generate a natural-language executive summary using an LLM.

    ``effective_architecture`` (per-engine tables, top query groups, eliminated engines)
    is passed to the model as the authority for every engine and table claim; the
    caller still post-checks the result (``check_summary_grounding``).

    Falls back to the deterministic summary if the LLM call fails.
    """
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel
    except ImportError:
        logger.warning("Strands not available — using deterministic summary")
        return deterministic_summary

    # Which in-scope engines have a schema design and which don't (#116, #132,
    # 370-1/370-2). An engine with a design is narrated as designed; one
    # without must never be, even when some other engine in the same run was
    # designed (the partial case the #132 review found still uncaught).
    designed_engines, not_designed_engines = designed_and_not_designed_engines(ranking)
    fragments = _executive_summary_prompt_fragments(designed_engines, not_designed_engines)

    # Build focused context — only what a CTO needs to see
    engine_workload = []
    for r in ranking:
        entry = {"engine": r["target"], "confidence": round(engine_confidence(r))}
        if r.get("assigned_queries"):
            entry["queries"] = r["assigned_queries"]
            entry["workload_pct"] = r.get("workload_percent", 0)
        if is_cache_layer(r):
            entry["role"] = "cache layer (owns no queries)"
            entry["cached_queries"] = r.get("cache_overlay_queries", 0)
            entry["cached_call_share_pct"] = r.get("cache_call_share_percent", 0)
        if r.get("schema_design_available"):
            entry["target_tables"] = r.get("target_tables", 0)
            entry["access_patterns"] = r.get("access_patterns", 0)
        engine_workload.append(entry)

    top_groups_ctx = [
        {
            "name": g["group_name"],
            "rps": round(g["total_design_rps"], 1),
            "patterns": len(g["access_patterns"]),
        }
        for g in query_groups[:5]
    ]

    high_risks = [
        {"severity": r["severity"], "desc": r["description"][:120]}
        for r in risks.get("risks", [])
        if r["severity"] in ("HIGH", "CRITICAL")
    ][:5]

    context = {
        "effective_architecture": effective_architecture or {},
        "engines": engine_workload,
        "table_mappings": len(table_mappings),
        "top_query_groups": top_groups_ctx,
        "risk_level": risks.get("overall_risk_level", "LOW"),
        "high_risks": high_risks,
        "key_trade_offs": [
            (
                {"description": t["description"], "impact": t["impact"]}
                if isinstance(t, dict) and "description" in t
                else str(t)
            )
            for t in trade_offs[:5]
        ],
    }

    premise = fragments["premise"]
    capability_gaps_bullet = fragments["capability_gaps_bullet"]
    sentence_1_2_detail = fragments["sentence_1_2_detail"]
    tone_line = fragments["tone_line"]

    prompt = (
        premise + "Now you are writing the final executive summary the CTO reads before "
        "deciding to proceed.\n\n"
        "Write 3-4 SHORT sentences. You are a trusted advisor having a conversation "
        "with the CTO, not writing a report. Be warm but authoritative, like a "
        "senior architect who has done this many times and genuinely wants the "
        "team to succeed.\n\n"
        "CRITICAL GUARDRAILS (violating any of these is a failure):\n"
        "- You are ALWAYS presenting a migration path forward. Never recommend "
        "stopping, going back, collecting more data, or deferring the decision.\n"
        "- NEVER express lack of confidence, doubt, or uncertainty about the "
        "migration. You completed the analysis, you have the answer.\n"
        "- NEVER mention confidence scores, percentages, or statistical metrics. "
        "The CTO does not need to see internal scoring.\n"
        "- Complexity is EXPECTED in modernization. Do not frame complexity as a "
        "blocker or a reason to pause. Frame it as 'here is the path and here is "
        "what we handle along the way.'\n"
        "- If multiple databases are needed, present that as a STRENGTH of the "
        "architecture (purpose-built databases for each workload pattern), not as "
        "a complication.\n"
        + capability_gaps_bullet
        + tone_line
        + "GROUNDING (a summary that breaks this is rejected):\n"
        f"- {SUMMARY_GROUNDING_RULE}\n\n"
        "SENTENCE 1-2: The architecture.\n"
        + sentence_1_2_detail
        + "- If multiple engines are involved, explain how data flows between them "
        "using the specific AWS managed service (see AWS INTEGRATIONS below). "
        "This is what makes it a real architecture, not just a list of databases.\n\n"
        "SENTENCE 3-4: What changes for them.\n"
        "The source database is relational (MySQL or PostgreSQL). The CTO's team "
        "is used to strong consistency, JOINs, GROUP BY, and ad-hoc queries. "
        "If the target architecture changes any of that, explain what is different "
        "and what it means IN PRACTICE. For example:\n"
        "- If search data flows through a pipeline, say that search results may "
        "be a few seconds behind writes and explain this is normal for this pattern.\n"
        "- If JOINs were replaced by denormalized tables, explain that adding a "
        "new query dimension later means a schema change, not just a new SQL query.\n"
        "- If eventual consistency applies, explain what that feels like to a user.\n"
        "Frame these as 'here is what is different and why it is worth it', not as "
        "'risks'. You are helping them understand the new world, not scaring them.\n"
        "If the risk level is LOW and trade-offs are minor, keep it brief or skip.\n\n"
        "AWS MANAGED INTEGRATIONS (use instead of generic pattern names):\n"
        "- DynamoDB to OpenSearch: via OpenSearch Ingestion (fully managed, near "
        "real-time replication via DynamoDB Streams)\n"
        "- DocumentDB to OpenSearch: zero-ETL for full-text search over document collections\n"
        "- DynamoDB to Redshift: analytics on transactional data without production impact\n"
        "- Aurora MySQL/PostgreSQL to Redshift: near real-time analytics on relational data\n"
        "Only mention one if it directly applies.\n\n"
        "STRICT STYLE RULES:\n"
        "- NEVER use em dashes (the long dash). Use commas, periods, or parentheses instead.\n"
        "- NEVER use the word 'straightforward', 'robust', 'leverage', 'comprehensive', "
        "'seamless', 'cutting-edge', 'holistic', 'synergy', 'paradigm', 'elevate', "
        "'landscape', 'realm', 'foster', 'delve', 'moreover', 'furthermore', 'notably'\n"
        "- Do NOT mention cost, pricing, savings, or dollar amounts. Cost is shown separately.\n"
        "- No buzzwords or marketing language\n"
        "- No markdown, bullet points, or headers\n"
        "- Write like a human talking to another human, not a language model writing a document\n"
        "- Keep it under 4 sentences total\n\n"
        # R1: the context carries customer-derived strings (query-group names,
        # risk/trade-off descriptions). Frame it as untrusted data, not instructions.
        + frame_untrusted(json.dumps(context, indent=2), label="assessment context")
        + "\n\nWrite the briefing now."
    )

    try:
        import os

        model = BedrockModel(
            model_id=os.environ.get(
                "SUMMARY_MODEL_ID",
                "us.anthropic.claude-sonnet-4-6",
            ),
            max_tokens=512,
            temperature=0.2,
        )
        agent = Agent(
            model=model,
            system_prompt=(
                "You are a senior database architect who just completed a thorough "
                "modernization assessment. You speak with absolute authority because "
                f"{fragments['authority_claim']}"
                "You are presenting "
                "the result, not deliberating. You NEVER express doubt, recommend "
                "going back for more data, or suggest the team is not ready. "
                "Complexity is your job and you have handled it. Short, direct, "
                "confident, solution-oriented. No filler, no hedging.\n\n"
                + SYSTEM_PROMPT_DATA_DIRECTIVE
            ),
            tools=[],
            callback_handler=None,
        )

        print("[synthesis] Generating executive summary with LLM...")
        result = agent(prompt)
        narrative = str(result).strip()

        if len(narrative) > 20:
            print(f"[synthesis] Executive summary generated ({len(narrative)} chars)")
            return narrative

    except Exception as exc:
        model_id = os.environ.get("SUMMARY_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        logger.error(
            "LLM executive summary failed (model=%s): %s — using deterministic",
            model_id,
            exc,
        )
        print(f"[synthesis] ERROR: LLM summary failed with model '{model_id}': {exc}")
        print(
            "[synthesis] Set SUMMARY_MODEL_ID env var to override. Falling back to deterministic summary."
        )

    return deterministic_summary
