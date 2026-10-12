#!/usr/bin/env python3
"""Run triage to select target NoSQL engines based on workload signals.

Usage:
    uv run python scripts/run_triage.py --job-id <id> --db <name> [--artifact-root ./artifacts]

Outputs JSON to stdout:
    {"status": "complete", "selected": [...], "skipped": [...], "deferred": [...]}
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Phase name for the durable failure marker (#517). Set once job/db are known
# (main(), right after the artifact store is created) so _error() and
# run_main's catch-all below can record a failure against this job.
_PHASE = "triage"
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
    parser = argparse.ArgumentParser(description="Run triage to select target engines.")
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

    # Verify collector output exists
    collector_path = f"{args.db}/{args.job_id}/collector/output.json"
    if not store.exists(collector_path):
        _error(f"Collector output not found at {collector_path}. Run /collect first.")

    # Run triage
    from src.agents.referee.triage_handler import run_triage

    run_triage(args.job_id, args.db, store)

    # Read results
    triage_path = f"{args.db}/{args.job_id}/referee-triage/triage.json"
    triage_output = store.read_json(triage_path)

    selected = [a["agent_type"] for a in triage_output.get("selected_agents", [])]
    skipped = [a["agent_type"] for a in triage_output.get("skipped_agents", [])]
    deferred = [a["agent_type"] for a in triage_output.get("deferred_agents", [])]

    _output(
        {
            "status": "complete",
            "selected": selected,
            "skipped": skipped,
            "deferred": deferred,
            "signals": [
                {
                    "signal": s.get("signal", ""),
                    "targets": s.get("targets", []),
                    "evidence": s.get("evidence", ""),
                }
                for s in triage_output.get("signals", [])
            ],
        }
    )


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, True)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
