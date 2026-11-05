#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run step 1's AWS DMS stack for ``/move`` from one inputs file (#1886).

Every subcommand prints one JSON line (exit 1 only on ``"status": "error"``):

- ``steps``, ``summary``, ``plan``: no AWS call. The job's steps; step 1's contract, sizing,
  cost and the customer-responsibility disclaimer; the stack name, the parameters and the
  equivalent ``aws cloudformation deploy`` command.
- ``preflight [--confirm <check_id>]``, ``gate``: read-only; they run
  ``scripts/check_dms_step_preflight.py`` and ``scripts/check_dms_step_gate.py`` with the
  inputs file's values and print those scripts' own line. The file's optional
  ``confirmed_checks`` (ids the customer checked themselves) become one ``--confirm`` each,
  next to any ``--confirm`` flag; an id that is not a preflight check id is refused.
- ``status [--until stack|connections|caught_up --wait-seconds N]``: read-only; the stack,
  the endpoint connections, the task and its table statistics, polled until the condition
  holds or the time is up.
- ``deploy --approve`` (create and show a change set), ``execute --change-set <name> --approve``,
  ``test --approve`` (test both endpoints), ``start --approve`` (start the task): AWS writes,
  refused without ``--approve`` and under ``MODERNIZER_CI_SANDBOX=1``. A template over
  CloudFormation's inline limit goes to the inputs file's ``template_bucket`` first (deploy
  uploads ``<stack name>/dms-step-1.yaml`` there and creates the change set from its URL).

