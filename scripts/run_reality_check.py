#!/usr/bin/env python3
"""Run reality check for a job with configurable LLM mode.

Usage:
    uv run python scripts/run_reality_check.py --job-id <id> --db <name>
    uv run python scripts/run_reality_check.py --job-id <id> --db <name> --llm-mode external
    uv run python scripts/run_reality_check.py --job-id <id> --db <name> --finalize
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


def run_standard(store, job_id: str, db: str, assignment_version: int, llm_mode: str) -> None:
    """Run reality check (none, bedrock, or external LLM mode)."""
    from src.agents.referee.reality_check_handler import run_reality_check_handler

    run_reality_check_handler(job_id, db, store, assignment_version, llm_mode=llm_mode)

    if llm_mode == "external":
        llm_request_path = f"{db}/{job_id}/llm_requests/reality_check.json"
        if store.exists(llm_request_path):
            _output({"status": "awaiting_llm", "llm_request": llm_request_path})
        else:
            _output({"status": "awaiting_llm"})
    else:
        _output({"status": "complete"})


def run_finalize(store, job_id: str, db: str, assignment_version: int) -> None:
    """Merge external LLM response into deterministic result and write output."""
    from src.agents.referee.reality_check_handler import (
        apply_reality_check_llm_output,
        run_reality_check_deterministic,
    )
    from src.contracts.reality_check_output import RealityCheckOutputContract

    det = run_reality_check_deterministic(job_id, db, store, assignment_version)

    llm_response_path = f"{db}/{job_id}/llm_responses/reality_check.json"
    if not store.exists(llm_response_path):
        _error(f"LLM response not found at {llm_response_path}")

    llm_response = store.read_json(llm_response_path)
    result = apply_reality_check_llm_output(det, llm_response)

    output = RealityCheckOutputContract.model_validate(
        {
            "source_assignment_version": assignment_version,
            "unique_value_assessment": result["unique_value_assessment"],
            "consolidations": result["consolidations"],
            "architectural_patterns": result["architectural_patterns"],
            "executive_summary": result["executive_summary"],
            "recommendations": result["recommendations"],
            "before_distribution": result["before_distribution"],
            "after_distribution": result["after_distribution"],
            "lightweight_recommendations": result.get("lightweight_recommendations", []),
        }
    )
    output_key = f"{db}/{job_id}/reality-check/output.json"
    store.write_json(output_key, output.model_dump(mode="json"))

    # If consolidation occurred, write a new assignment version
    assignment = result["assignment"]
    if result["consolidations"]:
        new_version = assignment_version + 1
        revised_assignment = {
            **assignment,
            "version": new_version,
            "query_assignments": result["revised_assignments"],
            "reality_check_applied": True,
        }
        revised_key = f"{db}/{job_id}/assignment/v{new_version}/assignment.json"
        store.write_json(revised_key, revised_assignment)

    _output({"status": "complete"})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run reality check for a job with configurable LLM mode."
    )
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument(
        "--llm-mode",
        default="none",
        choices=["bedrock", "external", "none"],
        help="LLM execution mode (default: none)",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Finalize by merging external LLM response into reality check output",
    )
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=1,
        help="Assignment version to load (default: 1)",
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
        run_finalize(store, args.job_id, args.db, args.assignment_version)
    else:
        run_standard(store, args.job_id, args.db, args.assignment_version, args.llm_mode)


if __name__ == "__main__":
    main()
