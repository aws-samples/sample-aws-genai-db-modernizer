#!/usr/bin/env python3
"""Run triage to select target NoSQL engines based on workload signals.

Usage:
    uv run python scripts/run_triage.py --job-id <id> --db <name> [--artifact-root .artifacts]

Outputs JSON to stdout:
    {"status": "complete", "selected": [...], "skipped": [...], "deferred": [...]}
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
    parser = argparse.ArgumentParser(description="Run triage to select target engines.")
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


if __name__ == "__main__":
    main()
