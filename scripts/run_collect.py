#!/usr/bin/env python3
"""Parse collector output and initialize a modernization job.

Usage:
    uv run python scripts/run_collect.py --file <collector-json> [--db <name> | --db-from-file-name] [--artifact-root ./artifacts]

Outputs JSON to stdout:
    {"status": "complete", "job_id": "...", "database_name": "...", "tables": N, "queries": N}
"""

import argparse
import json
import os
import sys
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.collection_database_name import (  # noqa: E402
    resolve_database_name,
)
from src.shared.database_name import validate_database_name  # noqa: E402
from src.shared.job_id import validate_job_id  # noqa: E402
from src.tools.database.collection_text import extract_json_object  # noqa: E402
from src.tools.validation.ingestion_limits import (  # noqa: E402
    IngestionLimitError,
    check_collection_counts,
    check_file_size,
)

# Phase name for the durable failure marker (#517). Set once job_id and
# db_name are both resolved -- unlike the other scripts/run_*.py entry
# points, this one creates the job rather than resuming one, so most
# _error() calls happen before there is anywhere to write a marker.
_PHASE = "collect"
_JOB: dict = {}


def _output(data: dict) -> None:
    """Print JSON to stdout (the only output the caller parses)."""
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> None:
    # Status line first (#517 round-1 review, I1): the marker write is a
    # best-effort side effect and must never delay or risk the one output
    # callers actually parse.
    _output({"status": "error", "message": message})
    if _JOB:
        from scripts._job_status import write_failure_marker

        # require_existing_job=False (#517 round-1 review, I2): this script
        # is what *creates* collector/output.json -- it never exists yet at
        # any point main() can still fail.
        write_failure_marker(
            _JOB["store"],
            _JOB["db"],
            _JOB["job_id"],
            _PHASE,
            "ScriptError",
            message,
            require_existing_job=False,
        )
    sys.exit(code)


