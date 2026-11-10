#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run step 1's AWS DMS stack for ``/move`` from one inputs file (#1886).

Every subcommand prints one JSON line, its result (exit 1 only on ``"status": "error"``); only
``status --until caught_up`` prints progress lines before it, so the result line is always last:

- ``steps``, ``summary``, ``plan``: no AWS call. The job's steps; step 1's contract, sizing,
  cost, what it does not move and the customer-responsibility disclaimer; the stack name, the
  parameters and the equivalent ``aws cloudformation deploy`` command, and ``empty_target``:
  the question, before the task starts, whether every step 1 table is empty on Aurora, with a
  read-only query for the customer to run (#2064).
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
  may not make them leaves the lag null. Once the task has run, ``source_retention`` (#2060)
  judges the source's change log and free storage from its RDS metrics (``GetMetricData``
  again; thresholds in ``src/tools/data_movement/source_retention.py``), "not measured" for a
  self-managed source or a metric with no datapoint; ``caught_up`` also stops when it reads
  ``critical``, and its result line carries ``stopped_for`` whenever the last reading is
  critical, ``reached`` or not; and a ``task_stopped`` line names what the stopped task costs
  the source (``source_retention.stopped_task``).
- ``deploy --approve`` (create and show a change set), ``execute --change-set <name> --approve``,
  ``test --approve`` (test both endpoints), ``start --approve`` (start the task): AWS writes,
  refused without ``--approve`` and under ``MODERNIZER_CI_SANDBOX=1``. A template over
  CloudFormation's inline limit goes to the inputs file's ``template_bucket`` first (deploy
  uploads ``<stack name>/dms-step-1.yaml`` there and creates the change set from its URL).
- ``stop --approve`` (stop the task and wait until it has stopped) and ``teardown --approve``
  (delete the step's stack once its task is not running, and wait until it is gone): the writes
  that end the step (#1896), with the same refusals. Both are no-ops when there is nothing left
  to do, act only on this job's step stack (exactly the template's six resources, and a task whose
  identifier and ``modernizer:job-id`` tag name this job), wait up to ``--wait-seconds`` (default and maximum 540) and
  print one progress line per poll on stderr. ``teardown``'s line carries ``source_reminder``
  and ``source_followups``: what is left on the source (on PostgreSQL, the replication slot DMS
  leaves behind, with the read-only query to find it and the statement that drops it; #2060).
- ``routines`` (#1947): once ``status`` reads the full load at 100% (read-only), write step 1's
  routine scripts for the customer to run: ``routines-deploy.sql`` (the carried routines in
  dependency order; PostgreSQL triggers created disabled; MySQL triggers and events left to
  the cutover script), the read-only ``routines-check.sql`` and ``routines.md``.
  ``routines-check`` (no AWS call): read the customer's ``routines-check.tsv`` strictly and write
  ``routines-check.json``, which the gate's routines check reads. Neither connects to a database.
- ``restart-validation --approve`` (#2048): when Prove reports ``validation_stalled``, stop this
  job's step 1 task, wait until it has stopped, start it again with ``resume-processing`` (CDC
  resumes from its checkpoint, nothing reloads) and wait until it runs; this restarts the DMS
  validator. The same refusals and ownership checks as ``stop``; both waits share one
  ``--wait-seconds`` (default and maximum 540); a task that is neither running nor stopped is
  refused, and one that has not stopped by the deadline is not resumed.
- ``resume --approve`` and ``retry --approve`` (#2061): Prove's way on from a task DMS stopped.
  ``resume`` starts this job's stopped task again with ``resume-processing`` only once its full
  load finished (every table ``Table completed``): CDC resumes from its checkpoint and nothing
  reloads. ``retry`` reloads this job's failed task, or a stopped one whose full load did not
  finish, from the start (``reload-target``) only when DMS's statistics show the task never
  wrote to Aurora (no full load ever started, every table counter 0). When neither may run,
  both refuse with ``"kind": "target_may_hold_rows"``, the step's tables and the customer's own
  steps (empty them on Aurora, then reload the task themselves): nothing here connects to a
  database. Each points at the other only when the other would run. Both wait until the task
  runs, with the same refusals and ownership checks as ``stop``.
- ``reload-tables --approve`` and ``revalidate --approve`` (#2064): Prove's way on from a
  ``table_errors`` phase or from validation failures, on this job's running task only.
  ``reload-tables`` reloads the errored tables (``ReloadTables``, ``data-reload``) only when
  DMS's statistics show none of them was ever written to; otherwise it refuses with
  ``"kind": "target_may_hold_rows"`` and the customer's ``table_steps`` (empty them on Aurora,
  then reload them). ``revalidate`` revalidates the tables with failed or suspended records
  (``validate-only``, which reloads no data). The same refusals and ownership checks as ``stop``.
- ``verify-scripts`` and ``verify`` (#1998, optional; no AWS call, never connects to a database):
  write step 1's read-only comparison scripts (``verify-source.sql``, ``verify-target.sql``,
  ``verify.md``) for the customer to run; read their output files strictly and write
  ``verify.json`` (per table match, changing, differ, not_compared or missing). It never
  changes the gate.
- A stack that is not plainly deployed (#2063, ``src/tools/data_movement/stack_states.py``):
  ``status`` reads ``stack_create_failed`` for a create that failed or rolled back,
  ``stack_in_review`` for a change set never executed (``--change-set`` names the one Move just
  executed, which is waited on while CloudFormation starts it) and ``stack_failed`` for a stuck stack,
  each with ``stack_failure`` (the first resource that failed, from ``DescribeStackEvents``,
  read-only, with a fix ``hint`` for a common cause) and ``stack_action`` (what the customer
  can do). A stack whose update rolled back (``UPDATE_ROLLBACK_COMPLETE``) is still deployed:
  its task is judged as usual. ``deploy`` sends no change set to a stack CloudFormation cannot
  update (``"kind": "stack_not_updatable"``); ``teardown`` also ends this job's stack whose
  create never completed, only with this job's own stack tag; no write ever turns termination
  protection off, and ``teardown`` sends no delete while it is on.
- Every line about the task carries ``hint`` (the fix for a failure DMS describes in a known
  way, ``src/tools/data_movement/dms_failures.py``; None for any other). ``status``'s line
  carries ``recovery``: ``resume`` or ``retry`` only when that command would run, by the same
  rule they use (``recovery_plan``, with the table states), else None; for a stopped or failed
  task neither may reload, it carries ``recovery_refused``, ``tables_to_empty`` and
  ``customer_steps`` instead. A running task's line carries ``table_problems`` (errored tables
  and validation failures, names and counts only, never a value; ``table_errors.py``); an
  errored table ends ``caught_up``'s wait as ``table_errors``, with ``table_recovery``
  (``reload-tables`` only when it would run) or ``table_steps``, and validation failures carry
  ``validation_failures_check`` (read-only SQL on DMS's failure table, for the customer). Credentials that are missing or have expired print
  ``"kind": "credentials"``, with what this run had already changed.

The inputs file (default ``<db>/<job>/data-movement/step-1/inputs.json`` under the artifact
root) holds ids and ARNs only, never a secret's value: an unknown key, such as ``password``,
is refused. Calls use the ambient credentials, or ``read_profile`` (preflight, gate, status)
and ``write_profile`` (deploy, execute, test, start) from the file. The only clients are
CloudFormation, DMS, S3 (that one upload), STS (the caller's account, so the upload
names the bucket's expected owner) and, for status's reads only, CloudWatch. No secret is ever read.

Usage:
    uv run python scripts/run_dms_step.py summary --job-id <id> --db <db>
    uv run python scripts/run_dms_step.py plan --job-id <id> --db <db> [--inputs <file>]
    uv run python scripts/run_dms_step.py deploy --job-id <id> --db <db> --approve
    uv run python scripts/run_dms_step.py status --job-id <id> --db <db> --until stack --wait-seconds 540
    uv run python scripts/run_dms_step.py stop --job-id <id> --db <db> --approve
    uv run python scripts/run_dms_step.py restart-validation --job-id <id> --db <db> --approve
    uv run python scripts/run_dms_step.py resume --job-id <id> --db <db> --approve
    uv run python scripts/run_dms_step.py retry --job-id <id> --db <db> --approve
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
RESTART = "restart-validation"  # Prove's stop-and-resume of a stalled validator (#2048)
RESUME, RETRY = "resume", "retry"  # Prove's way on from a stopped or a failed task (#2061)
RECOVER = (RESUME, RETRY)
RELOAD_TABLES, REVALIDATE = "reload-tables", "revalidate"  # Prove's per-table writes (#2064)
TABLE_RECOVER = (RELOAD_TABLES, REVALIDATE)
WRITES = ("deploy", "execute", "test", "start", RESTART, *RECOVER, *TABLE_RECOVER, *ENDING)
WAITS = (*ENDING, RESTART, *RECOVER)  # the writes that wait, by default up to MAX_WAIT_SECONDS
ROUTINES = "routines"  # writes step 1's routine scripts once the full load is done (#1947)
ROUTINES_CHECK = "routines-check"  # reads the customer's check output; no AWS call (#1947)
READS = ("preflight", "gate", "status", ROUTINES)
VERIFY_SCRIPTS, VERIFY = "verify-scripts", "verify"  # optional, no AWS call (#1998)
LOCAL = ("steps", "summary", "plan", VERIFY_SCRIPTS, VERIFY, ROUTINES_CHECK)
MAX_TEMPLATE_BODY = 51_200  # CloudFormation's TemplateBody limit (a TemplateURL takes 1 MB)
MAX_WAIT_SECONDS = 540  # under the 600 s Bash timeout a command runs with
TASK_STOPPED = {"failed", "stopped", "failed-move"}
TASK_IDLE = TASK_STOPPED | {"ready"}  # not running: nothing to stop, and the stack may be deleted
TASK_RUNNING = {"running", "starting"}  # stop-replication-task stops these
# Validation judged stalled (#1995): tables still Not enabled this long after the full load
# finished although the task turns validation on, or this many polls over this long in a row
# with no table changing its validation state and no pending record count falling.
VALIDATION_GRACE_SECONDS = 900
STALL_POLLS = 3
STALL_SECONDS = 300
NOT_ENABLED = "Not enabled"  # as DMS spells it, for messages; compared case-folded (#2042)
VALIDATION_FIX = ("check the endpoints' secrets and that the validator can connect to both "
                  "databases (the task's CloudWatch log names the validator's errors)")  # fmt: skip
# One "status": "progress" line per poll of status --until caught_up (#1997), these fields.
PROGRESS_FIELDS = ("time", "phase", "full_load_percent", "tables_loaded", "inserts_applied",
                   "updates_applied", "capture_lag_seconds", "apply_lag_seconds",
                   "validated_tables", "pending_tables", "not_enabled_tables",
                   "source_retention", "source_warning", "tables_errored", "errored_tables",
                   "errored_tables_not_shown", "validation_failures",
                   "validation_failures_not_shown")  # fmt: skip
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
    """The task fields every ending line carries: DMS's status, its words, its reasons and, for a
    failure the catalog knows, its fix ``hint`` (#2061; None otherwise)."""
    from src.tools.data_movement.dms_failures import failure_hint

    return {"task_status": "deleted" if task is None else task.get("Status"),
            "dms_reports": dms_reports(task),
            "stop_reason": None if task is None else task.get("StopReason"),
            "last_failure": None if task is None else task.get("LastFailureMessage"),
            "hint": failure_hint(task)}  # fmt: skip


def full_load_finished(task: dict[str, Any]) -> bool:
    """The task's own statistics say its full load finished: ``FullLoadProgressPercent`` 100, a
    ``FullLoadFinishDate`` ("the date the replication task full load was completed", DMS API
    Reference, ReplicationTaskStats) and no table loading, queued or errored."""
    stats = task.get("ReplicationTaskStats") or {}
    return (stats.get("FullLoadProgressPercent") == 100
            and stats.get("FullLoadFinishDate") is not None
            and not any(stats.get(k) for k in ("TablesLoading", "TablesQueued", "TablesErrored")))  # fmt: skip


def recovery_plan(
    task: dict[str, Any] | None, tables: list[dict[str, Any]]
) -> tuple[str | None, list[str]]:
    """The one rule ``status``'s ``recovery``, ``resume`` and ``retry`` share (#2061): (plan,
    why). ``resume``: a stopped task whose full load finished (``full_load_finished``) with every
    table ``Table completed``. ``retry``: a failed task, or a stopped one that cannot resume,
    that DMS's statistics show never wrote to Aurora (``_never_wrote``). ``steps``: a failed or
    stopped task that is neither, so Aurora may hold rows and the customer empties the tables
    and reloads themselves; ``why`` says what showed it. None: any other task."""
    if task is None or task.get("Status") not in ("stopped", "failed"):
        return None, []
    unfinished = [f"{r.get('SchemaName')}.{r.get('TableName')} is {r.get('TableState')}"
                  for r in tables
                  if str(r.get("TableState") or "").casefold() != TABLE_COMPLETED]  # fmt: skip
    if task.get("Status") == "stopped" and full_load_finished(task) and not unfinished:
        return RESUME, []
    why = _never_wrote(task, tables)
    if not why:
        return RETRY, []
    return "steps", why + [u for u in unfinished if u not in why]


def recovery(task: dict[str, Any] | None, tables: list[dict[str, Any]]) -> str | None:
    """The command Prove may offer: ``resume``, ``retry`` (each one that would run), or None."""
    plan, _ = recovery_plan(task, tables)
    return plan if plan in RECOVER else None


@dataclass(frozen=True)
class StepsContext:
    """What the customer's own steps name: the task's region and the source engine family."""

    region: str
    family: str | None
    dms_version: str | None = None  # the plan's DMS engine version: where validation fails go


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
    s3: Any | None
    sts: Any | None


@dataclass
class LagReader:
    """status's capture lag (#1997): CloudWatch, read-only, with the gate's own calls; the lag
    metrics listed once per wait. The writes never get a CloudWatch client."""

    cloudwatch: Any
    metrics: dict[str, list[dict[str, str]]] | None = None


@dataclass
class RetentionReader:
    """status's source retention (#2060): the source's RDS metrics in CloudWatch, read-only,
    for the inputs file's source and the step plan's engine family."""

    cloudwatch: Any
    source_kind: str
    source_id: str | None
    family: str | None


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
    p.add_argument("--change-set", help="execute: the change set deploy created; status: the "
                   "change set just executed, so a stack still in review is waited on")  # fmt: skip
    p.add_argument("--confirm", action="append", default=[], metavar="CHECK_ID",
                   help="preflight: a 'confirm' row the customer checked (repeatable)")  # fmt: skip
    p.add_argument(
        "--until", choices=["stack", "connections", "running", "caught_up", "task_stopped"]
    )
    p.add_argument("--wait-seconds", type=int, default=None,
                   help=f"status: default 0; {', '.join(WAITS)}: default {MAX_WAIT_SECONDS}")  # fmt: skip
    p.add_argument("--interval-seconds", type=int, default=30)
    p.add_argument("--target-user", help="routines: the Aurora MySQL user the DEFINER clauses "
                   "name (default: the routine-migration artifact's)")  # fmt: skip
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


def source_family(root: str, db: str, job_id: str) -> str | None:
    """The step plan's source engine family (``mysql`` or ``postgresql``), or None when the plan
    cannot be read: the source retention rules follow it (#2060)."""
    from src.contracts.data_movement_plan import DataMovementPlan
    from src.shared.contract_versioning import load_contract
    from src.tools.data_movement.source_retention import family_of

    path = _step_dir(root, db, job_id) / "plan.json"
    try:
        plan = load_contract(DataMovementPlan, json.loads(path.read_text()), artifact="step plan")
    except (OSError, ValueError):
        return None
    return family_of(plan.source.engine)


def summary(root: str, db: str, job_id: str) -> dict[str, Any]:
    """Step 1's figures, from the plan, the contract and the collection: quoted, never
    computed here (the not-moved list is ``dms_step.not_moved``, as the DDL header lists it)."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import (
        CUSTOMER_RESPONSIBILITY,
        engine_rules,
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
    not_moved_tables = [{"table": n.table_id, "reason": n.reason} for n in inputs.not_moved]
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
                     "warning_count": len(warnings), "warnings": warnings[:10],
                     "not_moved": not_moved_tables},
        "replication_instance": None if replication is None else {
            "instance_class": replication.sizing.instance_class,
            "allocated_storage_gb": replication.sizing.allocated_storage_gb,
            "engine_version": engine_rules(replication).version,
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


def empty_target(root: str, db: str, job_id: str) -> dict[str, Any]:
    """``plan``'s ``empty_target`` (#2064): the question the customer answers before the task
    starts, every step 1 table empty on Aurora, with a read-only query for them to run."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import contract_tables, load_step1
    from src.tools.data_movement.table_errors import empty_target_check

    try:  # the plan line also serves Ending the step: a contract it cannot read names none
        step = load_step1(LocalArtifactStore(base_dir=root), db, job_id, with_ddl=False)
        tables = sorted(t.table_id for t in contract_tables(step.contract))
    except (OSError, ValueError):  # DmsStepError is a ValueError
        tables = []
    return empty_target_check(source_family(root, db, job_id), tables)


def _status_context(root: str, db: str, job_id: str, region: str) -> StepsContext:
    from src.tools.data_movement.table_errors import dms_version, step_plan

    return StepsContext(region, source_family(root, db, job_id),
                        dms_version(step_plan(root, db, job_id)))  # fmt: skip


def deploy_plan(root: str, db: str, job_id: str, inputs: MoveInputs) -> dict[str, Any]:
    path, body, template_params = _template(root, db, job_id)
    params = parameters(inputs, template_params)
    name = stack_name(inputs, job_id)
    command = ["aws", "cloudformation", "deploy", "--template-file", str(path), "--stack-name", name,
               "--capabilities", "CAPABILITY_IAM", "--region", inputs.region, "--parameter-overrides",
               *(f"{k}={v}" for k, v in params.items()),
               "--tags", *(f"{t['Key']}={t['Value']}" for t in stack_tags(job_id))]  # fmt: skip
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


def status(clients: MoveClients, name: str, lag: LagReader | None = None,
           retention: RetentionReader | None = None,
           steps: StepsContext | None = None, change_set: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Where step 1 stands: the stack, both endpoint connections, the task, its tables and,
    with a ``lag`` reader, its capture lag; with a ``retention`` reader, once the task has run,
    the source's change-log retention (#2060)."""
    from src.tools.data_movement.stack_states import stack_kind

    stack = _stack(clients, name)
    kind = stack_kind(None if stack is None else stack["StackStatus"])
    if stack is None or kind == "gone":
        return {"status": "complete", "phase": "no_stack", "stack_name": name}
    out: dict[str, Any] = {"status": "complete", "stack_name": name,
                           "stack_status": stack["StackStatus"],
                           "stack_reason": stack.get("StackStatusReason")}  # fmt: skip
    if kind == "changing" or (kind == "not_created" and _executing(clients, stack, change_set)):
        return {**out, "phase": "stack_in_progress"}
    out.update(_stack_failure(clients, name, stack))
    if kind in ("not_created", "create_failed"):
        return {
            **out,
            "phase": "stack_in_review" if kind == "not_created" else "stack_create_failed",
        }
    outputs = _outputs(stack)
    if kind != "deployed" or not outputs.get("ReplicationTaskArn"):
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
    out.update({"connections": connections, **_task_report(task), "recovery": None,
                "full_load_percent": task_stats.get("FullLoadProgressPercent"),
                "tables_loaded": task_stats.get("TablesLoaded"),
                "tables_loading": task_stats.get("TablesLoading"),
                "tables_queued": task_stats.get("TablesQueued"),
                "tables_errored": task_stats.get("TablesErrored")})  # fmt: skip
    if task.get("Status") == "ready":
        statuses = {c["status"] for c in connections.values()}
        phase = ("connections_testing" if "testing" in statuses
                 else "connections_failed" if "failed" in statuses
                 else "ready_to_start" if statuses == {"successful"} else "connections_not_tested")  # fmt: skip
        return {**out, "phase": phase}
    if task.get("Status") in TASK_STOPPED:
        out = {**out, "phase": "task_stopped"}
        if task.get("Status") in ("stopped", "failed"):
            tables = _table_statistics(clients, outputs["ReplicationTaskArn"])
            out.update(_recovery_fields(task, tables, steps))
        return _source_retention(out, retention)
    from src.tools.data_movement import dms_gate as gate

    tables = _table_statistics(clients, outputs["ReplicationTaskArn"])
    states = [gate.validation_state(t) for t in tables]  # case-folded, as the gate reads them
    pending_tables = [s for s in states if s in gate.PENDING_RECORDS]
    pending_records = sum(int(t.get("ValidationPendingRecords") or 0) for t in tables)
    out["validation"] = {
        "tables": len(tables),
        "validated": states.count(gate.VALIDATED),
        "pending_tables": len(pending_tables),
        "pending_records": pending_records,
        "not_enabled_tables": states.count(gate.NOT_ENABLED),
        "states": _count(t.get("ValidationState") or "unknown" for t in tables),
    }
    out["changes_applied"] = {kind: sum(int(t.get(f"Applied{kind.title()}") or 0) for t in tables)
                              for kind in ("inserts", "updates", "deletes")}  # fmt: skip
    out.update(_capture_lag(clients, task, lag))
    loaded = task_stats.get("FullLoadProgressPercent") == 100
    phase = "caught_up" if loaded and not pending_tables and not pending_records else (
        "validating" if loaded else "loading")  # fmt: skip
    judged = _table_problems(_judge_validation({**out, "phase": phase}, task, loaded), task,
                             tables, steps)  # fmt: skip
    return _source_retention(judged, retention)


def _table_problems(out: dict[str, Any], task: dict[str, Any], tables: list[dict[str, Any]],
                    steps: StepsContext | None) -> dict[str, Any]:  # fmt: skip
    """``table_problems`` (#2064): the tables that did not load and the validation failures,
    names and counts only. A table in an error state makes the phase ``table_errors`` (the load
    cannot finish; a ``Table cancelled`` one does on its second poll, ``CancelWatch``); either
    carries ``table_recovery`` ``reload-tables`` only when that command would run, else the
    customer's ``table_steps``. Validation failures carry the read-only
    ``validation_failures_check``."""
    from src.tools.data_movement import table_errors as te

    problems = te.table_problems(task, tables)
    out = {**out, "tables_errored": problems["tables_errored"], "table_problems": problems}
    family = None if steps is None else steps.family
    if problems["validation_failures"]:
        out["validation_failures_check"] = te.failures_check(
            family, task["ReplicationTaskArn"], None if steps is None else steps.dms_version
        )
    if not problems["errored_tables"]:
        return out
    plan, why, names = te.reload_plan(task, tables)
    if problems["tables_errored"]:
        out["phase"] = "table_errors"
    out["table_recovery"] = RELOAD_TABLES if plan == RELOAD_TABLES else None
    if plan == "steps":
        out["table_recovery_refused"] = "; ".join(why)
        if steps is not None:
            out["table_steps"] = te.table_steps(task["ReplicationTaskArn"], steps.region, names,
                                                _truncate_sql(family, names))  # fmt: skip
    elif plan is None:
        out["table_recovery_refused"] = "; ".join(why)
    return out


def _executing(clients: MoveClients, stack: dict[str, Any], change_set: str | None) -> bool:
    """A ``REVIEW_IN_PROGRESS`` stack that is about to be created (#2063): the change set Move
    just executed (``change_set``) reads ``AVAILABLE`` or ``EXECUTE_IN_PROGRESS``, CloudFormation
    starting its create asynchronously; without one, any of the stack's change sets reads
    ``EXECUTE_IN_PROGRESS``. Any other: the change set was never executed. Read-only."""
    name = stack.get("StackId") or stack["StackName"]
    if change_set:
        found = clients.cloudformation.describe_change_set(StackName=name, ChangeSetName=change_set)
        return found.get("ExecutionStatus") in ("AVAILABLE", "EXECUTE_IN_PROGRESS")
    pages = clients.cloudformation.get_paginator("list_change_sets").paginate(StackName=name)
    return any(s.get("ExecutionStatus") == "EXECUTE_IN_PROGRESS"
               for page in pages for s in page.get("Summaries") or [])  # fmt: skip


MAX_EVENT_PAGES = 10  # DescribeStackEvents pages read for one failure, newest first


def _stack_events(clients: MoveClients, stack: dict[str, Any]) -> Any:
    """The stack's events, newest first, page by page and only as far as they are read."""
    pages = clients.cloudformation.get_paginator("describe_stack_events").paginate(
        StackName=stack.get("StackId") or stack["StackName"],
        PaginationConfig={"MaxItems": MAX_EVENT_PAGES * 100},
    )
    for page in pages:
        yield from page.get("StackEvents") or []


def _stack_failure(clients: MoveClients, name: str, stack: dict[str, Any]) -> dict[str, Any]:
    """``stack_failure`` (the first resource that failed in the latest operation, with its fix
    ``hint``; read-only) and ``stack_action`` (what the customer can do) for a stack that is not
    plainly deployed (#2063); nothing for one that is."""
    from src.tools.data_movement import stack_states as st

    state = stack["StackStatus"]
    action = st.stack_action(state, name, protected=bool(stack.get("EnableTerminationProtection")))
    if action is None:
        return {}
    failure = st.first_failure(_stack_events(clients, stack)) if st.reads_failure(state) else None
    return {"stack_failure": failure, "stack_action": action}


def _recovery_fields(
    task: dict[str, Any], tables: list[dict[str, Any]], steps: StepsContext | None
) -> dict[str, Any]:
    """``recovery`` for a stopped or failed task and, when neither command would run, the
    customer's own steps (``tables_to_empty``, ``customer_steps``) with why (#2061)."""
    plan, why = recovery_plan(task, tables)
    if plan != "steps":
        return {"recovery": plan}
    found: dict[str, Any] = {"recovery": None, "recovery_refused": "; ".join(why)}
    if steps is not None:
        found.update(customer_steps(task, tables, steps))
    return found


def _source_retention(out: dict[str, Any], reader: RetentionReader | None) -> dict[str, Any]:
    """``source_retention`` (#2060): the source's change log and free storage, judged; a stopped
    task's line also says what the stop costs the source."""
    if reader is None:
        return out
    from src.tools.data_movement import source_retention as sr

    found = sr.read_source_retention(reader.cloudwatch, source_kind=reader.source_kind,
                                     source_id=reader.source_id, family=reader.family,
                                     now=datetime.now(UTC))  # fmt: skip
    if out["phase"] == "task_stopped":
        found["stopped_task"] = sr.STOPPED_TASK_RISK.get(reader.family or "", " / ".join(
            sr.STOPPED_TASK_RISK.values()))  # fmt: skip
    return {**out, "source_retention": found}


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
    retention = current.get("source_retention") or {}
    problems = current.get("table_problems") or {}
    return {"status": "progress", "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "phase": current.get("phase"), "full_load_percent": current.get("full_load_percent"),
            "tables_loaded": current.get("tables_loaded"),
            "inserts_applied": applied.get("inserts"), "updates_applied": applied.get("updates"),
            "capture_lag_seconds": current.get("capture_lag_seconds"),
            "apply_lag_seconds": current.get("apply_lag_seconds"),
            "validated_tables": validation.get("validated"),
            "pending_tables": validation.get("pending_tables"),
            "not_enabled_tables": validation.get("not_enabled_tables"),
            "source_retention": retention.get("level"),
            "source_warning": retention.get("message"),
            "tables_errored": current.get("tables_errored"),
            "errored_tables": [t["table"] for t in problems.get("errored_tables") or []],
            "errored_tables_not_shown": problems.get("errored_tables_not_shown"),
            "validation_failures": problems.get("validation_failures"),
            "validation_failures_not_shown": problems.get("validation_failures_not_shown")}  # fmt: skip


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


# The phases where no task runs and none will until the customer acts on the stack (#2063).
NO_TASK = ("stack_failed", "stack_create_failed", "stack_in_review", "no_stack")
UNTIL: dict[str, Callable[[dict[str, Any]], bool]] = {
    "stack": lambda s: s["phase"] != "stack_in_progress",
    "connections": lambda s: s["phase"] not in ("stack_in_progress", "connections_testing"),
    "running": lambda s: s.get("task_status") == "running"
    or s["phase"] in ("task_stopped", *NO_TASK),
    "caught_up": lambda s: s["phase"]
    in ("caught_up", "validation_stalled", "table_errors", "task_stopped", *NO_TASK),
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
    retention: RetentionReader | None = None,
    steps: StepsContext | None = None,
    change_set: str | None = None,
) -> dict[str, Any]:
    """Poll until ``until`` holds or the time is up. Under ``caught_up``, ``on_poll`` gets each
    poll's progress line but the last one (the result line carries it), a validation with
    no progress over the window ends the wait as ``validation_stalled``, and a ``critical``
    source retention ends it with ``stopped_for`` ``source_retention`` (#2060)."""
    read: Callable[[MoveClients, str], dict[str, Any]] = functools.partial(
        status, lag=lag, retention=retention, steps=steps, change_set=change_set
    )
    done = UNTIL.get(until or "")
    if until == "task_stopped":
        read, done = task_state, lambda s: s["task_stopped"]
    watch = _StallWatch()
    from src.tools.data_movement.table_errors import CancelWatch

    cancels = CancelWatch()  # a Table cancelled seen on two polls in a row (#2064)
    current = read(clients, name)
    if until is None or done is None:
        return current
    now = clock()
    deadline = now + wait_seconds
    if until == "caught_up":
        current = cancels.judge(watch.judge(current, now))
    while (
        not done(current)
        and not _source_critical(current, until)
        and (now := clock()) + interval <= deadline
    ):
        if on_poll is not None and until == "caught_up":
            on_poll(progress_line(current))
        sleep(interval)
        current = read(clients, name)
        if until == "caught_up":
            current = cancels.judge(watch.judge(current, now + interval))  # one interval on
    out = {**current, "until": until, "reached": done(current)}
    if _source_critical(current, until):  # also when reached: a critical reading is never lost
        out["stopped_for"] = "source_retention"
    return out


def _source_critical(current: dict[str, Any], until: str | None) -> bool:
    """Under ``caught_up``, a source retention that reads critical ends the wait (#2060)."""
    level = (current.get("source_retention") or {}).get("level")
    return until == "caught_up" and level == "critical"


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
    large = plan["template_bytes"] > MAX_TEMPLATE_BODY
    if large and not inputs.template_bucket:
        raise MoveError(f"{TEMPLATE} is {plan['template_bytes']} bytes, above CloudFormation's "
                        f"{MAX_TEMPLATE_BODY}-byte inline limit: set template_bucket in the inputs "
                        "file to an S3 bucket you own in the stack's region, and deploy again")  # fmt: skip
    from src.tools.data_movement.stack_states import stack_kind

    name = plan["stack_name"]
    stack = _stack(clients, name)
    state = stack_kind(None if stack is None else stack["StackStatus"])
    if stack is not None and state == "changing":
        raise MoveError(
            f"stack {name} is {stack['StackStatus']}: wait for it with status --until stack"
        )
    if stack is not None and state in ("create_failed", "stuck"):
        return _not_updatable(clients, name, stack)
    kind = "CREATE" if state in ("gone", "not_created") else "UPDATE"
    change_set = f"move-step-1-{now().strftime('%Y%m%d%H%M%S')}"
    template: dict[str, str] = {"TemplateBody": body}
    if large:
        template = {"TemplateURL": _upload(clients, inputs, name, body)}
    clients.cloudformation.create_change_set(
        StackName=name, ChangeSetName=change_set, ChangeSetType=kind, **template,
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


def _not_updatable(clients: MoveClients, name: str, stack: dict[str, Any]) -> dict[str, Any]:
    """deploy's refusal of a stack CloudFormation cannot update as it is (#2063): no change set
    is sent; the line names the first failure and what the customer can do."""
    found = _stack_failure(clients, name, stack)
    failure, state = found["stack_failure"], stack["StackStatus"]
    reason = (failure or {}).get("reason") or stack.get("StackStatusReason")
    why = f" ({failure['logical_id']} {failure['status']}: {reason})" if failure else (
        f" ({reason})" if reason else "")  # fmt: skip
    return {"status": "error", "kind": "stack_not_updatable", "stack_name": name,
            "stack_status": state, "stack_reason": stack.get("StackStatusReason"), **found,
            "message": f"stack {name} is {state}{why}; deploy sends no change set. "
                       f"{found['stack_action']}"}  # fmt: skip


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
    from src.tools.data_movement.stack_states import stack_kind

    stack = _stack(clients, name)
    if stack is None or stack_kind(stack["StackStatus"]) != "deployed":
        state = "missing" if stack is None else stack["StackStatus"]
        raise MoveError(f"stack {name} is {state}: deploy it first (status says what to do)")
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
        and (stack["StackStatus"] not in ("DELETE_IN_PROGRESS", "REVIEW_IN_PROGRESS"))
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
                     "stack yourself; restart-validation, resume and retry act on that stack's task only)")  # fmt: skip


def _step_resources(
    clients: MoveClients, name: str, *, partial: bool = False
) -> dict[str, dict[str, Any]]:
    """Every resource of the stack, page by page; exactly the six ``dms-step-1.yaml`` creates or,
    ``partial`` (a create that never completed), only some of them and nothing else."""
    return _check_resources(name, _list_resources(clients, name), partial=partial)


def _list_resources(clients: MoveClients, name: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    pages = clients.cloudformation.get_paginator("list_stack_resources").paginate(StackName=name)
    for page in pages:
        for r in page.get("StackResourceSummaries") or []:
            found[r["LogicalResourceId"]] = r
    return found


def _check_resources(
    name: str, found: dict[str, dict[str, Any]], *, partial: bool
) -> dict[str, dict[str, Any]]:
    kinds = {logical: r["ResourceType"] for logical, r in found.items()}
    expected = (
        {k: v for k, v in STEP_RESOURCES.items() if k in kinds} if partial else STEP_RESOURCES
    )
    if kinds != expected:
        extra = sorted(f"{k} ({v})" for k, v in kinds.items() if STEP_RESOURCES.get(k) != v)
        missing = sorted(k for k in expected if kinds.get(k) != expected[k])
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
    """The stack's task, checked to be this job's; None once the task is gone (or, in a create
    that never completed, was never made)."""
    resource = resources.get("ReplicationTask")
    if resource is None or resource.get("ResourceStatus") == "DELETE_COMPLETE":
        return None
    arn = resource.get("PhysicalResourceId")
    if not arn:
        raise _not_the_step(name, "its ReplicationTask has no physical id")
    task = _task(clients, arn)
    if task is not None:
        _check_task_is_this_jobs(clients, name, task, job_id)
    return task


def _this_jobs_task(
    clients: MoveClients, name: str, job_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(stack, task): the step's stack and its task, both checked to be this job's; each None
    once it is gone."""
    stack = _step_stack(clients, name)
    if stack is None:
        return None, None
    _, resources = _owned_resources(clients, name, stack, job_id)
    return stack, _live_task(clients, name, resources, job_id)


def _owned_resources(
    clients: MoveClients, name: str, stack: dict[str, Any], job_id: str
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """(tagged, resources): no other job's tag on the stack, and its resources exactly the six
    of ``dms-step-1.yaml``. A stack whose create never completed (rolled back, failed, or a
    change set never executed; #2063) may hold fewer, never another, and only with this job's
    own stack tag: the task and the instance and endpoints left are checked as usual."""
    from src.tools.data_movement.stack_states import NEVER_CREATED

    tagged = _stack_tag_is_this_jobs(name, stack, job_id)
    never = stack["StackStatus"] in NEVER_CREATED
    if never and not tagged:
        raise _untagged_failed_create(name, stack)
    found = _list_resources(clients, name)
    if (
        not never
        and stack["StackStatus"] == "DELETE_FAILED"
        and ({k: r["ResourceType"] for k, r in found.items()} != STEP_RESOURCES)
    ):
        # A delete that failed after a failed create (#2063): the stack's own events tell
        # whether its create ever completed; read-only, and only for a stack that is not whole.
        never = _create_never_completed(clients, stack)
    if not never:
        return tagged, _check_resources(name, found, partial=False)
    if not tagged:
        raise _untagged_failed_create(name, stack)
    return tagged, _check_resources(name, found, partial=True)


def _untagged_failed_create(name: str, stack: dict[str, Any]) -> MoveError:
    return _not_the_step(name, f"its create never completed ({stack['StackStatus']}), and such a "
                               f"stack is ended only when its own {JOB_TAG} tag names this job")  # fmt: skip


def _create_never_completed(clients: MoveClients, stack: dict[str, Any]) -> bool:
    """True only when every event of the stack was read (at most ``MAX_EVENT_PAGES`` pages) and
    none says the stack reached a deployed status; a longer history counts as completed."""
    from src.tools.data_movement.stack_states import create_never_completed

    events: list[dict[str, Any]] = []
    pages = clients.cloudformation.get_paginator("describe_stack_events").paginate(
        StackName=stack.get("StackId") or stack["StackName"]
    )
    for count, page in enumerate(pages, start=1):
        events += page.get("StackEvents") or []
        if count >= MAX_EVENT_PAGES and page.get("NextToken"):
            return False
    return create_never_completed(events)


def _wait_task(
    clients: MoveClients,
    arn: str,
    current: dict[str, Any] | None,
    state: str | None,
    settled: Callable[[str | None], bool],
    command: str,
    waited: int,
    wait_seconds: int,
    interval: int,
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any] | None, str | None, int]:
    """Poll the task until ``settled(state)``, the task is gone or ``wait_seconds`` is up, one
    progress line per poll; ``waited`` is what an earlier wait of the same run already used."""
    while not settled(state) and waited + interval <= wait_seconds:
        sleep(interval)
        waited += interval
        current = _task(clients, arn)
        state = "deleted" if current is None else current.get("Status")
        _progress(command, f"task is {state}", waited, wait_seconds)
        if current is None:
            break
    return current, state, waited


def _stop_task(
    clients: MoveClients,
    task: dict[str, Any],
    command: str,
    wait_seconds: int,
    interval: int,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Stop this job's running task and wait until it has stopped; a task not running is left as
    is. ``reached`` true: DMS reports it not running, or it is gone."""
    arn = task["ReplicationTaskArn"]
    state = task.get("Status")
    if state in TASK_IDLE:
        return {"task": task, "already_stopped": True, "reached": True, "waited_seconds": 0}
    if state in TASK_RUNNING:
        clients.dms.stop_replication_task(ReplicationTaskArn=arn)
        state = "stopping"
    elif state != "stopping":
        raise MoveError(f"{dms_reports(task)}: wait until it is running or stopped, then run "
                        f"{command} again (status shows where it stands)")  # fmt: skip
    current, state, waited = _wait_task(clients, arn, task, state, lambda s: s in TASK_IDLE,
                                        command, 0, wait_seconds, interval, sleep)  # fmt: skip
    return {"task": current, "already_stopped": False,
            "reached": current is None or state in TASK_IDLE, "waited_seconds": waited}  # fmt: skip


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
    stack, task = _this_jobs_task(clients, name, job_id)
    if stack is None:
        return {"status": "no_stack", "stack_name": name}
    if task is None:
        return {"status": "no_task", "stack_name": name, **_task_report(None)}
    out = {"stack_name": name, "task_arn": task["ReplicationTaskArn"]}
    stopped = _stop_task(clients, task, "stop", wait_seconds, interval, sleep)
    if stopped["already_stopped"]:
        return {"status": "stopped", **out, **_task_report(task), "already_stopped": True}
    return {"status": "stopped" if stopped["reached"] else "stopping", **out,
            **_task_report(stopped["task"]), "already_stopped": False,
            "reached": stopped["reached"], "waited_seconds": stopped["waited_seconds"]}  # fmt: skip


def _owned_task(clients: MoveClients, name: str, job_id: str, verb: str) -> dict[str, Any]:
    """This job's step task, checked as ``stop`` checks it; an error when there is none."""
    stack, task = _this_jobs_task(clients, name, job_id)
    if stack is None or task is None:
        what = "no stack" if stack is None else "no task"
        raise MoveError(f"stack {name}: {what}, nothing to {verb} (status shows where it stands)")
    return task


def _start_and_wait(
    clients: MoveClients,
    task: dict[str, Any],
    start_type: str,
    command: str,
    waited: int,
    wait_seconds: int,
    interval: int,
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any] | None, str, int]:
    """Start this job's task with ``start_type`` and wait until it runs: (task, outcome, waited).

    Right after the call DMS may still read the task's earlier status, so the wait goes on
    through it and through ``starting``. The outcome is ``running``; ``starting`` (starting, or a
    stopped task still reading stopped, at the deadline); or ``failed`` (it failed, still reads
    failed, went back to its earlier status after it had started, or is gone)."""
    arn, before = task["ReplicationTaskArn"], task.get("Status")
    clients.dms.start_replication_task(ReplicationTaskArn=arn, StartReplicationTaskType=start_type)
    moved = False

    def settled(state: str | None) -> bool:
        nonlocal moved
        moved = moved or state != before
        return moved and state != "starting"

    current, state, waited = _wait_task(clients, arn, task, before, settled, command, waited,
                                        wait_seconds, interval, sleep)  # fmt: skip
    if state == "running":
        return current, "running", waited
    if state == "starting" or (state == before == "stopped" and not moved):
        return current, "starting", waited
    return current, "failed", waited


def _started(outcome: str, names: tuple[str, str, str]) -> str:
    """The line's status for a start's outcome: (running, starting, failed) names."""
    return dict(zip(("running", "starting", "failed"), names, strict=True))[outcome]


def restart_validation(
    clients: MoveClients,
    name: str,
    job_id: str,
    wait_seconds: int,
    interval: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Restart DMS validation (#2048): stop this job's task, then resume it.

    The task is stopped and waited on as ``stop`` does, then started with ``resume-processing``
    (CDC resumes from its checkpoint, no table reloads) and waited on until it runs; both waits
    share ``wait_seconds``. A task a previous run left stopped is only resumed. A task neither
    running nor stopped (failed, never started, changing) is refused, and one that has not
    stopped by the deadline is not resumed: ``stopping``, and the same run again finishes it."""
    task = _owned_task(clients, name, job_id, "restart")
    state = task.get("Status")
    if state not in TASK_RUNNING | {"stopping", "stopped"}:
        raise MoveError(f"{dms_reports(task)}: {RESTART} resumes only a task that was running "
                        "(status shows where it stands)")  # fmt: skip
    out = {"stack_name": name, "task_arn": task["ReplicationTaskArn"]}
    stopped = _stop_task(clients, task, RESTART, wait_seconds, interval, sleep)
    current, waited = stopped["task"], stopped["waited_seconds"]
    if not stopped["reached"] or current is None:
        return {"status": "stopping" if current else "no_task", **out, **_task_report(current),
                "resumed": False, "reached": False, "waited_seconds": waited}  # fmt: skip
    current, outcome, waited = _start_and_wait(clients, current, "resume-processing", RESTART,
                                               waited, wait_seconds, interval, sleep)  # fmt: skip
    return {"status": _started(outcome, ("restarted", "starting", "resume_failed")), **out,
            **_task_report(current), "resumed": True, "reached": outcome == "running",
            "waited_seconds": waited}  # fmt: skip


# DescribeTableStatistics' TableState once a table's full load finished (DMS API Reference,
# TableStatistics: "Table does not exist | Before load | Full load | Table completed | Table
# cancelled | Table error | Table is being reloaded"); compared case-folded.
TABLE_COMPLETED = "table completed"
# Every per-table counter of TableStatistics that says DMS wrote something to the target.
WRITE_COUNTERS = ("FullLoadRows", "FullLoadErrorRows", "FullLoadCondtnlChkFailedRows", "Inserts",
                  "Updates", "Deletes", "Ddls", "AppliedInserts", "AppliedUpdates",
                  "AppliedDeletes", "AppliedDdls")  # fmt: skip


def _not_this_command(
    command: str,
    task: dict[str, Any],
    plan: str | None,
    why: list[str],
    tables: list[dict[str, Any]],
    steps: StepsContext,
) -> dict[str, Any]:
    """The refusal of a command the shared rule does not name: the customer's steps when neither
    would run, else a pointer to the one that would (never to one that would refuse too)."""
    if plan == "steps":
        return {"status": "error", "kind": "target_may_hold_rows",
                "task_arn": task["ReplicationTaskArn"], **_task_report(task),
                "message": f"DMS may have written rows to Aurora ({'; '.join(why)}), and a "
                           f"{command} over them appends duplicates or hits key errors, so "
                           f"{command} refuses (and so would {RETRY if command == RESUME else RESUME}). "
                           "The modernizer never connects to your databases: empty the step's "
                           "tables on Aurora yourself, then reload the task (customer_steps)",
                **customer_steps(task, tables, steps)}  # fmt: skip
    other = {
        RESUME: "a stopped task whose full load finished: run resume",
        RETRY: "a task DMS's statistics show never wrote to Aurora: run retry",
    }[str(plan)]
    raise MoveError(f"{dms_reports(task)}: {command} does not cover this task; {other}")


def _state_pointer(command: str, task: dict[str, Any]) -> MoveError:
    state = str(task.get("Status"))
    pointer = {"ready": "a task that never ran: run start",
               "stopping": "wait until it has stopped (status --until task_stopped), then run "
                           f"{command} again",
               "running": f"the task is running: nothing to {command}",
               "starting": f"the task is starting: nothing to {command}"}.get(
        state, "status shows where it stands")  # fmt: skip
    return MoveError(f"{dms_reports(task)}: {command} acts only on a stopped or failed task; "
                     f"{pointer}")  # fmt: skip


def _recover(
    command: str,
    clients: MoveClients,
    name: str,
    job_id: str,
    wait_seconds: int,
    interval: int,
    steps: StepsContext,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """``resume`` or ``retry``: run it only when ``recovery_plan`` names it (#2061)."""
    task = _owned_task(clients, name, job_id, command)
    if task.get("Status") not in ("stopped", "failed"):
        raise _state_pointer(command, task)
    arn = task["ReplicationTaskArn"]
    rows = _table_statistics(clients, arn)
    plan, why = recovery_plan(task, rows)
    out = {"stack_name": name, "task_arn": arn}
    if plan != command:
        return {**out, **_not_this_command(command, task, plan, why, rows, steps)}
    start_type, names = {RESUME: ("resume-processing", ("resumed", "starting", "resume_failed")),
                         RETRY: ("reload-target", ("retried", "starting", "retry_failed"))}[command]  # fmt: skip
    current, outcome, waited = _start_and_wait(clients, task, start_type, command, 0,
                                               wait_seconds, interval, sleep)  # fmt: skip
    return {"status": _started(outcome, names), **out, **_task_report(current),
            "tables_checked": len(rows), "reached": outcome == "running",
            "waited_seconds": waited}  # fmt: skip


def resume(
    clients: MoveClients,
    name: str,
    job_id: str,
    wait_seconds: int,
    interval: int,
    *,
    steps: StepsContext,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Start this job's stopped task again with ``resume-processing`` and wait until it runs
    (#2061), only when ``recovery_plan`` says ``resume``: its full load finished.

    For a full-load-and-cdc task, ``resume-processing`` "replicate[s] the changes from the last
    stop position", but for the full load it "will reload any tables that were partially loaded
    or not yet loaded" (DMS API Reference, StartReplicationTask). Under ``TargetTablePrepMode:
    DO_NOTHING`` such a reload appends duplicates or hits key errors, so any other stopped or
    failed task is refused: with a pointer to ``retry`` when retry would run, otherwise with the
    customer's steps (``target_may_hold_rows``). Only an allowed resume reloads nothing."""
    return _recover(RESUME, clients, name, job_id, wait_seconds, interval, steps, sleep)


def _never_wrote(task: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """Why the task may have written to Aurora; empty only when DMS's statistics show it never
    did: no ``FullLoadStartDate`` ("the date the replication task full load was started", DMS
    API Reference, ReplicationTaskStats), no table loaded, loading or errored, and every table's
    write counters 0 with no full load start time and no reload."""
    stats = task.get("ReplicationTaskStats") or {}
    why = ["its full load started" if stats.get("FullLoadStartDate") is not None else "",
           "its full load made progress" if stats.get("FullLoadProgressPercent") else "",
           *(f"{k} is {stats[k]}" for k in ("TablesLoaded", "TablesLoading", "TablesErrored")
             if stats.get(k))]  # fmt: skip
    for r in rows:
        table = f"{r.get('SchemaName')}.{r.get('TableName')}"
        why += [f"{table} {k} is {r[k]}" for k in WRITE_COUNTERS if r.get(k)]
        if r.get("FullLoadStartTime") is not None or r.get("FullLoadReloaded"):
            why.append(f"{table} was loaded")
    return [w for w in why if w]


def _quoted_table(family: str | None, schema: str, table: str) -> str:
    quote = '"' if family == "postgresql" else "`"
    return ".".join(f"{quote}{part.replace(quote, quote * 2)}{quote}" for part in (schema, table))


def _truncate_sql(family: str | None, tables: list[tuple[str, str]]) -> list[str]:
    """The steps that empty ``tables`` on Aurora, each SQL entry SQL only (never run here)."""
    mysql = " ".join(["SET FOREIGN_KEY_CHECKS = 0;",
                      *(f"TRUNCATE TABLE {_quoted_table('mysql', s, t)};" for s, t in tables),
                      "SET FOREIGN_KEY_CHECKS = 1;"])  # fmt: skip
    pg = f"TRUNCATE TABLE {', '.join(_quoted_table('postgresql', s, t) for s, t in tables)};"
    pg_note = ("If PostgreSQL refuses because another table references one of these, add that "
               "table to the same TRUNCATE statement.")  # fmt: skip
    if family == "mysql":
        return ["On Aurora MySQL, run:", mysql]
    if family == "postgresql":
        return ["On Aurora PostgreSQL, run:", pg, pg_note]
    return ["On Aurora MySQL, run:", mysql, "On Aurora PostgreSQL, run:", pg, pg_note]


def customer_steps(
    task: dict[str, Any], tables: list[dict[str, Any]], steps: StepsContext
) -> dict[str, Any]:
    """The customer's own steps when no command may reload: empty every table of the step on
    Aurora, then reload the task themselves. Each SQL entry is SQL only; never run here."""
    names = [(str(r.get("SchemaName")), str(r.get("TableName"))) for r in tables]
    empty = (_truncate_sql(steps.family, names) if names else
             ["Empty every table step-1.ddl.sql created (DMS lists no table for this task)."])  # fmt: skip
    return {"tables_to_empty": [f"{s}.{n}" for s, n in names], "customer_steps": [
        "Fix what stopped the task first (its hint and last_failure): a reload before the fix "
        "fails the same way.",
        "On the Aurora cluster (the target), never on the source, as a user that may truncate "
        "these tables, empty every table of the step:",
        *empty,
        "Then reload the task from the start, with your own credentials: aws dms "
        f"start-replication-task --replication-task-arn {task['ReplicationTaskArn']} "
        f"--start-replication-task-type reload-target --region {steps.region}",
        "Then run /move step-1 again: it goes on at Prove.",
    ]}  # fmt: skip


def retry(
    clients: MoveClients,
    name: str,
    job_id: str,
    wait_seconds: int,
    interval: int,
    *,
    steps: StepsContext,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Reload this job's failed task, or a stopped one that cannot resume, from the start
    (``reload-target``) and wait until it runs, only when ``recovery_plan`` says ``retry``: DMS's
    statistics show the task never wrote to Aurora (#2061, ``_never_wrote``).

    With ``TargetTablePrepMode: DO_NOTHING`` a reload over rows already on Aurora appends
    duplicates or hits key errors, and DMS's table statistics restart with each reload, so
    counters of 0 alone do not prove the tables are empty: any sign of a full load refuses with
    the customer's own steps (empty the tables on Aurora, then reload the task themselves), or
    with a pointer to ``resume`` when resume would run. Nothing here connects to a database."""
    return _recover(RETRY, clients, name, job_id, wait_seconds, interval, steps, sleep)


def _running_task(clients: MoveClients, name: str, job_id: str, command: str) -> dict[str, Any]:
    """This job's task, checked as ``stop`` checks it, and running: DMS reloads or revalidates
    the tables of a running task only."""
    task = _owned_task(clients, name, job_id, command)
    if task.get("Status") != "running":
        raise MoveError(f"{dms_reports(task)}: {command} acts only on a running task "
                        "(status shows where it stands)")  # fmt: skip
    return task


def reload_tables(clients: MoveClients, name: str, job_id: str, *,
                  steps: StepsContext) -> dict[str, Any]:  # fmt: skip
    """Reload this job's errored tables (``ReloadTables``, ``data-reload``; #2064), only when
    DMS's statistics show no errored table was ever written to (``table_errors.reload_plan``):
    with ``TargetTablePrepMode: DO_NOTHING`` a reload into a table that holds rows appends
    duplicates or fails on the same keys. Otherwise it refuses with ``target_may_hold_rows``
    and the customer's ``table_steps`` (empty the tables on Aurora, then reload them)."""
    from src.tools.data_movement import table_errors as te

    task = _running_task(clients, name, job_id, RELOAD_TABLES)
    arn = task["ReplicationTaskArn"]
    rows = _table_statistics(clients, arn)
    plan, why, tables = te.reload_plan(task, rows)
    out = {"stack_name": name, "task_arn": arn, **_task_report(task)}
    if plan is None:
        raise MoveError(f"{RELOAD_TABLES}: {'; '.join(why)}, nothing to reload")
    names = [f"{s}.{t}" for s, t in tables]
    if plan == "steps":
        return {"status": "error", "kind": "target_may_hold_rows", **out,
                "tables_to_empty": names,
                "message": f"DMS may have written rows to these tables on Aurora ({'; '.join(why)}), "
                           f"and a reload over them appends duplicates or hits key errors, so "
                           f"{RELOAD_TABLES} refuses. The modernizer never connects to your "
                           "databases: empty the tables on Aurora yourself, then reload them "
                           "(table_steps)",
                "table_steps": te.table_steps(arn, steps.region, tables,
                                              _truncate_sql(steps.family, tables))}  # fmt: skip
    try:
        calls = te.reload(clients.dms, arn, tables, "data-reload")
    except te.PartialReload as exc:
        raise MoveError(str(exc)) from None
    return {"status": "reloading", **out, "tables_reloaded": names, "calls": calls}


def revalidate(clients: MoveClients, name: str, job_id: str) -> dict[str, Any]:
    """Revalidate this job's tables with failed or suspended records (``ReloadTables``,
    ``validate-only``; #2064): DMS validates them again and reloads no data. Refused unless the
    task runs with validation on and such a table exists."""
    from src.tools.data_movement import table_errors as te

    task = _running_task(clients, name, job_id, REVALIDATE)
    if validation_enabled(task) is False:
        raise MoveError(f"{REVALIDATE}: the task turns validation off, nothing to revalidate")
    arn = task["ReplicationTaskArn"]
    tables = te.revalidate_tables(_table_statistics(clients, arn))
    if not tables:
        raise MoveError(f"{REVALIDATE}: no table has failed or suspended validation records")
    try:
        calls = te.reload(clients.dms, arn, tables, "validate-only")
    except te.PartialReload as exc:
        raise MoveError(str(exc)) from None
    return {"status": "revalidating", "stack_name": name, "task_arn": arn, **_task_report(task),
            "tables_revalidated": [f"{s}.{t}" for s, t in tables], "calls": calls}  # fmt: skip


def _resources_are_this_jobs(
    clients: MoveClients, name: str, resources: dict[str, dict[str, Any]], job_id: str
) -> bool:
    """True when the stack's replication instance or endpoints still exist and every one of them
    carries this job's tag; refuses one that carries another job's tag or none."""
    from botocore.exceptions import ClientError

    proven = False
    for logical in ("ReplicationInstance", "SourceEndpoint", "TargetEndpoint"):
        resource = resources.get(logical) or {}
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
    family: str | None = None,
) -> dict[str, Any]:
    """``_delete_step_stack``'s line, with what is left on the source (#2060): the stack never
    held the PostgreSQL replication slot DMS made, so deleting it leaves the slot behind."""
    from src.tools.data_movement.source_retention import source_followups

    result = _delete_step_stack(clients, name, job_id, wait_seconds, interval, sleep=sleep)
    return {**result, **source_followups(family)}


def _delete_step_stack(
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
    tagged, resources = _owned_resources(clients, name, stack, job_id)
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
        if stack.get("EnableTerminationProtection"):
            from src.tools.data_movement.stack_states import protection_message

            raise MoveError(f"teardown sends no delete: {protection_message(name)}")
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


# --- Verify (#1998): optional scripts the customer runs; nothing connects to a database ---

VERIFY_SHOWN = 50  # tables named on a printed line; verify.json names every one


def verify_step(
    command: str, root: str, db: str, job_id: str, inputs: MoveInputs
) -> dict[str, Any]:
    """``verify-scripts``: write step 1's read-only comparison scripts and ``verify.md``.
    ``verify``: read the customer's output files strictly and write ``verify.json``; a
    refused file, or none yet, removes an earlier result. No AWS call; it never changes the
    gate."""
    from src.contracts.verify_result import VerifyResult
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import load_step1
    from src.tools.data_movement.verify_check import VerifyOutputError, read_verify
    from src.tools.data_movement.verify_scripts import OUTPUTS, write_verify_scripts

    store = LocalArtifactStore(base_dir=root)
    step = load_step1(store, db, job_id, with_ddl=False)
    key = f"{db}/{job_id}/data-movement/step-1/verify.json"
    step_dir = _step_dir(root, db, job_id)
    gate = "verify is optional and does not change the gate"
    if command == VERIFY_SCRIPTS:
        store.delete(key)  # a result read from the earlier scripts' files is stale
        for name in OUTPUTS.values():  # and so are those files: none may decide a result
            (step_dir / name).unlink(missing_ok=True)
        target_db = inputs.target_database_name or step.plan.source.database
        made = write_verify_scripts(step.contract, step.contract_sha256, step_dir,
                                    source_engine=step.plan.source.engine,
                                    target_database=target_db)  # fmt: skip
        return {"status": "ready", "tables": made.tables, "columns": made.columns,
                "not_checked": [{"table_id": t, "reason": r} for t, r in made.not_checked][:VERIFY_SHOWN],
                "files": made.files, "gate": gate}  # fmt: skip
    try:
        result = read_verify(step_dir, step.contract, job_id=job_id, database_name=db,
                             contract_sha=step.contract_sha256)  # fmt: skip
    except VerifyOutputError as exc:
        store.delete(key)
        raise MoveError(f"refused: {exc}") from None
    if result is None:
        store.delete(key)
        return {"status": "no_output", "expected": [str(step_dir / OUTPUTS[(s, 1)]) for s in
                                                    ("source", "target")], "gate": gate}  # fmt: skip
    checked: VerifyResult = result
    store.write_json(key, checked.model_dump(mode="json"))
    counts = {s: sum(1 for t in result.tables if t.status == s)
              for s in ("match", "changing", "differ", "not_compared", "missing")}  # fmt: skip
    named = [{"table_id": t.table_id, "status": t.status, "missing_on": t.missing_on,
              "unhashed_on": t.unhashed_on} for t in result.tables if t.status != "match"]  # fmt: skip
    return {"status": result.status, **counts, "tables": len(result.tables),
            "second_run": result.second_run, "not_matched": named[:VERIFY_SHOWN],
            "not_checked": len(result.not_checked), "result": str(Path(root) / key),
            "gate": gate}  # fmt: skip


# --- Routines (#1947): scripts the customer runs; nothing connects to a database ---------

ROUTINES_SHOWN = 50  # routines named on a printed line; the files name every one


def _not_carried(section: Any) -> list[dict[str, Any]]:
    return [{"routine_id": r.routine_id, "kind": r.kind, "reasons": r.reasons}
            for r in section.routines if r.carry == "not_carried"]  # fmt: skip


def routines(clients: MoveClients, root: str, db: str, job_id: str, inputs: MoveInputs, name: str,
             *, target_user: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Write step 1's routine scripts (``routine_scripts``) once the task's full load is at
    100%: the deploy script, the read-only check script and their instructions, for the
    customer to run. Read-only in AWS (the task's status); it never connects to a database."""
    from src.contracts.routine_check import STATIC_LABEL
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import load_step1
    from src.tools.data_movement.routine_scripts import write_routine_scripts

    store = LocalArtifactStore(base_dir=root)
    step = load_step1(store, db, job_id, with_ddl=False)
    section = step.contract.routines
    if section is None:
        raise MoveError("the step 1 contract has no routines section (written before #1947): run "
                        "Prepare's three commands again")  # fmt: skip
    not_carried = _not_carried(section)
    base = {"carried": section.carried, "not_carried": len(not_carried), "deferred": section.deferred,
            "not_carried_routines": not_carried[:ROUTINES_SHOWN], "label": STATIC_LABEL}  # fmt: skip
    if section.carried == 0 and section.deferred == 0:
        return {"status": "nothing_to_deploy", **base}
    now = status(clients, name)
    if now.get("full_load_percent") != 100:
        raise MoveError(f"the routines are created after the full load; the full load is at "
                        f"{now.get('full_load_percent')}% ({now.get('phase')}): run this again when "
                        "status shows full_load_percent 100")  # fmt: skip
    scripts = write_routine_scripts(store, db, job_id, step.contract, step.contract_sha256,
                                    _step_dir(root, db, job_id),
                                    target_database=inputs.target_database_name,
                                    target_user=target_user)  # fmt: skip
    refused = [{"routine_id": i, "kind": k, "reason": c} for k, i, c in scripts.refused]
    return {"status": "ready", **base, "in_script": scripts.in_script, "refused": len(refused),
            "refused_routines": refused[:ROUTINES_SHOWN], "files": scripts.files}  # fmt: skip


def routines_check(root: str, db: str, job_id: str, inputs: MoveInputs) -> dict[str, Any]:
    """Read the customer's output of the routine check script (``routines-check.tsv``),
    strictly, and write ``routines-check.json`` for the gate. No AWS call. A refused file
    removes an earlier result, so the gate never reads a check the file contradicts."""
    from src.contracts.routine_check import STATIC_LABEL, RoutineCheckResult
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import load_step1
    from src.tools.data_movement.routine_check import RoutineCheckOutputError, read_check
    from src.tools.data_movement.routine_scripts import CHECK_OUTPUT

    store = LocalArtifactStore(base_dir=root)
    step = load_step1(store, db, job_id, with_ddl=False)
    output = _step_dir(root, db, job_id) / CHECK_OUTPUT
    key = f"{db}/{job_id}/data-movement/step-1/routines-check.json"
    try:
        result = read_check(output, step.contract, job_id=job_id, database_name=db,
                            contract_sha=step.contract_sha256,
                            target_database=inputs.target_database_name)  # fmt: skip
    except RoutineCheckOutputError as exc:
        store.delete(key)
        raise MoveError(f"{output}: refused: {exc}") from None
    if result is None:
        store.delete(key)
        return {"status": "no_output", "expected": str(output), "label": STATIC_LABEL}
    checked: RoutineCheckResult = result
    store.write_json(key, checked.model_dump(mode="json"))
    return {"status": result.status, "label": result.label, "carried": result.carried,
            "confirmed": result.confirmed, "deferred": result.deferred,
            "mismatches": [m.model_dump(mode="json") for m in result.mismatches[:ROUTINES_SHOWN]],
            "mismatch_count": len(result.mismatches), "not_carried": len(result.not_carried),
            "not_carried_routines": [m.model_dump(mode="json") for m in result.not_carried[:ROUTINES_SHOWN]],
            "result": str(Path(root) / key)}  # fmt: skip


# --- Entry point -------------------------------------------------------------------------


# Botocore's errors for credentials that are missing or have run out (#2061).
CREDENTIAL_ERRORS = ("NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError",
                     "SSOError", "SSOTokenLoadError", "UnauthorizedSSOTokenError",
                     "TokenRetrievalError", "LoginTokenLoadError")  # fmt: skip
CREDENTIAL_CODES = {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
                    "UnrecognizedClientException"}  # fmt: skip
READ_CALLS = ("Describe", "List", "Get")  # any other call this script makes writes


def _track_writes(clients: MoveClients) -> tuple[list[str], Callable[[], None]]:
    """The AWS writes this run made, in order, and the call that stops recording them."""
    made: list[str] = []

    def record(http_response: Any, model: Any, **_kwargs: Any) -> None:
        if http_response.status_code < 300 and not model.name.startswith(READ_CALLS):
            made.append(model.name)

    targets = [c for c in (clients.cloudformation, clients.dms, clients.s3, clients.sts) if c]
    for client in targets:
        client.meta.events.register("after-call", record)

    def stop() -> None:
        for client in targets:
            client.meta.events.unregister("after-call", record)

    return made, stop


def _credential_error(exc: Exception) -> bool:
    from botocore import exceptions
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") in CREDENTIAL_CODES
    return isinstance(exc, tuple(getattr(exceptions, n) for n in CREDENTIAL_ERRORS))


def credentials_error(exc: Exception, made: list[str]) -> dict[str, Any]:
    """``kind`` ``credentials``: what to do, and what this run had already changed."""
    text = (f"the AWS credentials are missing or have expired ({type(exc).__name__}: {exc}): "
            "refresh your credentials and run /move step-1 again")  # fmt: skip
    if made:
        text += (f"; this run already called {', '.join(dict.fromkeys(made))} before the "
                 "credentials ran out, so status shows where the task stands")  # fmt: skip
    else:
        text += "; nothing else changed"
    return {"status": "error", "kind": "credentials", "message": text}


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
        args.wait_seconds = MAX_WAIT_SECONDS if args.command in WAITS else 0
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
    made: list[str] = []
    untrack: Callable[[], None] = lambda: None  # noqa: E731
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
            return emit({**deploy_plan(args.artifact_root, db, job_id, inputs),
                         "empty_target": empty_target(args.artifact_root, db, job_id)})  # fmt: skip
        if args.command in (VERIFY_SCRIPTS, VERIFY):
            return emit(verify_step(args.command, args.artifact_root, db, job_id, inputs))
        if args.command == ROUTINES_CHECK:
            return emit(routines_check(args.artifact_root, db, job_id, inputs))
        _use_profile(inputs.write_profile if args.command in WRITES else inputs.read_profile)
        if args.command in ("preflight", "gate"):
            return _delegate(args.command, args, inputs)
        if clients is None:
            from src.tools.aws.credentials import AWSCredentialManager

            cred = AWSCredentialManager(region=inputs.region)
            uploads = args.command == "deploy"  # only deploy may upload a large template
            clients = MoveClients(cloudformation=cred.client("cloudformation"),
                                  dms=cred.client("dms"),
                                  s3=cred.client("s3") if uploads else None,
                                  sts=cred.client("sts") if uploads else None)  # fmt: skip
            if args.command == "status":
                cloudwatch = cred.client("cloudwatch")
        name = stack_name(inputs, job_id)
        made, untrack = _track_writes(clients)
        if args.command == "status":
            retention = None if cloudwatch is None else RetentionReader(
                cloudwatch, inputs.source_kind, inputs.source_id,
                source_family(args.artifact_root, db, job_id))  # fmt: skip
            return emit(wait_status(clients, name, args.until, args.wait_seconds,
                                    args.interval_seconds, sleep=sleep, on_poll=_print_progress,
                                    lag=None if cloudwatch is None else LagReader(cloudwatch),
                                    retention=retention, change_set=args.change_set,
                                    steps=_status_context(args.artifact_root, db, job_id,
                                                          inputs.region)))  # fmt: skip
        if args.command == ROUTINES:
            return emit(routines(clients, args.artifact_root, db, job_id, inputs, name,
                                 target_user=args.target_user))  # fmt: skip
        if args.command == "deploy":
            return emit(deploy(clients, args.artifact_root, db, job_id, inputs, sleep=sleep))
        if args.command == "execute":
            return emit(execute(clients, name, args.change_set))
        if args.command == "test":
            return emit(connection_tests(clients, name))
        if args.command == "stop":
            return emit(stop(clients, name, job_id, args.wait_seconds, args.interval_seconds,
                             sleep=sleep))  # fmt: skip
        if args.command == RESTART:
            return emit(restart_validation(clients, name, job_id, args.wait_seconds,
                                           args.interval_seconds, sleep=sleep))  # fmt: skip
        if args.command in RECOVER:
            command = resume if args.command == RESUME else retry
            context = StepsContext(inputs.region, source_family(args.artifact_root, db, job_id))
            return emit(command(clients, name, job_id, args.wait_seconds, args.interval_seconds,
                                steps=context, sleep=sleep))  # fmt: skip
        if args.command == RELOAD_TABLES:
            context = StepsContext(inputs.region, source_family(args.artifact_root, db, job_id))
            return emit(reload_tables(clients, name, job_id, steps=context))
        if args.command == REVALIDATE:
            return emit(revalidate(clients, name, job_id))
        if args.command == "teardown":
            return emit(
                teardown(clients, name, job_id, args.wait_seconds, args.interval_seconds,
                         sleep=sleep, family=source_family(args.artifact_root, db, job_id))  # fmt: skip
            )
        return emit(start(clients, name))
    except (MoveError, ValueError) as exc:
        return emit({"status": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - this script must always print JSON
        if _credential_error(exc):
            return emit(credentials_error(exc, made))
        return emit({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        untrack()


if __name__ == "__main__":
    sys.exit(main())
