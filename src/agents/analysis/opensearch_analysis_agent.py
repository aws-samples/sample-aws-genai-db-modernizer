"""
OpenSearch Analysis Agent

Analyzes database workloads to identify OpenSearch migration patterns including:
- Search workloads (full-text, wildcard, regex, fuzzy)
- Time-series / log analytics (high-ingest, timestamp range, time aggregation)

Returns a 3-tuple (AnalysisOutputContract, decision_trace, "") matching the
DynamoDB handler pattern. Third element is empty string — no Mermaid diagram
for OpenSearch (indexes are independent; no ER diagram needed).
"""

from datetime import datetime

from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import AgentMetadata, AnalysisOutputContract, TargetDatabase
from src.tools.analysis.opensearch_analysis_tools import (
    analyze_opensearch_patterns,
    analyze_opensearch_use_cases,
    build_opensearch_decision_trace,
    estimate_opensearch_costs,
)


def analyze_for_opensearch(
    analysis_input: AnalysisInput,
) -> tuple[AnalysisOutputContract, dict, str]:
    """Main entry point for OpenSearch analysis agent.

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, "").
        Third element is empty string — no Mermaid ER diagram for OpenSearch.
        The handler writes the contract and decision trace to S3 as separate
        artifacts, and skips the empty diagram.

    Handles partial collector output gracefully — zero queries produces
    NOT_SUITABLE recommendations for all tables, zero tables produces
    empty results.
    """
    start_time = datetime.now()
    collector_output = analysis_input.collector_output

    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    if not tables:
        print("WARNING: No tables in collector output — producing empty recommendations")

    # === 1. Pattern detection ===
    workload_analysis = analyze_opensearch_use_cases(collector_output)

    # === 2. Scoring + classification ===
    table_recommendations, classifications, weights_used = analyze_opensearch_patterns(
        collector_output, workload_analysis
    )

    # === 3. Cost estimation ===
    cost_estimate = estimate_opensearch_costs(
        collector_output,
        analysis_input.target_region,
        analysis_input.analysis_options,
    )

    duration = (datetime.now() - start_time).total_seconds()

    # === 4. Output contract v2.1 ===
    output = AnalysisOutputContract(
        contract_version="2.1",
        agent_metadata=AgentMetadata(
            agent_name="opensearch-analysis-agent",
            agent_version="1.0.0",
            target_database=TargetDatabase.OPENSEARCH,
            analysis_timestamp=datetime.now(),
            analysis_duration_seconds=duration,
        ),
        table_recommendations=table_recommendations,
        workload_analysis=workload_analysis,
        cost_estimate=cost_estimate,
        load_test_results=None,
        # OpenSearch indexes are independent — no table aggregation needed.
        # Downstream consumers (synthesis, schema design) guard with `or []`.
        aggregate_recommendations=None,
    )

    # === 5. Decision trace ===
    decision_trace = build_opensearch_decision_trace(
        collector_output,
        workload_analysis,
        table_recommendations,
        classifications,
        weights_used,
    )

    # Third element is empty string — no Mermaid diagram for OpenSearch
    return output, decision_trace, ""
