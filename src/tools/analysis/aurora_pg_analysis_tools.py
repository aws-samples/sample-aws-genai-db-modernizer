"""
Aurora PostgreSQL Analysis Tools

PG-specific detection layer on top of the shared relational analysis tools.
Detects PG-native features (CTEs, window functions, JSONB, arrays, LATERAL,
tsvector) and applies PG-specific scoring adjustments.

Implements:
- detect_pg_specific_features() -> list[str]
- analyze_aurora_pg_use_cases() -> WorkloadAnalysis
- analyze_aurora_pg_patterns() -> list[TableRecommendation]
- build_aurora_pg_decision_trace() -> dict
"""

from __future__ import annotations

from src.contracts.analysis_output import (
    AntiPattern,
    Confidence,
    Pattern,
    ScoreBreakdown,
    TableRecommendation,
    WorkloadAnalysis,
)
from src.tools.analysis.aurora_common_analysis_tools import (
    _apply_aurora_common_adjustments,
    _count_inbound_fks,
    analyze_aurora_common_use_cases,
    assess_migration_complexity,
    build_concerns,
    build_rationale,
    compute_catalog_pattern_score,
    compute_relational_need_score,
)
from src.tools.analysis.aurora_common_pattern_catalog import AURORA_PG_SCORE_WEIGHTS
from src.tools.analysis.aurora_common_pattern_catalog import PATTERN_BY_ID as COMMON_PATTERN_BY_ID
from src.tools.analysis.aurora_pg_pattern_catalog import (
    ARRAY_RE,
    CTE_RECURSIVE_RE,
    JSONB_RE,
    LATERAL_RE,
    PG_PATTERN_BY_ID,
    TSVECTOR_RE,
    UPSERT_RE,
    WINDOW_FUNCTION_RE,
)
from src.tools.analysis.scoring import (
    TableProfile,
    build_table_profiles,
    compute_base_scores,
    compute_confidence,
)

# ==========================================================================
# PG-specific feature detection
# ==========================================================================


def detect_pg_specific_features(query_text: str) -> list[str]:
    """Detect PG-specific SQL features in query text using compiled regex patterns.

    Returns list of matched pattern_ids.
    """
    matched: list[str] = []

    if CTE_RECURSIVE_RE.search(query_text):
        matched.append("aurora-pg-01")

    if WINDOW_FUNCTION_RE.search(query_text):
        matched.append("aurora-pg-02")

    if JSONB_RE.search(query_text):
        matched.append("aurora-pg-03")

    if ARRAY_RE.search(query_text):
        matched.append("aurora-pg-04")

    if TSVECTOR_RE.search(query_text):
        matched.append("aurora-pg-05")

    if LATERAL_RE.search(query_text):
        matched.append("aurora-pg-06")

    if UPSERT_RE.search(query_text):
        matched.append("aurora-pg-07")

    return matched


# ==========================================================================
# Main detection function
# ==========================================================================


def analyze_aurora_pg_use_cases(collector_output: dict) -> WorkloadAnalysis:
    """Detect all Aurora PG patterns (common + PG-specific) in a single pass.

    Returns WorkloadAnalysis with combined pattern and anti-pattern lists.
    """
    # Get shared relational patterns
    common_patterns, common_anti_patterns = analyze_aurora_common_use_cases(collector_output)

    # PG-specific detection
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    total_query_count = len(queries)

    # Accumulators per PG pattern
    pg_accum: dict[str, dict] = {
        f"aurora-pg-0{i}": {"query_ids": [], "table_ids": []} for i in range(1, 8)
    }

    for q in queries:
        qid = q.get("query_id", "")
        text = q.get("query_text") or ""
        tables = q.get("tables_accessed") or []

        matched_ids = detect_pg_specific_features(text)
        for pid in matched_ids:
            if pid in pg_accum:
                pg_accum[pid]["query_ids"].append(qid)
                pg_accum[pid]["table_ids"].extend(tables)

    # Build PG-specific patterns
    pg_patterns: list[Pattern] = []
    for pid, data in pg_accum.items():
        if data["query_ids"]:
            catalog = PG_PATTERN_BY_ID[pid]
            frequency_pct = None
            if total_query_count > 0:
                frequency_pct = min(100.0, len(data["query_ids"]) / total_query_count * 100.0)
            pg_patterns.append(
                Pattern(
                    pattern_id=pid,
                    pattern_type=catalog.pattern_type,
                    confidence=Confidence.HIGH,
                    description=catalog.description,
                    query_ids=data["query_ids"],
                    table_ids=sorted(set(data["table_ids"])) if data["table_ids"] else None,
                    frequency_percent=(
                        round(frequency_pct, 2) if frequency_pct is not None else None
                    ),
                )
            )

    # Combine
    all_patterns = common_patterns + pg_patterns
    return WorkloadAnalysis(
        patterns_detected=all_patterns,
        anti_patterns_detected=common_anti_patterns if common_anti_patterns else None,
    )


