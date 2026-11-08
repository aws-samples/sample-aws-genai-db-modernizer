# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's DMS gate: the evidence the customer decides the cutover on (#1800).

On demand, as often as needed, read-only: the stack's ``ReplicationTaskArn`` output
(CloudFormation), the task, its tags and table statistics (DMS), and its CDC latency
(CloudWatch). Three checks:

- **load**: the task runs in CDC (``running``, full load at 100%), every contract table is
  in the table statistics without errors, and the task carries this contract's hash tag;
- **lag**: every ``CDCLatencySource`` and ``CDCLatencyTarget`` datapoint of the window is
  under the plan's ``max_lag_seconds``;
- **parity**: every table with a key DMS can validate on is ``Validated``; one still
  validating keeps the gate ``pending``; a validation failure fails it. Tables whose key DMS
  cannot validate on (none, a unique index that allows NULLs, a LOB or unbounded key column:
  the contract's key rule, ``validation_key``, #2042) and views are named exceptions, with
  their DMS row counts and the reason.

The result, ``pass``, ``pending`` or ``fail``, goes to ``data-movement/step-1/gate.json``
with ``cutover_decision: "customer"`` and the fixed cutover message's id: the gate never
recommends. It writes nothing else and changes nothing in AWS.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from botocore.exceptions import ClientError

from src.contracts.dms_gate import (
    NOT_VALIDATED,
    DmsGate,
    GateCheck,
    GateException,
    GateStatus,
    GateTable,
)
from src.shared.cutover_message import CUTOVER_MESSAGE_ID
from src.tools.aws.credentials import AWSCredentialManager
from src.tools.data_movement.dms_step import (
    CONTRACT_TAG,
    DmsStepError,
    Step1Inputs,
    contract_tables,
    load_step1,
)
from src.tools.data_movement.validation_key import gate_reason, key_type_message, key_type_problem

LAG_METRICS = ("CDCLatencySource", "CDCLatencyTarget")
DEFAULT_WINDOW_MINUTES = 15
PERIOD_SECONDS = 60
TASK_FAILED = frozenset({"failed", "stopped", "deleting", "failed-move"})
TABLE_FAILED = frozenset({"Table error", "Table cancelled", "Table does not exist"})
_STATUS_ORDER: tuple[GateStatus, ...] = ("pass", "pending", "fail")  # best to worst
TABLE_COMPLETED = "Table completed"
STALE_PERIODS = 3
DEFAULT_STALE_SECONDS = max(STALE_PERIODS * PERIOD_SECONDS, 300)


@dataclass(frozen=True)
class GateRequest:
    region: str
    stack_name: str
    window_minutes: int = DEFAULT_WINDOW_MINUTES
    stale_seconds: int = DEFAULT_STALE_SECONDS

    def __post_init__(self) -> None:
        if self.stale_seconds <= 0:
            raise DmsStepError(
                f"the staleness limit must be at least 1 second, not {self.stale_seconds}"
            )
        if self.window_minutes <= 0:
            raise DmsStepError(
                f"the lag window must be at least 1 minute, not {self.window_minutes}"
            )


@dataclass(frozen=True)
class GateClients:
    dms: Any
    cloudwatch: Any
    cloudformation: Any

    @classmethod
    def from_manager(cls, cred: AWSCredentialManager) -> GateClients:
        return cls(dms=cred.client("dms"), cloudwatch=cred.client("cloudwatch"),
                   cloudformation=cred.client("cloudformation"))  # fmt: skip


@dataclass(frozen=True)
class TaskView:
    arn: str
    task: Mapping[str, Any]
    tags: dict[str, str]
    statistics: dict[str, Mapping[str, Any]]
    latency: dict[str, list[tuple[datetime, float]]]
    now: datetime
    lag_dimension: str | None = None  # the task dimension value CloudWatch lists; None: no metric
    lag_metric_dimensions: Mapping[str, list[dict[str, str]]] | None = (
        None  # each listed metric's own dimensions
    )


def _worst(statuses: list[GateStatus]) -> GateStatus:
    return max(statuses, key=_STATUS_ORDER.index, default="pass")


def _check(
    check_id: Literal["load", "lag", "parity"],
    reasons: list[tuple[GateStatus, str]],
    summary: str,
    evidence: Mapping[str, Any],
) -> GateCheck:
    return GateCheck(id=check_id, status=_worst([s for s, _ in reasons]), summary=summary,
                     evidence={k: str(v) for k, v in evidence.items()},
                     reasons=[r for _, r in reasons])  # fmt: skip


def stack_output(clients: GateClients, stack_name: str, key: str) -> str:
    try:
        stacks = clients.cloudformation.describe_stacks(StackName=stack_name).get("Stacks") or []
    except ClientError as exc:
        raise DmsStepError(f"cannot read stack {stack_name}: {exc}") from exc
    outputs = (
        {o.get("OutputKey"): o.get("OutputValue") for o in (stacks[0].get("Outputs") or [])}
        if stacks
        else {}
    )
    value = outputs.get(key)
    if not value:
        raise DmsStepError(f"stack {stack_name} has no {key} output: is it the dms-step-1 stack?")
    return str(value)


def task_resource_id(task_arn: str) -> str:
    """CloudWatch's ReplicationTaskIdentifier dimension: the task's resource id, the ARN's last segment."""
    return task_arn.rsplit(":", 1)[-1]


def _task_metrics(
    clients: GateClients, task_arn: str, name: str
) -> dict[str, list[dict[str, str]]]:
    """Each lag metric CloudWatch lists for this task, with its own dimensions.

    The documentation does not say which dimensions a task metric carries or whether the task
    dimension holds the task's resource id or its name, so list the metrics by namespace and
    name (read-only, no instance filter) and match the task by either.
    """
    wanted = {task_resource_id(task_arn), name}
    found: dict[str, list[dict[str, str]]] = {}
    for metric_name in LAG_METRICS:
        pages = clients.cloudwatch.get_paginator("list_metrics").paginate(
            Namespace="AWS/DMS", MetricName=metric_name
        )
        for page in pages:
            for metric in page.get("Metrics") or []:
                dims = [{"Name": str(d["Name"]), "Value": str(d["Value"])}
                        for d in metric.get("Dimensions") or []]  # fmt: skip
                if metric_name not in found and any(
                    d["Name"] == "ReplicationTaskIdentifier" and d["Value"] in wanted for d in dims
                ):
                    found[metric_name] = dims
    return found


def _latency(
    clients: GateClients, metrics: Mapping[str, list[dict[str, str]]], now: datetime, window: int
) -> dict[str, list[tuple[datetime, float]]]:
    queries = [{"Id": f"lag{i}", "Label": name, "ReturnData": True,
                "MetricStat": {"Metric": {"Namespace": "AWS/DMS", "MetricName": name, "Dimensions": metrics[name]},
                               "Period": PERIOD_SECONDS, "Stat": "Maximum"}}
               for i, name in enumerate(LAG_METRICS) if name in metrics]  # fmt: skip
    points: dict[str, list[tuple[datetime, float]]] = {name: [] for name in LAG_METRICS}
    if not queries:
        return points
    pages = clients.cloudwatch.get_paginator("get_metric_data").paginate(
        MetricDataQueries=queries, StartTime=now - timedelta(minutes=window), EndTime=now
    )
    for page in pages:
        for result in page.get("MetricDataResults") or []:
            points[LAG_METRICS[int(str(result["Id"])[3:])]].extend(
                zip(
                    result.get("Timestamps") or [],
                    (float(v) for v in result.get("Values") or []),
                    strict=True,
                )
            )
    return points


def read_task(clients: GateClients, request: GateRequest, *, now: datetime) -> TaskView:
    arn = stack_output(clients, request.stack_name, "ReplicationTaskArn")
    tasks = (
        clients.dms.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [arn]}]
        ).get("ReplicationTasks")
        or []
    )
    if not tasks:
        raise DmsStepError(f"no replication task {arn}")
    task = tasks[0]
    tags = {str(t.get("Key")): str(t.get("Value") or "")
            for t in clients.dms.list_tags_for_resource(ResourceArn=arn).get("TagList") or []}  # fmt: skip
    statistics: dict[str, Mapping[str, Any]] = {}
    for page in clients.dms.get_paginator("describe_table_statistics").paginate(
        ReplicationTaskArn=arn
    ):
        for s in page.get("TableStatistics") or []:
            statistics[f"{s.get('SchemaName')}.{s.get('TableName')}"] = s
    instance_arn = task.get("ReplicationInstanceArn")
    if not instance_arn:
        raise DmsStepError(f"replication task {arn} reports no replication instance")
    instances = (
        clients.dms.describe_replication_instances(
            Filters=[{"Name": "replication-instance-arn", "Values": [instance_arn]}]
        ).get("ReplicationInstances")
        or []
    )
    if not instances:
        raise DmsStepError(f"no replication instance {instance_arn}")
    metrics = _task_metrics(clients, arn, str(task.get("ReplicationTaskIdentifier") or ""))
    task_dim = next((d["Value"] for dims in metrics.values() for d in dims
                     if d["Name"] == "ReplicationTaskIdentifier"), None)  # fmt: skip
    latency = _latency(clients, metrics, now, request.window_minutes)
    return TaskView(arn=arn, task=task, tags=tags, statistics=statistics, latency=latency, now=now,
                    lag_dimension=task_dim, lag_metric_dimensions=metrics)  # fmt: skip


