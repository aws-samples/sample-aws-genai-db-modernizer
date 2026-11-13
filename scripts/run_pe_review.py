#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The schema-design PE review for headless runs (``--llm-mode external``, #1059).

Usage:
    uv run python scripts/run_pe_review.py --job-id <id> --db <name>
    uv run python scripts/run_pe_review.py --job-id <id> --db <name> --engine documentdb
    uv run python scripts/run_pe_review.py --job-id <id> --db <name> --engine documentdb --finalize
    uv run python scripts/run_pe_review.py --job-id <id> --db <name> --engine dynamodb \
        --group 0 --finalize

Without ``--finalize`` it writes one review request per design on the
effective assignment version that has no review recorded yet (one per group
for DynamoDB; every designed engine, or only ``--engine``) and prints one JSON
line: ``awaiting_llm`` with ``reviews`` (``engine``, ``group``, ``request``,
``response``, ``skill_prompt``, ``finalize``), or ``complete`` when none is
needed; ``recorded`` and ``skipped`` say why the others were left out.
While a design is not complete and has no review recorded (its
``run_schema_design.py --status`` is ``not_finalized`` or ``finalize_pending``,
``not_split`` or ``drafts_pending`` for DynamoDB, including a design of the run
that has written nothing yet), it prints ``design_pending`` with
``designs_pending`` and ``next``, and writes no request, with or without
``--engine``: a review started early never reads a design still being written,
and the final check never prints ``complete`` with a design still out (#2200).

``--finalize`` validates the answer a review subagent wrote at ``response``
against the engine's ``PEReviewResult`` (the Bedrock reviewer's contract) and
records it in the engine's design trace, where the post-schema router reads
its ``routing_flags`` (``scripts/run_post_schema_routing.py``). DynamoDB's
group reviews are recorded per group; run them before ``--merge``, which
consolidates them (a review recorded later makes ``--status`` print
``merge_pending``). ``--finalize --failed`` records a review that gave no
valid answer (no flags), so the reroute does not wait for it.

