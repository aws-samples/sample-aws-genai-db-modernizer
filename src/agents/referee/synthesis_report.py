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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.referee.synthesis_data import SynthesisData

logger = logging.getLogger(__name__)


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


def build_ranking(data: SynthesisData) -> list[dict]:
    """Rank target engines by confidence, cost, and pattern coverage."""
    ranking = []
    assignment_dist = _compute_assignment_distribution(data)

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
        schema_tables = schema.get("table_definitions", [])
        if engine == "opensearch" and not schema_tables:
            schema_tables = schema.get("index_designs", []) + schema.get("data_stream_designs", [])
        if engine == "documentdb" and not schema_tables:
            schema_tables = schema.get("collections", [])
        access_patterns = schema.get("access_patterns", [])
        pattern_groups: dict[str, list] = {}
        for ap in access_patterns:
            g = ap.get("pattern_group", "ungrouped")
            pattern_groups.setdefault(g, []).append(ap)

        entry = {
            "target": engine,
            "confidence_score": round(avg_confidence),  # backward compat
            "analysis_confidence": round(avg_confidence),
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

        ranking.append(entry)

    ranking.sort(key=lambda r: r["weight"], reverse=True)
    return ranking


def build_table_mappings(data: SynthesisData) -> list[dict]:
    """Build source table → target engine mappings from schema design outputs.

    Each source table gets mapped to the engine(s) whose schema design
    includes it. A table can map to multiple engines (e.g., payment stays
    in Aurora for writes but gets a DynamoDB read replica).
    """
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

        # Collect table definitions — handle engine-specific formats
        table_defs = schema.get("table_definitions", [])
        if engine == "opensearch" and not table_defs:
            # OpenSearch uses index_designs + data_stream_designs
            for idx in schema.get("index_designs", []):
                table_defs.append(
                    {
                        "table_name": idx.get("index_name", ""),
                        "source_tables": idx.get("source_tables", []),
                        "aggregate_pattern": "search_index",
                    }
                )
            for ds in schema.get("data_stream_designs", []):
                table_defs.append(
                    {
                        "table_name": ds.get("data_stream_name", ""),
                        "source_tables": ds.get("source_tables", []),
                        "aggregate_pattern": "data_stream",
                    }
                )
        if engine == "documentdb" and not table_defs:
            # DocumentDB uses collections with source_tables
            for coll in schema.get("collections", []):
                table_defs.append(
                    {
                        "table_name": coll.get("collection_name", ""),
                        "source_tables": coll.get("source_tables", []),
                        "aggregate_pattern": "document_collection",
                    }
                )

        for table_def in table_defs:
            target_table_name = table_def.get("table_name", "")
            aggregate_pattern = table_def.get("aggregate_pattern", "separate")

            for source_table in table_def.get("source_tables", []):
                if source_table not in table_to_engines:
                    table_to_engines[source_table] = []

                table_to_engines[source_table].append(
                    {
                        "engine": engine,
                        "target_table": target_table_name,
                        "aggregate_pattern": aggregate_pattern,
                        "confidence_score": table_confidence.get(source_table, 0),
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


def build_tco_analysis(data: SynthesisData) -> dict:
    """Aggregate cost estimates from all analysis outputs."""
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
    # Rough RDS cost estimate based on instance class
    current_monthly = _estimate_rds_cost(rds_meta) if rds_meta else 0

    savings_pct = (
        round((1 - total_projected / current_monthly) * 100, 1) if current_monthly > 0 else 0
    )

    return {
        "current_monthly_cost": current_monthly,
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


def build_risk_assessment(data: SynthesisData) -> dict:
    """Compile risks from anti-patterns, migration notes, and unsupported patterns.

    Anti-patterns are cross-referenced against schema design access patterns:
    if all of an anti-pattern's query_ids are covered by in-scope access
    patterns, the risk is considered resolved and downgraded to a note.
    """
    risks = []
    risk_id = 0

    for engine, artifacts in data.engines.items():
        analysis = artifacts.analysis or {}
        schema = artifacts.schema_design or {}

        # Build set of query_ids covered by in-scope schema design access patterns
        covered_query_ids: set[str] = set()
        for ap in schema.get("access_patterns", []):
            if ap.get("in_scope", True):
                covered_query_ids.update(ap.get("query_ids", []))

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

            # If all flagged queries are covered by the schema design, it's resolved
            if ap_query_ids and ap_query_ids.issubset(covered_query_ids):
                continue

            # Partially resolved — note which queries are still uncovered
            uncovered = ap_query_ids - covered_query_ids
            risk_id += 1
            severity = "HIGH" if ap.get("severity_weight", 0) >= 0.7 else "MEDIUM"

            description = ap.get("description", ap.get("anti_pattern_type", "Unknown"))
            if uncovered and ap_query_ids:
                pct_covered = round((1 - len(uncovered) / len(ap_query_ids)) * 100)
                description += f" ({pct_covered}% of queries resolved by schema design, {len(uncovered)} remaining)"

            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "PERFORMANCE_DEGRADATION",
                    "severity": severity,
                    "description": f"[{engine}] {description}",
                    "affected_tables": ap.get("table_ids", []),
                    "mitigation": ap.get("recommendation"),
                }
            )

        # Unsupported patterns from schema design
        for up in schema.get("unsupported_patterns", []):
            risk_id += 1
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "MIGRATION_COMPLEXITY",
                    "severity": "MEDIUM",
                    "description": f"[{engine}] {up.get('pattern_type', 'unknown')}: {up.get('recommendation', '')}",
                    "affected_tables": [],
                    "mitigation": up.get("recommendation"),
                }
            )

        # Migration notes from schema design
        for mn in schema.get("migration_notes", []):
            risk_id += 1
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "OPERATIONAL_RISK",
                    "severity": "MEDIUM",
                    "description": f"[{engine}] {mn.get('object_type', '')}: {mn.get('object_name', '')} — {mn.get('application_logic_required', '')}",
                    "affected_tables": [mn["source_table"]] if mn.get("source_table") else [],
                    "mitigation": f"Implement as application logic: {mn.get('application_logic_required', '')}",
                }
            )

    # Determine overall risk level
    severities = [r["severity"] for r in risks]
    if "CRITICAL" in severities:
        overall = "CRITICAL"
    elif severities.count("HIGH") >= 3:
        overall = "HIGH"
    elif "HIGH" in severities:
        overall = "MEDIUM"
    else:
        overall = "LOW"

    return {
        "overall_risk_level": overall,
        "risks": risks,
        "mitigation_strategies": _build_mitigation_strategies(risks),
    }


