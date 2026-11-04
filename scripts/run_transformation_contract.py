#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Write step 1's transformation contract for a finished local job (#1764).

Reads the job's latest synthesis report, its assignment, the collector output and
the schema designs, and writes ``<db>/<job>/transformation-contract/v<N>/step-1.json``
(``src.contracts.transformation_contract.StepContract``), ``step-1.sha256`` and
``findings.json``. Deterministic, no model and no AWS call. Prints one JSON status line.
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
    from src.tools.transformation_contract.write import write_step1

    store = LocalArtifactStore(base_dir=artifact_root)
    try:
        result = write_step1(store, db, job_id)
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
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a step 1 transformation contract.")
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
