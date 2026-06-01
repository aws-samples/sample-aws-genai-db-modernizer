"""Analysis agent handler — dispatches to target-specific analysis agents."""

import json
from datetime import UTC, datetime

from src.contracts.analysis_output import (
    AgentMetadata,
    AnalysisOutputContract,
    CostEstimate,
    TargetDatabase,
    WorkloadAnalysis,
)
from src.storage.artifact_store import ArtifactStore


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

    print(f"[analysis/{agent_type}] Starting analysis for {database_name}")

    # Read collector output
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    print(f"[analysis/{agent_type}] Loaded collector: {len(tables)} tables, {len(queries)} queries")

    # Dispatch to target-specific agent
    match agent_type:
        case "dynamodb":
            from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
            from src.contracts.analysis_input import AnalysisInput
            from src.contracts.analysis_input import TargetDatabase as InputTargetDatabase

            print(
                f"[analysis/{agent_type}] Running DynamoDB analysis (deterministic + LLM advisor)..."
            )
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=InputTargetDatabase(agent_type),
            )
            result, decision_trace, mermaid_diagram = analyze_for_dynamodb(
                analysis_input, llm_mode=llm_mode
            )
            output_json = result.model_dump_json(indent=2)

            # Log key results
            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            print(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                print(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% confidence")
            print(
                f"[analysis/{agent_type}] Cost estimate: ${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            # Write decision trace
            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            store.write_json(trace_key, decision_trace)
            print(f"[analysis/{agent_type}] Decision trace written")

            # Write Mermaid ER diagram as JSON wrapper
            mermaid_key = f"{database_name}/{job_id}/analysis-{agent_type}/er-diagram.json"
            store.write_json(mermaid_key, {"content": mermaid_diagram})
            print(f"[analysis/{agent_type}] ER diagram written")

        case "documentdb":
            from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
            from src.contracts.analysis_input import AnalysisInput
            from src.contracts.analysis_input import TargetDatabase as InputTargetDatabase

            print(
                f"[analysis/{agent_type}] Running DocumentDB analysis "
                "(deterministic + LLM advisor)..."
            )
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=InputTargetDatabase(agent_type),
            )
            result, decision_trace, mermaid_diagram = analyze_for_documentdb(
                analysis_input, llm_mode=llm_mode
            )
            output_json = result.model_dump_json(indent=2)

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            print(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                print(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% confidence")
            print(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            store.write_json(trace_key, decision_trace)
            print(f"[analysis/{agent_type}] Decision trace written")

            mermaid_key = f"{database_name}/{job_id}/analysis-{agent_type}/er-diagram.json"
            store.write_json(mermaid_key, {"content": mermaid_diagram})
            print(f"[analysis/{agent_type}] ER diagram written")

        case "opensearch":
            from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
            from src.contracts.analysis_input import AnalysisInput
            from src.contracts.analysis_input import TargetDatabase as InputTargetDB

            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=InputTargetDB(agent_type),
            )
            result, decision_trace, _ = analyze_for_opensearch(analysis_input)
            output_json = result.model_dump_json(indent=2)

            # Write decision trace as separate artifact
            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            store.write_json(trace_key, decision_trace)
            print(f"Decision trace written to {trace_key}")
            # No Mermaid ER diagram for OpenSearch — skipped intentionally

        case "elasticache":
            from src.agents.analysis.elasticache_analysis_agent import analyze_for_elasticache
            from src.contracts.analysis_input import AnalysisInput
            from src.contracts.analysis_input import TargetDatabase as InputTargetDB

            print(f"[analysis/{agent_type}] Running ElastiCache/Redis analysis...")
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=InputTargetDB(agent_type),
            )
            result, decision_trace, _ = analyze_for_elasticache(analysis_input)
            output_json = result.model_dump_json(indent=2)

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            print(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                print(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% confidence")
            print(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            store.write_json(trace_key, decision_trace)
            print(f"[analysis/{agent_type}] Decision trace written")

        case "aurora_postgresql":
            from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
            from src.contracts.analysis_input import AnalysisInput
            from src.contracts.analysis_input import TargetDatabase as InputTargetDB

            print(f"[analysis/{agent_type}] Running Aurora PostgreSQL analysis...")
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=InputTargetDB(agent_type),
            )
            result, decision_trace, _ = analyze_for_aurora_pg(analysis_input, llm_mode=llm_mode)
            output_json = result.model_dump_json(indent=2)

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            print(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                print(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% confidence")
            print(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            store.write_json(trace_key, decision_trace)
            print(f"[analysis/{agent_type}] Decision trace written")

        case "aurora_mysql":
            from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql
            from src.contracts.analysis_input import AnalysisInput
            from src.contracts.analysis_input import TargetDatabase as InputTargetDB

            print(f"[analysis/{agent_type}] Running Aurora MySQL analysis...")
            analysis_input = AnalysisInput(
                job_id=job_id,
                collector_output=collector_output,
                target_database=InputTargetDB(agent_type),
            )
            result, decision_trace, _ = analyze_for_aurora_mysql(analysis_input, llm_mode=llm_mode)
            output_json = result.model_dump_json(indent=2)

            recs = result.table_recommendations
            patterns = result.workload_analysis.patterns_detected
            anti = result.workload_analysis.anti_patterns_detected or []
            print(
                f"[analysis/{agent_type}] Results: {len(recs)} table recommendations, "
                f"{len(patterns)} patterns, {len(anti)} anti-patterns"
            )
            for r in recs:
                print(f"[analysis/{agent_type}]   {r.table_id}: {r.confidence_score}% confidence")
            print(
                f"[analysis/{agent_type}] Cost estimate: "
                f"${result.cost_estimate.monthly_cost_usd:.2f}/mo"
            )

            trace_key = f"{database_name}/{job_id}/analysis-{agent_type}/decision-trace.json"
            store.write_json(trace_key, decision_trace)
            print(f"[analysis/{agent_type}] Decision trace written")

        case _:
            print(f"[analysis/{agent_type}] ⚠️ Agent not implemented — writing placeholder")
            try:
                target_db = TargetDatabase(agent_type)
            except ValueError:
                target_db = TargetDatabase.DYNAMODB
            result = AnalysisOutputContract(
                contract_version="2.1",
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
            output_json = result.model_dump_json(indent=2)

    key = f"{database_name}/{job_id}/analysis-{agent_type}/analysis.json"
    store.write_json(key, json.loads(output_json))
    elapsed = time.time() - start_time
    print(f"[analysis/{agent_type}] ✅ Complete in {elapsed:.1f}s — output written")
