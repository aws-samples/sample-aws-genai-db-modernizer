# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stubbed AWS responses for the step 1 gate tests (#1800). Nothing is sent."""

from __future__ import annotations

import datetime as dt

from src.tools.data_movement.dms_gate import GateClients, GateRequest
from tests.unit.tools.aws.aws_stubs import REGION, StubbedCredMgr

NOW = dt.datetime(2026, 11, 3, 12, tzinfo=dt.UTC)
STACK = "job1-dms-step-1"
TASK = f"arn:aws:dms:{REGION}:111122223333:task:TASKRESOURCEID01"
INSTANCE = f"arn:aws:dms:{REGION}:111122223333:rep:INSTANCERESOURCE"
INSTANCE_DIM = {"Name": "ReplicationInstanceIdentifier", "Value": "dm-job1-step-1-instance"}


def request(**overrides) -> GateRequest:
    return GateRequest(**{"region": REGION, "stack_name": STACK, **overrides})


def clients(cred: StubbedCredMgr) -> GateClients:
    for service in ("dms", "cloudwatch", "cloudformation"):
        cred.stub(service)
    return GateClients.from_manager(cred)


def stat(
    table, rows=10, state="Table completed", validation="Validated", failed=0, suspended=0, errors=0
):
    schema, _, name = table.rpartition(".")
    return {"SchemaName": schema, "TableName": name, "FullLoadRows": rows, "FullLoadErrorRows": errors,
            "TableState": state, "ValidationState": validation, "ValidationFailedRecords": failed,
            "ValidationSuspendedRecords": suspended, "ValidationPendingRecords": 0}  # fmt: skip


def ready_stats():
    return [
        stat("shop.audit_log", rows=12, validation="No primary key"),
        stat("shop.customers"),
        stat("shop.orders"),
    ]


def queue(
    cred,
    sha,
    *,
    status="running",
    progress=100,
    errored=0,
    migration="full-load-and-cdc",
    stats=None,
    stat_pages=None,
    source=(1.0, 2.0),
    target=(2.0, 3.0),
    tag=None,
    task_dim="TASKRESOURCEID01",
    age_minutes=0,
    instance_arn=INSTANCE,
    metric_dims=(INSTANCE_DIM,),
):
    """Queue every response ``read_task`` consumes. ``stat_pages`` splits the table statistics
    over several pages (a Marker each); ``task_dim`` is the CloudWatch task dimension value
    (None: no metric exists); ``age_minutes`` shifts the datapoints into the past."""
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [{
        "StackName": STACK, "CreationTime": NOW, "StackStatus": "CREATE_COMPLETE",
        "Outputs": [{"OutputKey": "ReplicationTaskArn", "OutputValue": TASK}]}]}, {"StackName": STACK})  # fmt: skip
    dms = cred.stub("dms")
    task = {
        "ReplicationTaskArn": TASK, "ReplicationTaskIdentifier": "dm-job1-step-1",
        "MigrationType": migration, "Status": status, "StopReason": "Stop reason FATAL_ERROR",
        "ReplicationTaskStats": {"FullLoadProgressPercent": progress, "TablesErrored": errored},
    }  # fmt: skip
    if instance_arn:
        task["ReplicationInstanceArn"] = instance_arn
    dms.add_response("describe_replication_tasks", {"ReplicationTasks": [task]},
                     {"Filters": [{"Name": "replication-task-arn", "Values": [TASK]}]})  # fmt: skip
    dms.add_response("list_tags_for_resource", {"TagList": [{"Key": "modernizer:contract-sha256", "Value": tag or sha}]},
                     {"ResourceArn": TASK})  # fmt: skip
    pages = stat_pages or [ready_stats() if stats is None else stats]
    for i, page in enumerate(pages):
        response = {"ReplicationTaskArn": TASK, "TableStatistics": page}
        params = {"ReplicationTaskArn": TASK}
        if i < len(pages) - 1:
            response["Marker"] = f"m{i + 1}"
        if i:
            params["Marker"] = f"m{i}"
        dms.add_response("describe_table_statistics", response, params)
    if not instance_arn:
        return
    dms.add_response("describe_replication_instances", {"ReplicationInstances": [{
        "ReplicationInstanceIdentifier": "dm-job1-step-1-instance", "ReplicationInstanceArn": INSTANCE}]},
        {"Filters": [{"Name": "replication-instance-arn", "Values": [INSTANCE]}]})  # fmt: skip
    instance_dim = {"Name": "ReplicationInstanceIdentifier", "Value": "dm-job1-step-1-instance"}
    cw = cred.stub("cloudwatch")
    for name in ("CDCLatencySource", "CDCLatencyTarget"):
        # no instance filter: the listing is by namespace and metric name only
        listed = [{"Namespace": "AWS/DMS", "MetricName": name,
                   "Dimensions": [instance_dim, {"Name": "ReplicationTaskIdentifier", "Value": "OTHERTASK"}]}]  # fmt: skip
        if task_dim:
            listed.append({"Namespace": "AWS/DMS", "MetricName": name,
                           "Dimensions": [*metric_dims, {"Name": "ReplicationTaskIdentifier", "Value": task_dim}]})  # fmt: skip
        cw.add_response(
            "list_metrics", {"Metrics": listed}, {"Namespace": "AWS/DMS", "MetricName": name}
        )
    if not task_dim:
        return
    results = [{"Id": f"lag{i}", "Label": name, "StatusCode": "Complete",
                "Timestamps": [NOW - dt.timedelta(minutes=age_minutes + k + 1) for k in range(len(values))],
                "Values": list(values)}
               for i, (name, values) in enumerate((("CDCLatencySource", source), ("CDCLatencyTarget", target)))]  # fmt: skip
    cw.add_response("get_metric_data", {"MetricDataResults": results})