The inputs file (default ``<db>/<job>/data-movement/step-1/inputs.json`` under the artifact
root) holds ids and ARNs only, never a secret's value: an unknown key, such as ``password``,
is refused. Calls use the ambient credentials, or ``read_profile`` (preflight, gate, status)
and ``write_profile`` (deploy, execute, test, start) from the file. The only clients are
CloudFormation, DMS, S3 (that one upload) and STS (the caller's account, so the upload
names the bucket's expected owner). No secret is ever read.

Usage:
    uv run python scripts/run_dms_step.py summary --job-id <id> --db <db>
    uv run python scripts/run_dms_step.py plan --job-id <id> --db <db> [--inputs <file>]
    uv run python scripts/run_dms_step.py deploy --job-id <id> --db <db> --approve
    uv run python scripts/run_dms_step.py status --job-id <id> --db <db> --until stack --wait-seconds 540
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, ValidationError  # noqa: E402

from src.contracts.dms_preflight import CHECK_ORDER  # noqa: E402

STEP_DIR = "data-movement/step-1"
TEMPLATE = "dms-step-1.yaml"
WRITES = ("deploy", "execute", "test", "start")
READS = ("preflight", "gate", "status")
LOCAL = ("steps", "summary", "plan")
MAX_TEMPLATE_BODY = 51_200  # CloudFormation's TemplateBody limit (a TemplateURL takes 1 MB)
MAX_WAIT_SECONDS = 540  # under the 600 s Bash timeout a command runs with
PENDING_VALIDATION = {"Pending records", "Pending validation", "Pending revalidation"}
TASK_STOPPED = {"failed", "stopped", "failed-move"}
_ARN = r"^arn:aws[a-z-]*:"
_SECRET = _ARN + r"secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+$"
_CERT = r"^$|" + _ARN + r"dms:[a-z0-9-]+:\d{12}:cert:[A-Za-z0-9-]+$"
_KMS = r"^$|" + _ARN + r"kms:[a-z0-9-]+:\d{12}:key/[A-Za-z0-9-]+$"
_NAME = r"^[A-Za-z0-9_.-]+$"
# AWS CLI profile names may hold + = , . @ / : (e.g. "team+account-ReadOnly"); no spaces or quotes.
_PROFILE = r"^[A-Za-z0-9_.+=,@/:-]{1,128}$"
# An S3 bucket name without dots: the template URL is virtual-hosted, and a dotted name breaks TLS.
_BUCKET = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"


class MoveInputs(BaseModel):
    """The ids and ARNs step 1's preflight, stack and gate take. Never a secret's value."""

    model_config = ConfigDict(extra="forbid")

    region: str = Field(..., pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    aurora_cluster_id: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9-]{0,62}$")
    source_kind: Literal["rds_instance", "aurora_cluster", "self_managed"] = "self_managed"
    source_id: str | None = Field(None, pattern=r"^[A-Za-z][A-Za-z0-9-]{0,62}$")
    source_secret_arn: str = Field(..., pattern=_SECRET)
    target_secret_arn: str = Field(..., pattern=_SECRET)
    source_certificate_arn: str = Field("", pattern=_CERT)
    target_certificate_arn: str = Field("", pattern=_CERT)
    subnet_ids: list[str] = Field(..., min_length=1)
    security_group_ids: list[str] = Field(..., min_length=1)
    source_secret_kms_key_arn: str = Field("", pattern=_KMS)
    target_secret_kms_key_arn: str = Field("", pattern=_KMS)
    replication_kms_key_arn: str = Field("", pattern=_KMS)
    secrets_manager_endpoint_override: str = Field("", pattern=r"^$|^[A-Za-z0-9.-]+$")
    instance_class: str | None = Field(None, pattern=r"^dms\.[a-z0-9]+\.[a-z0-9]+$")
    allocated_storage: int | None = Field(None, ge=5, le=6144)
    multi_az: bool | None = None
    source_database_name: str | None = Field(None, pattern=r"^[A-Za-z0-9_$-]{1,63}$")
    target_database_name: str | None = Field(None, pattern=r"^[A-Za-z0-9_$-]{1,63}$")
    stack_name: str | None = Field(None, pattern=r"^[A-Za-z][-A-Za-z0-9]{0,127}$")
    read_profile: str | None = Field(None, pattern=_PROFILE)
    write_profile: str | None = Field(None, pattern=_PROFILE)
    confirmed_checks: list[str] = Field(default_factory=list)
    template_bucket: str | None = Field(None, pattern=_BUCKET)

    def model_post_init(self, _context: Any) -> None:
        unknown = sorted(set(self.confirmed_checks) - set(CHECK_ORDER))
        if unknown:
            raise ValueError(
                f"confirmed_checks {', '.join(map(repr, unknown))}: not a preflight check id; "
                f"the ids are {', '.join(CHECK_ORDER)}"
            )
        for name, prefix in (("subnet_ids", "subnet-"), ("security_group_ids", "sg-")):
            bad = [v for v in getattr(self, name) if not v.startswith(prefix) or not v.isascii()]
            if bad:
                raise ValueError(f"{name}: {', '.join(bad)} is not a {prefix}... id")
        if self.source_kind != "self_managed" and not self.source_id:
            raise ValueError(f"source_kind {self.source_kind} needs source_id")


@dataclass(frozen=True)
class MoveClients:
    cloudformation: Any
    dms: Any
    s3: Any
    sts: Any


class MoveError(Exception):
    """A refusal: printed as ``{"status": "error", "message": ...}``."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("command", choices=[*LOCAL, *READS, *WRITES])
    p.add_argument("--job-id", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--artifact-root", default=os.environ.get("ARTIFACT_DIR", "./artifacts"))
    p.add_argument("--inputs", help="the inputs file (default: the step's inputs.json)")
    p.add_argument("--approve", action="store_true", help="the customer approved this AWS write")
    p.add_argument("--change-set", help="execute: the change set deploy created")
    p.add_argument("--confirm", action="append", default=[], metavar="CHECK_ID",
                   help="preflight: a 'confirm' row the customer checked (repeatable)")  # fmt: skip
    p.add_argument("--until", choices=["stack", "connections", "caught_up"])
    p.add_argument("--wait-seconds", type=int, default=0)
    p.add_argument("--interval-seconds", type=int, default=30)
    return p


def _step_dir(root: str, db: str, job_id: str) -> Path:
    return Path(root) / db / job_id / STEP_DIR


def load_inputs(path: Path) -> MoveInputs:
    if not path.is_file():
        raise MoveError(f"no inputs file at {path}: write it first (see /move, Check)")
    try:
        return MoveInputs.model_validate_json(path.read_text())
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in e['loc']) or 'inputs'}: {e['msg']}" for e in exc.errors()
        )
        raise MoveError(f"{path}: {problems}") from None


def stack_name(inputs: MoveInputs, job_id: str) -> str:
    from src.tools.data_movement.dms_step import task_identifier

    return inputs.stack_name or task_identifier(job_id)[:128]


# --- No AWS call -----------------------------------------------------------------------


def steps(root: str, db: str, job_id: str) -> dict[str, Any]:
    from src.contracts.data_movement_plan import DataMovementPlan
    from src.shared.contract_versioning import load_contract

    base = Path(root) / db / job_id / "data-movement"
    found = []
    for path in sorted(base.glob("step-*/plan.json")):
        try:
            plan = load_contract(
                DataMovementPlan, json.loads(path.read_text()), artifact="step plan"
            )
        except (OSError, ValueError) as exc:
            raise MoveError(f"{path}: {exc}") from None
        if plan.step is None:
            continue
        found.append({"step": plan.step, "title": plan.title, "wave": plan.wave,
                      "supported": plan.step == 1})  # fmt: skip
    if not found:
        raise MoveError(f"no step plans under {base}: run scripts/run_data_movement_plan.py first")
    return {"status": "complete", "steps": sorted(found, key=lambda s: s["step"])}


def summary(root: str, db: str, job_id: str) -> dict[str, Any]:
    """Step 1's figures, from the plan and contract the scripts wrote: quoted, never computed."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import (
        AURORA_TEMPLATE,
        CUSTOMER_RESPONSIBILITY,
        DDL_FILE,
        DMS_TEMPLATE,
        load_step1,
    )

    inputs = load_step1(LocalArtifactStore(base_dir=root), db, job_id, with_ddl=False)
    plan, contract = inputs.plan, inputs.contract
    replication = plan.replication
    warnings = [f.message for r in contract.rules for f in r.findings if f.severity == "warning"]
    blocked = [r.source_table for r in contract.rules if r.status == "blocked"]
    files = {
        name: f"{db}/{job_id}/{STEP_DIR}/{name}"
        for name in (AURORA_TEMPLATE, DDL_FILE, DMS_TEMPLATE)
    }
    missing = [n for n in files if not (Path(root) / files[n]).is_file()]
    return {
        "status": "complete",
        "title": plan.title,
        "source": {"engine": plan.source.engine, "database": plan.source.database},
        "target_engine": contract.target.engine,
        "table_count": len(contract.rules),
        "contract": {"status": contract.status, "status_reason": contract.status_reason,
                     "sha256": inputs.contract_sha256, "blocked_rules": blocked,
                     "unsupported_count": contract.unsupported_count,
                     "warning_count": len(warnings), "warnings": warnings[:10]},
        "replication_instance": None if replication is None else {
            "instance_class": replication.sizing.instance_class,
            "allocated_storage_gb": replication.sizing.allocated_storage_gb,
            "multi_az_default": replication.multi_az_default,
            "reason": replication.sizing.reason},
        "monthly_cost_usd": plan.monthly_cost_usd,
        "cost_assumptions": list(plan.cost_assumptions),
        "notes": list(plan.notes),
        "files": files,
        "files_missing": missing,
        "disclaimer": CUSTOMER_RESPONSIBILITY,
    }  # fmt: skip


