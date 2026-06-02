#!/usr/bin/env python3
"""Run analysis for a specific engine with configurable LLM mode.

Usage:
    uv run python scripts/run_analysis.py --job-id <id> --db <name> --engine dynamodb
    uv run python scripts/run_analysis.py --job-id <id> --db <name> --engine dynamodb --llm-mode external
    uv run python scripts/run_analysis.py --job-id <id> --db <name> --engine dynamodb --finalize
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Engines that support LLM seam (external/finalize flow)
_LLM_ENGINES = {"dynamodb", "documentdb"}
# All supported engines
_ALL_ENGINES = {
    "dynamodb",
    "documentdb",
    "opensearch",
    "elasticache",
    "aurora_postgresql",
    "aurora_mysql",
}


def _output(data: dict) -> None:
    """Print JSON to stdout (the only output the caller parses)."""
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> None:
    _output({"status": "error", "message": message})
    sys.exit(code)


def _build_analysis_input(job_id: str, db: str, engine: str, collector_output: dict):
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    engine_to_target = {
        "dynamodb": TargetDatabase.dynamodb,
        "documentdb": TargetDatabase.documentdb,
        "opensearch": TargetDatabase.opensearch,
        "elasticache": TargetDatabase.elasticache,
        "aurora_postgresql": TargetDatabase.aurora_postgresql,
        "aurora_mysql": TargetDatabase.aurora_mysql,
    }

    return AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=engine_to_target[engine],
    )


def _serialize_results(contract, decision_trace: dict, mermaid: str | None) -> dict:
    """Serialize the 3-tuple returned by analysis agents into a JSON-safe dict.

    Used only for partial.json (intermediate state during external LLM flow).
    """
    return {
        "contract": json.loads(contract.model_dump_json()),
        "decision_trace": decision_trace,
        "mermaid_diagram": mermaid or "",
    }


def _write_analysis_artifacts(
    store, db: str, job_id: str, engine: str, contract, trace: dict, mermaid: str | None
) -> None:
    """Write analysis artifacts matching cloud format.

    Cloud writes:
      - analysis-{engine}/analysis.json — the contract (top-level, no wrapper)
      - analysis-{engine}/decision-trace.json — the decision trace
      - analysis-{engine}/er-diagram.mmd — the mermaid diagram (if present)
    """
    prefix = f"{db}/{job_id}/analysis-{engine}"
    store.write_json(f"{prefix}/analysis.json", json.loads(contract.model_dump_json()))
    store.write_json(f"{prefix}/decision-trace.json", trace)
    if mermaid:
        # Write mermaid as plain text via raw write
        from pathlib import Path

        mermaid_path = Path(store.base_dir) / prefix / "er-diagram.mmd"
        mermaid_path.parent.mkdir(parents=True, exist_ok=True)
        mermaid_path.write_text(mermaid)


def _get_analysis_output_schema(engine: str) -> dict:  # type: ignore[type-arg]
    """Generate JSON schema from the engine's LLM output contract."""
    from src.tools.analysis.documentdb_analysis_tools import LlmDocumentDBOutput
    from src.tools.analysis.dynamodb_analysis_tools import LlmAdvisorOutput

    _ENGINE_LLM_CONTRACTS: dict[str, type] = {
        "dynamodb": LlmAdvisorOutput,
        "documentdb": LlmDocumentDBOutput,
    }

    contract_cls = _ENGINE_LLM_CONTRACTS[engine]
    return dict(contract_cls.model_json_schema())  # type: ignore[attr-defined]


def run_external(store, job_id: str, db: str, engine: str) -> None:
    """Run deterministic pass and write partial results + LLM request."""
    collector_output = store.read_json(f"{db}/{job_id}/collector/output.json")
    analysis_input = _build_analysis_input(job_id, db, engine, collector_output)

    if engine == "dynamodb":
        from src.agents.analysis.dynamodb_analysis_agent import (
            analyze_for_dynamodb_deterministic,
            prepare_dynamodb_llm_input,
        )

        contract, trace, mermaid = analyze_for_dynamodb_deterministic(analysis_input)
        llm_request = prepare_dynamodb_llm_input(contract, analysis_input)

    elif engine == "documentdb":
        from src.agents.analysis.documentdb_analysis_agent import (
            analyze_for_documentdb_deterministic,
            prepare_documentdb_llm_input,
        )

        contract, trace, mermaid = analyze_for_documentdb_deterministic(analysis_input)
        llm_request = prepare_documentdb_llm_input(contract, analysis_input)

    else:
        _error(f"Engine '{engine}' does not support external LLM mode")

    # Inject the output schema so the LLM knows exactly what to produce
    llm_request["output_schema"] = _get_analysis_output_schema(engine)

    partial_path = f"{db}/{job_id}/analysis-{engine}/partial.json"
    llm_request_path = f"{db}/{job_id}/llm_requests/analysis_{engine}.json"

    store.write_json(partial_path, _serialize_results(contract, trace, mermaid))
    store.write_json(llm_request_path, llm_request)

    _output({"status": "awaiting_llm", "llm_request": llm_request_path})


