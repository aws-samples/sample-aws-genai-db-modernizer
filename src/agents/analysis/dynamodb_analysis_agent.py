"""
DynamoDB Analysis Agent

Analyzes database workloads to identify DynamoDB migration patterns including:
- Key-value lookups (single-item by PK)
- Range queries (partition key + sort key range)
- Write-heavy ingestion (high-throughput inserts/updates)
- Time-series / event log (timestamp-ordered data)
- Metadata / config store (small reference tables)
- Session store (user-scoped data with TTL)
- Denormalizable relationships (simple JOINs → single-table design)
"""

import time
from datetime import datetime

from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import (
    AgentMetadata,
    AggregateRecommendation,
    AnalysisOutputContract,
    TargetDatabase,
)
from src.tools.analysis.dynamodb_analysis_tools import (
    LlmAdvisor,
    analyze_dynamodb_patterns,
    analyze_dynamodb_use_cases,
    build_decision_trace,
    classify_primary_keys,
    detect_denormalization_subtypes,
    detect_gsi_candidates,
    detect_relationships,
    detect_secondary_index_dominance,
    estimate_dynamodb_costs,
    generate_mermaid_er_diagram,
)
from src.tools.analysis.scoring import identify_aggregates


def analyze_for_dynamodb_deterministic(
    analysis_input: AnalysisInput,
) -> tuple[AnalysisOutputContract, dict, str]:
    """Run all deterministic logic without calling LlmAdvisor.

    Performs all detection and scoring steps (use_cases, patterns, costs,
    primary_keys, gsi_candidates, denorm_subtypes, si_dominance, relationships,
    aggregates, mermaid) and builds the full output contract and decision trace,
    with llm_status set to "skipped".

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, mermaid_diagram_str).
    """
    start_time = datetime.now()
    collector_output = analysis_input.collector_output

    # Validate minimum viable input
    tables = collector_output.get("database_schema", {}).get("tables", [])
    if not tables:
        print("WARNING: No tables in collector output — producing empty recommendations")

    # === Deterministic flow ===
    workload_analysis = analyze_dynamodb_use_cases(collector_output)
    table_recommendations = analyze_dynamodb_patterns(collector_output, workload_analysis)
    cost_estimate = estimate_dynamodb_costs(
        collector_output, analysis_input.target_region, analysis_input.analysis_options
    )
    relationships = detect_relationships(collector_output)

    pk_classifications = classify_primary_keys(collector_output)
    aggregates = identify_aggregates(collector_output, workload_analysis)
    gsi_candidates = detect_gsi_candidates(collector_output, pk_classifications)
    denorm_opportunities = detect_denormalization_subtypes(collector_output, relationships)
    si_dominance = detect_secondary_index_dominance(collector_output, pk_classifications)

    duration = (datetime.now() - start_time).total_seconds()

    output = AnalysisOutputContract(
        contract_version="2.1",
        agent_metadata=AgentMetadata(
            agent_name="dynamodb-analysis-agent",
            agent_version="1.1.0",
            target_database=TargetDatabase.DYNAMODB,
            analysis_timestamp=datetime.now(),
            analysis_duration_seconds=duration,
        ),
        table_recommendations=table_recommendations,
        workload_analysis=workload_analysis,
        cost_estimate=cost_estimate,
        load_test_results=None,
        aggregate_recommendations=(
            [
                AggregateRecommendation(
                    aggregate_id=a.aggregate_id,
                    root_table=a.root_table,
                    member_tables=a.member_tables,
                    co_access_confidence=a.co_access_confidence,
                    combined_migration_complexity=a.combined_migration_complexity,
                )
                for a in aggregates
            ]
            if aggregates
            else None
        ),
    )

    decision_trace = build_decision_trace(
        collector_output,
        workload_analysis,
        table_recommendations,
        pk_classifications=pk_classifications,
        aggregates=aggregates,
        gsi_candidates=gsi_candidates,
        denorm_opportunities=denorm_opportunities,
        si_dominance=si_dominance,
        llm_status="skipped",
        llm_duration=0.0,
        llm_attempts=0,
        llm_output=None,
    )
    decision_trace["relationships"] = relationships

    mermaid_diagram = generate_mermaid_er_diagram(collector_output, relationships)

    return output, decision_trace, mermaid_diagram


