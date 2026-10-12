#!/usr/bin/env python3
"""Parse collector output and initialize a modernization job.

Usage:
    uv run python scripts/run_collect.py --file <collector-json> [--db <name>] [--artifact-root ./artifacts]

Outputs JSON to stdout:
    {"status": "complete", "job_id": "...", "database_name": "...", "tables": N, "queries": N}
"""

import argparse
import json
import os
import sys
import time
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.database_name import (  # noqa: E402
    DERIVED_DATABASE_NAME_ERROR,
    validate_database_name,
)
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
        "--job-id",
        default=None,
        help="Job identifier (auto-generated if not provided)",
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

    if not os.path.exists(args.file):
        _error(f"File not found: {args.file}")

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
    is_contract = isinstance(input_data.get("contract_version"), str) and input_data[
        "contract_version"
    ].startswith("3")

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

    if is_contract:
        # Already in collector contract format — use directly
        collector_data = input_data
        db_name = args.db or (
            collector_data.get("metadata", {})
            .get("source_database", {})
            .get("database_name", "unknown_db")
        )
        if args.db is None:
            try:
                db_name = validate_database_name(db_name)
            except ValueError:
                _error(DERIVED_DATABASE_NAME_ERROR)
        _set_job_context(store, db_name, job_id)
    else:
        # Raw collection script output — parse with offline parser
        db_name = args.db or input_data.get("metadata", {}).get("database_name", "unknown_db")
        if not args.db and isinstance(input_data.get("metadata"), str):
            meta = json.loads(input_data["metadata"])
            db_name = meta.get("database_name", meta.get("schema_name", "unknown_db"))

        if args.db is None:
            try:
                db_name = validate_database_name(db_name)
            except ValueError:
                _error(DERIVED_DATABASE_NAME_ERROR)
        _set_job_context(store, db_name, job_id)

        # Upload raw file
        upload_path = f"{db_name}/{job_id}/uploads/collector-output.json"
        store.write_json(upload_path, input_data)

        # Parse using offline parser
        from src.tools.database.offline_parser import (
            detect_source_engine,
            offline_version_label,
            parse_offline_collection,
        )

        try:
            parsed = parse_offline_collection(input_data)
        except IngestionLimitError as exc:
            _error(str(exc))

        # Build collector contract output
        from src.agents.collector.mysql_collector import (
            _build_metrics,
            _build_output,
            _build_procedures,
            _build_queries,
            _build_tables,
            _build_triggers,
            _build_views,
        )
        from src.contracts.collector_input import CollectorInput

        start = time.monotonic()
        tables_built = _build_tables(parsed["tables"], db_name)
        queries_built = _build_queries(parsed.get("queries", []))
        views = _build_views(parsed.get("views", []))
        procedures = _build_procedures(parsed.get("procedures", []))
        triggers = _build_triggers(parsed.get("triggers", []))
        metrics = _build_metrics(queries_built, None)

        engine, port = detect_source_engine(parsed.get("metadata"))

        inp = CollectorInput.model_validate(
            {
                "job_id": job_id,
                "engine": engine,
                "cluster_endpoint": "offline",
                "port": port,
                "database_name": db_name,
                "mode": "offline",
                "offline_config": {"s3_bucket": "local", "s3_key": upload_path},
            }
        )

        offline_meta = parsed.get("metadata", {})
        collector_data = json.loads(
            _build_output(
                inp,
                start,
                # #381: a short, human-readable version string (product_version, or
                # the first line only of version/version_full) -- the full @@VERSION
                # banner (newlines, tabs, build date) must never reach wave 1's
                # user-visible rationale text.
                version=offline_version_label(offline_meta),
                db_size=offline_meta.get("database_size_gb"),
                tables=tables_built,
                queries=queries_built,
                metrics=metrics,
                rds_meta=None,
                views=views,
                procedures=procedures,
                triggers=triggers,
            ).model_dump_json()
        )

    # Write collector output
    collector_path = f"{db_name}/{job_id}/collector/output.json"
    store.write_json(collector_path, collector_data)

    tables = collector_data.get("database_schema", {}).get("tables", [])
    queries = collector_data.get("queries", {}).get("query_patterns", [])

    _output(
        {
            "status": "complete",
            "job_id": job_id,
            "database_name": db_name,
            "tables": len(tables),
            "queries": len(queries),
        }
    )


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, False)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