# ==========================================================================
# Scoring pipeline
# ==========================================================================


def _apply_aurora_pg_adjustments(
    scores: ScoreBreakdown,
    profile: TableProfile,
) -> ScoreBreakdown:
    """Apply PG-specific scoring adjustments on top of common adjustments."""
    # First apply common relational adjustments
    scores = _apply_aurora_common_adjustments(scores, profile)

    pattern = scores.pattern_match_score
    complexity = scores.complexity_score
    performance = scores.performance_score
    cost = scores.cost_score

    pattern_types = set(profile.pattern_types)

    # PG-specific pattern bonuses
    if "cte-recursive" in pattern_types:
        pattern += 15
    if "window-function" in pattern_types:
        pattern += 10
    if "jsonb-operations" in pattern_types:
        pattern += 10
    if "lateral-join" in pattern_types:
        pattern += 10
    if "upsert-conflict" in pattern_types:
        pattern += 5

    # PG advanced features increase complexity fit
    pg_feature_count = sum(
        1
        for pt in pattern_types
        if pt
        in {
            "cte-recursive",
            "window-function",
            "jsonb-operations",
            "array-operations",
            "lateral-join",
        }
    )
    if pg_feature_count >= 2:
        complexity += 15
    elif pg_feature_count >= 1:
        complexity += 10

    return ScoreBreakdown(
        pattern_match_score=max(0, min(pattern, 100)),
        complexity_score=max(0, min(complexity, 100)),
        performance_score=max(0, min(performance, 100)),
        cost_score=max(0, min(cost, 100)),
    )


def analyze_aurora_pg_patterns(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
) -> list[TableRecommendation]:
    """Produce per-table recommendations for Aurora PostgreSQL.

    Uses catalog-driven scoring with PG-specific adjustments.
    """
    profiles = build_table_profiles(collector_output, workload_analysis)

    # Combined pattern lookup (common + PG)
    all_pattern_by_id = {**COMMON_PATTERN_BY_ID, **PG_PATTERN_BY_ID}

    # per-table pattern/anti-pattern lists
    table_patterns_map: dict[str, list[Pattern]] = {}
    for p in workload_analysis.patterns_detected:
        for tid in p.table_ids or []:
            table_patterns_map.setdefault(tid, []).append(p)

    table_anti_patterns_map: dict[str, list[AntiPattern]] = {}
    for ap in workload_analysis.anti_patterns_detected or []:
        for tid in ap.table_ids or []:
            table_anti_patterns_map.setdefault(tid, []).append(ap)

    recommendations: list[TableRecommendation] = []

    for table_id, profile in profiles.items():
        tp = table_patterns_map.get(table_id, [])
        tap = table_anti_patterns_map.get(table_id, [])

        # Catalog-driven pattern_match override
        catalog_score = compute_catalog_pattern_score(tp, collector_output, all_pattern_by_id)
        base_scores = compute_base_scores(profile)

        # Determine pattern_match_score:
        # 1. If catalog patterns matched → use catalog score
        # 2. If table has queries but no catalog patterns → apply baseline floor
        #    (table is a valid relational workload, just nothing exceptional)
        # 3. No queries at all → use base_scores (table has no workload evidence)
        if catalog_score > 0:
            pm_score = max(0, min(catalog_score, 100))
        elif profile.total_query_count > 0:
            fk_inbound = _count_inbound_fks(table_id, collector_output)
            join_q_count = sum(1 for p in tp if p.pattern_type in {"complex-join", "simple-join"})
            multi_table_w_count = sum(1 for p in tp if p.pattern_type == "transactional-write")
            relational_baseline = compute_relational_need_score(
                fk_outbound_count=profile.foreign_key_count,
                fk_inbound_count=fk_inbound,
                join_query_count=join_q_count,
                multi_table_write_count=multi_table_w_count,
                total_query_count=profile.total_query_count,
            )
            pm_score = max(relational_baseline, base_scores.pattern_match_score)
        else:
            pm_score = base_scores.pattern_match_score

        scores = ScoreBreakdown(
            pattern_match_score=pm_score,
            complexity_score=base_scores.complexity_score,
            performance_score=base_scores.performance_score,
            cost_score=base_scores.cost_score,
        )

        # Apply PG-specific adjustments
        scores = _apply_aurora_pg_adjustments(scores, profile)

        confidence = compute_confidence(scores, weights=AURORA_PG_SCORE_WEIGHTS)

        recommendations.append(
            TableRecommendation(
                table_id=table_id,
                confidence_score=confidence,
                rationale=build_rationale(profile, tp, "Aurora PostgreSQL"),
                score_breakdown=scores,
                supporting_patterns=[p.pattern_id for p in tp] if tp else None,
                concerns=build_concerns(profile, tap) or None,
                migration_complexity=assess_migration_complexity(profile, tap),
            )
        )

    return recommendations