def load_check(inputs: Step1Inputs, view: TaskView) -> GateCheck:
    task, stats = view.task, view.task.get("ReplicationTaskStats") or {}
    status = str(task.get("Status") or "")
    progress = float(stats.get("FullLoadProgressPercent") or 0)
    reasons: list[tuple[GateStatus, str]] = []
    if task.get("MigrationType") != "full-load-and-cdc":
        reasons.append(
            (
                "fail",
                f"The task's migration type is {task.get('MigrationType')}, not full-load-and-cdc",
            )
        )
    tag = view.tags.get(CONTRACT_TAG)
    if tag != inputs.contract_sha256:
        reasons.append(("fail", f"The deployed task was rendered from another transformation contract (its "
                        f"{CONTRACT_TAG} tag is {tag or 'missing'}): render dms-step-1.yaml again and update the stack"))  # fmt: skip
    if status in TASK_FAILED:
        why = task.get("StopReason") or task.get("LastFailureMessage") or "no reason given"
        reasons.append(("fail", f"The task is {status}: {why}"))
    elif status != "running":
        reasons.append(("pending", f"The task is {status or 'not started'}, not running"))
    if progress < 100:
        reasons.append(("pending", f"The full load is at {progress:g}%"))
    if int(stats.get("TablesErrored") or 0):
        errored = int(stats["TablesErrored"])
        reasons.append(("fail", f"{errored} {'table' if errored == 1 else 'tables'} errored"))
    selected = {table.table_id for table in contract_tables(inputs.contract)}
    for table_id in sorted(selected):
        s = view.statistics.get(table_id)
        if s is None:
            reasons.append(("fail" if progress >= 100 else "pending",
                            f"{table_id} is not in the task's table statistics"))  # fmt: skip
            continue
        state = str(s.get("TableState") or "")
        bad_rows = int(s.get("FullLoadErrorRows") or 0) + int(
            s.get("FullLoadCondtnlChkFailedRows") or 0
        )
        if state in TABLE_FAILED or bad_rows:
            reasons.append(("fail", f"{table_id}: {state or 'no state'} "
                            f"({bad_rows} rows failed to load)"))  # fmt: skip
        elif state != TABLE_COMPLETED:
            reasons.append(
                (
                    "pending",
                    f"{table_id} is {state or 'in no reported state'}, not {TABLE_COMPLETED}",
                )
            )
    for table_id in sorted(set(view.statistics) - selected):
        reasons.append(("fail", f"DMS reports {table_id}, which the contract does not select"))
    rows = sum(int(s.get("FullLoadRows") or 0) for s in view.statistics.values())
    evidence = {"task_status": status or "unknown", "full_load_percent": f"{progress:g}", "rows_loaded": rows,
                "tables_reported": len(view.statistics), "contract_tag": tag or "missing"}  # fmt: skip
    summary = ("The task runs in CDC; every contract table is loaded" if not reasons
               else f"{len(reasons)} load finding(s)")  # fmt: skip
    return _check("load", reasons, summary, evidence)