See ``src/agents/schema_design/pe_review_external.py``.
"""

import argparse
import json
import os
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ENGINES = (
    "aurora_mysql",
    "aurora_postgresql",
    "documentdb",
    "dynamodb",
    "elasticache",
    "opensearch",
)
_PHASE = "schema_design"
_JOB: dict = {}


def _output(data: dict) -> None:
    print(json.dumps(data))


def _error(message: str, code: int = 1) -> NoReturn:
    _output({"status": "error", "message": message})
    if _JOB:
        from scripts._job_status import write_failure_marker

        write_failure_marker(
            _JOB["store"],
            _JOB["db"],
            _JOB["job_id"],
            _PHASE,
            "ScriptError",
            message,
            engine=_JOB.get("engine"),
        )
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Schema-design PE review (external mode).")
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument("--engine", choices=_ENGINES, default=None, help="Only this engine")
    parser.add_argument("--group", type=int, default=None, help="DynamoDB group (--finalize)")
    parser.add_argument(
        "--finalize", action="store_true", help="Validate and record the written review answer"
    )
    parser.add_argument(
        "--failed",
        action="store_true",
        help="With --finalize: the review gave no valid answer; record it with no flags",
    )
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Assignment version (default: the effective version)",
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

    if args.finalize and args.engine is None:
        _error("--finalize needs --engine")
    if args.failed and not args.finalize:
        _error("--failed is for --finalize")
    if args.group is not None and not args.finalize:
        _error("--group is for --finalize; without it every group's request is written")

    from src.agents.schema_design.pe_review_external import (
        apply_pe_review_llm_output,
        prepare_pe_review_llm_input,
    )
    from src.storage.assignment_versioning import resolve_downstream_assignment_version
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    if not store.exists(f"{args.db}/{args.job_id}/collector/output.json"):
        _error(f"no job {args.db}/{args.job_id} under {args.artifact_root}")
    version = args.assignment_version or resolve_downstream_assignment_version(
        store, args.db, args.job_id
    )
    _JOB.update(store=store, db=args.db, job_id=args.job_id, engine=args.engine)

    if args.finalize:
        result = apply_pe_review_llm_output(
            store, args.db, args.job_id, version, args.engine, args.group, failed=args.failed
        )
        if result["status"] == "error":
            _error(result["message"])
        _output(result)
        return

    pending = _design_pending(store, args.db, args.job_id, version, args.engine)
    if pending:
        _output(pending)
        return

    prepared = prepare_pe_review_llm_input(
        store,
        args.db,
        args.job_id,
        version,
        engines=[args.engine] if args.engine else None,
    )
    _output({"status": "awaiting_llm" if prepared["reviews"] else "complete", **prepared})


#: A design whose ``run_schema_design.py --status`` is one of these is not complete:
#: still being written or finalized, or not started yet. Its review must not read it.
_DESIGN_PENDING = {"not_split", "drafts_pending", "not_finalized", "finalize_pending"}


def _read_json(store, key: str):
    try:
        return store.read_json(key)
    except (OSError, ValueError):
        return None


def _pending_designs(store, db: str, job_id: str, version: int, engine: str | None) -> list[dict]:
    """The designs on ``version`` that are not complete and have no review recorded.

    The engines checked: ``engine``, or every engine the run designs on ``version``
    (``schema_design_engines``, the list ``/modernize`` dispatches); an engine outside
    that list never holds the check, even with a leftover design there. An engine the
    run designs whose design has written nothing yet is pending too (``not_finalized``;
    ``not_split`` for DynamoDB). A design whose review is recorded (its own, or carried
    forward with it) and a skipped placeholder (nothing to design, or the user's skip,
    ``run_schema_design.py --skip``) are done.
    """
    from scripts.run_schema_design import _status_line
    from src.agents.schema_design.pe_review_external import is_recorded
    from src.storage.assignment_versioning import schema_design_engines

    expected = schema_design_engines(store, db, job_id, version)
    pending: list[dict] = []
    for name in [engine] if engine is not None else expected:
        if name not in _ENGINES:
            continue
        if name not in expected:
            continue  # a leftover design of an engine the run does not design holds nothing
        base = f"{db}/{job_id}/schema-{name}/v{version}"
        output = _read_json(store, f"{base}/schema_output.json")
        if isinstance(output, dict) and output.get("status") == "skipped":
            continue  # nothing to design, or skipped by the user (run_schema_design.py --skip)
        if name != "dynamodb" and is_recorded(store, db, job_id, name, version, None):
            continue
        line = _status_line(store, job_id, db, name, version)
        if line["status"] not in _DESIGN_PENDING:
            continue
        entry: dict = {"engine": name, "design_status": line["status"]}
        for key in ("drafts_missing", "drafts_invalid"):
            if line.get(key):
                entry[key] = line[key]
        pending.append(entry)
    return pending


def _design_pending(store, db: str, job_id: str, version: int, engine: str | None) -> dict | None:
    """The ``design_pending`` line while a design is not complete, else None (#2200).

    ``/modernize`` starts a review as soon as its design's ``--status`` says it is
    complete, while other designs still run; this makes the same check here, for
    ``--engine`` and for the final check without it, so no review request is written
    for a design its subagent is still writing, and the check never prints
    ``complete`` while a design of the run is not in. ``next`` is the step to take.
    """
    pending = _pending_designs(store, db, job_id, version, engine)
    if not pending:
        return None
    line: dict = {
        "status": "design_pending",
        "assignment_version": version,
        "designs_pending": pending,
    }
    if engine is not None:
        line.update(pending[0])
    line["message"] = (
        "not complete on v"
        + str(version)
        + ": "
        + ", ".join(f"{p['engine']} ({p['design_status']})" for p in pending)
        + "; no review request was written"
    )
    line["next"] = (
        "run each listed engine's run_schema_design.py --status once (DynamoDB: with "
        "--wait 60); while its design subagent has not reported, wait for its report; a "
        "design never dispatched is dispatched now; once it has reported, complete "
        "(DynamoDB: merge_pending) means run this command again, any other line is that "
        "engine's phase failure"
    )
    return line


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, _JOB.get("engine"), True)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
