#!/usr/bin/env python3
"""Run schema design for a specific engine with configurable LLM mode.

Usage:
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --llm-mode external
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine documentdb --finalize
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --llm-mode bedrock
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --split
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --merge
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --status
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb \
        --check-costs artifacts/<name>/<id>/schema-dynamodb/v<N>/schema_draft_group_<G>.json
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb \
        --check-costs artifacts/<name>/<id>/schema-dynamodb/v<N>/schema_draft_group_0.json \
                       artifacts/<name>/<id>/schema-dynamodb/v<N>/schema_draft_group_1.json
    uv run python scripts/run_schema_design.py --job-id <id> --db <name> --engine dynamodb --check-costs-all

DynamoDB always designs split -> per-group drafts -> ``--merge``; ``--merge`` is
its final step. ``--finalize --engine dynamodb`` only reports whether the merged
output exists (it never reads an LLM response). ``--status`` (DynamoDB only,
read-only) lists the groups, which group drafts exist and whether ``--merge`` is
still pending, so an orchestrator can decide its next step without listing or
reading artifacts.

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


_AURORA_ENGINES = ("aurora_postgresql", "aurora_mysql")


def _aurora_delta_request(llm_request: dict, engine: str) -> dict:
    """Compact Aurora request: design view + delta schema (issue #273).

    ``--finalize`` rebuilds the same draft from the store and merges the
    model's delta into it, so neither the draft nor the full collector output
    has to reach the model.
    """
    from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
    from src.tools.schema.aurora_common.delta_merge import base_from_outputs
    from src.tools.schema.aurora_common.design_view import build_design_view

    collector_output = llm_request["collector_output"]
    base, agent_collector, agent_analysis = base_from_outputs(
        engine, collector_output, llm_request["analysis_output"]
    )
    return {
        "target_type": engine,
        "database_name": llm_request["database_name"],
        "job_id": llm_request["job_id"],
        "response_kind": "aurora_design_delta",
        "migration_strategy": base.migration_strategy,
        # --finalize refuses to merge into a draft other than this one.
        "draft_fingerprint": base.fingerprint(),
        "output_schema": AuroraDesignDeltaContract.model_json_schema(),
        "design_view": build_design_view(
            base, agent_collector, agent_analysis, raw_collector=collector_output
        ),
    }


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

    if engine in _AURORA_ENGINES:
        # Issue #273: the model writes only a delta against the deterministic
        # draft, so it gets a compact view, not the collector + full draft.
        llm_request = _aurora_delta_request(llm_request, engine)
    else:
        # Inject the output schema so the LLM knows exactly what to produce
        llm_request["output_schema"] = _get_output_schema(engine)

    llm_request_path = f"{db}/{job_id}/llm_requests/schema_design_{engine}.json"
    if engine in _AURORA_ENGINES:
        from src.tools.schema.aurora_common.design_view import render_request

        # One table per line, so the model can page it with Read offset/limit.
        store.write_bytes(llm_request_path, render_request(llm_request).encode("utf-8"))
    else:
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

    report = run_schema_design_auto(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    _output({**_scope_fields(report), "assignment_version": assignment_version})


def _scope_fields(report, output_path: str | None = None) -> dict:
    """Status fields for a scope report: ``validation_failed`` + ``errors`` on
    violations, ``complete`` otherwise; ``warnings`` whenever there are any."""
    fields: dict = {"status": "validation_failed" if report.violations else "complete"}
    if output_path is not None:
        fields["output_path"] = output_path
    if report.violations:
        fields["errors"] = report.violations
    if report.warnings:
        fields["warnings"] = report.warnings
    return fields


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

    base_key = f"{db}/{job_id}/schema-{engine}/v{assignment_version}"
    manifest = store.read_json(f"{base_key}/groups_manifest.json")
    excluded_queries = manifest.get("excluded_queries") or []
    _output(
        {
            "status": "split",
            "assignment_version": assignment_version,
            "manifest": _local_path(store, f"{base_key}/groups_manifest.json"),
            "groups": _group_entries(store, base_key, manifest),
            # Queries with no source table (#276/#369): left out of every
            # group, so their count would otherwise be invisible here.
            "excluded_count": len(excluded_queries),
        }
    )


_MERGE_RECORD = "merge_record.json"


def _local_path(store, key: str) -> str:
    """Filesystem path of an artifact key, under the artifact root.

    With the default ``./artifacts`` root this is cwd-relative
    (``artifacts/<db>/...``), the form subagents Read and Write (``Write(artifacts/**)``).
    """
    return str(Path(store.base_dir) / key)


def _draft_state(store, key: str) -> tuple[str, str | None]:
    """``("missing" | "invalid" | "ok", sha256 of the file or None)``.

    ``invalid``: the draft exists but is not a readable JSON object.
    merge_schema_groups skips drafts it cannot read, so an invalid draft would
    silently drop its group from the merge.
    """
    if not store.exists(key):
        return "missing", None
    import hashlib

    try:
        raw = store.read_bytes(key)
        if not isinstance(json.loads(raw), dict):
            return "invalid", None
    except (OSError, ValueError):
        return "invalid", None
    return "ok", hashlib.sha256(raw).hexdigest()


def _group_entries(store, base_key: str, manifest: dict) -> list[dict]:
    """One compact entry per group: what a group subagent's dispatch needs."""
    entries = []
    for group in manifest.get("groups", []):
        idx = group["group_index"]
        draft_key = f"{base_key}/schema_draft_group_{idx}.json"
        state, digest = _draft_state(store, draft_key)
        entries.append(
            {
                "group_index": idx,
                "primary_tables": group.get("primary_tables", []),
                "query_count": group.get("query_count"),
                "input_file": _local_path(store, f"{base_key}/input_group_{idx}.json"),
                "input_pages": group.get("input_pages") or [],
                "draft": _local_path(store, draft_key),
                "draft_exists": state != "missing",
                "draft_state": state,
                "_hash": digest,
            }
        )
    return entries


def _public(groups: list[dict]) -> list[dict]:
    return [{k: v for k, v in g.items() if not k.startswith("_")} for g in groups]


def _draft_problems(groups: list[dict]) -> tuple[list[int], list[int]]:
    missing = [g["group_index"] for g in groups if g["draft_state"] == "missing"]
    invalid = [g["group_index"] for g in groups if g["draft_state"] == "invalid"]
    return missing, invalid


def _hashes(groups: list[dict]) -> dict[str, str | None]:
    return {str(g["group_index"]): g["_hash"] for g in groups}


def run_status(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Report DynamoDB group-design progress as one JSON line (read-only, #246).

    ``not_split``: no manifest yet, run ``--split``. ``drafts_pending``: some
    group drafts are missing (``drafts_missing``) or not a readable JSON object
    (``drafts_invalid``). ``merge_pending``: every draft is readable and none of
    them was merged as it is now (no merge yet, or a draft changed since).
    ``merged``: the last ``--merge`` ran on exactly these drafts and passed.
    ``merge_failed``: it ran on these drafts and printed ``validation_failed``
    (``errors``). Both come from ``merge_record.json``, the verdict ``--merge``
    printed, not from the output's ``validation_passed``.
    """
    if engine != "dynamodb":
        _error("--status reports DynamoDB group drafts; other engines use --finalize")
    base_key = f"{db}/{job_id}/schema-{engine}/v{assignment_version}"
    output_key = f"{base_key}/schema_output.json"
    fields: dict = {
        "assignment_version": assignment_version,
        "output_path": _local_path(store, output_key),
    }
    if not store.exists(f"{base_key}/groups_manifest.json"):
        _output({"status": "not_split", **fields, "next": "run --split"})
        return

    groups = _group_entries(store, base_key, store.read_json(f"{base_key}/groups_manifest.json"))
    missing, invalid = _draft_problems(groups)
    extra: dict = {}
    record = _read_record(store, f"{base_key}/{_MERGE_RECORD}")
    if missing or invalid:
        status = "drafts_pending"
        next_step = f"redo the group drafts for groups {sorted(missing + invalid)}"
    elif (
        record is None
        or record.get("draft_hashes") != _hashes(groups)
        or not store.exists(output_key)
    ):
        status, next_step = "merge_pending", "every group draft is readable; run --merge"
    else:
        # The same verdict --merge printed. The output's validation_passed is not
        # used: a group draft with an unfixable cost check keeps it false while
        # the merge itself completes.
        if record.get("status") != "complete":
            status, next_step = (
                "merge_failed",
                "fix the group drafts for these errors, then --merge",
            )
            extra["errors"] = list(
                record.get("errors") or [f"--merge printed {record.get('status')}"]
            )
        else:
            status = "merged"
            next_step = "merged output is current; re-run --merge only after editing a draft"
        if record.get("warnings"):
            extra["warnings"] = record["warnings"]
    _output(
        {
            "status": status,
            **fields,
            "groups": _public(groups),
            "drafts_missing": missing,
            "drafts_invalid": invalid,
            **extra,
            "next": next_step,
        }
    )


def _read_record(store, key: str) -> dict | None:
    try:
        data = store.read_json(key)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_merge(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Merge per-group schema drafts into the final schema output.

    Refuses, printing ``drafts_pending`` with ``missing_groups`` and
    ``invalid_groups`` and writing nothing, while any group in the manifest has
    no draft or one that is not a readable JSON object (#246). Prints
    ``validation_failed`` with the ``errors`` when the merged design
    references tables or queries the assignment gives another engine (#203), or
    designs a source table in several tables without a trade-off saying why (#223).
    Records the outcome and the drafts' hashes in ``merge_record.json`` so
    ``--status`` can tell a current merge from a stale or failed one.
    """
    from src.agents.schema_design.handler import run_schema_merge

    base_key = f"{db}/{job_id}/schema-{engine}/v{assignment_version}"
    groups: list[dict] = []
    if store.exists(f"{base_key}/groups_manifest.json"):
        groups = _group_entries(
            store, base_key, store.read_json(f"{base_key}/groups_manifest.json")
        )
        missing, invalid = _draft_problems(groups)
        if missing or invalid:
            # Merging a partial set would drop those groups' queries (#246).
            # Not a validation failure: the caller re-dispatches those groups.
            _output(
                {
                    "status": "drafts_pending",
                    "missing_groups": missing,
                    "invalid_groups": invalid,
                    "assignment_version": assignment_version,
                }
            )
            return

    report = run_schema_merge(
        job_id=job_id,
        database_name=db,
        target_type=engine,
        store=store,
        assignment_version=assignment_version,
    )

    fields = _scope_fields(report, _local_path(store, f"{base_key}/schema_output.json"))
    store.write_json(
        f"{base_key}/{_MERGE_RECORD}",
        {
            "status": fields["status"],
            "errors": fields.get("errors", []),
            "warnings": fields.get("warnings", []),
            "draft_hashes": _hashes(groups),
        },
    )
    _output({**fields, "assignment_version": assignment_version})


_DYNAMODB_FINALIZE_MESSAGE = (
    "DynamoDB schema design finalizes with --merge; run --merge after the group drafts"
)


def run_finalize(store, job_id: str, db: str, engine: str, assignment_version: int) -> None:
    """Finalize schema design after external LLM has provided a response.

    DynamoDB never writes ``llm_responses/schema_design_dynamodb.json``: it is
    designed split -> per-group drafts -> ``--merge``, and ``--merge`` writes the
    final output. So for DynamoDB this only reports whether that merged output
    exists for the effective version, instead of failing on the missing LLM
    response (issue #197). The merged output is re-checked against the
    assignment's scope, so an out-of-scope design reports ``validation_failed``
    here too (issue #203), as does one with DynamoDB merge failures (#223).
    """
    if engine == "dynamodb":
        from src.agents.schema_design.group_merger import merge_failures, merge_warnings
        from src.agents.schema_design.handler import ScopeReport, apply_schema_scope

        output_key = f"{db}/{job_id}/schema-{engine}/v{assignment_version}/schema_output.json"
        if not store.exists(output_key):
            _error(_DYNAMODB_FINALIZE_MESSAGE)
        merged = store.read_json(output_key)
        checked, report = apply_schema_scope(store, db, job_id, engine, merged, assignment_version)
        if checked != merged:  # new violations, or stale ones cleared after a hand fix
            store.write_json(output_key, checked)
        # Merge failures (#223) recorded by --merge fail finalize too; they clear
        # only by fixing the group drafts and re-running --merge.
        report = ScopeReport(
            report.violations + merge_failures(checked),
            report.warnings + merge_warnings(checked),
        )
        _output({**_scope_fields(report, output_key), "assignment_version": assignment_version})
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


def _group_draft_paths(artifact_root: str, db: str, job_id: str, version: int) -> list[str]:
    """Every ``schema_draft_group_*.json`` for ``job_id``'s current assignment
    version, sorted by group index (issue #313).

    Used by ``--check-costs-all`` so re-checking every group draft after a
    merge-fix pass is one command, not a shell loop over ``--status``'s group
    count.
    """
    import re

    schema_dir = Path(artifact_root) / db / job_id / "schema-dynamodb" / f"v{version}"
    pattern = re.compile(r"schema_draft_group_(\d+)\.json$")
    found: list[tuple[int, Path]] = []
    if schema_dir.is_dir():
        for entry in schema_dir.iterdir():
            match = pattern.match(entry.name)
            if match:
                found.append((int(match.group(1)), entry))
    found.sort(key=lambda item: item[0])
    return [str(path) for _, path in found]


def run_check_costs(
    artifact_root: str, job_id: str, db: str, engine: str, drafts: list[str]
) -> None:
    """Run the DynamoDB hot-partition/capacity check on one or more group
    drafts (issues #198, #313).

    External-mode counterpart of the Bedrock agent's Strands tool
    ``compute_performances_and_costs``: both call the same function. Read-only:
    no draft is modified; the caller records ``validation_passed`` /
    ``validation_failures`` from the printed result per the skill's rules.
    Exit 0 whenever every check ran (``passed`` says whether each passed, and
    whether all of them did); exit 1 as soon as one draft could not be
    checked (wrong engine, path outside the job, unreadable draft) -- no
    partial result is printed.

    One draft keeps today's shape: ``{"status": "complete", "draft": ...,
    "passed": ..., "results": [...], ...}`` at the top level. More than one
    draft prints ``{"status": "complete", "groups": [<that same shape minus
    "results", ...], "passed": <all of them passed>}`` -- "results" (the
    full validated entry per access pattern) is dropped per group because it
    is the one field that scales with entry count on every group at once,
    and a headless session's stdout must stay compact (AGENTS.md): on a
    sample with several thousand access patterns across many groups, printing
    every group's full "results" made --check-costs-all's own output large
    enough that the harness persisted it to a file outside the repo, which no
    allowed tool in a headless session can then read back (#375's second
    e2e-llm run found exactly this). "per_table" and "hot_partition_findings"
    already summarise everything "results" would otherwise be needed for; a
    single draft's own --check-costs call still prints "results" in full,
    since one group's own entries are the shape its own merge-fix pass reads.
    """
    if engine != "dynamodb":
        _error("--check-costs is only available for --engine dynamodb")

    from src.tools.schema.dynamodb_cost_check import check_draft_costs

    group_results = []
    for draft in drafts:
        path = _contained_draft_path(artifact_root, db, job_id, draft)
        if isinstance(path, str):
            _error(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _error(f"cannot read draft {draft!r}: {exc}")
        if not isinstance(payload, dict):
            _error(f"draft {draft!r} must be a JSON object")
        group_results.append({"draft": draft, **check_draft_costs(payload)})

    if len(group_results) == 1:
        _output({"status": "complete", **group_results[0]})
        return

    compact_groups = [
        {k: v for k, v in result.items() if k != "results"} for result in group_results
    ]
    _output(
        {
            "status": "complete",
            "groups": compact_groups,
            "passed": all(result["passed"] for result in group_results),
        }
    )


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
        "--status",
        action="store_true",
        help="Report which DynamoDB group drafts exist and whether --merge is pending (read-only)",
    )
    parser.add_argument(
        "--check-costs",
        metavar="DRAFT",
        nargs="+",
        default=None,
        help=(
            "Run the hot-partition/capacity check on one or more DynamoDB group drafts "
            "under the job's schema-dynamodb/ dir and print the result (read-only). "
            "One draft prints today's single-result shape; more than one prints "
            '{"groups": [...], "passed": <all passed>}. Pass several paths rather than '
            "looping the command: a shell loop is not an allowed command under CI."
        ),
    )
    parser.add_argument(
        "--check-costs-all",
        action="store_true",
        help=(
            "Run --check-costs on every schema_draft_group_*.json of the job's current "
            "assignment version (read-only, DynamoDB only); the single command to use to "
            "re-check every group draft, e.g. after a merge-fix pass"
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
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    args = parser.parse_args()

    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        _error(violation)

    if args.check_costs is not None and args.check_costs_all:
        _error("--check-costs and --check-costs-all are mutually exclusive")

    from src.storage.local_store import LocalArtifactStore

    if args.check_costs_all:
        if args.engine != "dynamodb":
            _error("--check-costs-all is only available for --engine dynamodb")
        store = LocalArtifactStore(base_dir=args.artifact_root)
        version = _resolve_version(store, args.job_id, args.db, args.assignment_version)
        drafts = _group_draft_paths(args.artifact_root, args.db, args.job_id, version)
        if not drafts:
            _error(
                f"no schema_draft_group_*.json found under schema-dynamodb/v{version} for "
                f"{args.db}/{args.job_id}; run --split first"
            )
        run_check_costs(args.artifact_root, args.job_id, args.db, args.engine, drafts)
        return

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
    elif args.status:
        run_status(store, args.job_id, args.db, args.engine, version)
    elif args.llm_mode == "external":
        run_external(store, args.job_id, args.db, args.engine, version)
    else:
        run_bedrock(store, args.job_id, args.db, args.engine, version)


if __name__ == "__main__":
    main()
