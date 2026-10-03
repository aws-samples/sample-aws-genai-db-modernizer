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


def run_standard(
    store, job_id: str, db: str, assignment_version: int | None, llm_mode: str
) -> None:
    """Run reality check (none, bedrock, or external LLM mode)."""
    from src.agents.referee.reality_check_handler import run_reality_check_handler

    summary = run_reality_check_handler(job_id, db, store, assignment_version, llm_mode=llm_mode)

    if summary["status"] == "awaiting_llm":
        llm_request_path = f"{db}/{job_id}/reality-check/llm_input.json"
        llm_response_path = f"{db}/{job_id}/llm_responses/reality_check.json"
        if store.exists(llm_request_path):
            _output(
                {
                    "status": "awaiting_llm",
                    "llm_request": llm_request_path,
                    "llm_response": llm_response_path,
                }
            )
        else:
            _output({"status": "awaiting_llm"})
    else:
        # "skipped" (input already consolidated) is still a completed phase.
        _output(
            {
                "status": "complete",
                "skipped": summary["status"] == "skipped",
                "input_version": summary["input_version"],
                "output_version": summary["output_version"],
            }
        )


def run_finalize(store, job_id: str, db: str, assignment_version: int | None) -> None:
    """Merge external LLM response into deterministic result and write output."""
    from src.agents.referee.reality_check_handler import finalize_reality_check

    llm_response_path = f"{db}/{job_id}/llm_responses/reality_check.json"
    if not store.exists(llm_response_path):
        _error(f"LLM response not found at {llm_response_path}")

    finalize_reality_check(
        store, job_id, db, store.read_json(llm_response_path), assignment_version
    )
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
        default=None,
        help=(
            "Assignment version to consolidate (default: the newest version Reality "
            "Check did not produce; the run is skipped if it is already consolidated). "
            "An explicit version always runs. The revision is written to the next "
            "free version either way."
        ),
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

    store = LocalArtifactStore(base_dir=args.artifact_root)

    if args.finalize:
        run_finalize(store, args.job_id, args.db, args.assignment_version)
    else:
        run_standard(store, args.job_id, args.db, args.assignment_version, args.llm_mode)


if __name__ == "__main__":
    main()
