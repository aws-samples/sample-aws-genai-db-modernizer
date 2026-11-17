#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Repair the routines the deterministic path can't carry, in the repair loop (#1940, #1941).

Reads the job's newest routine-migration artifact at or below the effective assignment
version (``--assignment-version`` to name one) and writes
``routines/v<N>/routine_repair.json`` (``src.agents.routine_repair.loop``):

    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --llm-mode none
    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --llm-mode bedrock --proof local
    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --llm-mode external --proof local
    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --finalize

``none`` runs no repair: the routines stay not carried, with their reasons. Each
candidate is rebuilt onto the exact text, checked again and, with ``--proof local``,
proved side by side against the original on two disposable engines of the source's
major (no network listener); only a candidate the proof accepts is ``model_repaired``.
With ``--proof none`` (the default) a candidate that passes every check is
``repaired_pending_proof``. A failed candidate is asked again, with its error, up to
three attempts. ``external`` writes round 1's request ``routines/v<N>/repair/
llm_input.json`` and prints ``awaiting_llm`` with ``round`` and the request and
response paths; each ``--finalize`` applies the answer written to
``routines/v<N>/repair/llm_response.json`` and prints ``awaiting_llm`` for the next
round or ``complete``. The proof target given to the first run holds for its rounds.

One JSON status line on stdout; nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PHASE = "routine_repair"
_JOB: dict = {}


def _output(data: dict) -> None:
    print(json.dumps({"phase": _PHASE, **data}))


def _error(message: str, code: int = 1) -> NoReturn:
    _output({"status": "error", "message": message})
    if _JOB:
        from scripts._job_status import write_failure_marker

        write_failure_marker(
            _JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, "ScriptError", message
        )
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair the routines the checks can't carry.")
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument(
        "--llm-mode",
        default="none",
        choices=["bedrock", "external", "none"],
        help="Who repairs: Bedrock, Claude Code (external), or nobody (default: none)",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Apply the external session's answer and write the result",
    )
    parser.add_argument(
        "--proof",
        default="none",
        choices=["none", "local"],
        help=(
            "Where the side-by-side proof runs: local (two disposable engines of the "
            "source's major) or none (a candidate waits for the proof; default: none)"
        ),
    )
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Read the routine-migration artifact at or below this version (default: effective)",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    args = parser.parse_args()
    if args.llm_mode == "bedrock" and not args.finalize:
        from src.shared.bedrock_guardrail import exit_on_invalid_guardrail_config

        exit_on_invalid_guardrail_config()

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

    from pydantic import ValidationError

    from scripts._job_status import clear_failure_marker
    from src.agents.routine_repair.handler import (
        StaleRepairRequest,
        finalize_routine_repair,
        run_routine_repair,
    )
    from src.contracts.model_flag import validation_error_note
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    _JOB.update(store=store, db=args.db, job_id=args.job_id)
    clear_failure_marker(store, args.db, args.job_id, _PHASE)

    if args.finalize:
        try:
            status = finalize_routine_repair(
                store, args.db, args.job_id, assignment_version=args.assignment_version
            )
        except (FileNotFoundError, StaleRepairRequest) as exc:
            _error(str(exc))
        except ValidationError as exc:
            _error(f"the answer is off RoutineRepairResponse: {validation_error_note(exc)}")
    else:
        status = run_routine_repair(
            store,
            args.db,
            args.job_id,
            llm_mode=args.llm_mode,
            assignment_version=args.assignment_version,
            proof=args.proof,
        )
    _output(status)


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, True)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
