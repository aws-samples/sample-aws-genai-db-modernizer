"""Analysis agent handler — dispatches to target-specific analysis agents."""

from datetime import UTC, datetime

from src.agents.analysis.decision_trace import write_decision_trace
from src.contracts.analysis_output import (
    AgentMetadata,
    AnalysisOutputContract,
    CostEstimate,
    TargetDatabase,
    WorkloadAnalysis,
)
from src.shared.progress import say
from src.storage.artifact_store import ArtifactStore
from src.storage.collector_for_models import read_collector_for_models
from src.tools.sql.query_facts import remember_collected_facts


def run_analysis(
    job_id: str,
    database_name: str,
    agent_type: str,
    store: ArtifactStore,
    llm_mode: str = "none",
) -> None:
    """Run an analysis agent. Reads collector output, writes analysis via ArtifactStore."""
    import time

    start_time = time.time()

    say(f"[analysis/{agent_type}] Starting analysis for {database_name}")

    # Read collector output
    collector_output = read_collector_for_models(store, database_name, job_id)
    remember_collected_facts(collector_output)
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    say(f"[analysis/{agent_type}] Loaded collector: {len(tables)} tables, {len(queries)} queries")

    # Dispatch to target-specific agent
    match agent_type:
        case "dynamodb":
            from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
            from src.contracts.analysis_input import AnalysisInput

            say(
                f"[analysis/{agent_type}] Running DynamoDB analysis (deterministic + LLM advisor)..."
            )
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=TargetDatabase(agent_type),
            )
            result, decision_trace, mermaid_diagram = analyze_for_dynamodb(
                analysis_input, llm_mode=llm_mode
            )

            # Log key results
            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            say(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                say(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% fit")
            say(
                f"[analysis/{agent_type}] Cost estimate: ${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            # Write decision trace (through its contract, #973)
            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            write_decision_trace(store, trace_key, decision_trace)
            say(f"[analysis/{agent_type}] Decision trace written")

            # Write Mermaid ER diagram as JSON wrapper
            mermaid_key = f"{database_name}/{job_id}/analysis-{agent_type}/er-diagram.json"
            store.write_json(mermaid_key, {"content": mermaid_diagram})
            say(f"[analysis/{agent_type}] ER diagram written")

        case "documentdb":
            from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
            from src.contracts.analysis_input import AnalysisInput

            say(
                f"[analysis/{agent_type}] Running DocumentDB analysis "
                "(deterministic + LLM advisor)..."
            )
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=TargetDatabase(agent_type),
            )
            result, decision_trace, mermaid_diagram = analyze_for_documentdb(
                analysis_input, llm_mode=llm_mode
            )

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            say(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                say(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% fit")
            say(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            write_decision_trace(store, trace_key, decision_trace)
            say(f"[analysis/{agent_type}] Decision trace written")

            mermaid_key = f"{database_name}/{job_id}/analysis-{agent_type}/er-diagram.json"
            store.write_json(mermaid_key, {"content": mermaid_diagram})
            say(f"[analysis/{agent_type}] ER diagram written")

        case "opensearch":
            from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
            from src.contracts.analysis_input import AnalysisInput

            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=TargetDatabase(agent_type),
            )
            result, decision_trace, _ = analyze_for_opensearch(analysis_input)

            # Write decision trace as separate artifact
            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            write_decision_trace(store, trace_key, decision_trace)
            say(f"Decision trace written to {trace_key}")
            # No Mermaid ER diagram for OpenSearch — skipped intentionally

        case "elasticache":
            from src.agents.analysis.elasticache_analysis_agent import analyze_for_elasticache
            from src.contracts.analysis_input import AnalysisInput

            say(f"[analysis/{agent_type}] Running ElastiCache/Redis analysis...")
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=TargetDatabase(agent_type),
            )
            result, decision_trace, _ = analyze_for_elasticache(analysis_input)

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            say(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                say(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% fit")
            say(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            write_decision_trace(store, trace_key, decision_trace)
            say(f"[analysis/{agent_type}] Decision trace written")

        case "aurora_postgresql":
            from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
            from src.contracts.analysis_input import AnalysisInput

            say(f"[analysis/{agent_type}] Running Aurora PostgreSQL analysis...")
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=TargetDatabase(agent_type),
            )
            result, decision_trace, _ = analyze_for_aurora_pg(analysis_input, llm_mode=llm_mode)

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            say(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                say(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% fit")
            say(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            write_decision_trace(store, trace_key, decision_trace)
            say(f"[analysis/{agent_type}] Decision trace written")

        case "aurora_mysql":
            from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql
            from src.contracts.analysis_input import AnalysisInput

            say(f"[analysis/{agent_type}] Running Aurora MySQL analysis...")
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=TargetDatabase(agent_type),
            )
            result, decision_trace, _ = analyze_for_aurora_mysql(analysis_input, llm_mode=llm_mode)

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            say(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                say(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% fit")
            say(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            write_decision_trace(store, trace_key, decision_trace)
            say(f"[analysis/{agent_type}] Decision trace written")

        case _:
            say(f"[analysis/{agent_type}] ⚠️ Agent not implemented — writing placeholder")
            try:
                target_db = TargetDatabase(agent_type)
            except ValueError:
                target_db = TargetDatabase.DYNAMODB
            result = AnalysisOutputContract(
                agent_metadata=AgentMetadata(
                    agent_name=f"{agent_type}-analysis-agent",
                    agent_version="0.0.1",
                    target_database=target_db,
                    analysis_timestamp=datetime.now(UTC),
                    analysis_duration_seconds=0,
                ),
                table_recommendations=[],
                workload_analysis=WorkloadAnalysis(patterns_detected=[]),
                cost_estimate=CostEstimate(
                    monthly_cost_usd=0,
                    cost_components={"note": "Agent not yet implemented"},
                    pricing_assumptions=["Placeholder — agent not yet implemented"],
                ),
            )

    key = f"{database_name}/{job_id}/analysis-{agent_type}/analysis.json"
    store.write_json(key, result.model_dump(mode="json"))
    elapsed = time.time() - start_time
    say(f"[analysis/{agent_type}] ✅ Complete in {elapsed:.1f}s — output written")
