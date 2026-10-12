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

# Phase name for the durable failure marker (#517). Set once job/db are known
# (main(), right after the artifact store is created) so _error() and the
# __main__ catch-all below can record a failure against this job.
_PHASE = "reality_check"
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
    output_key = f"{db}/{job_id}/reality-check/output.json"
    output = store.read_json(output_key) if store.exists(output_key) else {}
    # deterministic_fallback: the LLM summary contradicted the final records (#236).
    _output(
        {"status": "complete", "executive_summary_source": output.get("executive_summary_source")}
    )


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
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
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

    if args.finalize:
        run_finalize(store, args.job_id, args.db, args.assignment_version)
    else:
        run_standard(store, args.job_id, args.db, args.assignment_version, args.llm_mode)


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, True)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
