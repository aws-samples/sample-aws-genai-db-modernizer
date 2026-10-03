#!/usr/bin/env python3
"""Run schema design for a specific engine with configurable LLM mode.

Usage:
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --llm-mode external
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine documentdb --finalize
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --llm-mode bedrock
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --split
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --merge
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb \
        --check-costs artifacts/<name>/<id>/schema-dynamodb/v<N>/schema_draft_group_<G>.json

DynamoDB always designs split -> per-group drafts -> ``--merge``; ``--merge`` is
its final step. ``--finalize --engine dynamodb`` only reports whether the merged
output exists (it never reads an LLM response).

The assignment version defaults to the effective one (ADR-028): v2 when Reality
Check consolidated, else v1. Every status line reports it as
``assignment_version`` so callers build ``schema-<engine>/v<N>/`` paths from it.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ALL_ENGINES = {
    "dynamodb",
    "documentdb",
    "opensearch",
    "elasticache",
    "aurora_postgresql",
    "aurora_mysql",
}

# Maps engine -> skill prompt path (relative to repo root)
_SKILL_PROMPTS = {
    "dynamodb": "src/skills/dynamodb-data-modeling.md",
    "documentdb": "src/skills/documentdb-data-modeling.md",
    "opensearch": "src/skills/opensearch-index-modeling.md",
    "elasticache": "src/skills/elasticache-data-modeling.md",
    "aurora_postgresql": "src/skills/aurora_postgresql-data-modeling.md",
    "aurora_mysql": "src/skills/aurora_mysql-data-modeling.md",
}


def _output(data: dict) -> None:
    """Print JSON to stdout (the only output the caller parses)."""
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> NoReturn:
    _output({"status": "error", "message": message})
    sys.exit(code)


def _get_output_schema(engine: str) -> dict:  # type: ignore[type-arg]
    """Generate JSON schema from the engine's Pydantic output contract."""
    from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
    from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
    from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
    from src.contracts.opensearch_model_output import OpenSearchModelOutputContract

    _ENGINE_CONTRACTS: dict[str, type] = {
        "dynamodb": DynamoDBModelOutputContract,
        "documentdb": DocumentDBModelOutputContract,
        "opensearch": OpenSearchModelOutputContract,
        "elasticache": ElastiCacheModelOutputContract,
        "aurora_postgresql": AuroraPostgresqlModelOutputContract,
        "aurora_mysql": AuroraMySQLModelOutputContract,
    }

    contract_cls = _ENGINE_CONTRACTS[engine]
    return dict(contract_cls.model_json_schema())  # type: ignore[attr-defined]