def lag_check(
    view: TaskView,
    max_lag_seconds: int,
    window_minutes: int,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> GateCheck:
    evidence: dict[str, Any] = {
        "window_minutes": window_minutes,
        "max_lag_seconds": max_lag_seconds,
        "stale_seconds": stale_seconds,
    }
    reasons: list[tuple[GateStatus, str]] = []
    if view.lag_dimension is None:
        reasons.append(
            ("pending", "Lag cannot be judged: no CDC latency metric found for the task")
        )
    stale_before = view.now - timedelta(seconds=stale_seconds)
    for name in LAG_METRICS:
        points = view.latency.get(name) or []
        values = [v for _, v in points]
        evidence[f"{name}_datapoints"] = len(values)
        evidence[f"{name}_max"] = f"{max(values):g}" if values else "none"
        evidence[f"{name}_newest_age_s"] = (
            f"{(view.now - max(t for t, _ in points)).total_seconds():g}" if points else "none"
        )
        if not values:
            if view.lag_dimension is not None:
                reasons.append(
                    ("pending", f"No {name} datapoints in the last {window_minutes} minutes")
                )
            continue
        if max(t for t, _ in points) < stale_before:
            reasons.append(("pending", f"Lag cannot be judged: no recent latency data, the newest {name} datapoint is older "
                            f"than {stale_seconds} s"))  # fmt: skip
            continue
        over = [v for v in values if v >= max_lag_seconds]
        if over:
            reasons.append(("fail", f"{len(over)} of {len(values)} {name} datapoints at or over {max_lag_seconds} s "
                            f"(highest {max(over):g} s)"))  # fmt: skip
    summary = (f"Every latency datapoint of the last {window_minutes} minutes is under {max_lag_seconds} s"
               if not reasons else f"{len(reasons)} lag finding(s)")  # fmt: skip
    return _check("lag", reasons, summary, evidence)


VALIDATED = "validated"
NOT_ENABLED = "not enabled"
# Every ValidationState value botocore's DMS model documents, apart from Validated, lands in one
# set. DMS's own spelling varies in letter case (3.6.1 reports "No primary Key"), so every state
# is compared through validation_state(), case-folded (#2042), here and in run_dms_step.py.
# States with records still waiting for validation; with "Preparing table", the pending set.
PENDING_RECORDS = frozenset({"pending records", "pending validation", "pending revalidation"})
VALIDATION_PENDING = PENDING_RECORDS | {"preparing table"}
# "Not enabled", "No primary key" and "Suspended records" on a table the contract says DMS can
# validate on its key mean validation cannot vouch for it: never a pass.
VALIDATION_FAILED = frozenset({"mismatched records", "suspended records", "table error", "error",
                               "not enabled", "no primary key"})  # fmt: skip
KEYLESS_LOB = (
    "a table without a primary key with LOB columns: DMS supports LOB data types only on tables "
    "that include a primary key, so LOB values can be lost"
)
# The contract findings that say DMS cannot validate a table (validation_key.validation_problem).
_KEY_FINDINGS = ("no_key", "key_not_validatable")


def validation_state(stat: Mapping[str, Any]) -> str:
    """A table statistic's ``ValidationState``, case-folded, to compare with this module's sets."""
    return str(stat.get("ValidationState") or "").casefold()


def _count(stat: Mapping[str, Any], key: str) -> int:
    return int(stat.get(key) or 0)


def _table_verdict(s: Mapping[str, Any]) -> tuple[GateStatus, str | None]:
    """One keyed table's verdict from its DMS statistics; a pass only on a clean ``Validated``."""
    state = s.get("ValidationState")
    folded = validation_state(s)
    failed = _count(s, "ValidationFailedRecords")
    suspended = _count(s, "ValidationSuspendedRecords")
    pending = _count(s, "ValidationPendingRecords")
    if failed or suspended or folded in VALIDATION_FAILED:
        return "fail", (
            f"validation {state or 'failed'}: {failed} failed and {suspended} suspended records"
        )
    if folded == VALIDATED:
        if pending:
            return "pending", f"validated, with {pending} records still pending validation"
        return "pass", None
    if folded in VALIDATION_PENDING:
        return "pending", f"validation {state}"
    return "pending", f"DMS reports the validation state {state or 'none'}, not Validated"


def _not_validated(inputs: Step1Inputs, table_id: str) -> str | None:
    """Why DMS cannot validate the table, from the contract's key rule over the collection
    (never from DMS's reported state), or None when it can.

    The contract's ``no_key`` or ``key_not_validatable`` finding says why. A contract without
    that finding (one written before #2042) is checked here with the same type rule, on its
    key columns' target types (``key_columns``, else ``primary_key``)."""
    rule = next((r for r in inputs.contract.rules if r.source_table == table_id), None)
    if rule is None:
        return None
    finding = next((f for f in rule.findings if f.code in _KEY_FINDINGS), None)
    if finding is not None:
        return gate_reason(finding.message)
    types = {f.source_column: f.target_type for f in rule.fields}
    dialect = inputs.plan.source.dialect
    key = rule.key_columns or rule.primary_key
    bad = [(c, why) for c in key if c in types
           and (why := key_type_problem(types[c], dialect=dialect))]  # fmt: skip
    label = "primary key" if key == rule.primary_key else "unique key"
    return gate_reason(key_type_message(label, bad)) if bad else None


def parity_check(
    inputs: Step1Inputs, view: TaskView
) -> tuple[GateCheck, list[GateTable], list[GateException]]:
    reasons: list[tuple[GateStatus, str]] = []
    tables: list[GateTable] = []
    exceptions: list[GateException] = []
    keyed = validated = 0
    pending_records = pending_tables = 0
    replication = inputs.plan.replication
    lob_tables = {t.table for t in replication.lob_tables} if replication else set()

    def rows_of(table_id: str) -> int | None:
        rows = (view.statistics.get(table_id) or {}).get("FullLoadRows")
        return None if rows is None else int(rows)

    for t in contract_tables(inputs.contract):
        s = view.statistics.get(t.table_id) or {}
        rows = rows_of(t.table_id)
        not_validated = _not_validated(inputs, t.table_id) or (None if t.keyed else NOT_VALIDATED)
        if not_validated:
            exceptions.append(GateException(table=t.table_id, kind="table", reason=not_validated,
                                            rows_loaded=rows))  # fmt: skip
            if not t.keyed and t.table_id in lob_tables:
                exceptions.append(GateException(table=t.table_id, kind="keyless_lob", reason=KEYLESS_LOB,
                                                rows_loaded=rows))  # fmt: skip
            status: GateStatus | Literal["exception"] = "exception"
            why: str | None = not_validated
        else:
            keyed += 1
            status, why = (
                ("pending", "not in the table statistics yet") if not s else _table_verdict(s)
            )
            if status == "pass":
                validated += 1
            if _count(s, "ValidationPendingRecords"):
                pending_records += _count(s, "ValidationPendingRecords")
                pending_tables += 1
            if status != "exception" and why:
                reasons.append((status, f"{t.table_id}: {why}"))
        tables.append(GateTable(table=t.table_id, rows_loaded=rows, table_state=s.get("TableState"),
                                validation_state=s.get("ValidationState"),
                                validation_failed_records=_count(s, "ValidationFailedRecords"),
                                validation_suspended_records=_count(s, "ValidationSuspendedRecords"),
                                status=status, reason=why))  # fmt: skip
    for view_id in inputs.contract.views:
        exceptions.append(GateException(table=view_id, kind="view", reason=NOT_VALIDATED))
    for u in inputs.unsupported:
        exceptions.append(GateException(table=u.table_id, kind="unsupported_field", column=u.column,
                                        reason=u.description or "no operation can express this field",
                                        rows_loaded=rows_of(u.table_id)))  # fmt: skip
    for sk in inputs.skipped:
        exceptions.append(GateException(table=sk.table_id, kind="skipped_rule", reason=sk.reason or "blocked",
                                        rows_loaded=rows_of(sk.table_id)))  # fmt: skip
    for nm in inputs.not_moved:
        exceptions.append(GateException(table=nm.table_id, kind="not_moved", reason=nm.reason))
    exceptions.sort(key=lambda e: (e.table, e.kind, e.column or ""))
    tables.sort(key=lambda t: t.table)
    if keyed:
        names = len({e.table for e in exceptions})
        summary = (f"{validated} of {keyed} tables with a key validated; {len(exceptions)} named exceptions "
                   f"({names} {'table' if names == 1 else 'tables'})")  # fmt: skip
    else:
        summary = (
            "No table has a key DMS can validate with: nothing was validated; see the exceptions."
        )
    check = _check("parity", reasons, summary, {"keyed_tables": keyed, "validated": validated,
                                                "exceptions": len(exceptions),
                                                "pending_records": pending_records,
                                                "tables_with_pending_records": pending_tables})  # fmt: skip
    return check, tables, exceptions


def run_gate(
    inputs: Step1Inputs, request: GateRequest, clients: GateClients, *, now: datetime
) -> DmsGate:
    view = read_task(clients, request, now=now)
    max_lag = inputs.plan.gates.max_lag_seconds
    parity, tables, exceptions = parity_check(inputs, view)
    checks = [
        load_check(inputs, view),
        lag_check(view, max_lag, request.window_minutes, request.stale_seconds),
        parity,
    ]
    status = _worst([c.status for c in checks])
    return DmsGate(
        job_id=inputs.plan.job_id, database_name=inputs.plan.database_name, region=request.region,
        stack_name=request.stack_name, task_arn=view.arn, checked_at=now, window_minutes=request.window_minutes,
        max_lag_seconds=max_lag, contract_sha256=inputs.contract_sha256, status=status,
        cutover_decision="customer", cutover_message_id=CUTOVER_MESSAGE_ID, checks=checks, tables=tables,
        exceptions=exceptions,
    )  # fmt: skip


def write_gate(store: Any, db: str, job_id: str, request: GateRequest, clients: GateClients, *,
               now: datetime | None = None) -> tuple[str, DmsGate]:  # fmt: skip
    inputs = load_step1(store, db, job_id, with_ddl=False)
    gate = run_gate(inputs, request, clients, now=now or datetime.now(UTC))
    path = f"{db}/{job_id}/data-movement/step-1/gate.json"
    store.write_json(path, gate.model_dump(mode="json"))
    return path, gate