def run_standard(store, job_id: str, db: str, engine: str, llm_mode: str) -> None:
    """Run full analysis (none or bedrock LLM mode) and write final results."""
    collector_output = store.read_json(f"{db}/{job_id}/collector/output.json")
    analysis_input = _build_analysis_input(job_id, db, engine, collector_output)

    if engine == "dynamodb":
        from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb

        contract, trace, mermaid = analyze_for_dynamodb(analysis_input, llm_mode=llm_mode)

    elif engine == "documentdb":
        from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb

        contract, trace, mermaid = analyze_for_documentdb(analysis_input, llm_mode=llm_mode)

    elif engine == "opensearch":
        from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch

        contract, trace, mermaid = analyze_for_opensearch(analysis_input)

    elif engine == "elasticache":
        from src.agents.analysis.elasticache_analysis_agent import analyze_for_elasticache

        contract, trace, mermaid = analyze_for_elasticache(analysis_input)

    elif engine == "aurora_postgresql":
        from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg

        contract, trace, mermaid = analyze_for_aurora_pg(analysis_input, llm_mode=llm_mode)

    elif engine == "aurora_mysql":
        from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql

        contract, trace, mermaid = analyze_for_aurora_mysql(analysis_input, llm_mode=llm_mode)

    else:
        _error(f"Unsupported engine: {engine}")

    _write_analysis_artifacts(store, db, job_id, engine, contract, trace, mermaid)

    _output({"status": "complete"})


def run_finalize(store, job_id: str, db: str, engine: str) -> None:
    """Merge LLM response into partial results and write final analysis."""
    if engine not in _LLM_ENGINES:
        _error(f"--finalize is not supported for engine '{engine}'")

    partial_path = f"{db}/{job_id}/analysis-{engine}/partial.json"
    llm_response_path = f"{db}/{job_id}/llm_responses/analysis_{engine}.json"

    partial_data = store.read_json(partial_path)
    llm_response = store.read_json(llm_response_path)

    if engine == "dynamodb":
        from src.agents.analysis.dynamodb_analysis_agent import apply_dynamodb_llm_output
        from src.contracts.analysis_output import AnalysisOutputContract
        from src.tools.analysis.dynamodb_analysis_tools import LlmAdvisorOutput

        contract = AnalysisOutputContract.model_validate(partial_data["contract"])
        llm_ddb = LlmAdvisorOutput.model_validate(llm_response)
        updated_contract = apply_dynamodb_llm_output(contract, llm_ddb)
        trace = partial_data["decision_trace"]
        mermaid = partial_data.get("mermaid_diagram", "")

    elif engine == "documentdb":
        from src.agents.analysis.documentdb_analysis_agent import apply_documentdb_llm_output
        from src.contracts.analysis_output import AnalysisOutputContract
        from src.tools.analysis.documentdb_analysis_tools import LlmDocumentDBOutput

        contract = AnalysisOutputContract.model_validate(partial_data["contract"])
        trace = partial_data["decision_trace"]
        mermaid = partial_data.get("mermaid_diagram", "")
        llm_docdb = LlmDocumentDBOutput.model_validate(llm_response)
        updated_contract, trace = apply_documentdb_llm_output(contract, llm_docdb, trace)

    _write_analysis_artifacts(store, db, job_id, engine, updated_contract, trace, mermaid)

    _output({"status": "complete"})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run analysis for a specific engine with configurable LLM mode."
    )
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument(
        "--engine",
        required=True,
        choices=sorted(_ALL_ENGINES),
        help="Target engine to analyze for",
    )
    parser.add_argument(
        "--llm-mode",
        default="none",
        choices=["bedrock", "external", "none"],
        help="LLM execution mode (default: none)",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Finalize analysis by merging external LLM response (dynamodb/documentdb only)",
    )
    parser.add_argument(
        "--artifact-root",
        default="./artifacts",
        help="Root directory for local artifacts (default: ./artifacts)",
    )
    args = parser.parse_args()

    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)

    # Engines without LLM support always run in "none" mode
    if args.engine not in _LLM_ENGINES:
        if args.finalize:
            _error(f"--finalize is not supported for engine '{args.engine}'")
        run_standard(store, args.job_id, args.db, args.engine, llm_mode="none")
        return

    # LLM-capable engines (dynamodb, documentdb)
    if args.finalize:
        run_finalize(store, args.job_id, args.db, args.engine)
    elif args.llm_mode == "external":
        run_external(store, args.job_id, args.db, args.engine)
    else:
        run_standard(store, args.job_id, args.db, args.engine, llm_mode=args.llm_mode)


if __name__ == "__main__":
    main()
