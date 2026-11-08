#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Check the waves again after synthesis and start the next redesign round (#1992).

Claude Code (``/modernize``) runs this after each synthesis. It reads the latest
``report.json``; when a wave is held for a reason its design can fix and rounds remain,
it moves that engine's design aside, records the failed checks for the next design, and
prints ``awaiting_redesign`` with the engines to design again. Otherwise it prints
``complete`` (nothing design-fixable is held) or ``exhausted`` (the last round ran, and
``held`` engines are still held). It prints ``awaiting_synthesis`` when it put an
engine's earlier design back (the round's design was missing, or no better): synthesize
again, then run it again. The engine stays: no assignment version is written.

    uv run python scripts/run_wave_redesign.py --job-id <id> --db <name>
    uv run python scripts/run_wave_redesign.py --job-id <id> --db <name> \
        --rollback --reason "redesign round 1 failed: schema_design_dynamodb"

``--rollback``: a design of the running round failed (its subagent, after the one
retry): put each engine's earlier design back and record the round ``failed`` with
``--reason``; synthesize again after it. It moves and deletes only the job's own design
files (``schema-<engine>/v<N>/`` and ``wave-redesign/``).

One JSON status line on stdout. A run without a model (``--llm-mode none``) has no
design, so it is always ``complete`` with 0 rounds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PHASE = "wave_redesign"
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
    parser = argparse.ArgumentParser(description="Bounded redesign round for held waves.")
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="The running round's design failed: put the earlier designs back",
    )
    parser.add_argument("--reason", default=None, help="With --rollback: what failed")
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

    from src.agents.schema_design.wave_redesign import fail_round, latest_report, plan_round
    from src.storage.assignment_versioning import resolve_downstream_assignment_version
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    if not store.exists(f"{args.db}/{args.job_id}/collector/output.json"):
        _error(f"no job {args.db}/{args.job_id} under {args.artifact_root}")
    _JOB.update(store=store, db=args.db, job_id=args.job_id)
    version = resolve_downstream_assignment_version(store, args.db, args.job_id)
    if args.rollback:
        reason = args.reason or "a design of the redesign round failed"
        _output(fail_round(store, args.db, args.job_id, version, reason))
        return
    report = latest_report(store, args.db, args.job_id)
    if report is None:
        _error("no synthesis report yet: run synthesis first")
    _output(plan_round(store, args.db, args.job_id, report, version))


if __name__ == "__main__":
    main()
