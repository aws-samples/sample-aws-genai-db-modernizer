#!/usr/bin/env python3
"""Parse collector output and initialize a modernization job.

Usage:
    uv run python scripts/run_collect.py --file <collector-json> [--db <name>] [--artifact-root .artifacts]

Outputs JSON to stdout:
    {"status": "complete", "job_id": "...", "database_name": "...", "tables": N, "queries": N}
"""

import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _output(data: dict) -> None:
    """Print JSON to stdout (the only output the caller parses)."""
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> None:
    _output({"status": "error", "message": message})
    sys.exit(code)


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
        default="./artifacts",
        help="Root directory for local artifacts (default: ./artifacts)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.file):
        _error(f"File not found: {args.file}")

    # Read and parse the collector file
    with open(args.file, encoding="utf-8") as f:
        content = f.read().strip()
        # MySQL outputs a column header (e.g. "collection_output") on the first
        # line when run without -N. Strip it so we get valid JSON.
        if not content.startswith("{") and "\n" in content:
            content = content[content.index("\n") + 1 :]
        input_data = json.loads(content)

    # Detect format: contract (processed) vs raw (collection script output)
    is_contract = isinstance(input_data.get("contract_version"), str) and input_data[
        "contract_version"
    ].startswith("3")

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
    else:
        # Raw collection script output — parse with offline parser
        db_name = args.db or input_data.get("metadata", {}).get("database_name", "unknown_db")
        if not args.db and isinstance(input_data.get("metadata"), str):
            meta = json.loads(input_data["metadata"])
            db_name = meta.get("database_name", meta.get("schema_name", "unknown_db"))

        # Upload raw file
        upload_path = f"{db_name}/{job_id}/uploads/collector-output.json"
        store.write_json(upload_path, input_data)

        # Parse using offline parser
        from src.tools.database.offline_parser import parse_offline_collection

        parsed = parse_offline_collection(input_data)

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

        inp = CollectorInput.model_validate(
            {
                "job_id": job_id,
                "engine": "mysql",
                "cluster_endpoint": "offline",
                "port": 3306,
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
                version=offline_meta.get("version", "unknown"),
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

    # Materialize query journey files
    from src.agents.query_journey_materializer import materialize_source

    materialize_source(collector_data, db_name, job_id, store)

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


if __name__ == "__main__":
    main()