# ==========================================================================
# Decision trace
# ==========================================================================


def build_aurora_pg_decision_trace(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
    table_recommendations: list[TableRecommendation],
) -> dict:
    """Build Aurora PG decision trace artifact for specialist calibration."""
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []

    # Combined pattern lookup
    all_pattern_by_id = {**COMMON_PATTERN_BY_ID, **PG_PATTERN_BY_ID}

    # Build per-query match lookup
    query_pattern_map: dict[str, list[str]] = {}
    query_anti_pattern_map: dict[str, list[str]] = {}
    for p in workload_analysis.patterns_detected:
        for qid in p.query_ids or []:
            query_pattern_map.setdefault(qid, []).append(p.pattern_id)
    for ap in workload_analysis.anti_patterns_detected or []:
        for qid in ap.query_ids or []:
            query_anti_pattern_map.setdefault(qid, []).append(ap.anti_pattern_id)

    matched_qids = set(query_pattern_map.keys()) | set(query_anti_pattern_map.keys())

    # Per-query matches
    query_matches = []
    for q in queries:
        qid = q.get("query_id", "")
        query_matches.append(
            {
                "query_id": qid,
                "query_text_preview": (q.get("query_text") or "")[:120],
                "matched_patterns": query_pattern_map.get(qid, []),
                "matched_anti_patterns": query_anti_pattern_map.get(qid, []),
                "pg_features": detect_pg_specific_features(q.get("query_text") or ""),
            }
        )

    # Per-pattern summaries
    query_cps: dict[str, float] = {
        q.get("query_id", ""): float(q.get("calls_per_second") or 0) for q in queries
    }
    pattern_summaries = []
    for p in workload_analysis.patterns_detected:
        catalog = all_pattern_by_id.get(p.pattern_id)
        total_cps = sum(query_cps.get(qid, 0) for qid in (p.query_ids or []))
        pattern_summaries.append(
            {
                "pattern_id": p.pattern_id,
                "pattern_type": p.pattern_type,
                "catalog_base_score": catalog.base_score if catalog else None,
                "queries_matched_count": len(p.query_ids or []),
                "tables_involved": p.table_ids or [],
                "total_calls_per_second": round(total_cps, 3),
            }
        )

    # Per-table recommendation derivations
    derivations = []
    for rec in table_recommendations:
        derivations.append(
            {
                "table_id": rec.table_id,
                "score_breakdown": {
                    "pattern_match": rec.score_breakdown.pattern_match_score,
                    "complexity": rec.score_breakdown.complexity_score,
                    "performance": rec.score_breakdown.performance_score,
                    "cost": rec.score_breakdown.cost_score,
                },
                "weights_used": AURORA_PG_SCORE_WEIGHTS,
                "weighted_confidence": rec.confidence_score,
                "migration_complexity": rec.migration_complexity,
            }
        )

    return {
        "trace_version": "1.0",
        "agent": "aurora-pg-analysis-agent",
        "summary": {
            "queries_analyzed": len(queries),
            "queries_matched": len(matched_qids),
            "queries_unmatched": len(queries) - len(matched_qids),
            "patterns_detected": len(workload_analysis.patterns_detected),
            "anti_patterns_detected": len(workload_analysis.anti_patterns_detected or []),
        },
        "query_matches": query_matches,
        "pattern_summaries": pattern_summaries,
        "recommendation_derivations": derivations,
        "llm_advisor": {
            "status": "skipped",
            "duration_seconds": 0.0,
            "attempts": 0,
        },
    }
