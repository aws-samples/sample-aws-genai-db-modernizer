#!/usr/bin/env python3
"""Run synthesis for a job with configurable LLM mode.

Usage:
    uv run python scripts/run_synthesis.py --job-id <id> --db <name>
    uv run python scripts/run_synthesis.py --job-id <id> --db <name> --llm-mode external
    uv run python scripts/run_synthesis.py --job-id <id> --db <name> --finalize
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
    """Run synthesis (none, bedrock, or external LLM mode)."""
    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis(job_id, db, store, assignment_version, llm_mode=llm_mode)

    if llm_mode == "external":
        if assignment_version > 0:
            llm_input_key = f"{db}/{job_id}/synthesis/v{assignment_version}/llm_input.json"
        else:
            llm_input_key = f"{db}/{job_id}/synthesis/llm_input.json"

        if store.exists(llm_input_key):
            _output({"status": "awaiting_llm", "llm_request": llm_input_key})
        else:
            _output({"status": "awaiting_llm"})
    else:
        _output({"status": "complete"})


def run_finalize(store, job_id: str, db: str, assignment_version: int) -> None:
    """Merge external LLM response into deterministic result and write synthesis report."""
    from src.agents.referee.synthesis_handler import (
        _write_synthesis_report,
        apply_synthesis_llm_output,
        run_synthesis_deterministic,
    )
    from src.contracts.synthesis_output import SynthesisOutputContract  # noqa: F401

    result = run_synthesis_deterministic(job_id, db, store, assignment_version)

    if assignment_version > 0:
        llm_response_path = f"{db}/{job_id}/synthesis/v{assignment_version}/llm_response.json"
    else:
        llm_response_path = f"{db}/{job_id}/synthesis/llm_response.json"

    if not store.exists(llm_response_path):
        _error(f"LLM response not found at {llm_response_path}")

    llm_response = store.read_json(llm_response_path)
    result = apply_synthesis_llm_output(result, llm_response)

    _write_synthesis_report(store, result, assignment_version)

    _output({"status": "complete"})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run synthesis for a job with configurable LLM mode."
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
        help="Finalize by merging external LLM response into synthesis report",
    )
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=0,
        help="Assignment version to load (default: 0)",
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
