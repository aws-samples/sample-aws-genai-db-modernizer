"""
Aurora PostgreSQL Analysis Agent

Analyzes database workloads to identify queries that should stay on (or migrate to)
Aurora PostgreSQL. Detects relational patterns where Aurora excels — complex joins,
aggregations, transactions, referential integrity, and PG-specific features
(CTEs, window functions, JSONB, arrays, LATERAL joins).

LLM Seam Pattern: exposes deterministic, prepare_llm_input, apply_llm_output,
and main entry point.
"""

from datetime import datetime

from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import AgentMetadata, AnalysisOutputContract, TargetDatabase
from src.tools.analysis.aurora_common_analysis_tools import estimate_aurora_costs
from src.tools.analysis.aurora_pg_analysis_tools import (
    analyze_aurora_pg_patterns,
    analyze_aurora_pg_use_cases,
    build_aurora_pg_decision_trace,
)


def analyze_for_aurora_pg_deterministic(
    analysis_input: AnalysisInput,
) -> tuple[AnalysisOutputContract, dict, str]:
    """Run all deterministic logic without calling LLM.

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, "").
        Third element is empty string — no Mermaid ER diagram for Aurora PG.
    """
    start_time = datetime.now()
    collector_output = analysis_input.collector_output

    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    if not tables:
        print("WARNING: No tables in collector output — producing empty recommendations")

    # === 1. Pattern detection ===
    workload_analysis = analyze_aurora_pg_use_cases(collector_output)

    # === 2. Scoring + recommendations ===
    table_recommendations = analyze_aurora_pg_patterns(collector_output, workload_analysis)

    # === 3. Cost estimation ===
    cost_estimate = estimate_aurora_costs(
        collector_output,
        analysis_input.target_region,
        analysis_input.analysis_options,
        engine="postgresql",
    )

    duration = (datetime.now() - start_time).total_seconds()

    # === 4. Output contract v2.1 ===
    output = AnalysisOutputContract(
        contract_version="2.1",
        agent_metadata=AgentMetadata(
            agent_name="aurora-pg-analysis-agent",
            agent_version="1.0.0",
            target_database=TargetDatabase.AURORA_POSTGRESQL,
            analysis_timestamp=datetime.now(),
            analysis_duration_seconds=duration,
        ),
        table_recommendations=table_recommendations,
        workload_analysis=workload_analysis,
        cost_estimate=cost_estimate,
        load_test_results=None,
        aggregate_recommendations=None,
    )

    # === 5. Decision trace ===
    decision_trace = build_aurora_pg_decision_trace(
        collector_output, workload_analysis, table_recommendations
    )

    return output, decision_trace, ""


def prepare_aurora_pg_llm_input(
    deterministic_result: AnalysisOutputContract,
    analysis_input: AnalysisInput,
) -> dict:
    """Format the LLM request payload from deterministic results.

    Stub — returns structured input for future LLM enrichment.
    """
    collector_output = analysis_input.collector_output
    workload = deterministic_result.workload_analysis

    return {
        "deterministic_results": {
            "patterns": [p.model_dump() for p in workload.patterns_detected],
            "anti_patterns": [ap.model_dump() for ap in (workload.anti_patterns_detected or [])],
        },
        "schema": collector_output.get("database_schema", {}),
        "queries": collector_output.get("queries", {}).get("query_patterns", []),
    }


def apply_aurora_pg_llm_output(
    deterministic_result: AnalysisOutputContract,
    llm_output,
) -> AnalysisOutputContract:
    """Merge LLM enrichment into the deterministic contract.

    Stub — returns contract unchanged (no LLM enrichment implemented yet).
    """
    if llm_output is None:
        return deterministic_result
    return deterministic_result


def analyze_for_aurora_pg(
    analysis_input: AnalysisInput,
    llm_mode: str = "none",
) -> tuple[AnalysisOutputContract, dict, str]:
    """Main entry point for Aurora PostgreSQL analysis agent.

    Args:
        analysis_input: Input contract with collector output and analysis options.
        llm_mode:       Controls LLM behavior.
                        - "none" (default): deterministic only, no LLM.
                        - "external":       deterministic only; marks output as
                                            awaiting external LLM enrichment.
                        - "bedrock":        deterministic + LLM (not yet implemented).

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, "").
        Third element is empty string — no Mermaid ER diagram for Aurora PG.
    """
    if llm_mode == "none":
        return analyze_for_aurora_pg_deterministic(analysis_input)

    if llm_mode == "external":
        contract, trace, mermaid = analyze_for_aurora_pg_deterministic(analysis_input)
        trace["llm_advisor"]["status"] = "awaiting_external"
        return contract, trace, mermaid

    # "bedrock" mode — for now, same as deterministic (LLM not implemented)
    return analyze_for_aurora_pg_deterministic(analysis_input)
