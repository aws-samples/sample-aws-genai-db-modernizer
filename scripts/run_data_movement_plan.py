#!/usr/bin/env python3
"""Write one data movement plan per migration wave for a finished local job (#1413).

Reads the job's latest synthesis report, its assignment, the collector output and
the schema designs, and writes ``<db>/<job>/data-movement/wave-<n>/plan.json``
(``src.contracts.data_movement_plan.DataMovementPlan``). Deterministic, no AWS call.
Prints one JSON status line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(job_id: str, db: str, artifact_root: str) -> dict:
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.plan import write_plans

    store = LocalArtifactStore(base_dir=artifact_root)
    try:
        written = write_plans(store, db, job_id)
    except FileNotFoundError as e:
        return {
            "status": "error",
            "message": (
                f"No finished job at {db}/{job_id} ({e}): run the assessment through "
                "synthesis first."
            ),
        }
    except Exception as e:  # noqa: BLE001 - this script must always print JSON
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    if not written:
        return {"status": "error", "message": f"{db}/{job_id} has no migration waves to plan."}
    return {"status": "complete", "wave_count": len(written), "plans": written}


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a data movement plan per migration wave.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
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

    result = run(args.job_id, args.db, args.artifact_root)
    print(json.dumps(result))
    sys.exit(0 if result["status"] != "error" else 1)


if __name__ == "__main__":
    main()
