#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Check, read-only, that step 1's DMS stack can deploy and run (#1799).

Reads the job's step 1 plan and contract, calls only describe, get and list AWS APIs
(never a secret's value), stops at the first failure and writes
``<db>/<job>/data-movement/step-1/preflight.json``. Use read-only credentials
(``AWS_PROFILE``). Prints one JSON status line with ``status`` ``pass``, ``fail`` or
``needs_confirmation`` (exit 0), or ``error`` when the preflight could not run (exit 1).
``--confirm <check_id>`` (repeatable) records that you checked a ``confirm`` row yourself;
``--confirm-source-checklist`` is the same as ``--confirm source_cdc``.

Usage:
    uv run python scripts/check_dms_step_preflight.py --job-id <id> --db <db> --region <region> \\
      --aurora-cluster-id <cluster> --source-secret-arn <arn> --target-secret-arn <arn> \\
      [--source-kind rds_instance|aurora_cluster|self_managed --source-id <id>] \\
      [--source-certificate-arn <arn> --target-certificate-arn <arn>] \\
      [--subnet-ids subnet-a,subnet-b] [--confirm source_cdc]
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

from src.contracts.dms_preflight import CHECK_ORDER  # noqa: E402
from src.tools.data_movement.dms_preflight import (  # noqa: E402
    PreflightClients,
    PreflightRequest,
    write_preflight,
)
from src.tools.data_movement.dms_step import DmsStepError  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--job-id", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--artifact-root", default=os.environ.get("ARTIFACT_DIR", "./artifacts"))
    p.add_argument("--region", required=True)
    p.add_argument("--aurora-cluster-id", required=True)
    p.add_argument("--source-secret-arn", required=True)
    p.add_argument("--target-secret-arn", required=True)
    p.add_argument("--source-secret-kms-key-arn", default="")
    p.add_argument("--target-secret-kms-key-arn", default="")
    p.add_argument(
        "--instance-class",
        help="the class you pass as the stack's InstanceClass, if not the plan's",
    )
    p.add_argument(
        "--source-kind",
        choices=["rds_instance", "aurora_cluster", "self_managed"],
        default="self_managed",
    )
    p.add_argument("--source-id", help="the source RDS instance or Aurora cluster identifier")
    p.add_argument(
        "--source-certificate-arn", default="", help="the source endpoint's DMS certificate ARN"
    )
    p.add_argument(
        "--target-certificate-arn", default="", help="the target endpoint's DMS certificate ARN"
    )
    p.add_argument("--subnet-ids", default="", help="the stack's SubnetIds, comma separated")
    p.add_argument(
        "--secrets-manager-endpoint-override",
        default="",
        help="the stack's SecretsManagerEndpointOverride",
    )
    p.add_argument("--confirm", action="append", default=[], metavar="CHECK_ID",
                   help="record that you checked this 'confirm' row yourself (repeatable)")  # fmt: skip
    p.add_argument(
        "--confirm-source-checklist", action="store_true", help="alias for --confirm source_cdc"
    )
    return p


def main(argv: list[str] | None = None, *, clients: PreflightClients | None = None) -> int:
    args = _parser().parse_args(argv)
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
    confirmed = set(args.confirm) | ({"source_cdc"} if args.confirm_source_checklist else set())
    unknown = sorted(confirmed - set(CHECK_ORDER))
    if unknown:
        return error(
            f"--confirm {', '.join(unknown)}: not a check id; the ids are {', '.join(CHECK_ORDER)}"
        )
    if args.source_kind != "self_managed" and not args.source_id:
        return error(f"--source-kind {args.source_kind} needs --source-id")
    request = PreflightRequest(
        region=args.region, aurora_cluster_id=args.aurora_cluster_id, source_secret_arn=args.source_secret_arn,
        target_secret_arn=args.target_secret_arn, source_secret_kms_key_arn=args.source_secret_kms_key_arn,
        target_secret_kms_key_arn=args.target_secret_kms_key_arn, instance_class=args.instance_class,
        source_kind=args.source_kind, source_id=args.source_id,
        source_certificate_arn=args.source_certificate_arn, target_certificate_arn=args.target_certificate_arn,
        subnet_ids=tuple(x.strip() for x in args.subnet_ids.split(",") if x.strip()),
        secrets_manager_endpoint_override=args.secrets_manager_endpoint_override,
    )  # fmt: skip
    if clients is None:
        from src.tools.aws.credentials import AWSCredentialManager

        clients = PreflightClients.from_manager(AWSCredentialManager(region=args.region))
    try:
        path, result = write_preflight(
            LocalArtifactStore(base_dir=args.artifact_root),
            db,
            job_id,
            request,
            clients,
            confirmed=confirmed,
        )
    except (DmsStepError, FileNotFoundError) as exc:
        return error(str(exc))
    failed = next((c for c in result.checks if c.status == "fail"), None)
    to_confirm = [c for c in result.checks if c.status == "confirm"]
    notes = [i for c in result.checks if c.status == "pass" and "confirmed_by" not in c.evidence
             for i in c.checklist]  # fmt: skip
    if args.source_kind == "self_managed":
        notes.append(
            "--source-kind is the default self_managed, so the source's change capture settings are a checklist "
            "only; pass --source-kind rds_instance or aurora_cluster with --source-id to have them read."
        )
    message = failed.message if failed else (
        f"Check {', '.join(c.id for c in to_confirm)} yourself, then run again with "
        + " ".join(f"--confirm {c.id}" for c in to_confirm) + ".") if to_confirm else None  # fmt: skip
    print(json.dumps({"status": result.status, "preflight": path,
                      "first_failure": failed.id if failed else None, "message": message,
                      "to_confirm": [c.id for c in to_confirm], "notes": notes}))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