def _template(root: str, db: str, job_id: str) -> tuple[Path, str, dict[str, Any]]:
    path = _step_dir(root, db, job_id) / TEMPLATE
    if not path.is_file():
        raise MoveError(f"no {TEMPLATE} at {path}: run scripts/render_dms_step.py first")
    body = path.read_text()
    params = (yaml.safe_load(body) or {}).get("Parameters") or {}
    return path, body, params


_PARAMETERS: tuple[tuple[str, str], ...] = (
    ("SubnetIds", "subnet_ids"),
    ("SecurityGroupIds", "security_group_ids"),
    ("SourceSecretArn", "source_secret_arn"),
    ("TargetSecretArn", "target_secret_arn"),
    ("SourceCertificateArn", "source_certificate_arn"),
    ("TargetCertificateArn", "target_certificate_arn"),
    ("SourceSecretKmsKeyArn", "source_secret_kms_key_arn"),
    ("TargetSecretKmsKeyArn", "target_secret_kms_key_arn"),
    ("ReplicationKmsKeyArn", "replication_kms_key_arn"),
    ("SecretsManagerEndpointOverride", "secrets_manager_endpoint_override"),
    ("InstanceClass", "instance_class"),
    ("AllocatedStorage", "allocated_storage"),
    ("MultiAZ", "multi_az"),
    ("SourceDatabaseName", "source_database_name"),
    ("TargetDatabaseName", "target_database_name"),
)


