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

from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import AgentMetadata, AnalysisOutputContract, TargetDatabase
from src.contracts.decision_trace_base import TimestampColumnsTrace
from src.contracts.elasticache_decision_trace import ElastiCacheDecisionTrace
from src.tools.analysis.redis_analysis_tools import (
    analyze_caching_patterns,
    analyze_redis_use_cases,
    estimate_redis_costs,
)
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
        print("WARNING: No tables in collector output — producing empty recommendations")

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
    decision_trace = ElastiCacheDecisionTrace(
        patterns_detected=len(workload_analysis.patterns_detected),
        anti_patterns_detected=len(workload_analysis.anti_patterns_detected or []),
        tables_analyzed=len(tables),
        recommendations_generated=len(table_recommendations),
        # How the timestamp-column fact was read (#935): by type, or by name and why.
        timestamp_columns=TimestampColumnsTrace.model_validate(
            timestamp_column_trace(collector_output)
        ),
    )

    return output, decision_trace.model_dump(mode="json"), None
