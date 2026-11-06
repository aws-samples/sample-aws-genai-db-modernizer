#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run step 1's AWS DMS stack for ``/move`` from one inputs file (#1886).

Every subcommand prints one JSON line, its result (exit 1 only on ``"status": "error"``); only
``status --until caught_up`` prints progress lines before it, so the result line is always last:

- ``steps``, ``summary``, ``plan``: no AWS call. The job's steps; step 1's contract, sizing,
  cost, what it does not move and the customer-responsibility disclaimer; the stack name, the
  parameters and the equivalent ``aws cloudformation deploy`` command.
- ``preflight [--confirm <check_id>]``, ``gate``: read-only; they run
  ``scripts/check_dms_step_preflight.py`` and ``scripts/check_dms_step_gate.py`` with the
  inputs file's values and print those scripts' own line. The file's optional
  ``confirmed_checks`` (ids the customer checked themselves) become one ``--confirm`` each,
  next to any ``--confirm`` flag; an id that is not a preflight check id is refused.
- ``status [--until stack|connections|running|caught_up --wait-seconds N]``: read-only; the
  stack, the endpoint connections, the task, its table statistics and its capture lag, polled
  until the condition holds or the time is up. ``running`` waits for the task to read running
  (``/move --until moving``, #1996). ``caught_up`` also stops on ``validation_stalled``: the full
  load is done but DMS validation is not running (#1995); while it waits it prints one
  ``"status": "progress"`` line per poll before its result line (#1997). The lag comes from
  CloudWatch's ``ListMetrics`` and ``GetMetricData``, the gate's own read calls; a profile that
  may not make them leaves the lag null.
- ``deploy --approve`` (create and show a change set), ``execute --change-set <name> --approve``,
  ``test --approve`` (test both endpoints), ``start --approve`` (start the task): AWS writes,
  refused without ``--approve`` and under ``MODERNIZER_CI_SANDBOX=1``.
- ``stop --approve`` (stop the task and wait until it has stopped) and ``teardown --approve``
  (delete the step's stack once its task is not running, and wait until it is gone): the writes
  that end the step (#1896), with the same refusals. Both are no-ops when there is nothing left
  to do, act only on this job's step stack (exactly the template's six resources, and a task whose
  identifier and ``modernizer:job-id`` tag name this job), wait up to ``--wait-seconds`` (default and maximum 540) and
  print one progress line per poll on stderr.

The inputs file (default ``<db>/<job>/data-movement/step-1/inputs.json`` under the artifact
root) holds ids and ARNs only, never a secret's value: an unknown key, such as ``password``,
is refused. Calls use the ambient credentials, or ``read_profile`` (preflight, gate, status)
and ``write_profile`` (deploy, execute, test, start) from the file. No secret is ever read.

Usage:
    uv run python scripts/run_dms_step.py summary --job-id <id> --db <db>
    uv run python scripts/run_dms_step.py plan --job-id <id> --db <db> [--inputs <file>]
    uv run python scripts/run_dms_step.py deploy --job-id <id> --db <db> --approve
    uv run python scripts/run_dms_step.py status --job-id <id> --db <db> --until stack --wait-seconds 540
    uv run python scripts/run_dms_step.py stop --job-id <id> --db <db> --approve
    uv run python scripts/run_dms_step.py teardown --job-id <id> --db <db> --approve
"""

from __future__ import annotations

import argparse
import functools
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
ENDING = ("stop", "teardown")
WRITES = ("deploy", "execute", "test", "start", *ENDING)
READS = ("preflight", "gate", "status")
LOCAL = ("steps", "summary", "plan")
MAX_TEMPLATE_BODY = 51_200  # CloudFormation's TemplateBody limit
MAX_WAIT_SECONDS = 540  # under the 600 s Bash timeout a command runs with
PENDING_VALIDATION = {"Pending records", "Pending validation", "Pending revalidation"}
TASK_STOPPED = {"failed", "stopped", "failed-move"}
TASK_IDLE = TASK_STOPPED | {"ready"}  # not running: nothing to stop, and the stack may be deleted
TASK_RUNNING = {"running", "starting"}  # stop-replication-task stops these
# Validation judged stalled (#1995): tables still Not enabled this long after the full load
# finished although the task turns validation on, or this many polls over this long in a row
# with no table changing its validation state and no pending record count falling.
VALIDATION_GRACE_SECONDS = 900
STALL_POLLS = 3
STALL_SECONDS = 300
NOT_ENABLED = "Not enabled"
VALIDATION_FIX = ("check the endpoints' secrets and that the validator can connect to both "
                  "databases (the task's CloudWatch log names the validator's errors)")  # fmt: skip
# One "status": "progress" line per poll of status --until caught_up (#1997), these fields.
PROGRESS_FIELDS = ("time", "phase", "full_load_percent", "tables_loaded", "inserts_applied",
                   "updates_applied", "capture_lag_seconds", "apply_lag_seconds",
                   "validated_tables", "pending_tables", "not_enabled_tables")  # fmt: skip
LAG_WINDOW_MINUTES = 5
# What DMS's own task status means, in the words the customer sees (``dms_reports``).
_DMS_WORDS = {"stopped": "stopped (not running)", "failed": "failed (not running)",
              "failed-move": "failed-move (not running)",
              "ready": "never started (ready, not running)", "running": "running",
              "starting": "starting (running)", "stopping": "stopping (still running)"}  # fmt: skip


def dms_reports(task: dict[str, Any] | None) -> str:
    """The task's status exactly as DMS reports it; a failed task carries DMS's reason."""
    if task is None:
        return "DMS has no such task (the task is gone, not running)"
    state = task.get("Status") or "unknown"
    text = f"DMS reports the task {_DMS_WORDS.get(state, state)}"
    if state in ("failed", "failed-move"):
        reasons = [r for r in (task.get("LastFailureMessage"), task.get("StopReason")) if r]
        text += f": {'; '.join(reasons)}" if reasons else ": DMS gives no reason"
    return text


def _task_report(task: dict[str, Any] | None) -> dict[str, Any]:
    """The task fields every ending line carries: DMS's status, its words, and its reasons."""
    return {"task_status": "deleted" if task is None else task.get("Status"),
            "dms_reports": dms_reports(task),
            "stop_reason": None if task is None else task.get("StopReason"),
            "last_failure": None if task is None else task.get("LastFailureMessage")}  # fmt: skip


# The logical ids and types dms-step-1.yaml creates: stop and teardown act only on a stack that
# holds exactly these (#1896).
STEP_RESOURCES: dict[str, str] = {
    "SecretsAccessRole": "AWS::IAM::Role",  # pragma: allowlist secret
    "SubnetGroup": "AWS::DMS::ReplicationSubnetGroup",
    "ReplicationInstance": "AWS::DMS::ReplicationInstance",
    "SourceEndpoint": "AWS::DMS::Endpoint",
    "TargetEndpoint": "AWS::DMS::Endpoint",
    "ReplicationTask": "AWS::DMS::ReplicationTask",
}
JOB_TAG = "modernizer:job-id"  # on the stack (deploy) and on every resource (the template)


def stack_tags(job_id: str) -> list[dict[str, str]]:
    """The tags deploy puts on the stack itself, as the template puts them on its resources."""
    return [{"Key": JOB_TAG, "Value": job_id}, {"Key": "modernizer:step", "Value": "1"}]


_ARN = r"^arn:aws[a-z-]*:"
_SECRET = _ARN + r"secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+$"
_CERT = r"^$|" + _ARN + r"dms:[a-z0-9-]+:\d{12}:cert:[A-Za-z0-9-]+$"
_KMS = r"^$|" + _ARN + r"kms:[a-z0-9-]+:\d{12}:key/[A-Za-z0-9-]+$"
_NAME = r"^[A-Za-z0-9_.-]+$"
# AWS CLI profile names may hold + = , . @ / : (e.g. "team+account-ReadOnly"); no spaces or quotes.
_PROFILE = r"^[A-Za-z0-9_.+=,@/:-]{1,128}$"


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


@dataclass
class LagReader:
    """status's capture lag (#1997): CloudWatch, read-only, with the gate's own calls; the lag
    metrics listed once per wait. The writes never get a CloudWatch client."""

    cloudwatch: Any
    metrics: dict[str, list[dict[str, str]]] | None = None


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
    p.add_argument(
        "--until", choices=["stack", "connections", "running", "caught_up", "task_stopped"]
    )
    p.add_argument("--wait-seconds", type=int, default=None,
                   help=f"status: default 0; stop, teardown: default {MAX_WAIT_SECONDS}")  # fmt: skip
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
    """Step 1's figures, from the plan, the contract and the collection: quoted, never
    computed here (the not-moved list is ``dms_step.not_moved``, as the DDL header lists it)."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import (
        CUSTOMER_RESPONSIBILITY,
        load_step1,
        not_moved,
        step1_files,
    )

    # The whole step 1 input, as render_dms_step.py reads it: the not-moved list is the
    # DDL header's, from the same function.
    inputs = load_step1(LocalArtifactStore(base_dir=root), db, job_id)
    plan, contract = inputs.plan, inputs.contract
    replication = plan.replication
    warnings = [f.message for r in contract.rules for f in r.findings if f.severity == "warning"]
    blocked = [r.source_table for r in contract.rules if r.status == "blocked"]
    files = {
        name: f"{db}/{job_id}/{STEP_DIR}/{name}" for name in step1_files(contract.target.engine)
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
        "not_moved": [{"kind": n.kind, "count": n.count, "note": n.note} for n in not_moved(inputs)],
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
               *(f"{k}={v}" for k, v in params.items()),
               "--tags", *(f"{t['Key']}={t['Value']}" for t in stack_tags(job_id))]  # fmt: skip
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


def status(clients: MoveClients, name: str, lag: LagReader | None = None) -> dict[str, Any]:
    """Where step 1 stands: the stack, both endpoint connections, the task, its tables and,
    with a ``lag`` reader, its capture lag."""
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
                "last_failure": task.get("LastFailureMessage"),
                "dms_reports": dms_reports(task)})  # fmt: skip
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
        "not_enabled_tables": sum(1 for t in tables if t.get("ValidationState") == NOT_ENABLED),
        "states": _count(t.get("ValidationState") or "unknown" for t in tables),
    }
    out["changes_applied"] = {kind: sum(int(t.get(f"Applied{kind.title()}") or 0) for t in tables)
                              for kind in ("inserts", "updates", "deletes")}  # fmt: skip
    out.update(_capture_lag(clients, task, lag))
    loaded = task_stats.get("FullLoadProgressPercent") == 100
    phase = "caught_up" if loaded and not pending_tables and not pending_records else (
        "validating" if loaded else "loading")  # fmt: skip
    return _judge_validation({**out, "phase": phase}, task, loaded)


def validation_enabled(task: dict[str, Any]) -> bool | None:
    """The task settings' ``ValidationSettings.EnableValidation``; None when DMS gives none."""
    try:
        settings = json.loads(task.get("ReplicationTaskSettings") or "null")
    except ValueError:
        return None
    flag = ((settings or {}).get("ValidationSettings") or {}).get("EnableValidation")
    return flag if isinstance(flag, bool) else None


def _seconds_since(when: Any) -> int | None:
    if not isinstance(when, datetime):
        return None
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    return int((datetime.now(UTC) - when).total_seconds())


def _judge_validation(out: dict[str, Any], task: dict[str, Any], loaded: bool) -> dict[str, Any]:
    """``validation_running`` (#1995): None while loading or when the task turns validation off;
    False when, long after the full load, tables are still Not enabled with validation on."""
    enabled = validation_enabled(task)
    since = _seconds_since((task.get("ReplicationTaskStats") or {}).get("FullLoadFinishDate"))
    out.update(validation_enabled=enabled, full_load_done_seconds=since if loaded else None,
               validation_running=None if not loaded or enabled is False else True)  # fmt: skip
    not_enabled = out["validation"]["not_enabled_tables"]
    if (
        out["validation_running"]
        and enabled
        and not_enabled
        and (since or 0) >= VALIDATION_GRACE_SECONDS
    ):
        pending = out["validation"]["pending_tables"]
        found = f"{not_enabled} tables {NOT_ENABLED}" + (
            f" and {pending} pending" if pending else ""
        )
        return {**out, "phase": "validation_stalled", "validation_running": False,
                "validation_check": f"{found} {since} s after the full load although the task "
                                    f"turns validation on: {VALIDATION_FIX}"}  # fmt: skip
    return out


def _capture_lag(
    clients: MoveClients, task: dict[str, Any], reader: LagReader | None
) -> dict[str, Any]:
    """The latest CDCLatencySource / CDCLatencyTarget maxima of the last minutes, in seconds.

    The same read-only CloudWatch calls the gate makes; lag is progress, not proof, so a
    profile that may not read CloudWatch, or no datapoint yet, leaves both None."""
    lag: dict[str, Any] = {"capture_lag_seconds": None, "apply_lag_seconds": None}
    if reader is None:
        return lag
    from botocore.exceptions import BotoCoreError, ClientError

    from src.tools.data_movement.dms_gate import LAG_METRICS, GateClients, _latency, _task_metrics

    gate = GateClients(dms=clients.dms, cloudwatch=reader.cloudwatch,
                       cloudformation=clients.cloudformation)  # fmt: skip
    try:
        if reader.metrics is None:
            reader.metrics = _task_metrics(gate, str(task.get("ReplicationTaskArn")),
                                           str(task.get("ReplicationTaskIdentifier")))  # fmt: skip
        points = _latency(gate, reader.metrics, datetime.now(UTC), LAG_WINDOW_MINUTES)
    except (BotoCoreError, ClientError):
        return lag
    for key, metric in zip(lag, LAG_METRICS, strict=True):
        if points.get(metric):
            lag[key] = max(points[metric])[1]
    return lag


def progress_line(current: dict[str, Any]) -> dict[str, Any]:
    """One compact line per poll of status --until caught_up (#1997)."""
    validation = current.get("validation") or {}
    applied = current.get("changes_applied") or {}
    return {"status": "progress", "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "phase": current.get("phase"), "full_load_percent": current.get("full_load_percent"),
            "tables_loaded": current.get("tables_loaded"),
            "inserts_applied": applied.get("inserts"), "updates_applied": applied.get("updates"),
            "capture_lag_seconds": current.get("capture_lag_seconds"),
            "apply_lag_seconds": current.get("apply_lag_seconds"),
            "validated_tables": validation.get("validated"),
            "pending_tables": validation.get("pending_tables"),
            "not_enabled_tables": validation.get("not_enabled_tables")}  # fmt: skip


class _StallWatch:
    """No validation progress over STALL_POLLS polls and STALL_SECONDS in a row (#1995)."""

    def __init__(self) -> None:
        self.last: tuple[Any, int] | None = None
        self.since = 0.0
        self.polls = 0

    def judge(self, current: dict[str, Any], now: float) -> dict[str, Any]:
        if current.get("phase") != "validating" or not current.get("validation_running"):
            self.last = None
            return current
        v = current["validation"]
        seen = (v["states"], v["pending_records"])
        moved = self.last is None or seen[0] != self.last[0] or seen[1] < self.last[1]
        self.last = seen
        if moved:
            self.since, self.polls = now, 1
            return current
        self.polls += 1
        quiet = int(now - self.since)
        done = current.get("full_load_done_seconds")
        if (
            self.polls < STALL_POLLS
            or quiet < STALL_SECONDS
            or (done is not None and done < VALIDATION_GRACE_SECONDS)
        ):
            return current
        return {**current, "phase": "validation_stalled", "validation_running": False,
                "validation_check": f"{v['pending_tables']} tables pending with no validation "
                                    f"progress for {quiet} s: {VALIDATION_FIX}"}  # fmt: skip


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


UNTIL: dict[str, Callable[[dict[str, Any]], bool]] = {
    "stack": lambda s: s["phase"] != "stack_in_progress",
    "connections": lambda s: s["phase"] not in ("stack_in_progress", "connections_testing"),
    "running": lambda s: s.get("task_status") == "running"
    or s["phase"] in ("task_stopped", "stack_failed", "no_stack"),
    "caught_up": lambda s: s["phase"]
    in ("caught_up", "validation_stalled", "task_stopped", "stack_failed", "no_stack"),
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
    on_poll: Callable[[dict[str, Any]], None] | None = None,
    lag: LagReader | None = None,
) -> dict[str, Any]:
    """Poll until ``until`` holds or the time is up. Under ``caught_up``, ``on_poll`` gets each
    poll's progress line but the last one (the result line carries it), and a validation with
    no progress over the window ends the wait as ``validation_stalled``."""
    read: Callable[[MoveClients, str], dict[str, Any]] = functools.partial(status, lag=lag)
    done = UNTIL.get(until or "")
    if until == "task_stopped":
        read, done = task_state, lambda s: s["task_stopped"]
    watch = _StallWatch()
    current = read(clients, name)
    if until is None or done is None:
        return current
    now = clock()
    deadline = now + wait_seconds
    if until == "caught_up":
        current = watch.judge(current, now)
    while not done(current) and (now := clock()) + interval <= deadline:
        if on_poll is not None and until == "caught_up":
            on_poll(progress_line(current))
        sleep(interval)
        current = read(clients, name)
        if until == "caught_up":
            current = watch.judge(current, now + interval)  # read one interval after the check
    return {**current, "until": until, "reached": done(current)}


def task_state(clients: MoveClients, name: str) -> dict[str, Any]:
    """Read-only: whether the stack's replication task is still running, in any stack state.

    The task is read from the stack's resources whatever the stack's status (a failed or
    rolled-back stack included). ``task_stopped`` is true only when DMS reports the task not
    running (stopped, failed or never started) or the task is gone: no stack, no task resource,
    a deleted one, or a task DMS no longer has."""
    out: dict[str, Any] = {"status": "complete", "stack_name": name}
    stack = _stack(clients, name)
    if stack is None:
        return {**out, "phase": "no_stack", **_task_report(None), "task_stopped": True}
    out["stack_status"] = stack["StackStatus"]
    rows = [r for page in clients.cloudformation.get_paginator("list_stack_resources").paginate(
                StackName=name) for r in page.get("StackResourceSummaries") or []
            if r["LogicalResourceId"] == "ReplicationTask"]  # fmt: skip
    arn = rows[0].get("PhysicalResourceId") if rows else None
    if not rows or rows[0].get("ResourceStatus") == "DELETE_COMPLETE" or not arn:
        return {**out, "phase": "task_gone", **_task_report(None), "task_stopped": True}
    task = _task(clients, arn)
    if task is None:
        return {**out, "phase": "task_gone", **_task_report(None), "task_stopped": True}
    state = task.get("Status")
    return {**out, "phase": "task_stopped" if state in TASK_IDLE else "task_running",
            **_task_report(task), "task_stopped": state in TASK_IDLE}  # fmt: skip


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
    if plan["template_bytes"] > MAX_TEMPLATE_BODY:
        raise MoveError(f"{TEMPLATE} is {plan['template_bytes']} bytes, above CloudFormation's "
                        f"{MAX_TEMPLATE_BODY}-byte inline limit: deploy it yourself with "
                        "`aws cloudformation deploy --s3-bucket <bucket>` and the plan's parameters")  # fmt: skip
    name = plan["stack_name"]
    stack = _stack(clients, name)
    if stack is not None and stack["StackStatus"].endswith("_IN_PROGRESS"):
        raise MoveError(
            f"stack {name} is {stack['StackStatus']}: wait for it with status --until stack"
        )
    kind = "CREATE" if stack is None or stack["StackStatus"] == "REVIEW_IN_PROGRESS" else "UPDATE"
    change_set = f"move-step-1-{now().strftime('%Y%m%d%H%M%S')}"
    clients.cloudformation.create_change_set(
        StackName=name, ChangeSetName=change_set, ChangeSetType=kind, TemplateBody=body,
        Capabilities=["CAPABILITY_IAM"], Tags=stack_tags(job_id),
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


# --- AWS: ending the step, each after the customer's approval (#1896) ------------------------


def _progress(command: str, state: str, waited: int, wait_seconds: int) -> None:
    print(f"{command}: the {state} (waited {waited} s of {wait_seconds} s)", file=sys.stderr)


def _step_stack(clients: MoveClients, name: str) -> dict[str, Any] | None:
    """The step's stack, or None when it is gone; refuses one that is changing."""
    stack = _stack(clients, name)
    if (
        stack is not None
        and stack["StackStatus"].endswith("_IN_PROGRESS")
        and (stack["StackStatus"] != "DELETE_IN_PROGRESS")
    ):
        raise MoveError(
            f"stack {name} is {stack['StackStatus']}: wait for it with status --until stack"
        )
    return stack


def _stack_tag_is_this_jobs(name: str, stack: dict[str, Any], job_id: str) -> bool:
    """True when the stack carries this job's tag; refuses one that carries another job's."""
    values = [t.get("Value") for t in stack.get("Tags") or [] if t.get("Key") == JOB_TAG]
    if values and values != [job_id]:
        raise _not_the_step(name, f"the stack's {JOB_TAG} tag is {values}, not [{job_id!r}]")
    return values == [job_id]


def _not_the_step(name: str, why: str) -> MoveError:
    return MoveError(f"stack {name} is not this job's step 1 DMS stack: {why}; stop and teardown "
                     "only end the stack dms-step-1.yaml created for this job (end any other "
                     "stack yourself)")  # fmt: skip


def _step_resources(clients: MoveClients, name: str) -> dict[str, dict[str, Any]]:
    """Every resource of the stack, page by page; exactly the six ``dms-step-1.yaml`` creates."""
    found: dict[str, dict[str, Any]] = {}
    pages = clients.cloudformation.get_paginator("list_stack_resources").paginate(StackName=name)
    for page in pages:
        for r in page.get("StackResourceSummaries") or []:
            found[r["LogicalResourceId"]] = r
    kinds = {logical: r["ResourceType"] for logical, r in found.items()}
    if kinds != STEP_RESOURCES:
        extra = sorted(f"{k} ({v})" for k, v in kinds.items() if STEP_RESOURCES.get(k) != v)
        missing = sorted(k for k in STEP_RESOURCES if kinds.get(k) != STEP_RESOURCES[k])
        parts = [f"unexpected: {', '.join(extra)}"] if extra else []
        parts += [f"missing: {', '.join(missing)}"] if missing else []
        raise _not_the_step(
            name, f"its resources are not the six of dms-step-1.yaml ({'; '.join(parts)})"
        )
    return found


def _task(clients: MoveClients, arn: str) -> dict[str, Any] | None:
    from botocore.exceptions import ClientError

    try:
        found: list[dict[str, Any]] | None = clients.dms.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [arn]}]
        ).get("ReplicationTasks")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundFault":
            return None
        raise
    return found[0] if found else None


def _check_task_is_this_jobs(
    clients: MoveClients, name: str, task: dict[str, Any], job_id: str
) -> None:
    """The task's identifier and its ``modernizer:job-id`` tag both name this job."""
    from src.tools.data_movement.dms_step import task_identifier

    expected = task_identifier(job_id)
    if task.get("ReplicationTaskIdentifier") != expected:
        raise _not_the_step(
            name, f"its task is {task.get('ReplicationTaskIdentifier')!r}, not {expected!r}"
        )
    tags = clients.dms.list_tags_for_resource(ResourceArn=task["ReplicationTaskArn"])
    job_tags = [
        t.get("Value") for t in tags.get("TagList") or [] if t.get("Key") == "modernizer:job-id"
    ]
    if job_tags != [job_id]:
        raise _not_the_step(
            name, f"its task's modernizer:job-id tag is {job_tags or 'missing'}, not [{job_id!r}]"
        )


def _live_task(
    clients: MoveClients, name: str, resources: dict[str, dict[str, Any]], job_id: str
) -> dict[str, Any] | None:
    """The stack's task, checked to be this job's; None once the task is gone."""
    resource = resources["ReplicationTask"]
    if resource.get("ResourceStatus") == "DELETE_COMPLETE":
        return None
    arn = resource.get("PhysicalResourceId")
    if not arn:
        raise _not_the_step(name, "its ReplicationTask has no physical id")
    task = _task(clients, arn)
    if task is not None:
        _check_task_is_this_jobs(clients, name, task, job_id)
    return task


def stop(
    clients: MoveClients,
    name: str,
    job_id: str,
    wait_seconds: int,
    interval: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Stop the step's task and wait until it has stopped; a task not running is left as is."""
    stack = _step_stack(clients, name)
    if stack is None:
        return {"status": "no_stack", "stack_name": name}
    _stack_tag_is_this_jobs(name, stack, job_id)
    task = _live_task(clients, name, _step_resources(clients, name), job_id)
    if task is None:
        return {"status": "no_task", "stack_name": name, **_task_report(None)}
    arn = task["ReplicationTaskArn"]
    state = task.get("Status")
    out = {"stack_name": name, "task_arn": arn}
    if state in TASK_IDLE:
        return {"status": "stopped", **out, **_task_report(task), "already_stopped": True}
    if state in TASK_RUNNING:
        clients.dms.stop_replication_task(ReplicationTaskArn=arn)
        state = "stopping"
    elif state != "stopping":
        raise MoveError(f"{dms_reports(task)}: wait until it is running or stopped, then run "
                        "stop again (status shows where it stands)")  # fmt: skip
    waited = 0
    current: dict[str, Any] | None = task
    while state not in TASK_IDLE and waited + interval <= wait_seconds:
        sleep(interval)
        waited += interval
        current = _task(clients, arn)
        state = "deleted" if current is None else current.get("Status")
        _progress("stop", f"task is {state}", waited, wait_seconds)
        if current is None:
            break
    reached = current is None or state in TASK_IDLE
    return {"status": "stopped" if reached else "stopping", **out, **_task_report(current),
            "already_stopped": False, "reached": reached, "waited_seconds": waited}  # fmt: skip


def _resources_are_this_jobs(
    clients: MoveClients, name: str, resources: dict[str, dict[str, Any]], job_id: str
) -> bool:
    """True when the stack's replication instance or endpoints still exist and every one of them
    carries this job's tag; refuses one that carries another job's tag or none."""
    from botocore.exceptions import ClientError

    proven = False
    for logical in ("ReplicationInstance", "SourceEndpoint", "TargetEndpoint"):
        resource = resources[logical]
        arn = resource.get("PhysicalResourceId")
        if resource.get("ResourceStatus") == "DELETE_COMPLETE" or not arn:
            continue
        try:
            tags = clients.dms.list_tags_for_resource(ResourceArn=arn).get("TagList") or []
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundFault":
                continue
            raise
        values = [t.get("Value") for t in tags if t.get("Key") == JOB_TAG]
        if values != [job_id]:
            raise _not_the_step(
                name, f"its {logical}'s {JOB_TAG} tag is {values or 'missing'}, not [{job_id!r}]"
            )
        proven = True
    return proven


def teardown(
    clients: MoveClients,
    name: str,
    job_id: str,
    wait_seconds: int,
    interval: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Delete the step's stack once its task is not running, and wait until it is gone.

    At most one ``DeleteStack`` per run, on the step's own stack only, proven from AWS state and
    never from a local file: exactly the six resources of ``dms-step-1.yaml``, no other job's tag
    on the stack, and a task whose identifier and tag name this job or, once the task is gone, a
    replication instance or endpoint carrying this job's tag (or this job's stack tag). A stack
    already being deleted is only waited on; a ``DELETE_FAILED`` seen while waiting is an error,
    so a new delete needs a new run (and a new approval)."""
    stack = _step_stack(clients, name)
    if stack is None:
        return {"status": "deleted", "stack_name": name, "already_deleted": True}
    tagged = _stack_tag_is_this_jobs(name, stack, job_id)
    resources = _step_resources(clients, name)
    task = _live_task(clients, name, resources, job_id)
    if task is None and not (_resources_are_this_jobs(clients, name, resources, job_id) or tagged):
        raise _not_the_step(name, "its task is gone and no replication instance, endpoint or "
                                  f"stack tag shows {job_id!r} owns it")  # fmt: skip
    if task is not None and task.get("Status") not in TASK_IDLE:
        raise MoveError(f"{dms_reports(task)}: run stop first; CloudFormation "
                        "cannot delete a running replication task")  # fmt: skip
    # The stack checked above, by its id: a stack re-created under the same name is not it.
    stack_id = stack.get("StackId") or name
    if stack["StackStatus"] != "DELETE_IN_PROGRESS":
        clients.cloudformation.delete_stack(StackName=stack_id)
    waited, state = 0, "DELETE_IN_PROGRESS"
    # What DMS reported for the task when the delete was decided (gone: no such task).
    out = {"stack_name": name, "stack_id": stack_id, "already_deleted": False,
           **_task_report(task)}  # fmt: skip
    while waited + interval <= wait_seconds:
        sleep(interval)
        waited += interval
        current = _stack(clients, stack_id)
        state = "deleted" if current is None else current["StackStatus"]
        state = "deleted" if state == "DELETE_COMPLETE" else state  # by id, a gone stack stays
        _progress("teardown", f"stack is {state}", waited, wait_seconds)
        if state == "deleted":
            break
        if state == "DELETE_FAILED":
            # Not an error: the same state status --until stack reports; a new delete needs a
            # new run and a new approval.
            return {"status": "delete_failed", **out, "reached": False, "waited_seconds": waited,
                    "stack_reason": (current or {}).get("StackStatusReason")}  # fmt: skip
    reached = state == "deleted"
    return {"status": "deleted" if reached else "deleting", **out, "reached": reached,
            "waited_seconds": waited}  # fmt: skip


# --- Entry point -------------------------------------------------------------------------


def _print_progress(line: dict[str, Any]) -> None:
    print(json.dumps(line), flush=True)


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
         sleep: Callable[[float], None] = time.sleep, cloudwatch: Any = None) -> int:  # fmt: skip
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
    if args.wait_seconds is None:
        args.wait_seconds = MAX_WAIT_SECONDS if args.command in ENDING else 0
    if not 0 <= args.wait_seconds <= MAX_WAIT_SECONDS:
        return emit(
            {"status": "error", "message": f"--wait-seconds must be 0 to {MAX_WAIT_SECONDS}"}
        )
    if not 10 <= args.interval_seconds <= 300:
        return emit({"status": "error", "message": "--interval-seconds must be 10 to 300"})
    if args.until and args.command != "status":
        return emit(
            {"status": "error", "message": f"--until is a status option, not one of {args.command}"}
        )
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
            clients = MoveClients(
                cloudformation=cred.client("cloudformation"), dms=cred.client("dms")
            )
            if args.command == "status":
                cloudwatch = cred.client("cloudwatch")
        name = stack_name(inputs, job_id)
        if args.command == "status":
            return emit(wait_status(clients, name, args.until, args.wait_seconds,
                                    args.interval_seconds, sleep=sleep, on_poll=_print_progress,
                                    lag=None if cloudwatch is None else LagReader(cloudwatch)))  # fmt: skip
        if args.command == "deploy":
            return emit(deploy(clients, args.artifact_root, db, job_id, inputs, sleep=sleep))
        if args.command == "execute":
            return emit(execute(clients, name, args.change_set))
        if args.command == "test":
            return emit(connection_tests(clients, name))
        if args.command == "stop":
            return emit(stop(clients, name, job_id, args.wait_seconds, args.interval_seconds,
                             sleep=sleep))  # fmt: skip
        if args.command == "teardown":
            return emit(
                teardown(clients, name, job_id, args.wait_seconds, args.interval_seconds,
                         sleep=sleep)  # fmt: skip
            )
        return emit(start(clients, name))
    except (MoveError, ValueError) as exc:
        return emit({"status": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - this script must always print JSON
        return emit({"status": "error", "message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    sys.exit(main())
