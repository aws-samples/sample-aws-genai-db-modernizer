#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read step 1's DMS gate: load, lag and validation evidence for the cutover decision (#1800).

On demand, read-only (DMS, CloudWatch and CloudFormation describe, get and list calls), writes
``<db>/<job>/data-movement/step-1/gate.json`` and prints one JSON status line with the
contract hash, ``cutover_decision: "customer"`` and the fixed cutover message. When to cut
over is the customer's decision. Use read-only credentials (``AWS_PROFILE``). Exit 1 only
when the gate could not run.

Usage:
    uv run python scripts/check_dms_step_gate.py --job-id <id> --db <db> --region <region> \\
      --stack-name dm-<job-id>-step-1 [--window-minutes 15] [--stale-seconds 300]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from src.shared.cutover_message import cutover_message_text  # noqa: E402
from src.tools.data_movement.dms_gate import (  # noqa: E402
    DEFAULT_STALE_SECONDS,
    DEFAULT_WINDOW_MINUTES,
    GateClients,
    GateRequest,
    write_gate,
)
from src.tools.data_movement.dms_step import DmsStepError  # noqa: E402


def main(argv: list[str] | None = None, *, clients: GateClients | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--job-id", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--artifact-root", default=os.environ.get("ARTIFACT_DIR", "./artifacts"))
    p.add_argument("--region", required=True)
    p.add_argument("--stack-name", required=True, help="the deployed dms-step-1 stack")
    p.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    p.add_argument(
        "--stale-seconds",
        type=int,
        default=DEFAULT_STALE_SECONDS,
        help="the newest latency datapoint may be this old (default: 300)",
    )
    args = p.parse_args(argv)

    from scripts._sandbox import sandbox_violation
    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id
    from src.storage.local_store import LocalArtifactStore

    def error(message: str) -> int:
        print(json.dumps({"status": "error", "message": message}))
        return 1

    violation = sandbox_violation(args)
    if violation:
        return error(violation)
    try:
        db, job_id = validate_database_name(args.db), validate_job_id(args.job_id)
    except ValueError as exc:
        return error(str(exc))
    if not 1 <= args.window_minutes <= 1440:
        return error("--window-minutes must be between 1 and 1440")
    if not 1 <= args.stale_seconds <= 86400:
        return error("--stale-seconds must be between 1 and 86400")
    if clients is None:
        from src.tools.aws.credentials import AWSCredentialManager

        clients = GateClients.from_manager(AWSCredentialManager(region=args.region))
    request = GateRequest(
        region=args.region,
        stack_name=args.stack_name,
        window_minutes=args.window_minutes,
        stale_seconds=args.stale_seconds,
    )
    try:
        path, gate = write_gate(
            LocalArtifactStore(base_dir=args.artifact_root), db, job_id, request, clients
        )
    except (DmsStepError, FileNotFoundError, ClientError, BotoCoreError) as exc:
        return error(str(exc))
    print(json.dumps({"status": gate.status, "gate": path, "contract_sha256": gate.contract_sha256,
                      "cutover_decision": gate.cutover_decision,
                      "cutover_message": cutover_message_text(gate.cutover_message_id),
                      "reasons": [f"{c.id}: {r}" for c in gate.checks for r in c.reasons],
                      "exceptions": [{"table": e.table, "kind": e.kind} for e in gate.exceptions]}))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