def prepare_dynamodb_llm_input(
    deterministic_result: AnalysisOutputContract,
    analysis_input: AnalysisInput,
) -> dict:
    """Format the LLM request payload from deterministic results and raw input.

    Returns a dict with keys:
        - deterministic_results: patterns and anti-patterns from workload analysis
        - schema:               database_schema from the collector output
        - queries:              query_patterns list from the collector output
        - aggregates:           aggregate_recommendations from the contract (serialized)
        - denorm_opportunities: list of denorm opportunity dicts derived from the contract
    """
    collector_output = analysis_input.collector_output
    workload = deterministic_result.workload_analysis

    deterministic_results = {
        "patterns": [p.model_dump() for p in workload.patterns_detected],
        "anti_patterns": [ap.model_dump() for ap in (workload.anti_patterns_detected or [])],
    }

    aggregates = (
        [r.model_dump() for r in deterministic_result.aggregate_recommendations]
        if deterministic_result.aggregate_recommendations
        else []
    )

    return {
        "deterministic_results": deterministic_results,
        "schema": collector_output.get("database_schema", {}),
        "queries": collector_output.get("queries", {}).get("query_patterns", []),
        "aggregates": aggregates,
        "denorm_opportunities": [],  # populated from decision trace in full pipeline
    }


def apply_dynamodb_llm_output(
    deterministic_result: AnalysisOutputContract,
    llm_output,
) -> AnalysisOutputContract:
    """Merge LLM aggregate_recommendations into the deterministic contract.

    If llm_output is None (LLM skipped or failed), returns the contract unchanged.

    Args:
        deterministic_result: The contract produced by analyze_for_dynamodb_deterministic.
        llm_output:           An LlmAdvisorOutput instance, or None.

    Returns:
        Updated AnalysisOutputContract with aggregate_recommendations merged from
        the LLM output (each AggregateKeyDesign becomes an AggregateRecommendation).
    """
    if llm_output is None:
        return deterministic_result

    # Index LLM key designs by aggregate_id for O(1) lookup
    llm_by_id = {rec.aggregate_id: rec for rec in (llm_output.aggregate_recommendations or [])}

    # Enrich existing deterministic aggregates with LLM key design fields
    existing = deterministic_result.aggregate_recommendations or []
    enriched = [
        (
            agg.model_copy(
                update={
                    "partition_key": llm_by_id[agg.aggregate_id].partition_key,
                    "sort_key": llm_by_id[agg.aggregate_id].sort_key,
                    "key_design_rationale": llm_by_id[agg.aggregate_id].rationale,
                    "supporting_access_patterns": llm_by_id[
                        agg.aggregate_id
                    ].supporting_access_patterns,
                }
            )
            if agg.aggregate_id in llm_by_id
            else agg
        )
        for agg in existing
    ]

    return deterministic_result.model_copy(update={"aggregate_recommendations": enriched or None})


def analyze_for_dynamodb(
    analysis_input: AnalysisInput,
    llm_mode: str = "bedrock",
) -> tuple[AnalysisOutputContract, dict, str]:
    """Main entry point for DynamoDB analysis agent.

    Args:
        analysis_input: Input contract with collector output and analysis options.
        llm_mode:       Controls LLM behavior.
                        - "bedrock"  (default): existing behavior — calls LlmAdvisor
                                                when ENABLE_LLM_ADVISOR=true.
                        - "none":               deterministic only, no LLM.
                        - "external":           deterministic only; marks output as
                                                awaiting external LLM enrichment.

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, mermaid_diagram_str).
        The handler writes all three to S3 as separate artifacts.

    Handles partial collector output gracefully — zero queries produces
    structure-only recommendations, zero tables produces empty results.
    """
    if llm_mode == "none":
        return analyze_for_dynamodb_deterministic(analysis_input)

    if llm_mode == "external":
        contract, trace, mermaid = analyze_for_dynamodb_deterministic(analysis_input)
        trace["llm_advisor"]["status"] = "awaiting_external"
        return contract, trace, mermaid

    # === "bedrock" mode: delegate to deterministic, then call LlmAdvisor ===
    contract, decision_trace, mermaid_diagram = analyze_for_dynamodb_deterministic(analysis_input)

    llm_payload = prepare_dynamodb_llm_input(contract, analysis_input)

    # === LLM Advisor (optional, with retry + exponential backoff) ===
    advisor = LlmAdvisor()
    llm_output = None
    llm_status = "skipped"
    llm_duration = 0.0
    llm_attempts = 0

    if advisor.enabled:
        llm_start = time.time()
        llm_output = advisor.advise(**llm_payload)
        llm_duration = time.time() - llm_start
        llm_attempts = advisor.attempts_made
        llm_status = "success" if llm_output else f"failed_after_{llm_attempts}_attempts"

    contract = apply_dynamodb_llm_output(contract, llm_output)

    # Update trace with LLM results
    decision_trace["llm_advisor"]["status"] = llm_status
    decision_trace["llm_advisor"]["duration_seconds"] = llm_duration
    decision_trace["llm_advisor"]["attempts"] = llm_attempts

    return contract, decision_trace, mermaid_diagram
