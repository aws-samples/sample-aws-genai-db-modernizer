#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Render the customer deliverables for a completed local job.

Writes the job's latest synthesis version into the customer folder
``artifacts/<db>/<job>/deliverables/`` under stable names (the executive report, the
engineering report, the assessment data JSON, and the detailed engineering report with
the full routing detail, read from the job's saved routing records) with an ``index.md``
(``src.report.deliverables.FOLDER_ENTRIES``). Nothing is written next to the
synthesis ``report.json``: model I/O, traces and the versioned folders stay as
the pipeline left them. Deterministic, no LLM calls.

Usage:
    uv run python scripts/run_report.py --job-id <id> --db <name>
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(
    job_id: str,
    db: str,
    artifact_root: str,
    assignment_version: int | None,
    *,
    graph: bool = True,
) -> dict:
    """Render the deliverables (``src.report.deliverables_folder``, #1991: the API runs
    the same after an assumptions change); with ``graph``, also prebuild the job's
    context graph (the result's ``graph`` field: ``built``, ``skipped`` or ``failed``)."""
    from src.report.deliverables_folder import render_job_deliverables

    return render_job_deliverables(job_id, db, artifact_root, assignment_version, graph=graph)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render deliverables for a local job.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Synthesis version to render; only the latest is rendered (default: the latest)",
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
        print(json.dumps({"status": "error", "message": violation}))
        sys.exit(1)

    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    try:
        args.db = validate_database_name(args.db)
        args.job_id = validate_job_id(args.job_id)
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    result = run(args.job_id, args.db, args.artifact_root, args.assignment_version)
    print(json.dumps(result))
    sys.exit(0 if result["status"] != "error" else 1)


if __name__ == "__main__":
    main()
