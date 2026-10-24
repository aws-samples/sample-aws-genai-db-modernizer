#!/usr/bin/env python3
"""The post-schema reroute for headless runs (``--llm-mode external``, #1059).

Usage:
    uv run python scripts/run_post_schema_routing.py --job-id <id> --db <name>
    uv run python scripts/run_post_schema_routing.py --job-id <id> --db <name> --rollback

Runs the same post-schema router as Bedrock mode
(``core.run_post_schema_routing_core``, #1492): the PE reviewers' routing
flags (design traces) and the designers' unsupported patterns gate the engine
that reported them, and the shared scorer moves each flagged query (with its
co-dependency group) to its best engine among those the assignment keeps,
written as a new ``rerouted_after_approval`` assignment version through the
move ledger. A model never picks the engine.

Bedrock mode redesigns the engines a reroute touched in-process; here that is
the caller's design subagents, so the script takes one pass per call and
prints one JSON line:

- ``awaiting_redesign``: a pass wrote ``assignment_version``. Design every
  engine in ``redesign_engines`` on it (and review it), then run this again.
  ``missing_designs`` lists the engines with no design on it yet; no pass runs
  while one is missing.
- ``complete``: nothing left to move (``rerouted`` and ``terminal_queries``
  count the whole run).
- ``rolled_back`` (``--rollback``, after a redesign failed): the version the
  reroute started from is restated with its designs, as Bedrock mode does.

The cascade has the Bedrock bound (passes 0-2). Progress goes to
``<artifact root>/<db>/<job>/_logs/run_post_schema_routing.log``.
"""

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PHASE = "schema_design"
_JOB: dict = {}


def _output(data: dict) -> None:
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> NoReturn:
    _output({"status": "error", "message": message})
    if _JOB:
        from scripts._job_status import write_failure_marker

        write_failure_marker(
            _JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, "ScriptError", message
        )
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-schema reroute (external mode).")
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="A redesign failed: restate the version the last reroute started from",
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

    from src.orchestrator.local_orchestrator import LocalOrchestrator
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    if not store.exists(f"{args.db}/{args.job_id}/collector/output.json"):
        _error(f"no job {args.db}/{args.job_id} under {args.artifact_root}")
    _JOB.update(store=store, db=args.db, job_id=args.job_id)

    orch = LocalOrchestrator(store=store, llm_mode="external")
    log_dir = Path(args.artifact_root) / args.db / args.job_id / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # stdout carries only the status line; the router's progress goes to the log.
    with (log_dir / "run_post_schema_routing.log").open("a", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log):
            try:
                if args.rollback:
                    status = orch.roll_back_post_schema_routing_external(args.job_id, args.db)
                else:
                    status = orch.run_post_schema_routing_external(args.job_id, args.db)
            except RuntimeError as exc:
                status = {"status": "error", "message": str(exc)}
    if status["status"] == "error":
        _error(status["message"])
    _output({**status, "log": str(log_dir / "run_post_schema_routing.log")})


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, True)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
