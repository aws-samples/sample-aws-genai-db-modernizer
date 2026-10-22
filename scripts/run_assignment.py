#!/usr/bin/env python3
"""Run assignment resolution — map queries to their best-fit target engine.

Usage:
    uv run python scripts/run_assignment.py --job-id <id> --db <name> [--artifact-root ./artifacts]

Outputs JSON to stdout:
    {"status": "complete", "distribution": {...}, "total_queries": N}
    (plus "unservable_queries": M when the customer's engine scope leaves M queries no
    engine, #1329)
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Phase name for the durable failure marker (#517). Set once job/db are known
# (main(), right after the artifact store is created) so _error() and the
# __main__ catch-all below can record a failure against this job.
_PHASE = "assignment"
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

        write_failure_marker(
            _JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, "ScriptError", message
        )
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run assignment resolution.")
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
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

    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    try:
        args.db = validate_database_name(args.db)
        args.job_id = validate_job_id(args.job_id)
    except ValueError as exc:
        _error(str(exc))

    from scripts._job_status import clear_failure_marker
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    _JOB.update(store=store, db=args.db, job_id=args.job_id)
    clear_failure_marker(store, args.db, args.job_id, _PHASE)

    # Verify prerequisites
    triage_path = f"{args.db}/{args.job_id}/referee-triage/triage.json"
    if not store.exists(triage_path):
        _error(f"Triage output not found at {triage_path}. Run /triage first.")

    # Run assignment resolver
    from src.agents.referee.assignment_handler import run_assignment_resolver

    run_assignment_resolver(args.job_id, args.db, store)

    # Read results: the resolver writes next_assignment_version (v1 on a fresh
    # job, v3+ on a re-run), so read the version it just wrote.
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        resolve_downstream_assignment_version,
    )

    assignment_path = assignment_artifact_path(
        args.db, args.job_id, resolve_downstream_assignment_version(store, args.db, args.job_id)
    )
    if not store.exists(assignment_path):
        _error("Assignment output not produced. Check logs for errors.")

    assignment = store.read_json(assignment_path)

    # Calculate distribution
    distribution: dict[str, int] = {}
    assignments_list = assignment.get("query_assignments", [])
    for a in assignments_list:
        engine = a.get("assigned_engine", "unknown")
        distribution[engine] = distribution.get(engine, 0) + 1

    status = {
        "status": "complete",
        "distribution": distribution,
        "total_queries": len(assignments_list),
        # Queries the cache layer fronts (#296); never part of the distribution
        "cache_overlay": assignment.get("cache_overlay"),
    }
    unservable = len(assignment.get("unservable_queries") or [])
    if unservable:
        # #1329: queries no engine in the customer's engine scope can serve (no
        # engine, so never in the distribution or the total).
        status["unservable_queries"] = unservable
    _output(status)


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, True)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
