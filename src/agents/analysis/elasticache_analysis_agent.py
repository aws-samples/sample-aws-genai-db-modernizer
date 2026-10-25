"""
ElastiCache (Redis) Analysis Agent

Analyzes database workloads to identify Redis/ElastiCache migration patterns including:
- Caching (high-frequency SELECT queries)
- Session store (user/token lookups)
- Leaderboard (ORDER BY + LIMIT patterns)
- Time-series aggregation
- Geospatial queries
"""

from datetime import datetime
from typing import NamedTuple

from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import (
    AgentMetadata,
    AnalysisOutputContract,
    TableRecommendation,
    TargetDatabase,
    WorkloadAnalysis,
)
from src.contracts.decision_trace_base import (
    DerivationScores,
    PatternSummary,
    QueryMatch,
    RecommendationDerivation,
    TimestampColumnsTrace,
    TraceSummary,
)
from src.contracts.elasticache_decision_trace import ElastiCacheDecisionTrace
from src.shared.progress import say
from src.tools.analysis.redis_analysis_tools import (
    analyze_caching_patterns,
    analyze_redis_use_cases,
    estimate_redis_costs,
)
from src.tools.analysis.scoring import DEFAULT_WEIGHTS
from src.tools.sql.query_facts import timestamp_column_trace


def analyze_for_elasticache(
    analysis_input: AnalysisInput,
) -> tuple[AnalysisOutputContract, dict, str | None]:
    """Main entry point for ElastiCache/Redis analysis agent.

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, mermaid_diagram_str | None).
    """
    start_time = datetime.now()
    collector_output = analysis_input.collector_output

    tables = collector_output.get("database_schema", {}).get("tables", [])
    if not tables:
        say("WARNING: No tables in collector output — producing empty recommendations")

    workload_analysis = analyze_redis_use_cases(collector_output)
    table_recommendations = analyze_caching_patterns(collector_output, workload_analysis)
    cost_estimate = estimate_redis_costs(
        collector_output, analysis_input.target_region, analysis_input.analysis_options
    )

    duration = (datetime.now() - start_time).total_seconds()

    output = AnalysisOutputContract(
        agent_metadata=AgentMetadata(
            agent_name="elasticache-analysis-agent",
            agent_version="1.0.0",
            target_database=TargetDatabase.ELASTICACHE,
            analysis_timestamp=datetime.now(),
            analysis_duration_seconds=duration,
        ),
        table_recommendations=table_recommendations,
        workload_analysis=workload_analysis,
        cost_estimate=cost_estimate,
    )

    # Built as its contract (#1191), and returned as the dict the one writer stores.
    shared = _shared_sections(collector_output, workload_analysis, table_recommendations)
    decision_trace = ElastiCacheDecisionTrace(
        patterns_detected=len(workload_analysis.patterns_detected),
        anti_patterns_detected=len(workload_analysis.anti_patterns_detected or []),
        tables_analyzed=len(tables),
        recommendations_generated=len(table_recommendations),
        # How the timestamp-column fact was read (#935): by type, or by name and why.
        timestamp_columns=TimestampColumnsTrace.model_validate(
            timestamp_column_trace(collector_output)
        ),
        trace_version=shared.trace_version,
        summary=shared.summary,
        query_matches=shared.query_matches,
        pattern_summaries=shared.pattern_summaries,
        recommendation_derivations=shared.recommendation_derivations,
    )

    return output, decision_trace.model_dump(mode="json"), None


class _SharedSections(NamedTuple):
    trace_version: str
    summary: TraceSummary
    query_matches: list[QueryMatch]
    pattern_summaries: list[PatternSummary]
    recommendation_derivations: list[RecommendationDerivation]


def _shared_sections(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
    table_recommendations: list[TableRecommendation],
) -> _SharedSections:
    """The sections every engine's trace shares (#1276), from what the agent already has.

    Nothing is re-derived: matches are the queries each detected pattern and
    anti-pattern names, derivations are the scores the recommendations carry.
    """
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    patterns = workload_analysis.patterns_detected
    anti_patterns = workload_analysis.anti_patterns_detected or []

    matched_patterns: dict[str, list[str]] = {}
    matched_anti_patterns: dict[str, list[str]] = {}
    for p in patterns:
        for qid in p.query_ids or []:
            matched_patterns.setdefault(qid, []).append(p.pattern_id)
    for ap in anti_patterns:
        for qid in ap.query_ids or []:
            matched_anti_patterns.setdefault(qid, []).append(ap.anti_pattern_id)
    matched = set(matched_patterns) | set(matched_anti_patterns)

    return _SharedSections(
        trace_version="1.0",
        summary=TraceSummary(
            queries_analyzed=len(queries),
            queries_matched=len(matched),
            queries_unmatched=len(queries) - len(matched),
            patterns_detected=len(patterns),
            anti_patterns_detected=len(anti_patterns),
        ),
        query_matches=[
            QueryMatch(
                query_id=q.get("query_id", ""),
                query_text_preview=(q.get("query_text") or "")[:120],
                matched_patterns=matched_patterns.get(q.get("query_id", ""), []),
                matched_anti_patterns=matched_anti_patterns.get(q.get("query_id", ""), []),
            )
            for q in queries
        ],
        # ElastiCache has no pattern catalog with base scores, so there is none to quote.
        pattern_summaries=[
            PatternSummary(
                pattern_id=p.pattern_id,
                pattern_type=p.pattern_type,
                catalog_base_score=None,
                queries_matched_count=len(p.query_ids or []),
                tables_involved=p.table_ids or [],
            )
            for p in patterns
        ],
        recommendation_derivations=[
            RecommendationDerivation(
                table_id=rec.table_id,
                segments_contributing=rec.supporting_patterns or [],
                score_breakdown=DerivationScores(
                    pattern_match=rec.score_breakdown.pattern_match_score,
                    complexity=rec.score_breakdown.complexity_score,
                    performance=rec.score_breakdown.performance_score,
                    cost=rec.score_breakdown.cost_score,
                ),
                weights_used=dict(DEFAULT_WEIGHTS),
                weighted_confidence=rec.confidence_score,
            )
            for rec in table_recommendations
        ],
    )