def _resolve_version(store, job_id: str, db: str, requested: int | None) -> int:
    """Return the explicit ``--assignment-version`` or the effective one (ADR-028)."""
    if requested is not None:
        return requested
    from src.storage.assignment_versioning import resolve_downstream_assignment_version

    return resolve_downstream_assignment_version(store, db, job_id)


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

    if engine in ("aurora_postgresql", "aurora_mysql"):
        from src.contracts.analysis_output import AnalysisOutputContract
        from src.contracts.collector_output import CollectorOutputContract
        from src.contracts.schema_design_input import project_schema_design_input
        from src.tools.schema.aurora_common.draft_builder import build_mysql_draft, build_pg_draft

        _DRAFT_BUILDERS = {
            "aurora_postgresql": build_pg_draft,
            "aurora_mysql": build_mysql_draft,
        }

        collector = CollectorOutputContract.model_validate(llm_request["collector_output"])
        analysis = AnalysisOutputContract.model_validate(llm_request["analysis_output"])
        agent_collector, _, _ = project_schema_design_input(collector, analysis)
        build_draft = _DRAFT_BUILDERS[engine]
        draft, strategy = build_draft(
            agent_collector.tables, agent_collector.source_database_engine
        )
        llm_request["draft"] = draft
        llm_request["migration_strategy"] = strategy

    llm_request_path = f"{db}/{job_id}/llm_requests/schema_design_{engine}.json"
    store.write_json(llm_request_path, llm_request)

    _output(
        {
            "status": "awaiting_llm",
            "assignment_version": assignment_version,
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

    _output({"status": "complete", "assignment_version": assignment_version})


def run_split(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Split schema design input into per-group files (DynamoDB split/merge flow)."""
    from src.agents.schema_design.handler import run_schema_split

    run_schema_split(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    _output(
        {
            "status": "split",
            "assignment_version": assignment_version,
            "manifest": f"{db}/{job_id}/schema-{engine}/v{assignment_version}/groups_manifest.json",
        }
    )


def run_merge(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Merge per-group schema drafts into the final schema output."""
    from src.agents.schema_design.handler import run_schema_merge

    run_schema_merge(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    _output(
        {
            "status": "complete",
            "assignment_version": assignment_version,
            "output_path": (
                f"{db}/{job_id}/schema-{engine}/v{assignment_version}/schema_output.json"
            ),
        }
    )


_DYNAMODB_FINALIZE_MESSAGE = (
    "DynamoDB schema design finalizes with --merge; run --merge after the group drafts"
)


def run_finalize(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Finalize schema design after external LLM has provided a response.

    DynamoDB never writes ``llm_responses/schema_design_dynamodb.json``: it is
    designed split -> per-group drafts -> ``--merge``, and ``--merge`` writes the
    final output. So for DynamoDB this only reports whether that merged output
    exists for the effective version, instead of failing on the missing LLM
    response (issue #197).
    """
    if engine == "dynamodb":
        output_key = f"{db}/{job_id}/schema-{engine}/v{assignment_version}/schema_output.json"
        if not store.exists(output_key):
            _error(_DYNAMODB_FINALIZE_MESSAGE)
        _output(
            {
                "status": "complete",
                "assignment_version": assignment_version,
                "output_path": output_key,
            }
        )
        return

    from src.agents.schema_design.handler import finalize_schema_design

    result = finalize_schema_design(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    _output({**result, "assignment_version": assignment_version})


def _contained_draft_path(artifact_root: str, db: str, job_id: str, draft: str) -> Path | str:
    """Resolve ``draft`` and require it under ``{root}/{db}/{job}/schema-dynamodb/``.

    Returns the resolved path, or an error message. Symlinks are followed, so a
    link inside the job dir that points elsewhere is refused too.
    """
    for flag, name in (("--db", db), ("--job-id", job_id)):
        if name in ("", ".", "..") or Path(name).name != name:
            return f"{flag} {name!r} must be a single path component"
    schema_dir = (Path(artifact_root) / db / job_id / "schema-dynamodb").resolve()
    resolved = Path(draft).resolve()
    if resolved == schema_dir or not resolved.is_relative_to(schema_dir):
        return f"--check-costs {draft!r} must be a group draft under {schema_dir}"
    return resolved


def run_check_costs(artifact_root: str, job_id: str, db: str, engine: str, draft: str) -> None:
    """Run the DynamoDB hot-partition/capacity check on one group draft (issue #198).

    External-mode counterpart of the Bedrock agent's Strands tool
    ``compute_performances_and_costs``: both call the same function. Read-only:
    the draft is not modified; the caller records ``validation_passed`` /
    ``validation_failures`` from the printed result per the skill's rules.
    Exit 0 whenever the check ran (``passed`` says whether it passed); exit 1
    only when it could not run (wrong engine, path outside the job, unreadable
    draft).
    """
    if engine != "dynamodb":
        _error("--check-costs is only available for --engine dynamodb")
    path = _contained_draft_path(artifact_root, db, job_id, draft)
    if isinstance(path, str):
        _error(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _error(f"cannot read draft {draft!r}: {exc}")
    if not isinstance(payload, dict):
        _error(f"draft {draft!r} must be a JSON object")

    from src.tools.schema.dynamodb_cost_check import check_draft_costs

    _output({"status": "complete", "draft": draft, **check_draft_costs(payload)})


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
        "--split",
        action="store_true",
        help="Split schema design input into per-group files (DynamoDB)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge per-group schema drafts into the final schema output (DynamoDB)",
    )
    parser.add_argument(
        "--check-costs",
        metavar="DRAFT",
        default=None,
        help=(
            "Run the hot-partition/capacity check on a DynamoDB group draft under the "
            "job's schema-dynamodb/ dir and print the result (read-only)"
        ),
    )
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Assignment version to use (default: the effective version, v2 after consolidation)",
    )
    parser.add_argument(
        "--artifact-root",
        default="./artifacts",
        help="Root directory for local artifacts (default: ./artifacts)",
    )
    args = parser.parse_args()

    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        _error(violation)

    from src.storage.local_store import LocalArtifactStore

    if args.check_costs is not None:
        run_check_costs(args.artifact_root, args.job_id, args.db, args.engine, args.check_costs)
        return

    store = LocalArtifactStore(base_dir=args.artifact_root)
    version = _resolve_version(store, args.job_id, args.db, args.assignment_version)

    if args.finalize:
        run_finalize(store, args.job_id, args.db, args.engine, version)
    elif args.split:
        run_split(store, args.job_id, args.db, args.engine, version)
    elif args.merge:
        run_merge(store, args.job_id, args.db, args.engine, version)
    elif args.llm_mode == "external":
        run_external(store, args.job_id, args.db, args.engine, version)
    else:
        run_bedrock(store, args.job_id, args.db, args.engine, version)


if __name__ == "__main__":
    main()