def _set_job_context(store, db_name: str, job_id: str) -> None:
    """Record job/db once both are known so a later _error() in this same
    run can mark the failure (#517). A fresh job has nothing to clear, but
    clear anyway in case --job-id reused an existing one."""
    from scripts._job_status import clear_failure_marker

    _JOB.update(store=store, db=db_name, job_id=job_id)
    clear_failure_marker(store, db_name, job_id, _PHASE, require_existing_job=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse collector output and initialize a job.")
    parser.add_argument("--file", required=True, help="Path to collector output JSON file")
    parser.add_argument("--db", default=None, help="Database name (auto-detected if not provided)")
    parser.add_argument(
        "--db-from-file-name",
        action="store_true",
        help=(
            "With --file and no --db: name the database from the collection file name "
            "(the rule /modernize and /collect use) instead of preferring the file's own "
            "metadata name"
        ),
    )
    parser.add_argument(
        "--job-id",
        default=None,
        help="Job identifier (auto-generated if not provided)",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    parser.add_argument(
        "--engines-in-use",
        default=None,
        metavar="ENGINES",
        help=(
            "Comma-separated engine ids of the other AWS databases the application already "
            "runs today (for example opensearch,elasticache). Recorded in the collection so "
            "retiring one shows a saving; added to any the file already lists"
        ),
    )
    args = parser.parse_args()

    from src.shared.engines_in_use import merge_engines_in_use, parse_engines_in_use_option

    try:
        engines_in_use = parse_engines_in_use_option(args.engines_in_use)
    except ValueError as exc:
        _error(str(exc))

    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        _error(violation)

    if args.db is not None and args.db_from_file_name:
        _error("--db and --db-from-file-name cannot be used together")

    if args.db is not None:
        try:
            args.db = validate_database_name(args.db)
        except ValueError as exc:
            _error(str(exc))

    if args.job_id is not None:
        try:
            args.job_id = validate_job_id(args.job_id)
        except ValueError as exc:
            _error(str(exc))

    from src.shared.sample_collection import CollectionPathError, resolve_collection_path

    try:
        args.file = resolve_collection_path(args.file)
    except CollectionPathError as exc:
        _error(str(exc))

    # Check the file size before reading it into memory (#142).
    try:
        check_file_size(os.path.getsize(args.file), source=args.file)
    except IngestionLimitError as exc:
        _error(str(exc))

    if zipfile.is_zipfile(args.file):
        _error(f"{args.file} is a zip archive; unzip it first and pass the .json file inside.")

    # Read and parse the collector file
    try:
        with open(args.file, encoding="utf-8") as f:
            raw_text = f.read()
    except UnicodeDecodeError:
        _error(f"{args.file} is not a UTF-8 JSON collection.")

    content = extract_json_object(raw_text, source=args.file)
    input_data = json.loads(content)

    # Detect format: contract (processed) vs raw (collection script output)
    from src.contracts.collector_output import CollectorOutputContract
    from src.shared.contract_versioning import (
        ContractVersionError,
        carries_contract,
        stamp_artifact_version,
    )

    # A stored collector output carries a contract_version (ADR-008 Decision 4: same
    # MAJOR only); a raw collection carries none.
    try:
        is_contract = carries_contract(input_data, CollectorOutputContract, artifact=args.file)
    except ContractVersionError as exc:
        _error(str(exc))

    if is_contract:
        try:
            check_collection_counts(
                table_count=len((input_data.get("database_schema") or {}).get("tables") or []),
                query_count=len((input_data.get("queries") or {}).get("query_patterns") or []),
                source=args.file,
            )
        except IngestionLimitError as exc:
            _error(str(exc))

    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    job_id = args.job_id or str(uuid.uuid4())[:8]
    table_refs: dict | None = None  # parser counts, raw collections only (#553)

    # No --db: the file's own valid metadata name, else one derived from its file
    # name (#1096). The status line says which.
    db_source = "explicit"
    db_name = args.db
    if db_name is None:
        db_name, db_source = resolve_database_name(
            input_data, args.file, from_file_name=args.db_from_file_name
        )
    _set_job_context(store, db_name, job_id)

    if is_contract:
        # Already in collector contract format — use directly
        collector_data = input_data
    else:
        # Raw collection script output — parse with offline parser
        # Upload raw file
        upload_path = f"{db_name}/{job_id}/uploads/collector-output.json"
        store.write_json(upload_path, input_data)

        # Parse using offline parser
        from src.tools.database.offline_parser import parse_offline_collection
        from src.tools.sql.shadow import TableShadow, write_shadow_artifact

        try:
            # Table references come from the sqlglot facade, with the regex as
            # its fallback (#553); the regex also runs as a cross-check, and
            # both counts go on the collect status line (``table_refs``).
            sql_shadow = TableShadow()
            parsed = parse_offline_collection(input_data, shadow=sql_shadow)
        except IngestionLimitError as exc:
            _error(str(exc))
        write_shadow_artifact(store, db_name, job_id, sql_shadow)
        table_refs = sql_shadow.status_summary()

        # Build collector contract output
        from src.agents.collector.offline_ingest import build_offline_collector_output

        collector_data = build_offline_collector_output(
            parsed, job_id=job_id, database_name=db_name, s3_key=upload_path
        )

    # Write collector output
    collector_path = f"{db_name}/{job_id}/collector/output.json"
    if engines_in_use:
        # #963: engines the operator declared, added to any the collection lists.
        meta = collector_data.setdefault("metadata", {})
        meta["engines_in_use"] = merge_engines_in_use(meta.get("engines_in_use"), engines_in_use)
    # An uploaded collector output may be at an older MINOR: written at today's (#1018).
    store.write_json(collector_path, stamp_artifact_version(collector_path, collector_data))

    tables = collector_data.get("database_schema", {}).get("tables", [])
    queries = collector_data.get("queries", {}).get("query_patterns", [])

    _output(
        {
            "status": "complete",
            "job_id": job_id,
            "database_name": db_name,
            "database_name_source": db_source,
            "tables": len(tables),
            "queries": len(queries),
            **({"table_refs": table_refs} if table_refs is not None else {}),
        }
    )


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, False)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
