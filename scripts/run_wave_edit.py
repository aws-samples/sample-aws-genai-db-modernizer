#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Revisit one wave's design and its review warnings (``/wave-edit <N>``, #2067).

    uv run python scripts/run_wave_edit.py --job-id <id> --db <name> --wave <N>
    uv run python scripts/run_wave_edit.py --job-id <id> --db <name> --wave <N> \
        --reject 1,3            # or --reject none (accept every warning); add --auto
    uv run python scripts/run_wave_edit.py --job-id <id> --db <name> --wave <N> --check

With ``--wave`` alone it shows wave N from the latest ``report.json``: its readiness
line, engines and query count, and each warning and blocker, numbered (``shown``).

``--reject``: the warning numbers the customer does not accept (``none``: every warning
is accepted; ``--auto`` records that an unattended run accepted them). It records the
decision under ``decisions/wave-edit/wave-<N>/v<k>.json`` (a new version, never
overwritten), then prints ``awaiting_redesign`` with the wave's engines to design and
review again (only them; their design input carries the rejected warnings and the
blockers), ``awaiting_synthesis`` (nothing to redesign: synthesize so the report states
the decision) or ``nothing_to_edit``.

``--check``, after each synthesis: closes the round by the #1992 loop's rule and prints
``awaiting_synthesis`` (the earlier design is back: synthesize again, then check again),
``awaiting_redesign`` (the next round), ``awaiting_review`` (a redesign's review is not
recorded yet: review it, synthesize, then check again) or ``complete`` with the wave's
new readiness line. A design that fails is rolled back with ``run_wave_redesign.py --rollback``.

Routing never changes: no assignment version is written. Without a model no redesign
can run: with ``--no-model`` (what ``/wave-edit --llm-mode none`` passes), ``--reject``
prints ``no_model`` and records nothing. One JSON status line on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PHASE = "wave_edit"
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


def _numbers(text: str) -> list[int]:
    if text.strip().lower() in ("none", ""):
        return []
    try:
        return sorted({int(p) for p in text.replace(" ", "").split(",") if p})
    except ValueError:
        _error(f"--reject takes warning numbers separated by commas, or none: {text!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Revisit one wave's design and warnings.")
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument("--wave", required=True, type=int, help="The wave to revisit")
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--reject", default=None, help="Warning numbers not accepted (1,3), or none"
    )
    action.add_argument(
        "--check", action="store_true", help="After synthesis: close the round, go on"
    )
    parser.add_argument(
        "--auto", action="store_true", help="With --reject: an unattended run decided"
    )
    parser.add_argument(
        "--no-model",
        action="store_true",
        help="The job runs without a model (/wave-edit --llm-mode none): no redesign can run",
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

    from src.agents.schema_design import wave_edit
    from src.agents.schema_design.wave_redesign import latest_report
    from src.storage.assignment_versioning import resolve_downstream_assignment_version
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    if not store.exists(f"{args.db}/{args.job_id}/collector/output.json"):
        _error(f"no job {args.db}/{args.job_id} under {args.artifact_root}")
    _JOB.update(store=store, db=args.db, job_id=args.job_id)
    report = latest_report(store, args.db, args.job_id)
    if report is None:
        _error("no synthesis report yet: run synthesis first")
    version = resolve_downstream_assignment_version(store, args.db, args.job_id)
    if args.check:
        _output(wave_edit.check(store, args.db, args.job_id, report, version, wave=args.wave))
        return
    if args.reject is not None:
        _output(
            wave_edit.decide(
                store,
                args.db,
                args.job_id,
                report,
                version,
                wave=args.wave,
                reject=_numbers(args.reject),
                auto=args.auto,
                llm_mode="none" if args.no_model else "external",
            )
        )
        return
    view = wave_edit.wave_view(report, args.wave)
    if args.no_model and view["status"] == "shown":
        view["message"] = wave_edit.NO_MODEL
    _output(view)


if __name__ == "__main__":
    main()