def parameters(inputs: MoveInputs, template_params: dict[str, Any]) -> dict[str, str]:
    """The stack's parameter overrides: only the values the inputs set, only names the rendered
    template declares, and every parameter without a default."""
    out: dict[str, str] = {}
    for name, field in _PARAMETERS:
        value = getattr(inputs, field)
        if value is None or value == "" or value == []:
            continue
        if name not in template_params:
            raise MoveError(f"{field}: the rendered {TEMPLATE} has no {name} parameter")
        if isinstance(value, list):
            out[name] = ",".join(value)
        elif isinstance(value, bool):
            out[name] = "true" if value else "false"
        else:
            out[name] = str(value)
    missing = [n for n, p in template_params.items() if "Default" not in p and n not in out]
    if missing:
        fields = dict(_PARAMETERS)
        raise MoveError("the inputs leave these stack parameters without a value: "
                        + ", ".join(f"{n} ({fields.get(n, n)})" for n in missing))  # fmt: skip
    return out


def deploy_plan(root: str, db: str, job_id: str, inputs: MoveInputs) -> dict[str, Any]:
    path, body, template_params = _template(root, db, job_id)
    params = parameters(inputs, template_params)
    name = stack_name(inputs, job_id)
    command = ["aws", "cloudformation", "deploy", "--template-file", str(path), "--stack-name", name,
               "--capabilities", "CAPABILITY_IAM", "--region", inputs.region, "--parameter-overrides",
               *(f"{k}={v}" for k, v in params.items())]  # fmt: skip
    if inputs.template_bucket and len(body.encode()) > MAX_TEMPLATE_BODY:
        command += ["--s3-bucket", inputs.template_bucket]
    return {"status": "ready", "stack_name": name, "region": inputs.region, "template": str(path),
            "template_bytes": len(body.encode()), "parameters": params,
            "command": shlex.join(command)}  # fmt: skip


# --- AWS: read-only ----------------------------------------------------------------------


def _stack(clients: MoveClients, name: str) -> dict[str, Any] | None:
    from botocore.exceptions import ClientError

    try:
        stacks = clients.cloudformation.describe_stacks(StackName=name)["Stacks"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise
    first: dict[str, Any] | None = stacks[0] if stacks else None
    return first


def _outputs(stack: dict[str, Any]) -> dict[str, str]:
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs") or []}


def _table_statistics(clients: MoveClients, task_arn: str) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"ReplicationTaskArn": task_arn}
    while True:
        page = clients.dms.describe_table_statistics(**kwargs)
        stats.extend(page.get("TableStatistics") or [])
        if not page.get("Marker"):
            return stats
        kwargs["Marker"] = page["Marker"]


def status(clients: MoveClients, name: str) -> dict[str, Any]:
    """Where step 1 stands: the stack, both endpoint connections, the task, its tables."""
    stack = _stack(clients, name)
    if stack is None:
        return {"status": "complete", "phase": "no_stack", "stack_name": name}
    out: dict[str, Any] = {"status": "complete", "stack_name": name,
                           "stack_status": stack["StackStatus"],
                           "stack_reason": stack.get("StackStatusReason")}  # fmt: skip
    if stack["StackStatus"].endswith("_IN_PROGRESS"):
        return {**out, "phase": "stack_in_progress"}
    outputs = _outputs(stack)
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE") or not outputs.get(
        "ReplicationTaskArn"
    ):
        return {**out, "phase": "stack_failed"}
    instance = outputs["ReplicationInstanceArn"]
    endpoints = {"source": outputs["SourceEndpointArn"], "target": outputs["TargetEndpointArn"]}
    found = (
        clients.dms.describe_connections(
            Filters=[{"Name": "replication-instance-arn", "Values": [instance]}]
        ).get("Connections")
        or []
    )
    by_arn = {c["EndpointArn"]: c for c in found}
    connections = {role: {"status": by_arn.get(arn, {}).get("Status", "not_tested"),
                          "message": by_arn.get(arn, {}).get("LastFailureMessage")}
                   for role, arn in endpoints.items()}  # fmt: skip
    tasks = (
        clients.dms.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [outputs["ReplicationTaskArn"]]}]
        ).get("ReplicationTasks")
        or []
    )
    if not tasks:
        raise MoveError(f"stack {name}: its replication task was not found")
    task = tasks[0]
    task_stats = task.get("ReplicationTaskStats") or {}
    out.update({"connections": connections, "task_status": task.get("Status"),
                "full_load_percent": task_stats.get("FullLoadProgressPercent"),
                "tables_loaded": task_stats.get("TablesLoaded"),
                "tables_loading": task_stats.get("TablesLoading"),
                "tables_queued": task_stats.get("TablesQueued"),
                "tables_errored": task_stats.get("TablesErrored"),
                "stop_reason": task.get("StopReason"),
                "last_failure": task.get("LastFailureMessage")})  # fmt: skip
    if task.get("Status") == "ready":
        statuses = {c["status"] for c in connections.values()}
        phase = ("connections_testing" if "testing" in statuses
                 else "connections_failed" if "failed" in statuses
                 else "ready_to_start" if statuses == {"successful"} else "connections_not_tested")  # fmt: skip
        return {**out, "phase": phase}
    if task.get("Status") in TASK_STOPPED:
        return {**out, "phase": "task_stopped"}
    tables = _table_statistics(clients, outputs["ReplicationTaskArn"])
    pending_tables = [t for t in tables if t.get("ValidationState") in PENDING_VALIDATION]
    pending_records = sum(int(t.get("ValidationPendingRecords") or 0) for t in tables)
    out["validation"] = {
        "tables": len(tables),
        "validated": sum(1 for t in tables if t.get("ValidationState") == "Validated"),
        "pending_tables": len(pending_tables),
        "pending_records": pending_records,
        "states": _count(t.get("ValidationState") or "unknown" for t in tables),
    }
    loaded = task_stats.get("FullLoadProgressPercent") == 100
    phase = "caught_up" if loaded and not pending_tables and not pending_records else (
        "validating" if loaded else "loading")  # fmt: skip
    return {**out, "phase": phase}


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


