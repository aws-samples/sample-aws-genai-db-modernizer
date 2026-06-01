"""
Redis Analysis Agent

Analyzes database workloads to identify Redis use cases including:
- Caching patterns (frequent SELECT queries)
- Session stores
- Leaderboards (ranking queries)
- Geospatial search
- Time series data
- JSON documents
- Real-time analytics (HyperLogLog, Bloom filters)
- Event sourcing (Redis Streams)
- Recommendation engines (Sets)
- Low-latency reference data lookups
"""

from datetime import datetime

from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import AgentMetadata, AnalysisOutputContract, TargetDatabase
from src.tools.analysis.redis_analysis_tools import (
    analyze_caching_patterns,
    analyze_redis_use_cases,
    estimate_redis_costs,
)


def analyze_for_redis(analysis_input: AnalysisInput) -> AnalysisOutputContract:
    """
    Main entry point for Redis analysis agent.

    Args:
        analysis_input: Input contract with collector output and analysis options

    Returns:
        AnalysisOutputContract with Redis recommendations
    """
    start_time = datetime.now()

    # Extract collector output
    collector_output = analysis_input.collector_output

    # Analyze workload patterns for Redis use cases
    workload_analysis = analyze_redis_use_cases(collector_output)

    # Generate table recommendations
    table_recommendations = analyze_caching_patterns(collector_output, workload_analysis)

    # Estimate costs
    cost_estimate = estimate_redis_costs(
        collector_output, analysis_input.target_region, analysis_input.analysis_options
    )

    # Build output contract
    duration = (datetime.now() - start_time).total_seconds()

    return AnalysisOutputContract(
        contract_version="2.1",
        agent_metadata=AgentMetadata(
            agent_name="redis-analysis-agent",
            agent_version="1.0.0",
            target_database=TargetDatabase.ELASTICACHE,
            analysis_timestamp=datetime.now(),
            analysis_duration_seconds=duration,
        ),
        table_recommendations=table_recommendations,
        workload_analysis=workload_analysis,
        cost_estimate=cost_estimate,
        load_test_results=None,
        aggregate_recommendations=None,
    )
