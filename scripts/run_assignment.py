#!/usr/bin/env python3
"""Run assignment resolution — map queries to their best-fit target engine.

Usage:
    uv run python scripts/run_assignment.py --job-id <id> --db <name> [--artifact-root .artifacts]

Outputs JSON to stdout:
    {"status": "complete", "distribution": {...}, "total_queries": N}
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _output(data: dict) -> None:
    """Print JSON to stdout (the only output the caller parses)."""
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> None:
    _output({"status": "error", "message": message})
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

    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)

    # Verify prerequisites
    triage_path = f"{args.db}/{args.job_id}/referee-triage/triage.json"
    if not store.exists(triage_path):
        _error(f"Triage output not found at {triage_path}. Run /triage first.")

    # Run assignment resolver
    from src.agents.referee.assignment_handler import run_assignment_resolver

    run_assignment_resolver(args.job_id, args.db, store)

    # Read results
    assignment_path = f"{args.db}/{args.job_id}/assignment/v1/assignment.json"
    if not store.exists(assignment_path):
        _error("Assignment output not produced. Check logs for errors.")

    assignment = store.read_json(assignment_path)

    # Calculate distribution
    distribution: dict[str, int] = {}
    assignments_list = assignment.get("query_assignments", [])
    for a in assignments_list:
        engine = a.get("assigned_engine", "unknown")
        distribution[engine] = distribution.get(engine, 0) + 1

    _output(
        {
            "status": "complete",
            "distribution": distribution,
            "total_queries": len(assignments_list),
        }
    )


if __name__ == "__main__":
    main()