UNTIL: dict[str, Callable[[dict[str, Any]], bool]] = {
    "stack": lambda s: s["phase"] != "stack_in_progress",
    "connections": lambda s: s["phase"] not in ("stack_in_progress", "connections_testing"),
    "caught_up": lambda s: s["phase"] in ("caught_up", "task_stopped", "stack_failed", "no_stack"),
}


def wait_status(
    clients: MoveClients,
    name: str,
    until: str | None,
    wait_seconds: int,
    interval: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    current = status(clients, name)
    if until is None:
        return current
    deadline = clock() + wait_seconds
    while not UNTIL[until](current) and clock() + interval <= deadline:
        sleep(interval)
        current = status(clients, name)
    return {**current, "until": until, "reached": UNTIL[until](current)}


# --- AWS: writes, each after the customer's approval -----------------------------------------


def deploy(
    clients: MoveClients,
    root: str,
    db: str,
    job_id: str,
    inputs: MoveInputs,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Create a change set for the stack and describe it; nothing is executed."""
    plan = deploy_plan(root, db, job_id, inputs)
    _, body, _ = _template(root, db, job_id)
    large = plan["template_bytes"] > MAX_TEMPLATE_BODY
    if large and not inputs.template_bucket:
        raise MoveError(f"{TEMPLATE} is {plan['template_bytes']} bytes, above CloudFormation's "
                        f"{MAX_TEMPLATE_BODY}-byte inline limit: set template_bucket in the inputs "
                        "file to an S3 bucket you own in the stack's region, and deploy again")  # fmt: skip
    name = plan["stack_name"]
    stack = _stack(clients, name)
    if stack is not None and stack["StackStatus"].endswith("_IN_PROGRESS"):
        raise MoveError(
            f"stack {name} is {stack['StackStatus']}: wait for it with status --until stack"
        )
    kind = "CREATE" if stack is None or stack["StackStatus"] == "REVIEW_IN_PROGRESS" else "UPDATE"
    change_set = f"move-step-1-{now().strftime('%Y%m%d%H%M%S')}"
    template: dict[str, str] = {"TemplateBody": body}
    if large:
        template = {"TemplateURL": _upload(clients, inputs, name, body)}
    clients.cloudformation.create_change_set(
        StackName=name, ChangeSetName=change_set, ChangeSetType=kind, **template,
        Capabilities=["CAPABILITY_IAM"],
        Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in plan["parameters"].items()],
    )  # fmt: skip
    for _ in range(60):
        described = clients.cloudformation.describe_change_set(
            StackName=name, ChangeSetName=change_set
        )
        if described["Status"] in ("CREATE_COMPLETE", "FAILED"):
            break
        sleep(5)
    else:
        raise MoveError(f"change set {change_set} was not ready after 5 minutes: run deploy again")
    if described["Status"] == "FAILED":
        reason = described.get("StatusReason") or ""
        if "didn't contain changes" in reason or "No updates" in reason:
            return {"status": "no_changes", "stack_name": name, "change_set_name": change_set,
                    "message": reason}  # fmt: skip
        raise MoveError(f"change set {change_set} failed: {reason}")
    changes = [{"action": c["ResourceChange"].get("Action"),
                "logical_id": c["ResourceChange"].get("LogicalResourceId"),
                "type": c["ResourceChange"].get("ResourceType"),
                "replacement": c["ResourceChange"].get("Replacement")}
               for c in described.get("Changes") or [] if "ResourceChange" in c]  # fmt: skip
    return {"status": "change_set_ready", "stack_name": name, "change_set_name": change_set,
            "change_set_type": kind, "parameters": plan["parameters"], "changes": changes}  # fmt: skip


def _upload(clients: MoveClients, inputs: MoveInputs, name: str, body: str) -> str:
    """Put the template, and nothing else, in the customer's bucket; return its URL."""
    bucket, key = str(inputs.template_bucket), f"{name}/{TEMPLATE}"
    account = clients.sts.get_caller_identity()["Account"]
    clients.s3.put_object(Bucket=bucket, Key=key, Body=body.encode(),
                          ServerSideEncryption="AES256", ExpectedBucketOwner=account)  # fmt: skip
    return f"https://{bucket}.s3.{inputs.region}.amazonaws.com/{key}"


def execute(clients: MoveClients, name: str, change_set: str) -> dict[str, Any]:
    described = clients.cloudformation.describe_change_set(StackName=name, ChangeSetName=change_set)
    if described["Status"] != "CREATE_COMPLETE" or described.get("ExecutionStatus") != "AVAILABLE":
        raise MoveError(f"change set {change_set} is {described['Status']}/"
                        f"{described.get('ExecutionStatus')}: run deploy again")  # fmt: skip
    clients.cloudformation.execute_change_set(StackName=name, ChangeSetName=change_set)
    return {"status": "executing", "stack_name": name, "change_set_name": change_set}


def _deployed(clients: MoveClients, name: str) -> dict[str, str]:
    stack = _stack(clients, name)
    if stack is None or stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        state = "missing" if stack is None else stack["StackStatus"]
        raise MoveError(f"stack {name} is {state}: deploy it first")
    outputs = _outputs(stack)
    if not outputs.get("ReplicationTaskArn"):
        raise MoveError(f"stack {name} has no ReplicationTaskArn output")
    return outputs


def connection_tests(clients: MoveClients, name: str) -> dict[str, Any]:
    outputs = _deployed(clients, name)
    tested = []
    for role in ("Source", "Target"):
        clients.dms.test_connection(ReplicationInstanceArn=outputs["ReplicationInstanceArn"],
                                    EndpointArn=outputs[f"{role}EndpointArn"])  # fmt: skip
        tested.append(role.lower())
    return {"status": "testing", "stack_name": name, "endpoints": tested}


def start(clients: MoveClients, name: str) -> dict[str, Any]:
    current = status(clients, name)
    if current.get("task_status") != "ready":
        raise MoveError(f"the task is {current.get('task_status')}: start-replication starts a task "
                        "that has never run; read status for where it stands")  # fmt: skip
    if current["phase"] != "ready_to_start":
        raise MoveError(f"both endpoint connections must be successful first ({current['phase']}): "
                        "run test, then status --until connections")  # fmt: skip
    arn = _deployed(clients, name)["ReplicationTaskArn"]
    clients.dms.start_replication_task(
        ReplicationTaskArn=arn, StartReplicationTaskType="start-replication"
    )
    return {"status": "starting", "stack_name": name, "task_arn": arn}


# --- Entry point -------------------------------------------------------------------------


def _use_profile(profile: str | None) -> None:
    if profile:
        os.environ["AWS_PROFILE"] = profile


def _delegate(command: str, args: argparse.Namespace, inputs: MoveInputs) -> int:
    base = ["--job-id", args.job_id, "--db", args.db, "--artifact-root", args.artifact_root,
            "--region", inputs.region]  # fmt: skip
    if command == "gate":
        from scripts import check_dms_step_gate

        return check_dms_step_gate.main([*base, "--stack-name", stack_name(inputs, args.job_id)])
    from scripts import check_dms_step_preflight

    argv = [*base, "--aurora-cluster-id", inputs.aurora_cluster_id,
            "--source-secret-arn", inputs.source_secret_arn, "--target-secret-arn", inputs.target_secret_arn,
            "--source-kind", inputs.source_kind, "--subnet-ids", ",".join(inputs.subnet_ids)]  # fmt: skip
    for flag, value in (
        ("--source-id", inputs.source_id),
        ("--source-secret-kms-key-arn", inputs.source_secret_kms_key_arn),
        ("--target-secret-kms-key-arn", inputs.target_secret_kms_key_arn),
        ("--instance-class", inputs.instance_class),
        ("--source-certificate-arn", inputs.source_certificate_arn),
        ("--target-certificate-arn", inputs.target_certificate_arn),
        ("--secrets-manager-endpoint-override", inputs.secrets_manager_endpoint_override),
    ):
        if value:
            argv += [flag, value]
    for check in dict.fromkeys([*inputs.confirmed_checks, *args.confirm]):
        argv += ["--confirm", check]
    return check_dms_step_preflight.main(argv)


def main(argv: list[str] | None = None, *, clients: MoveClients | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:  # fmt: skip
    args = _parser().parse_args(argv)
    from scripts._sandbox import PATH_ARGS, SANDBOX_ENV, sandbox_violation
    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    def emit(result: dict[str, Any]) -> int:
        print(json.dumps(result))
        return 1 if result.get("status") == "error" else 0

    violation = sandbox_violation(args, path_args=(*PATH_ARGS, "inputs"))
    if violation:
        return emit({"status": "error", "message": violation})
    if args.command in WRITES and os.environ.get(SANDBOX_ENV) == "1":
        return emit({"status": "error", "message": f"{args.command} is an AWS write: refused under "
                     f"{SANDBOX_ENV}=1 (a headless CI run makes no AWS write)"})  # fmt: skip
    if args.command in WRITES and not args.approve:
        return emit({"status": "error", "message": f"{args.command} is an AWS write: pass --approve "
                     "only after the customer approved it"})  # fmt: skip
    if not 0 <= args.wait_seconds <= MAX_WAIT_SECONDS:
        return emit(
            {"status": "error", "message": f"--wait-seconds must be 0 to {MAX_WAIT_SECONDS}"}
        )
    if not 10 <= args.interval_seconds <= 300:
        return emit({"status": "error", "message": "--interval-seconds must be 10 to 300"})
    if args.command == "execute" and not args.change_set:
        return emit({"status": "error", "message": "execute needs --change-set <name> from deploy"})
    try:
        db, job_id = validate_database_name(args.db), validate_job_id(args.job_id)
        args.db, args.job_id = db, job_id
        if args.command == "steps":
            return emit(steps(args.artifact_root, db, job_id))
        if args.command == "summary":
            return emit(summary(args.artifact_root, db, job_id))
        inputs_path = (
            Path(args.inputs)
            if args.inputs
            else _step_dir(args.artifact_root, db, job_id) / "inputs.json"
        )
        inputs = load_inputs(inputs_path)
        if args.command == "plan":
            return emit(deploy_plan(args.artifact_root, db, job_id, inputs))
        _use_profile(inputs.write_profile if args.command in WRITES else inputs.read_profile)
        if args.command in ("preflight", "gate"):
            return _delegate(args.command, args, inputs)
        if clients is None:
            from src.tools.aws.credentials import AWSCredentialManager

            cred = AWSCredentialManager(region=inputs.region)
            clients = MoveClients(cloudformation=cred.client("cloudformation"),
                                  dms=cred.client("dms"), s3=cred.client("s3"),
                                  sts=cred.client("sts"))  # fmt: skip
        name = stack_name(inputs, job_id)
        if args.command == "status":
            return emit(wait_status(clients, name, args.until, args.wait_seconds,
                                    args.interval_seconds, sleep=sleep))  # fmt: skip
        if args.command == "deploy":
            return emit(deploy(clients, args.artifact_root, db, job_id, inputs, sleep=sleep))
        if args.command == "execute":
            return emit(execute(clients, name, args.change_set))
        if args.command == "test":
            return emit(connection_tests(clients, name))
        return emit(start(clients, name))
    except (MoveError, ValueError) as exc:
        return emit({"status": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - this script must always print JSON
        return emit({"status": "error", "message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    sys.exit(main())