def _build_mitigation_strategies(risks: list[dict]) -> list[str]:
    """Generate high-level mitigation strategies from identified risks."""
    strategies = []
    risk_types = {r["risk_type"] for r in risks}

    if "PERFORMANCE_DEGRADATION" in risk_types:
        strategies.append(
            "Run load tests with production-scale data before migration to validate performance targets"
        )
    if "MIGRATION_COMPLEXITY" in risk_types:
        strategies.append(
            "Implement unsupported patterns (text search, aggregations) via complementary services (OpenSearch, application-layer computation)"
        )
    if "OPERATIONAL_RISK" in risk_types:
        strategies.append(
            "Refactor stored procedures, triggers, and views into application logic before migration"
        )
    if any(r["severity"] in ("HIGH", "CRITICAL") for r in risks):
        strategies.append(
            "Use blue-green deployment with rollback capability for high-risk table migrations"
        )

    strategies.append(
        "Monitor target database metrics closely during the first 2 weeks post-migration"
    )
    return strategies


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
        if r.get("assigned_queries", 0) > 0 or r.get("schema_design_available")
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

    databases = []
    for r in ranking:
        engine = r["target"]
        tables = engine_tables.get(engine, [])
        if not tables and not r.get("schema_design_available"):
            continue

        databases.append(
            {
                "service": engine,
                "table_count": len(tables),
                "rationale": _engine_rationale(r),
                "tables": tables,
                "confidence_score": r["confidence_score"],
            }
        )

    return {
        "databases": databases,
        "architecture_type": arch_type,
        "rationale": _architecture_rationale(arch_type, databases, ranking),
    }


