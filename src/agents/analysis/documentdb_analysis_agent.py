"""
DocumentDB Analysis Agent

Analyzes database workloads to identify DocumentDB migration patterns including:
- Content management (polymorphic documents)
- Product catalogs (variable attributes)
- Nested documents (parent-child embedding)
- Aggregation pipelines (GROUP BY → $group)
- Extended references (denormalized lookup fields)
- Write-time aggregation (pre-computed via CDC)
"""

import time
from dataclasses import asdict
from datetime import datetime

from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import (
    AgentMetadata,
    AggregateRecommendation,
    AnalysisOutputContract,
    TargetDatabase,
)
from src.tools.analysis.documentdb_analysis_tools import (
    LlmDocumentDBAdvisor,
    _fallback_embedding_strategy,
    analyze_documentdb_patterns,
    analyze_documentdb_use_cases,
    build_decision_trace,
    detect_embedding_candidates,
    detect_polymorphic_tables,
    estimate_documentdb_costs,
    generate_mermaid_er_diagram,
)
from src.tools.analysis.dynamodb_analysis_tools import detect_relationships
from src.tools.analysis.scoring import identify_aggregates


def analyze_for_documentdb_deterministic(
    analysis_input: AnalysisInput,
) -> tuple[AnalysisOutputContract, dict, str]:
    """Run all deterministic logic without calling LlmDocumentDBAdvisor.

    Performs all detection and scoring steps (use_cases, patterns, costs,
    embedding_candidates, polymorphic_tables, relationships, aggregates, mermaid)
    and builds the full output contract and decision trace, using
    _fallback_embedding_strategy for each embedding candidate.

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, mermaid_diagram_str).
    """
    start_time = datetime.now()
    collector_output = analysis_input.collector_output

    tables = collector_output.get("database_schema", {}).get("tables", [])
    if not tables:
        print("WARNING: No tables in collector output — producing empty recommendations")

    # === Deterministic detection ===
    workload_analysis = analyze_documentdb_use_cases(collector_output)
    table_recommendations = analyze_documentdb_patterns(collector_output, workload_analysis)
    cost_estimate = estimate_documentdb_costs(
        collector_output, analysis_input.target_region, analysis_input.analysis_options
    )

    # === Structural detection ===
    relationships = detect_relationships(collector_output)
    embedding_candidates = detect_embedding_candidates(collector_output)
    polymorphic_tables = detect_polymorphic_tables(collector_output)
    aggregates = identify_aggregates(collector_output, workload_analysis)

    # Fallback denormalization strategies (no LLM)
    denorm_strategies = [_fallback_embedding_strategy(c) for c in embedding_candidates]

    duration = (datetime.now() - start_time).total_seconds()

    output = AnalysisOutputContract(
        contract_version="2.1",
        agent_metadata=AgentMetadata(
            agent_name="documentdb-analysis-agent",
            agent_version="1.0.0",
            target_database=TargetDatabase.DOCUMENTDB,
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
        embedding_candidates=embedding_candidates,
        polymorphic_tables=polymorphic_tables,
        denorm_strategies=denorm_strategies,
        llm_status="skipped",
        llm_duration=0.0,
        llm_attempts=0,
    )

    mermaid_diagram = generate_mermaid_er_diagram(collector_output, embedding_candidates)
    decision_trace["relationships"] = relationships

    return output, decision_trace, mermaid_diagram


def prepare_documentdb_llm_input(
    deterministic_result: AnalysisOutputContract,
    analysis_input: AnalysisInput,
) -> dict:
    """Format the LLM request payload from deterministic results and raw input.

    Returns a dict with keys:
        - deterministic_results:  patterns and anti-patterns from workload analysis
        - schema:                 database_schema from the collector output
        - queries:                query_patterns list from the collector output
        - embedding_candidates:   serialized list of EmbeddingCandidate dicts
    """
    collector_output = analysis_input.collector_output
    workload = deterministic_result.workload_analysis

    deterministic_results = {
        "patterns": [p.model_dump() for p in workload.patterns_detected],
        "anti_patterns": [ap.model_dump() for ap in (workload.anti_patterns_detected or [])],
    }

    embedding_candidates = detect_embedding_candidates(collector_output)

    return {
        "deterministic_results": deterministic_results,
        "schema": collector_output.get("database_schema", {}),
        "queries": collector_output.get("queries", {}).get("query_patterns", []),
        "embedding_candidates": [asdict(c) for c in embedding_candidates],
    }


def apply_documentdb_llm_output(
    deterministic_result: AnalysisOutputContract,
    llm_output,
    trace: dict,
) -> tuple[AnalysisOutputContract, dict]:
    """Merge LLM denormalization_strategies into the decision trace.

    If llm_output is None (LLM skipped or failed), returns the contract and trace
    unchanged.

    Args:
        deterministic_result: The contract produced by analyze_for_documentdb_deterministic.
        llm_output:           An LlmDocumentDBOutput instance, or None.
        trace:                The decision trace dict to update.

    Returns:
        Tuple of (AnalysisOutputContract, updated_trace_dict).
    """
    if llm_output is None:
        return deterministic_result, trace

    denorm_strategies = [s.model_dump() for s in llm_output.denormalization_strategies]
    updated_trace = {**trace, "denormalization_strategies": denorm_strategies}
    return deterministic_result, updated_trace


def analyze_for_documentdb(
    analysis_input: AnalysisInput,
    llm_mode: str = "bedrock",
) -> tuple[AnalysisOutputContract, dict, str]:
    """Main entry point for DocumentDB analysis agent.

    Args:
        analysis_input: Input contract with collector output and analysis options.
        llm_mode:       Controls LLM behavior.
                        - "bedrock"  (default): existing behavior — calls LlmDocumentDBAdvisor
                                                when ENABLE_LLM_ADVISOR=true and candidates exist.
                        - "none":               deterministic only, no LLM.
                        - "external":           deterministic only; marks output as
                                                awaiting external LLM enrichment.

    Returns:
        Tuple of (AnalysisOutputContract, decision_trace_dict, mermaid_diagram_str).
    """
    if llm_mode == "none":
        return analyze_for_documentdb_deterministic(analysis_input)

    if llm_mode == "external":
        contract, trace, mermaid = analyze_for_documentdb_deterministic(analysis_input)
        trace["llm_advisor"]["status"] = "awaiting_external"
        return contract, trace, mermaid

    # === "bedrock" mode: delegate to deterministic, then call LlmDocumentDBAdvisor ===
    contract, decision_trace, mermaid_diagram = analyze_for_documentdb_deterministic(analysis_input)

    collector_output = analysis_input.collector_output
    embedding_candidates = detect_embedding_candidates(collector_output)

    # === LLM Advisor (embedding vs referencing trade-offs) ===
    advisor = LlmDocumentDBAdvisor()
    llm_output = None
    llm_status = "skipped"
    llm_duration = 0.0
    llm_attempts = 0

    if advisor.enabled and embedding_candidates:
        llm_start = time.time()
        llm_output = advisor.advise(
            deterministic_results={
                "patterns": [p.model_dump() for p in contract.workload_analysis.patterns_detected],
                "anti_patterns": [
                    ap.model_dump()
                    for ap in (contract.workload_analysis.anti_patterns_detected or [])
                ],
            },
            schema=collector_output.get("database_schema", {}),
            queries=collector_output.get("queries", {}).get("query_patterns", []),
            embedding_candidates=embedding_candidates,
        )
        llm_duration = time.time() - llm_start
        llm_attempts = advisor.attempts_made
        llm_status = "success" if llm_output else f"failed_after_{llm_attempts}_attempts"

    contract, decision_trace = apply_documentdb_llm_output(contract, llm_output, decision_trace)

    # Update trace with LLM metadata
    decision_trace["llm_advisor"]["status"] = llm_status
    decision_trace["llm_advisor"]["duration_seconds"] = llm_duration
    decision_trace["llm_advisor"]["attempts"] = llm_attempts

    return contract, decision_trace, mermaid_diagram
