#!/usr/bin/env python3
"""Run schema design for a specific engine with configurable LLM mode.

Usage:
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --llm-mode external
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --finalize
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --llm-mode bedrock
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ALL_ENGINES = {"dynamodb", "documentdb", "opensearch", "elasticache"}

# Maps engine -> skill prompt path (relative to repo root)
_SKILL_PROMPTS = {
    "dynamodb": "src/skills/dynamodb-data-modeling.md",
    "documentdb": "src/skills/documentdb-data-modeling.md",
    "opensearch": "src/skills/opensearch-index-modeling.md",
    "elasticache": "src/skills/elasticache-data-modeling.md",
}


def _output(data: dict) -> None:
    """Print JSON to stdout (the only output the caller parses)."""
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> None:
    _output({"status": "error", "message": message})
    sys.exit(code)


def _get_output_schema(engine: str) -> dict:  # type: ignore[type-arg]
    """Generate JSON schema from the engine's Pydantic output contract."""
    from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
    from src.contracts.opensearch_model_output import OpenSearchModelOutputContract

    _ENGINE_CONTRACTS: dict[str, type] = {
        "dynamodb": DynamoDBModelOutputContract,
        "documentdb": DocumentDBModelOutputContract,
        "opensearch": OpenSearchModelOutputContract,
        "elasticache": ElastiCacheModelOutputContract,
    }

    contract_cls = _ENGINE_CONTRACTS[engine]
    return dict(contract_cls.model_json_schema())  # type: ignore[attr-defined]


def run_external(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Prepare LLM input payload and write it; print awaiting_llm status."""
    from src.agents.schema_design.handler import prepare_schema_design_input

    llm_request = prepare_schema_design_input(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    # Inject the output schema so the LLM knows exactly what to produce
    llm_request["output_schema"] = _get_output_schema(engine)

    llm_request_path = f"{db}/{job_id}/llm_requests/schema_design_{engine}.json"
    store.write_json(llm_request_path, llm_request)

    _output(
        {
            "status": "awaiting_llm",
            "llm_request": llm_request_path,
            "skill_prompt": _SKILL_PROMPTS.get(engine, f"src/skills/{engine}-data-modeling.md"),
        }
    )


def run_bedrock(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Run the full Strands/Bedrock schema design flow."""
    from src.agents.schema_design.handler import run_schema_design_auto

    run_schema_design_auto(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    _output({"status": "complete"})


def run_finalize(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Finalize schema design after external LLM has provided a response."""
    from src.agents.schema_design.handler import finalize_schema_design

    result = finalize_schema_design(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    _output(result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run schema design for a specific engine with configurable LLM mode."
    )
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument(
        "--engine",
        required=True,
        choices=sorted(_ALL_ENGINES),
        help="Target engine to design schema for",
    )
    parser.add_argument(
        "--llm-mode",
        default="external",
        choices=["bedrock", "external"],
        help="LLM execution mode (default: external)",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Finalize schema design by validating and storing external LLM response",
    )
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=1,
        help="Assignment version to use (default: 1)",
    )
    parser.add_argument(
        "--artifact-root",
        default="./artifacts",
        help="Root directory for local artifacts (default: .artifacts)",
    )
    args = parser.parse_args()

    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)

    if args.finalize:
        run_finalize(store, args.job_id, args.db, args.engine, args.assignment_version)
    elif args.llm_mode == "external":
        run_external(store, args.job_id, args.db, args.engine, args.assignment_version)
    else:
        run_bedrock(store, args.job_id, args.db, args.engine, args.assignment_version)


if __name__ == "__main__":
    main()
