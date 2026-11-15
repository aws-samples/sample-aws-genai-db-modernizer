# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Botocore Stubber answers for ``scripts/run_dms_step.py``'s step 1 reads (#1886).

Shared by the unit tests of ``run_dms_step.py`` and the e2e tests that take a sample job
to step 1 (``tests/e2e/test_sample_routine_scripts.py``): the inputs file, the stack's
outputs and the DMS reads ``status`` makes. Every answer goes to a Stubber, so nothing
is sent."""

from __future__ import annotations

from scripts import run_dms_step as script
from tests.unit.tools.aws.aws_stubs import REGION, StubbedCredMgr
from tests.unit.tools.data_movement import preflight_stubs as s

STACK = "dm-job1-step-1"
STACK_ID = f"arn:aws:cloudformation:us-east-1:111122223333:stack/{STACK}/1"
ACCOUNT = "111122223333"
INSTANCE = f"arn:aws:dms:{REGION}:{ACCOUNT}:rep:INSTANCE"
TASK = f"arn:aws:dms:{REGION}:{ACCOUNT}:task:TASK"
SOURCE_EP = f"arn:aws:dms:{REGION}:{ACCOUNT}:endpoint:SOURCE"
TARGET_EP = f"arn:aws:dms:{REGION}:{ACCOUNT}:endpoint:TARGET"
OUTPUTS = {"ReplicationInstanceArn": INSTANCE, "ReplicationTaskArn": TASK,
           "SourceEndpointArn": SOURCE_EP, "TargetEndpointArn": TARGET_EP}  # fmt: skip
# A complete inputs file: ids and ARNs only, never a password.
INPUTS = {
    "region": REGION,
    "aurora_cluster_id": s.CLUSTER,
    "source_kind": "rds_instance",
    "source_id": "shop-source",
    "source_secret_arn": s.SOURCE_SECRET,
    "target_secret_arn": s.TARGET_SECRET,
    "source_certificate_arn": s.SOURCE_CERT,
    "target_certificate_arn": s.TARGET_CERT,
    "subnet_ids": list(s.SUBNETS),
    "security_group_ids": ["sg-0123"],
    "multi_az": False,
}


def clients(cred: StubbedCredMgr, uploads: bool = False) -> script.MoveClients:
    """The clients main() builds: S3 and STS only for a deploy that may upload a template."""
    return script.MoveClients(cloudformation=cred.client("cloudformation"), dms=cred.client("dms"),
                              s3=cred.client("s3") if uploads else None,
                              sts=cred.client("sts") if uploads else None)  # fmt: skip


def stack(cred, status="CREATE_COMPLETE", outputs=OUTPUTS, job_tag=None, name=STACK):
    stack = {
        "StackName": name,
        "StackId": f"arn:aws:cloudformation:us-east-1:111122223333:stack/{name}/1",
        "CreationTime": "2026-11-04T00:00:00Z",
        "StackStatus": status,
        "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
    }
    if job_tag is not None:
        stack["Tags"] = [{"Key": "modernizer:job-id", "Value": job_tag}]
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [stack]},
                                             {"StackName": name})  # fmt: skip


def connections(cred, source="successful", target="successful"):
    cred.stub("dms").add_response("describe_connections", {"Connections": [
        {"EndpointArn": SOURCE_EP, "ReplicationInstanceArn": INSTANCE, "Status": source},
        {"EndpointArn": TARGET_EP, "ReplicationInstanceArn": INSTANCE, "Status": target,
         **({"LastFailureMessage": "access denied"} if target == "failed" else {})}]})  # fmt: skip


def task(cred, status="ready", percent=0):
    cred.stub("dms").add_response("describe_replication_tasks", {"ReplicationTasks": [{
        "ReplicationTaskArn": TASK, "Status": status,
        "ReplicationTaskStats": {"FullLoadProgressPercent": percent, "TablesLoaded": 2}}]})  # fmt: skip


def stats(cred, validation="Validated", pending=0, marker=None):
    page = {"ReplicationTaskArn": TASK, "TableStatistics": [
        {"SchemaName": "shop", "TableName": "orders", "ValidationState": validation,
         "ValidationPendingRecords": pending},
        {"SchemaName": "shop", "TableName": "audit_log", "ValidationState": "No primary key"}]}  # fmt: skip
    if marker:
        page["Marker"] = marker
    cred.stub("dms").add_response("describe_table_statistics", page)


def full_load_done(cred: StubbedCredMgr, name: str) -> None:
    """The reads ``status`` makes for a stack whose task's full load is at 100%."""
    stack(cred, name=name)
    connections(cred)
    task(cred, "running", 100)
    stats(cred)