def _engine_rationale(r: dict) -> str:
    """Generate a rationale string for an engine recommendation."""
    parts = [f"{r['confidence_score']}% average confidence across {r['tables_analyzed']} tables"]
    if r["patterns_detected"] > 0:
        parts.append(f"{r['patterns_detected']} matching workload patterns")
    if r.get("target_tables", 0) > 0:
        parts.append(
            f"schema design: {r['target_tables']} target tables, {r.get('access_patterns', 0)} access patterns"
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
            return f"Workload analysis indicates {databases[0]['service']} as the primary target with {databases[0]['confidence_score']}% confidence."
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


def build_summary(
    data: SynthesisData,
    ranking: list[dict],
    table_mappings: list[dict],
    tco: dict,
    risks: dict,
    query_groups: list[dict],
) -> str:
    """Build a comprehensive executive summary."""
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

    # Top recommendation — show workload split when assignment data is available
    has_assignment = any("assigned_queries" in r for r in ranking)
    if has_assignment:
        # Workload split view
        engine_parts = []
        for r in ranking:
            aq = r.get("assigned_queries", 0)
            wp = r.get("workload_percent", 0)
            if aq > 0:
                engine_parts.append(f"{r['target']} handles {aq} queries ({wp}%)")
        if engine_parts:
            parts.append(f"Workload split: {', '.join(engine_parts)}.")
        mapped_engines = list({m["recommended_database"] for m in table_mappings})
        if table_mappings:
            parts.append(
                f"{len(table_mappings)} source tables mapped across "
                f"{len(mapped_engines)} engine(s)."
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
            f"{top['access_patterns']} access patterns across "
            f"{top['pattern_groups']} query groups."
        )

    # Table mapping summary
    if table_mappings:
        engines_used = list({m["recommended_database"] for m in table_mappings})
        parts.append(
            f"{len(table_mappings)} source tables mapped to " f"{', '.join(engines_used)}."
        )

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
        parts.append(f"{risk_count} risk(s) identified (overall: {risks['overall_risk_level']}).")

    # Query groups
    if query_groups:
        top_groups = [g["group_name"] for g in query_groups[:3]]
        parts.append(f"Top query groups by throughput: {', '.join(top_groups)}.")

    # Other engines
    if len(ranking) > 1:
        others = ", ".join(f"{r['target']} ({r['confidence_score']}%)" for r in ranking[1:])
        parts.append(f"Other targets evaluated: {others}.")

    return " ".join(parts)


def generate_executive_summary(
    deterministic_summary: str,
    ranking: list[dict],
    query_groups: list[dict],
    tco: dict,
    risks: dict,
    table_mappings: list[dict],
    trade_offs: list[dict | str],
) -> str:
    """Generate a natural-language executive summary using an LLM.

    Falls back to the deterministic summary if the LLM call fails.
    """
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel
    except ImportError:
        logger.warning("Strands not available — using deterministic summary")
        return deterministic_summary

    # Build focused context — only what a CTO needs to see
    engine_workload = []
    for r in ranking:
        entry = {"engine": r["target"], "confidence": r["confidence_score"]}
        if r.get("assigned_queries"):
            entry["queries"] = r["assigned_queries"]
            entry["workload_pct"] = r.get("workload_percent", 0)
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

    prompt = (
        "You just completed a full database modernization assessment. You designed "
        "the target schemas, mapped every access pattern, and validated everything. "
        "Now you are writing the final executive summary the CTO reads before "
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
        "- Capability gaps between source and target (e.g., JOINs, GROUP BY, "
        "recursive queries) are SOLVED by the schema design you produced. You "
        "already designed the access patterns that replace them. Present the "
        "solution, not the gap.\n"
        "- Your tone is: 'We analyzed this, here is what we built, here is how "
        "it works.' Not: 'There are concerns, risks, and unknowns.'\n\n"
        "SENTENCE 1-2: The architecture.\n"
        "- Which engines, how many target tables/indexes were designed, and what "
        "role each engine plays in the workload.\n"
        "- If multiple engines are involved, explain how data flows between them "
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
        f"Context:\n{json.dumps(context, indent=2)}\n\n"
        "Write the briefing now."
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
                "you have already done the work: analyzed every query, designed every "
                "target schema, and mapped every access pattern. You are presenting "
                "the result, not deliberating. You NEVER express doubt, recommend "
                "going back for more data, or suggest the team is not ready. "
                "Complexity is your job and you have handled it. Short, direct, "
                "confident, solution-oriented. No filler, no hedging."
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
