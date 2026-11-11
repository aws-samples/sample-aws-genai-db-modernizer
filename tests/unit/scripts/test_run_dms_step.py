# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py: /move's step 1 helper, one JSON line per call (#1886).

Every AWS call goes to a botocore Stubber: nothing is sent."""

from __future__ import annotations

import dataclasses
import functools
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.stub import ANY

from scripts import check_dms_step_gate, check_dms_step_preflight
from scripts import run_dms_step as script
from src.tools.data_movement.dms_step import (
    CUSTOMER_RESPONSIBILITY,
    bundle_inputs,
    render_files,
    write_step1_delivery,
)
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.aws.aws_stubs import REGION, StubbedCredMgr
from tests.unit.tools.data_movement import preflight_stubs as s
from tests.unit.tools.data_movement.dms_step_fixtures import (
    make_bundle,
    make_step1_inputs,
    write_job,
    write_pg_job,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GONE = "DMS has no such task (the task is gone, not running)"
STACK = "dm-job1-step-1"
STACK_ID = f"arn:aws:cloudformation:us-east-1:111122223333:stack/{STACK}/1"
ACCOUNT = "111122223333"
INSTANCE = f"arn:aws:dms:{REGION}:{ACCOUNT}:rep:INSTANCE"
TASK = f"arn:aws:dms:{REGION}:{ACCOUNT}:task:TASK"
SOURCE_EP = f"arn:aws:dms:{REGION}:{ACCOUNT}:endpoint:SOURCE"
TARGET_EP = f"arn:aws:dms:{REGION}:{ACCOUNT}:endpoint:TARGET"
OUTPUTS = {"ReplicationInstanceArn": INSTANCE, "ReplicationTaskArn": TASK,
           "SourceEndpointArn": SOURCE_EP, "TargetEndpointArn": TARGET_EP}  # fmt: skip
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


@pytest.fixture(autouse=True)
def _restore_aws_profile(monkeypatch):
    """The helper sets AWS_PROFILE from the inputs file: put the caller's back afterwards."""
    monkeypatch.setenv("AWS_PROFILE", "unset-by-test")
    monkeypatch.delenv("AWS_PROFILE")


def _job(tmp_path: Path, **inputs) -> Path:
    b = make_bundle()
    # Its LOB tables measured, so step 1 writes the stack (an unmeasured MySQL LOB table gets
    # none, #2123: test_run_dms_step_lob_sizes.py).
    b = b.model_copy(update={"plan": make_step1_inputs(measured=True).plan})
    # The finished job (report, assignment, collector) the summary reads the collection from.
    store = write_job(tmp_path, contract=False, plan=False)
    store.write_json("shop/job1/data-movement/step-1/plan.json", b.plan.model_dump(mode="json"))
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    for name, text in render_files(bundle_inputs(b)).items():
        store.write_bytes(f"shop/job1/data-movement/step-1/{name}", text.encode())
    path = tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "inputs.json"
    path.write_text(json.dumps({**INPUTS, **inputs}))
    return path


def _run(tmp_path, command, *extra, clients=None, capsys):
    code = script.main([command, "--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path),
                        *extra], clients=clients, sleep=lambda _s: None)  # fmt: skip
    return code, json.loads(capsys.readouterr().out.splitlines()[-1])  # the result line is last


def _clients(cred: StubbedCredMgr, uploads: bool = False) -> script.MoveClients:
    """The clients main() builds: S3 and STS only for a deploy that may upload a template."""
    return script.MoveClients(cloudformation=cred.client("cloudformation"), dms=cred.client("dms"),
                              s3=cred.client("s3") if uploads else None,
                              sts=cred.client("sts") if uploads else None)  # fmt: skip


def _stack(cred, status="CREATE_COMPLETE", outputs=OUTPUTS, job_tag=None, name=STACK):
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


def _no_stack(cred):
    cred.stub("cloudformation").add_client_error(
        "describe_stacks", "ValidationError", f"Stack with id {STACK} does not exist"
    )


def _poll(cred, status, reason=None):
    """describe_stacks by the stack id teardown deleted: a gone stack stays, as DELETE_COMPLETE."""
    stack = {"StackName": STACK, "StackId": STACK_ID, "CreationTime": "2026-11-04T00:00:00Z",
             "StackStatus": status, **({"StackStatusReason": reason} if reason else {})}  # fmt: skip
    cred.stub("cloudformation").add_response(
        "describe_stacks", {"Stacks": [stack]}, {"StackName": STACK_ID}
    )


def _connections(cred, source="successful", target="successful"):
    cred.stub("dms").add_response("describe_connections", {"Connections": [
        {"EndpointArn": SOURCE_EP, "ReplicationInstanceArn": INSTANCE, "Status": source},
        {"EndpointArn": TARGET_EP, "ReplicationInstanceArn": INSTANCE, "Status": target,
         **({"LastFailureMessage": "access denied"} if target == "failed" else {})}]})  # fmt: skip


def _task(cred, status="ready", percent=0):
    cred.stub("dms").add_response("describe_replication_tasks", {"ReplicationTasks": [{
        "ReplicationTaskArn": TASK, "Status": status,
        "ReplicationTaskStats": {"FullLoadProgressPercent": percent, "TablesLoaded": 2}}]})  # fmt: skip


def _stats(cred, validation="Validated", pending=0, marker=None):
    page = {"ReplicationTaskArn": TASK, "TableStatistics": [
        {"SchemaName": "shop", "TableName": "orders", "ValidationState": validation,
         "ValidationPendingRecords": pending},
        {"SchemaName": "shop", "TableName": "audit_log", "ValidationState": "No primary key"}]}  # fmt: skip
    if marker:
        page["Marker"] = marker
    cred.stub("dms").add_response("describe_table_statistics", page)


# --- Inputs -------------------------------------------------------------------------------


def test_the_inputs_file_refuses_an_unknown_key_such_as_a_password(tmp_path, capsys):
    _job(tmp_path, password="hunter2")  # pragma: allowlist secret
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 1 and out["status"] == "error"
    assert "password" in out["message"] and "hunter2" not in out["message"]


def test_the_inputs_file_refuses_a_value_that_is_not_an_arn_or_id(tmp_path, capsys):
    bad: dict[str, object] = {"security_group_ids": ["default"]}
    bad["source_secret_arn"] = "not-an-arn"  # pragma: allowlist secret
    _job(tmp_path, **bad)
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 1 and "source_secret_arn" in out["message"]


def test_a_missing_inputs_file_says_where_it_belongs(tmp_path, capsys):
    _job(tmp_path).unlink()
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 1 and "inputs.json" in out["message"]


def test_a_db_that_is_not_a_plain_name_is_refused(tmp_path, capsys):
    code = script.main(
        ["plan", "--job-id", "job1", "--db", "../x", "--artifact-root", str(tmp_path)]
    )
    assert code == 1 and json.loads(capsys.readouterr().out)["status"] == "error"


# --- No AWS call --------------------------------------------------------------------------


def test_plan_maps_the_inputs_to_the_template_parameters_with_no_aws_call(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    code, out = _run(tmp_path, "plan", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "ready" and out["stack_name"] == STACK
    assert out["parameters"] == {
        "SubnetIds": "subnet-aaa,subnet-bbb", "SecurityGroupIds": "sg-0123",
        "SourceSecretArn": s.SOURCE_SECRET, "TargetSecretArn": s.TARGET_SECRET,
        "SourceCertificateArn": s.SOURCE_CERT, "TargetCertificateArn": s.TARGET_CERT, "MultiAZ": "false",
    }  # fmt: skip
    assert (
        "aws cloudformation deploy" in out["command"]
        and "--capabilities CAPABILITY_IAM" in out["command"]
    )
    assert "MultiAZ=false" in out["command"]
    assert out["command"].endswith("--tags modernizer:job-id=job1 modernizer:step=1")
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()


def test_plan_names_the_parameters_the_inputs_leave_without_a_value(tmp_path, capsys):
    _job(tmp_path, source_certificate_arn="", target_certificate_arn="")
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 1
    assert "SourceCertificateArn (source_certificate_arn)" in out["message"]
    assert "TargetCertificateArn (target_certificate_arn)" in out["message"]


def test_plan_refuses_a_value_the_rendered_template_has_no_parameter_for(tmp_path, capsys):
    _job(tmp_path, source_database_name="shop")  # a MySQL template has no SourceDatabaseName
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 1 and "SourceDatabaseName" in out["message"]


def test_plan_takes_the_stack_name_from_the_inputs(tmp_path, capsys):
    _job(tmp_path, stack_name="my-step-1")
    assert _run(tmp_path, "plan", capsys=capsys)[1]["stack_name"] == "my-step-1"


def test_summary_quotes_the_disclaimer_and_the_plans_figures(tmp_path, capsys):
    _job(tmp_path)
    code, out = _run(tmp_path, "summary", capsys=capsys)
    b = make_bundle()
    assert code == 0 and out["disclaimer"] == CUSTOMER_RESPONSIBILITY
    assert out["contract"]["status"] == b.contract.status and out["contract"]["blocked_rules"] == []
    assert out["contract"]["warning_count"] == 1 and out["table_count"] == len(b.contract.rules)
    assert out["replication_instance"]["instance_class"] == b.plan.replication.sizing.instance_class
    assert out["replication_instance"]["engine_version"] == "3.6.1"
    assert out["monthly_cost_usd"] == b.plan.monthly_cost_usd
    assert set(out["files"]) == {"aurora-target.yaml", "step-1.ddl.sql", "dms-step-1.yaml"}
    assert out["files_missing"] == []


def test_summary_lists_what_a_mysql_step_1_does_not_move(tmp_path, capsys):
    _job(tmp_path)
    out = _run(tmp_path, "summary", capsys=capsys)[1]
    kinds = {n["kind"]: n for n in out["not_moved"]}
    assert list(kinds) == [
        "procedures",
        "functions",
        "triggers",
        "views",
        "events",
        "sequence values",
    ]
    assert kinds["views"]["count"] == 1 and kinds["procedures"]["count"] == 0
    assert kinds["events"]["count"] is None and "SHOW EVENTS" in kinds["events"]["note"]
    assert kinds["sequence values"]["count"] is None
    assert kinds["sequence values"]["note"].startswith("none to set: an AUTO_INCREMENT counter")


def test_summary_lists_the_sequences_file_and_the_not_moved_counts_on_postgresql(tmp_path, capsys):
    routines = [{"procedure_id": "public.f", "procedure_name": "f", "procedure_type": "FUNCTION"}]
    store = write_pg_job(tmp_path, procedures=routines)
    write_step1_delivery(store, "shop", "job1")
    out = _run(tmp_path, "summary", capsys=capsys)[1]
    assert out["status"] == "complete", out
    assert (
        out["files"]["step-1.sequences.sql"]
        == "shop/job1/data-movement/step-1/step-1.sequences.sql"
    )
    assert out["files_missing"] == []
    assert out["not_moved"] == [
        {"kind": "procedures", "count": 0, "note": "none in the collection"},
        {"kind": "functions", "count": 1, "note": "recreate them on Aurora yourself"},
        {"kind": "triggers", "count": 0, "note": "none in the collection"},
        {"kind": "views", "count": 1, "note": "recreate them on Aurora yourself"},
        {"kind": "sequence values", "count": 2, "note": "step-1.sequences.sql sets them"},
    ]


def test_steps_lists_the_plans_and_supports_step_1_only(tmp_path, capsys):
    _job(tmp_path)
    later = tmp_path / "shop" / "job1" / "data-movement" / "step-2"
    later.mkdir(parents=True)
    plan = make_bundle().plan.model_copy(update={"step": 2, "title": "Move to DynamoDB", "wave": 2})
    (later / "plan.json").write_text(plan.model_dump_json())
    code, out = _run(tmp_path, "steps", capsys=capsys)
    assert code == 0
    assert [(x["step"], x["supported"]) for x in out["steps"]] == [(1, True), (2, False)]


def test_steps_refuses_a_plan_that_is_not_a_plan(tmp_path, capsys):
    _job(tmp_path)
    (tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "plan.json").write_text("{}")
    code, out = _run(tmp_path, "steps", capsys=capsys)
    assert code == 1 and "plan.json" in out["message"]


def test_steps_without_plans_is_an_error(tmp_path, capsys):
    code, out = _run(tmp_path, "steps", capsys=capsys)
    assert code == 1 and "run_data_movement_plan.py" in out["message"]


# --- Writes need approval ------------------------------------------------------------------


@pytest.mark.parametrize("command", script.WRITES)
def test_every_write_is_refused_without_approve_before_any_aws_call(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    code, out = _run(tmp_path, command, "--change-set", "x", clients=_clients(cred), capsys=capsys)
    assert code == 1 and out["message"].startswith(f"{command} is an AWS write: pass --approve")
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("command", script.WRITES)
def test_every_write_is_refused_under_the_ci_sandbox_even_with_approve(
    capsys, monkeypatch, command
):
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    code = script.main([command, "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(REPO_ROOT / "artifacts"), "--approve", "--change-set", "x"])  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "refused under MODERNIZER_CI_SANDBOX=1" in out["message"]


def test_reads_under_the_sandbox_still_refuse_an_inputs_path_outside_the_repo(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    code = script.main(["plan", "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(REPO_ROOT / "artifacts"), "--inputs", str(tmp_path / "i.json")])  # fmt: skip
    assert code == 1 and "--inputs" in json.loads(capsys.readouterr().out)["message"]


def test_only_cloudformation_dms_s3_and_sts_clients_exist_so_no_secret_is_read() -> None:
    assert [f.name for f in dataclasses.fields(script.MoveClients)] == [
        "cloudformation", "dms", "s3", "sts"]  # fmt: skip
    source = (REPO_ROOT / "scripts" / "run_dms_step.py").read_text()
    assert "get_secret_value" not in source and 'client("secretsmanager")' not in source


# --- deploy / execute ----------------------------------------------------------------------


def test_deploy_creates_and_describes_a_change_set_and_executes_nothing(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _no_stack(cred)
    cfn = cred.stub("cloudformation")
    cfn.add_response("create_change_set", {"Id": "cs-arn", "StackId": "stack-arn"}, {
        "StackName": STACK, "ChangeSetName": ANY, "ChangeSetType": "CREATE", "TemplateBody": ANY,
        "Capabilities": ["CAPABILITY_IAM"], "Parameters": ANY,
        "Tags": [{"Key": "modernizer:job-id", "Value": "job1"}, {"Key": "modernizer:step", "Value": "1"}]})  # fmt: skip
    cfn.add_response(
        "describe_change_set",
        {"Status": "CREATE_PENDING"},
        {"StackName": STACK, "ChangeSetName": ANY},
    )
    cfn.add_response("describe_change_set", {"Status": "CREATE_COMPLETE", "Changes": [{"Type": "Resource",
        "ResourceChange": {"Action": "Add", "LogicalResourceId": "ReplicationTask",
                           "ResourceType": "AWS::DMS::ReplicationTask"}}]}, {"StackName": STACK, "ChangeSetName": ANY})  # fmt: skip
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "change_set_ready" and out["change_set_type"] == "CREATE"
    assert out["changes"] == [{"action": "Add", "logical_id": "ReplicationTask",
                               "type": "AWS::DMS::ReplicationTask", "replacement": None}]  # fmt: skip
    assert out["change_set_name"].startswith("move-step-1-")
    cfn.assert_no_pending_responses()  # and no execute_change_set was queued or made


def test_deploy_reports_no_changes_for_an_up_to_date_stack(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    cfn = cred.stub("cloudformation")
    cfn.add_response("create_change_set", {"Id": "cs"}, {"StackName": STACK, "ChangeSetName": ANY,
        "ChangeSetType": "UPDATE", "TemplateBody": ANY, "Capabilities": ["CAPABILITY_IAM"], "Parameters": ANY,
        "Tags": script.stack_tags("job1")})  # fmt: skip
    cfn.add_response("describe_change_set", {"Status": "FAILED",
        "StatusReason": "The submitted information didn't contain changes."}, {"StackName": STACK, "ChangeSetName": ANY})  # fmt: skip
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "no_changes"


def test_deploy_refuses_a_stack_that_is_still_changing(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="CREATE_IN_PROGRESS")
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "CREATE_IN_PROGRESS" in out["message"]


def _large_template(tmp_path: Path, **inputs) -> bytes:
    """Pad the rendered template past the inline limit with a YAML comment; return its bytes."""
    _job(tmp_path, **inputs)
    path = tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "dms-step-1.yaml"
    path.write_text(path.read_text() + "# " + "x" * script.MAX_TEMPLATE_BODY + "\n")
    return path.read_bytes()


def _change_set_ready(cfn, template: dict[str, object]) -> None:
    cfn.add_response("create_change_set", {"Id": "cs-arn"}, {
        "StackName": STACK, "ChangeSetName": ANY, "ChangeSetType": "CREATE", **template,
        "Capabilities": ["CAPABILITY_IAM"], "Parameters": ANY, "Tags": ANY})  # fmt: skip
    cfn.add_response("describe_change_set", {"Status": "CREATE_COMPLETE", "Changes": []},
                     {"StackName": STACK, "ChangeSetName": ANY})  # fmt: skip


def test_deploy_uploads_a_large_template_to_the_bucket_and_creates_the_change_set_from_its_url(
    tmp_path, capsys
):
    body = _large_template(tmp_path, template_bucket="my-templates")
    cred = StubbedCredMgr()
    _no_stack(cred)
    cred.stub("sts").add_response("get_caller_identity", {"Account": ACCOUNT}, {})
    cred.stub("s3").add_response("put_object", {}, {
        "Bucket": "my-templates", "Key": f"{STACK}/dms-step-1.yaml", "Body": body,
        "ServerSideEncryption": "AES256", "ExpectedBucketOwner": ACCOUNT})  # fmt: skip
    url = f"https://my-templates.s3.{REGION}.amazonaws.com/{STACK}/dms-step-1.yaml"
    _change_set_ready(cred.stub("cloudformation"), {"TemplateURL": url})
    code, out = _run(
        tmp_path, "deploy", "--approve", clients=_clients(cred, uploads=True), capsys=capsys
    )
    assert code == 0 and out["status"] == "change_set_ready"
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()


def test_deploy_keeps_a_small_template_inline_even_with_a_bucket(tmp_path, capsys):
    _job(tmp_path, template_bucket="my-templates")
    body = (tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "dms-step-1.yaml").read_text()
    cred = StubbedCredMgr()
    _no_stack(cred)
    _change_set_ready(cred.stub("cloudformation"), {"TemplateBody": body})
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "change_set_ready"
    assert "--s3-bucket" not in _run(tmp_path, "plan", capsys=capsys)[1]["command"]
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()  # no sts or s3 call was queued or made


def test_deploy_of_a_large_template_without_a_bucket_names_template_bucket_and_calls_nothing(
    tmp_path, capsys
):
    _large_template(tmp_path)
    cred = StubbedCredMgr()
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "template_bucket" in out["message"] and "inline limit" in out["message"]
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()  # a stubbed client fails on any unqueued call


def test_plan_of_a_large_template_with_a_bucket_shows_the_s3_bucket_option(tmp_path, capsys):
    _large_template(tmp_path, template_bucket="my-templates")
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 0 and out["command"].endswith("--s3-bucket my-templates")


@pytest.mark.passes_on_main  # the sandbox refuses every deploy before it reads the inputs
def test_the_upload_is_refused_under_the_ci_sandbox_before_any_aws_call(capsys, monkeypatch):
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    cred = StubbedCredMgr()
    code = script.main(["deploy", "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(REPO_ROOT / "artifacts"), "--approve"], clients=_clients(cred))  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "refused under MODERNIZER_CI_SANDBOX=1" in out["message"]
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("bucket", ["My-Bucket", "a.b.c", "ab", "-bucket", "s3://bucket"])
def test_a_template_bucket_that_is_not_a_plain_bucket_name_is_refused(tmp_path, capsys, bucket):
    _job(tmp_path, template_bucket="my-templates-1")
    assert _run(tmp_path, "plan", capsys=capsys)[0] == 0
    _job(tmp_path, template_bucket=bucket)
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 1 and "template_bucket" in out["message"]


def test_execute_runs_only_an_available_change_set(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    cfn = cred.stub("cloudformation")
    cfn.add_response("describe_change_set", {"Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE"},
                     {"StackName": STACK, "ChangeSetName": "cs1"})  # fmt: skip
    cfn.add_response("execute_change_set", {}, {"StackName": STACK, "ChangeSetName": "cs1"})
    code, out = _run(
        tmp_path,
        "execute",
        "--change-set",
        "cs1",
        "--approve",
        clients=_clients(cred),
        capsys=capsys,
    )
    assert code == 0 and out["status"] == "executing"
    cfn.add_response("describe_change_set", {"Status": "CREATE_COMPLETE", "ExecutionStatus": "EXECUTE_COMPLETE"},
                     {"StackName": STACK, "ChangeSetName": "cs1"})  # fmt: skip
    code, out = _run(
        tmp_path,
        "execute",
        "--change-set",
        "cs1",
        "--approve",
        clients=_clients(cred),
        capsys=capsys,
    )
    assert code == 1 and "run deploy again" in out["message"]
    cfn.assert_no_pending_responses()


# --- test / start ------------------------------------------------------------------------


def test_test_tests_both_endpoints_of_the_deployed_stack(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    dms = cred.stub("dms")
    for ep in (SOURCE_EP, TARGET_EP):
        dms.add_response(
            "test_connection", {}, {"ReplicationInstanceArn": INSTANCE, "EndpointArn": ep}
        )
    code, out = _run(tmp_path, "test", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out == {
        "status": "testing",
        "stack_name": STACK,
        "endpoints": ["source", "target"],
    }
    dms.assert_no_pending_responses()


def test_start_refuses_until_both_connections_are_successful(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred, target="failed")
    _task(cred)
    code, out = _run(tmp_path, "start", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "connections_failed" in out["message"]
    cred.stub("dms").assert_no_pending_responses()


def test_start_starts_a_ready_task_with_start_replication(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred)
    _task(cred)
    _stack(cred)
    cred.stub("dms").add_response("start_replication_task", {}, {
        "ReplicationTaskArn": TASK, "StartReplicationTaskType": "start-replication"})  # fmt: skip
    code, out = _run(tmp_path, "start", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "starting"
    cred.stub("dms").assert_no_pending_responses()


def test_start_refuses_a_task_that_already_ran(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred)
    _task(cred, status="running", percent=40)
    _stats(cred)
    code, out = _run(tmp_path, "start", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "the task is running" in out["message"]


# --- status ------------------------------------------------------------------------------


def test_status_without_a_stack_says_so(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["phase"] == "no_stack"


def test_status_polls_until_the_full_load_is_done_and_every_table_validated(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred)
    _task(cred, status="running", percent=60)
    _stats(cred, validation="Pending records", pending=5)
    _stack(cred)
    _connections(cred)
    _task(cred, status="running", percent=100)
    _stats(cred, marker="page-2")
    cred.stub("dms").add_response("describe_table_statistics", {"TableStatistics": [
        {"SchemaName": "shop", "TableName": "customers", "ValidationState": "Validated"}]},
        {"ReplicationTaskArn": TASK, "Marker": "page-2"})  # fmt: skip
    code, out = _run(tmp_path, "status", "--until", "replicating", "--wait-seconds", "60",
                     "--interval-seconds", "30", clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["reached"] is True and out["phase"] == "replicating"
    assert out["validation"]["tables"] == 3 and out["validation"]["validated"] == 2
    assert out["full_load_percent"] == 100
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()


def test_status_stops_waiting_at_the_deadline():
    cred = StubbedCredMgr()
    for _ in range(3):
        _stack(cred, status="CREATE_IN_PROGRESS")
    ticks = iter([0, 0, 30, 60, 90])
    out = script.wait_status(_clients(cred), STACK, "stack", 60, 30, sleep=lambda _s: None,
                             clock=lambda: next(ticks))  # fmt: skip
    assert out["reached"] is False and out["phase"] == "stack_in_progress"
    cred.stub("cloudformation").assert_no_pending_responses()


def test_status_reports_endpoints_still_testing(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred, source="testing")
    _task(cred)
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert (
        out["phase"] == "connections_testing"
        and out["connections"]["source"]["status"] == "testing"
    )


def test_wait_seconds_is_bounded_by_the_bash_timeout(tmp_path, capsys):
    _job(tmp_path)
    code, out = _run(tmp_path, "status", "--wait-seconds", "900", capsys=capsys)
    assert code == 1 and "--wait-seconds" in out["message"]


# --- preflight / gate run the read-only scripts ----------------------------------------------


def test_preflight_runs_the_preflight_script_with_the_inputs_values_and_the_read_profile(
    tmp_path, capsys, monkeypatch
):
    _job(tmp_path, read_profile="readonly", write_profile="deployer")
    seen = {}

    def fake(argv):
        seen.update(argv=argv, profile=os.environ.get("AWS_PROFILE"))
        print(json.dumps({"status": "pass"}))
        return 0

    monkeypatch.setattr(check_dms_step_preflight, "main", fake)
    code, out = _run(tmp_path, "preflight", "--confirm", "source_cdc", capsys=capsys)
    assert code == 0 and out == {"status": "pass"} and seen["profile"] == "readonly"
    argv = seen["argv"]
    pairs = dict(zip(argv[::2], argv[1::2], strict=True))
    assert pairs["--aurora-cluster-id"] == s.CLUSTER and pairs["--source-kind"] == "rds_instance"
    assert (
        pairs["--source-id"] == "shop-source" and pairs["--subnet-ids"] == "subnet-aaa,subnet-bbb"
    )
    assert pairs["--source-certificate-arn"] == s.SOURCE_CERT and pairs["--confirm"] == "source_cdc"


def _argv_seen(tmp_path, capsys, monkeypatch, *extra):
    seen = {}

    def fake(argv):
        seen["argv"] = argv
        print(json.dumps({"status": "pass"}))
        return 0

    monkeypatch.setattr(check_dms_step_preflight, "main", fake)
    code, out = _run(tmp_path, "preflight", *extra, capsys=capsys)
    return code, out, seen.get("argv")


def _confirms(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--confirm"]


def test_preflight_passes_the_inputs_files_confirmed_checks_as_confirm_flags(
    tmp_path, capsys, monkeypatch
):
    _job(tmp_path, confirmed_checks=["source_cdc", "dms_certificates"])
    code, _out, argv = _argv_seen(tmp_path, capsys, monkeypatch)
    assert code == 0 and _confirms(argv) == ["source_cdc", "dms_certificates"]


@pytest.mark.passes_on_main  # no confirmed_checks means no --confirm, as before
def test_preflight_without_confirmed_checks_passes_no_confirm(tmp_path, capsys, monkeypatch):
    _job(tmp_path)
    _code, _out, argv = _argv_seen(tmp_path, capsys, monkeypatch)
    assert _confirms(argv) == []


def test_preflight_deduplicates_the_flag_and_the_file(tmp_path, capsys, monkeypatch):
    _job(tmp_path, confirmed_checks=["source_cdc", "source_cdc"])
    _code, _out, argv = _argv_seen(
        tmp_path, capsys, monkeypatch, "--confirm", "source_cdc", "--confirm", "secrets"
    )
    assert _confirms(argv) == ["source_cdc", "secrets"]


def test_an_unknown_confirmed_check_is_refused_before_any_aws_call(tmp_path, capsys, monkeypatch):
    _job(tmp_path, confirmed_checks=["source_cdc", "source_cdd"])

    def boom(_argv):
        raise AssertionError("the preflight must not run")

    monkeypatch.setattr(check_dms_step_preflight, "main", boom)
    code, out = _run(tmp_path, "preflight", capsys=capsys)
    assert code == 1 and out["status"] == "error"
    assert "'source_cdd'" in out["message"] and "'source_cdc'" not in out["message"]


def test_every_preflight_check_id_is_accepted_as_a_confirmed_check(tmp_path, capsys, monkeypatch):
    from src.contracts.dms_preflight import CHECK_ORDER

    _job(tmp_path, confirmed_checks=list(CHECK_ORDER))
    code, _out, argv = _argv_seen(tmp_path, capsys, monkeypatch)
    assert code == 0 and _confirms(argv) == list(CHECK_ORDER)


def test_gate_runs_the_gate_script_on_the_stack(tmp_path, capsys, monkeypatch):
    _job(tmp_path, read_profile="readonly")
    seen = {}

    def fake(argv):
        seen.update(argv=argv, profile=os.environ.get("AWS_PROFILE"))
        print(json.dumps({"status": "pending"}))
        return 0

    monkeypatch.setattr(check_dms_step_gate, "main", fake)
    code, out = _run(tmp_path, "gate", capsys=capsys)
    assert code == 0 and out == {"status": "pending"} and seen["profile"] == "readonly"
    assert seen["argv"][-2:] == ["--stack-name", STACK]


def test_writes_use_the_write_profile(tmp_path, capsys, monkeypatch):
    _job(tmp_path, read_profile="readonly", write_profile="deployer")
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out = _run(tmp_path, "test", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and os.environ["AWS_PROFILE"] == "deployer"


@pytest.mark.parametrize("profile", ["team+account-ReadOnly", "user@example.com", "a=b,c/d:e"])
def test_profile_names_with_cli_punctuation_are_accepted(tmp_path, capsys, profile):
    _job(tmp_path, read_profile=profile)
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code == 0 and out["status"] == "ready"


@pytest.mark.parametrize("profile", ["read only", "x;rm", 'a"b', ""])
def test_profile_names_with_spaces_or_shell_characters_are_refused(tmp_path, capsys, profile):
    _job(tmp_path, read_profile=profile)
    code, out = _run(tmp_path, "plan", capsys=capsys)
    assert code != 0 and out["status"] == "error" and "read_profile" in out["message"]


# --- stop / teardown: ending the step (#1896) ---------------------------------------------

STEP_RESOURCES = {"SecretsAccessRole": "AWS::IAM::Role", "SubnetGroup": "AWS::DMS::ReplicationSubnetGroup",  # pragma: allowlist secret
                  "ReplicationInstance": "AWS::DMS::ReplicationInstance",
                  "SourceEndpoint": "AWS::DMS::Endpoint", "TargetEndpoint": "AWS::DMS::Endpoint",
                  "ReplicationTask": "AWS::DMS::ReplicationTask"}  # fmt: skip
# The writes that act only on this job's step stack, each refused for any other stack (#2048,
# #2061).
OWNED = ("stop", "restart-validation", "resume", "retry", "teardown")


def _summary(logical, kind, status="CREATE_COMPLETE"):
    physical = {"ReplicationTask": TASK, "ReplicationInstance": INSTANCE,
                "SourceEndpoint": SOURCE_EP, "TargetEndpoint": TARGET_EP}.get(logical, logical)  # fmt: skip
    return {"LogicalResourceId": logical, "PhysicalResourceId": physical, "ResourceType": kind,
            "LastUpdatedTimestamp": "2026-11-04T00:00:00Z", "ResourceStatus": status}  # fmt: skip


def _resources(cred, resources=None, task_status="CREATE_COMPLETE", pages=1, name=STACK):
    """list_stack_resources, split into ``pages`` pages joined by NextToken."""
    resources = STEP_RESOURCES if resources is None else resources
    rows = [_summary(k, v, task_status if k == "ReplicationTask" else "CREATE_COMPLETE")
            for k, v in resources.items()]  # fmt: skip
    size = -(-len(rows) // pages) if rows else 0
    chunks = [rows[i * size : (i + 1) * size] for i in range(pages)] if rows else [[]]
    for i, chunk in enumerate(chunks):
        response: dict = {"StackResourceSummaries": chunk}
        params = {"StackName": name, **({"NextToken": f"t{i}"} if i else {})}
        if i + 1 < len(chunks):
            response["NextToken"] = f"t{i + 1}"
        cred.stub("cloudformation").add_response("list_stack_resources", response, params)


def _task_by_arn(cred, status, identifier=STACK, job_tag="job1", tags=True, **fields):
    cred.stub("dms").add_response("describe_replication_tasks", {"ReplicationTasks": [
        {k: v for k, v in {"ReplicationTaskArn": TASK, "ReplicationTaskIdentifier": identifier,
                           "Status": status, "StopReason": "Stop Reason NORMAL", **fields}.items()
         if v is not None}]},
        {"Filters": [{"Name": "replication-task-arn", "Values": [TASK]}]})  # fmt: skip
    if tags:
        tag_list = [] if job_tag is None else [{"Key": "modernizer:job-id", "Value": job_tag}]
        cred.stub("dms").add_response("list_tags_for_resource",
                                      {"TagList": [{"Key": "modernizer:step", "Value": "1"}, *tag_list]},
                                      {"ResourceArn": TASK})  # fmt: skip


def _poll_task(cred, status, **fields):
    """A task poll during a wait: the task is not checked again."""
    _task_by_arn(cred, status, tags=False, **fields)


def _no_pending(cred):
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()


def _dms_tags(cred, job_tag="job1", arns=(INSTANCE, SOURCE_EP, TARGET_EP)):
    """The modernizer:job-id tag of the stack's instance and endpoints, read once the task is gone."""
    for arn in arns:
        tags = [] if job_tag is None else [{"Key": "modernizer:job-id", "Value": job_tag}]
        cred.stub("dms").add_response(
            "list_tags_for_resource", {"TagList": tags}, {"ResourceArn": arn}
        )


def _marker(tmp_path: Path) -> Path:
    """A file an agent could write: teardown never reads it."""
    return tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "teardown.json"


def test_stop_stops_a_running_task_and_waits_until_it_has_stopped(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "running")
    cred.stub("dms").add_response("stop_replication_task", {}, {"ReplicationTaskArn": TASK})
    _poll_task(cred, "stopping")
    _poll_task(cred, "stopped")
    code = script.main(["stop", "--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path),
                        "--approve", "--interval-seconds", "15"], clients=_clients(cred),
                       sleep=lambda _s: None)  # fmt: skip
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0 and len(lines) == 1
    out = json.loads(lines[0])
    assert out["status"] == "stopped" and out["task_status"] == "stopped"
    assert out["already_stopped"] is False and out["reached"] is True and out["task_arn"] == TASK
    assert out["waited_seconds"] == 30
    progress = captured.err.splitlines()
    assert progress == ["stop: the task is stopping (waited 15 s of 540 s)",
                        "stop: the task is stopped (waited 30 s of 540 s)"]  # fmt: skip
    _no_pending(cred)


@pytest.mark.parametrize("status", ["stopped", "failed", "ready"])
def test_stop_on_a_task_that_is_not_running_is_a_no_op(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, status)
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "stopped" and out["already_stopped"] is True
    assert out["task_status"] == status
    _no_pending(cred)  # and no stop_replication_task was queued or made


def test_stop_on_a_task_already_stopping_waits_without_stopping_it_again(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopping")
    _poll_task(cred, "stopped")
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "stopped" and out["already_stopped"] is False
    _no_pending(cred)


def test_stop_without_a_stack_or_with_its_task_gone_is_a_no_op(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out == {"status": "no_stack", "stack_name": STACK}
    _stack(cred, status="DELETE_FAILED")
    _resources(cred, task_status="DELETE_COMPLETE")
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "no_task" and out["dms_reports"] == GONE
    _no_pending(cred)  # the deleted task was not looked up


def test_stop_reports_a_task_that_has_not_stopped_by_the_deadline(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "running")
    cred.stub("dms").add_response("stop_replication_task", {}, {"ReplicationTaskArn": TASK})
    for _ in range(2):
        _poll_task(cred, "stopping")
    code, out = _run(tmp_path, "stop", "--approve", "--wait-seconds", "60", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    assert code == 0 and out["status"] == "stopping" and out["reached"] is False
    assert out["waited_seconds"] == 60
    _no_pending(cred)


def test_stop_refuses_a_task_that_is_changing(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "modifying")
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "DMS reports the task modifying" in out["message"]
    _no_pending(cred)


# --- restart-validation: a stalled validator restarted by a stop and a resume (#2048) ------


def _resume(cred):
    cred.stub("dms").add_response(
        "start_replication_task", {},
        {"ReplicationTaskArn": TASK, "StartReplicationTaskType": "resume-processing"},
    )  # fmt: skip


def _restart(tmp_path, capsys, cred, *extra):
    code = script.main(["restart-validation", "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(tmp_path), "--approve", "--interval-seconds", "30", *extra],
                       clients=_clients(cred), sleep=lambda _s: None)  # fmt: skip
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, lines  # one compact JSON status line; progress goes to stderr
    return code, json.loads(lines[0]), captured.err.splitlines()


def test_restart_validation_stops_the_task_then_resumes_it_and_waits_until_it_runs(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "running")
    cred.stub("dms").add_response("stop_replication_task", {}, {"ReplicationTaskArn": TASK})
    _poll_task(cred, "stopping")
    _poll_task(cred, "stopped")
    _resume(cred)
    _poll_task(cred, "starting")
    _poll_task(cred, "running")
    code, out, progress = _restart(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "restarted" and out["reached"] is True
    assert out["resumed"] is True and out["task_status"] == "running" and out["task_arn"] == TASK
    assert out["stack_name"] == STACK and out["waited_seconds"] == 120
    assert progress == [f"restart-validation: the task is {s} (waited {w} s of 540 s)"
                        for s, w in (("stopping", 30), ("stopped", 60), ("starting", 90),
                                     ("running", 120))]  # fmt: skip
    _no_pending(cred)  # stop, then resume-processing (never start-replication), in that order


def test_restart_validation_resumes_a_task_a_previous_run_left_stopped(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped")
    _resume(cred)
    _poll_task(cred, "running")
    code, out, _ = _restart(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "restarted" and out["waited_seconds"] == 30
    _no_pending(cred)  # no stop_replication_task for a task already stopped


def test_restart_validation_does_not_resume_a_task_that_has_not_stopped_by_the_deadline(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "running")
    cred.stub("dms").add_response("stop_replication_task", {}, {"ReplicationTaskArn": TASK})
    for _ in range(2):
        _poll_task(cred, "stopping")
    code, out, _ = _restart(tmp_path, capsys, cred, "--wait-seconds", "60")
    assert code == 0 and out["status"] == "stopping" and out["reached"] is False
    assert out["resumed"] is False and out["waited_seconds"] == 60
    _no_pending(cred)  # and no start_replication_task was queued or made


def test_restart_validation_reports_a_task_that_is_not_running_by_the_deadline(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "running")
    cred.stub("dms").add_response("stop_replication_task", {}, {"ReplicationTaskArn": TASK})
    _poll_task(cred, "stopped")
    _resume(cred)
    for _ in range(2):
        _poll_task(cred, "starting")
    code, out, _ = _restart(tmp_path, capsys, cred, "--wait-seconds", "90")
    assert code == 0 and out["status"] == "starting" and out["reached"] is False
    assert out["resumed"] is True and out["waited_seconds"] == 90  # one budget for both waits
    assert out["task_status"] == "starting"
    _no_pending(cred)


def test_restart_validation_reports_a_task_that_failed_to_resume(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped")
    _resume(cred)
    _poll_task(cred, "failed", LastFailureMessage="validator could not connect")
    code, out, _ = _restart(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "resume_failed" and out["reached"] is False
    assert "validator could not connect" in out["dms_reports"]
    _no_pending(cred)


@pytest.mark.parametrize("status", ["failed", "ready", "modifying"])
def test_restart_validation_refuses_a_task_that_is_not_running_or_stopped(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, status)
    code, out, _ = _restart(tmp_path, capsys, cred)
    assert code == 1 and out["message"].startswith(
        script.dms_reports({"Status": status, "StopReason": "Stop Reason NORMAL"})
    )
    assert "restart-validation resumes only a task that was running" in out["message"]
    _no_pending(cred)  # no stop and no start


def test_restart_validation_without_a_stack_is_an_error(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out, _ = _restart(tmp_path, capsys, cred)
    assert (
        code == 1 and f"stack {STACK}" in out["message"] and "nothing to restart" in out["message"]
    )
    _no_pending(cred)


def test_restart_validation_is_refused_without_approve_and_under_the_sandbox(
    tmp_path, capsys, monkeypatch
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    code, out = _run(tmp_path, "restart-validation", clients=_clients(cred), capsys=capsys)
    assert code == 1 and out["message"].startswith(
        "restart-validation is an AWS write: pass --approve"
    )
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    code = script.main(["restart-validation", "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(REPO_ROOT / "artifacts"), "--approve"], clients=_clients(cred))  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "refused under MODERNIZER_CI_SANDBOX=1" in out["message"]
    _no_pending(cred)


def test_teardown_deletes_the_steps_stack_once_its_task_has_stopped_and_waits(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped")
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_IN_PROGRESS")
    _poll(cred, "DELETE_COMPLETE")
    code = script.main(["teardown", "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(tmp_path), "--approve"], clients=_clients(cred),
                       sleep=lambda _s: None)  # fmt: skip
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0 and len(lines) == 1
    out = json.loads(lines[0])
    assert out["status"] == "deleted" and out["already_deleted"] is False and out["reached"] is True
    assert captured.err.splitlines() == [
        "teardown: the stack is DELETE_IN_PROGRESS (waited 30 s of 540 s)",
        "teardown: the stack is deleted (waited 60 s of 540 s)",
    ]
    assert not _marker(tmp_path).exists()  # nothing local records or grants a delete
    _no_pending(cred)
    assert cred.asked_for == ["cloudformation", "dms"]  # no RDS, EC2 or Secrets Manager client


@pytest.mark.parametrize("status", ["running", "starting", "stopping"])
def test_teardown_refuses_while_the_task_runs(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, status)
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert (
        code == 1
        and script.dms_reports({"Status": status}) in out["message"]
        and "run stop first" in out["message"]
    )
    _no_pending(cred)  # and no delete_stack was queued or made


def test_teardown_of_a_stack_that_is_already_gone_is_a_no_op(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    ended = {k: v for k, v in out.items() if k not in ("source_reminder", "source_followups")}
    assert code == 0 and ended == {
        "status": "deleted",
        "stack_name": STACK,
        "already_deleted": True,
    }
    # A step ended earlier still leaves the source's change-log follow-ups to do (#2060).
    assert "binary log retention" in out["source_reminder"] and out["source_followups"]
    _no_pending(cred)


def test_teardown_of_a_stack_it_is_deleting_waits_without_deleting_it_again(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_IN_PROGRESS")
    _resources(cred, task_status="DELETE_COMPLETE")
    _dms_tags(cred)
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "deleted" and out["already_deleted"] is False
    _no_pending(cred)  # and no delete_stack was queued or made


def test_a_delete_in_progress_of_another_jobs_stack_is_refused(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_IN_PROGRESS")
    _resources(cred, task_status="DELETE_COMPLETE")
    _dms_tags(cred, job_tag="otherjob", arns=(INSTANCE,))
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert (
        code == 1
        and "ReplicationInstance's modernizer:job-id tag is ['otherjob']" in out["message"]
    )
    _no_pending(cred)


def test_teardown_retries_a_failed_delete_once_the_task_has_stopped(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_FAILED")
    _resources(cred, task_status="DELETE_FAILED")
    _task_by_arn(cred, "stopped")
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "deleted"
    _no_pending(cred)


def test_a_delete_failed_after_a_timed_out_wait_needs_a_new_run_to_delete_again(tmp_path, capsys):
    """One run, one approval, one DeleteStack: the wait runs out; the next run finds the stack
    still deleting, only waits, and reports DELETE_FAILED instead of deleting a second time."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    cfn = cred.stub("cloudformation")
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped")
    cfn.add_response("delete_stack", {}, {"StackName": STACK_ID})
    for _ in range(2):
        _poll(cred, "DELETE_IN_PROGRESS")
    code, out = _run(tmp_path, "teardown", "--approve", "--wait-seconds", "60",
                     clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["status"] == "deleting" and out["reached"] is False
    _stack(
        cred, status="DELETE_IN_PROGRESS"
    )  # a rerun: the stack is being deleted, so it only waits
    _resources(cred, task_status="DELETE_COMPLETE")
    _dms_tags(cred)
    _poll(cred, "DELETE_FAILED", reason="ReplicationTask: still in use")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "delete_failed" and out["reached"] is False
    assert out["stack_reason"] == "ReplicationTask: still in use"
    _no_pending(cred)  # exactly one delete_stack across both runs


def test_teardown_reports_a_delete_that_failed_as_a_state_not_an_error(tmp_path, capsys):
    """DELETE_FAILED seen by teardown's own wait is the state status --until stack reports, so
    /move asks the same "delete again?" question for both."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped")
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_FAILED", reason="ReplicationTask: still in use")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "delete_failed"
    assert out["stack_reason"] == "ReplicationTask: still in use" and out["stack_id"] == STACK_ID
    _no_pending(cred)


def test_once_the_task_is_gone_the_instance_and_endpoint_tags_prove_the_stack_is_this_jobs(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_FAILED")
    _resources(cred, task_status="DELETE_COMPLETE")
    _dms_tags(cred)
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "deleted"
    _no_pending(cred)  # and no describe_replication_tasks was queued or made


def test_a_resource_dms_no_longer_has_is_skipped_and_the_others_prove_it(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_FAILED")
    _resources(cred, task_status="DELETE_COMPLETE")
    cred.stub("dms").add_client_error("list_tags_for_resource", "ResourceNotFoundFault", "gone",
                                      expected_params={"ResourceArn": INSTANCE})  # fmt: skip
    _dms_tags(cred, arns=(SOURCE_EP, TARGET_EP))
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "deleted"
    _no_pending(cred)


def test_once_the_task_is_gone_the_stacks_own_tag_proves_it(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    cfn = cred.stub("cloudformation")
    _stack(cred, status="DELETE_FAILED", job_tag="job1")
    cfn.add_response("list_stack_resources", {"StackResourceSummaries": [
        _summary(k, v, "DELETE_COMPLETE") for k, v in STEP_RESOURCES.items()]}, {"StackName": STACK})  # fmt: skip
    cfn.add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "deleted"
    _no_pending(cred)


@pytest.mark.parametrize("command", OWNED)
def test_a_stack_tagged_for_another_job_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, job_tag="otherjob")
    code, out = _run(tmp_path, command, "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "the stack's modernizer:job-id tag is ['otherjob']" in out["message"]
    _no_pending(cred)


@pytest.mark.parametrize("job_tag", ["otherjob", None])
def test_a_stack_whose_task_is_gone_and_whose_resources_are_not_this_jobs_is_refused(
    tmp_path, capsys, job_tag
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_FAILED")
    _resources(cred, task_status="DELETE_COMPLETE")
    _dms_tags(cred, job_tag=job_tag, arns=(INSTANCE,))
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "not this job's step 1 DMS stack" in out["message"]
    _no_pending(cred)


def test_a_stack_whose_task_and_dms_resources_are_all_gone_and_has_no_tag_is_refused(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_FAILED")
    cred.stub("cloudformation").add_response("list_stack_resources", {"StackResourceSummaries": [
        _summary(k, v, "DELETE_COMPLETE") for k, v in STEP_RESOURCES.items()]}, {"StackName": STACK})  # fmt: skip
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "no replication instance, endpoint or stack tag" in out["message"]
    _no_pending(cred)


@pytest.mark.parametrize("marker", ["object", "list", "string"])
@pytest.mark.parametrize("task_gone", ["resource_deleted", "dms_not_found"])
@pytest.mark.parametrize("stack_status", ["CREATE_COMPLETE", "DELETE_FAILED"])
def test_a_forged_marker_never_lets_teardown_delete_another_jobs_stack(
    tmp_path, capsys, stack_status, task_gone, marker
):
    """The review's probe: inputs name dm-otherjob-step-1, its task is gone, and this job's
    step folder holds a teardown.json naming that stack's id. Ownership comes from AWS only."""
    other = "dm-otherjob-step-1"
    _job(tmp_path, stack_name=other)
    other_id = f"arn:aws:cloudformation:us-east-1:111122223333:stack/{other}/1"
    content = {"object": {"stack_id": other_id, "stack_name": other}, "list": [other_id],
               "string": other_id}[marker]  # fmt: skip
    _marker(tmp_path).write_text(json.dumps(content))
    cred = StubbedCredMgr()
    _stack(cred, status=stack_status, name=other)
    if task_gone == "resource_deleted":
        _resources(cred, task_status="DELETE_COMPLETE", name=other)
    else:
        _resources(cred, name=other)
        cred.stub("dms").add_client_error(
            "describe_replication_tasks", "ResourceNotFoundFault", "gone"
        )
    _dms_tags(cred, job_tag="otherjob", arns=(INSTANCE,))
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": other_id})
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "not this job's step 1 DMS stack" in out["message"], out
    assert "otherjob" in out["message"] and "AttributeError" not in out["message"]
    with pytest.raises(AssertionError):
        cred.stub("cloudformation").assert_no_pending_responses()  # delete_stack was never used


def test_a_task_dms_no_longer_has_is_nothing_to_stop_and_teardown_proves_ownership_from_tags(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    for _ in range(2):
        _stack(cred)
        _resources(cred)
        cred.stub("dms").add_client_error(
            "describe_replication_tasks", "ResourceNotFoundFault", "No Replication Tasks found"
        )
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "no_task" and out["dms_reports"] == GONE
    _dms_tags(cred, job_tag=None, arns=(INSTANCE,))
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "ReplicationInstance's modernizer:job-id tag is missing" in out["message"]
    _no_pending(cred)


def _task_rows(cred, task_status="CREATE_COMPLETE", name=STACK):
    """The listing task_state reads: just the ReplicationTask row is enough."""
    cred.stub("cloudformation").add_response("list_stack_resources", {"StackResourceSummaries": [
        _summary("ReplicationTask", "AWS::DMS::ReplicationTask", task_status)]}, {"StackName": name})  # fmt: skip


def _wait_task(cred, *, seconds=60, interval=30, ticks=(0, 0, 30, 60, 90)):
    clock = iter(ticks)
    return script.wait_status(_clients(cred), STACK, "task_stopped", seconds, interval,
                              sleep=lambda _s: None, clock=lambda: next(clock))  # fmt: skip


@pytest.mark.parametrize("stack_status", ["CREATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
                                          "UPDATE_ROLLBACK_FAILED", "DELETE_FAILED"])  # fmt: skip
def test_the_task_stopped_wait_reads_the_task_in_every_stack_state(stack_status):
    """A failed or rolled-back stack is no proof the task stopped: the task itself is read."""
    cred = StubbedCredMgr()
    _stack(cred, status=stack_status)
    _task_rows(cred)
    _poll_task(cred, "stopping")
    _stack(cred, status=stack_status)
    _task_rows(cred)
    _poll_task(cred, "stopped")
    out = _wait_task(cred)
    assert out["reached"] is True and out["task_status"] == "stopped"
    assert out["stack_status"] == stack_status
    _no_pending(cred)  # no stop_replication_task or delete_stack: the wait is read-only


@pytest.mark.parametrize("stack_status", ["CREATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"])
def test_the_task_stopped_wait_is_not_reached_while_the_task_still_runs(stack_status):
    cred = StubbedCredMgr()
    for state in ("stopping", "running", "running"):
        _stack(cred, status=stack_status)
        _task_rows(cred)
        _poll_task(cred, state)
    out = _wait_task(cred)
    assert out["reached"] is False and out["task_status"] == "running"
    assert out["phase"] == "task_running"
    _no_pending(cred)


@pytest.mark.parametrize("gone", ["resource_deleted", "dms_not_found", "no_stack", "no_row"])
def test_a_task_that_is_gone_counts_as_stopped_not_as_an_error(gone):
    cred = StubbedCredMgr()
    if gone == "no_stack":
        _no_stack(cred)
    else:
        _stack(cred, status="DELETE_FAILED")
    if gone == "resource_deleted":
        _task_rows(cred, task_status="DELETE_COMPLETE")
    elif gone == "dms_not_found":
        _task_rows(cred)
        cred.stub("dms").add_client_error(
            "describe_replication_tasks", "ResourceNotFoundFault", "gone"
        )
    elif gone == "no_row":
        cred.stub("cloudformation").add_response(
            "list_stack_resources", {"StackResourceSummaries": []}, {"StackName": STACK}
        )
    out = _wait_task(cred)
    assert out["status"] == "complete" and out["reached"] is True
    assert out["task_status"] == "deleted"
    _no_pending(cred)


def test_the_task_stopped_wait_through_the_cli_prints_one_line(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="UPDATE_ROLLBACK_COMPLETE")
    _task_rows(cred)
    _poll_task(cred, "stopped")
    code, out = _run(tmp_path, "status", "--until", "task_stopped", "--wait-seconds", "60",
                     clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["reached"] is True and out["until"] == "task_stopped"
    _no_pending(cred)


def test_a_stack_whose_dms_resources_dms_no_longer_has_and_with_no_tag_is_refused(tmp_path, capsys):
    """A resource DMS has no record of proves nothing: with the task gone, the instance and both
    endpoints unknown to DMS and no stack tag, teardown refuses."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_FAILED")
    _resources(cred, task_status="DELETE_COMPLETE")
    for arn in (INSTANCE, SOURCE_EP, TARGET_EP):
        cred.stub("dms").add_client_error("list_tags_for_resource", "ResourceNotFoundFault", "gone",
                                          expected_params={"ResourceArn": arn})  # fmt: skip
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "no replication instance, endpoint or stack tag" in out["message"]
    with pytest.raises(AssertionError):
        cred.stub("cloudformation").assert_no_pending_responses()  # delete_stack was never used
    cred.stub("dms").assert_no_pending_responses()


# Each stack below is refused by stop, restart-validation and teardown, before any write.


def _refused(tmp_path, capsys, cred, command, *needles):
    code, out = _run(tmp_path, command, "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "not this job's step 1 DMS stack" in out["message"], out
    for needle in needles:
        assert needle in out["message"], (needle, out["message"])
    _no_pending(cred)  # no stop_replication_task or delete_stack was queued or made


@pytest.mark.passes_on_main  # an empty stack whose create completed stays refused
@pytest.mark.parametrize("command", OWNED)
def test_an_empty_stack_is_refused(tmp_path, capsys, command):
    """An empty stack whose create completed is no step stack. An empty one whose create rolled
    back is ended only with this job's own stack tag (#2063):
    test_a_failed_create_without_this_jobs_stack_tag_is_refused."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="CREATE_COMPLETE", outputs={})
    _resources(cred, resources={})
    _refused(tmp_path, capsys, cred, command, "missing: ReplicationInstance")


@pytest.mark.parametrize("command", OWNED)
def test_a_stack_holding_only_an_iam_role_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, outputs={})
    _resources(cred, resources={"SecretsAccessRole": "AWS::IAM::Role"})  # pragma: allowlist secret
    _refused(tmp_path, capsys, cred, command, "missing: ReplicationInstance")


@pytest.mark.parametrize("command", OWNED)
def test_another_jobs_stopped_step_stack_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped", identifier="dm-otherjob-step-1", tags=False)
    _refused(tmp_path, capsys, cred, command, "'dm-otherjob-step-1'", "'dm-job1-step-1'")


@pytest.mark.parametrize("job_tag", ["otherjob", None])
@pytest.mark.parametrize("command", OWNED)
def test_a_task_whose_job_tag_does_not_name_this_job_is_refused(tmp_path, capsys, command, job_tag):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped", job_tag=job_tag)
    _refused(tmp_path, capsys, cred, command, "modernizer:job-id")


@pytest.mark.parametrize("command", OWNED)
def test_the_customers_aurora_stack_is_refused(tmp_path, capsys, command):
    _job(tmp_path, stack_name="my-aurora")
    cred = StubbedCredMgr()
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [{"StackName": "my-aurora",
        "CreationTime": "2026-11-04T00:00:00Z", "StackStatus": "CREATE_COMPLETE"}]},
        {"StackName": "my-aurora"})  # fmt: skip
    _resources(cred, resources={"Cluster": "AWS::RDS::DBCluster"}, name="my-aurora")
    _refused(tmp_path, capsys, cred, command, "Cluster (AWS::RDS::DBCluster)")


@pytest.mark.parametrize("command", OWNED)
def test_a_resource_on_a_later_page_is_still_checked(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred, resources={**STEP_RESOURCES, "Cluster": "AWS::RDS::DBCluster"}, pages=3)
    _refused(tmp_path, capsys, cred, command, "Cluster (AWS::RDS::DBCluster)")


def test_the_six_resources_across_pages_are_this_jobs_stack(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred, pages=3)
    _task_by_arn(cred, "stopped")
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "stopped"
    _no_pending(cred)


@pytest.mark.parametrize("command", OWNED)
def test_a_resource_list_that_cannot_be_read_is_an_error_and_nothing_is_written(
    tmp_path, capsys, command
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    cred.stub("cloudformation").add_client_error("list_stack_resources", "AccessDenied", "denied")
    code, out = _run(tmp_path, command, "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and out["status"] == "error" and "AccessDenied" in out["message"]
    _no_pending(cred)


@pytest.mark.parametrize("command", OWNED)
def test_ending_refuses_a_stack_that_is_still_changing(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="UPDATE_IN_PROGRESS")
    code, out = _run(tmp_path, command, "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "UPDATE_IN_PROGRESS" in out["message"]
    _no_pending(cred)


def test_the_resources_ending_accepts_are_the_rendered_stacks():
    from src.tools.data_movement.dms_step import render_dms_template
    from tests.unit.tools.data_movement.dms_step_fixtures import make_step1_inputs

    for engine in ("aurora_mysql", "aurora_postgresql"):
        resources = render_dms_template(make_step1_inputs(engine))["Resources"]
        assert {n: r["Type"] for n, r in resources.items()} == script.STEP_RESOURCES
        assert script.STEP_RESOURCES == STEP_RESOURCES
        tags = resources["ReplicationTask"]["Properties"]["Tags"]
        assert {"Key": "modernizer:job-id", "Value": "job1"} in tags


@pytest.mark.passes_on_main
def test_no_resource_of_the_step_stack_is_retained_on_delete():
    """Deleting the stack deletes all six resources: none has a Retain or Snapshot policy."""
    from src.tools.data_movement.dms_step import render_dms_template
    from tests.unit.tools.data_movement.dms_step_fixtures import make_step1_inputs

    for engine in ("aurora_mysql", "aurora_postgresql"):
        for name, resource in render_dms_template(make_step1_inputs(engine))["Resources"].items():
            assert "DeletionPolicy" not in resource and "UpdateReplacePolicy" not in resource, name


def test_ending_uses_the_write_profile(tmp_path, capsys):
    _job(tmp_path, read_profile="readonly", write_profile="deployer")
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and os.environ["AWS_PROFILE"] == "deployer"


@pytest.mark.parametrize("command", OWNED)
def test_a_task_resource_without_a_physical_id_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    rows = [_summary(k, v) for k, v in STEP_RESOURCES.items()]
    for row in rows:
        if row["LogicalResourceId"] == "ReplicationTask":
            del row["PhysicalResourceId"]
    cred.stub("cloudformation").add_response(
        "list_stack_resources", {"StackResourceSummaries": rows}, {"StackName": STACK}
    )
    _refused(tmp_path, capsys, cred, command, "no physical id")


@pytest.mark.parametrize("command", OWNED)
def test_a_task_that_cannot_be_read_is_an_error_and_nothing_is_written(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    cred.stub("dms").add_client_error("describe_replication_tasks", "AccessDeniedFault", "denied")
    code, out = _run(tmp_path, command, "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "AccessDeniedFault" in out["message"]
    _no_pending(cred)


# --- What DMS reports, in the customer's words (maintainer feedback on #1907) -------------

FAILURE = {"StopReason": "Stop Reason FATAL_ERROR",
           "LastFailureMessage": "Last Error Table error; Task error notification received"}  # fmt: skip
DMS_SAYS = [
    ("stopped", {}, "DMS reports the task stopped (not running)", True),
    (
        "failed",
        FAILURE,
        "DMS reports the task failed (not running): Last Error Table error; "
        "Task error notification received; Stop Reason FATAL_ERROR",
        True,
    ),
    (
        "failed",
        {"StopReason": None},
        "DMS reports the task failed (not running): DMS gives no reason",
        True,
    ),
    ("failed-move", {}, "DMS reports the task failed-move (not running): Stop Reason NORMAL", True),
    ("ready", {}, "DMS reports the task never started (ready, not running)", True),
    ("running", {}, "DMS reports the task running", False),
    ("starting", {}, "DMS reports the task starting (running)", False),
    ("stopping", {}, "DMS reports the task stopping (still running)", False),
]


@pytest.mark.parametrize(("status", "fields", "says", "stopped"), DMS_SAYS,
                         ids=[f"{s}-{i}" for i, (s, *_) in enumerate(DMS_SAYS)])  # fmt: skip
def test_the_task_stopped_wait_names_what_dms_reports(status, fields, says, stopped):
    """Failed and never-started tasks count as not running, and the line says which one."""
    cred = StubbedCredMgr()
    _stack(cred, status="UPDATE_ROLLBACK_COMPLETE")
    _task_rows(cred)
    _poll_task(cred, status, **fields)
    out = script.wait_status(_clients(cred), STACK, "task_stopped", 0, 30, sleep=lambda _s: None)
    assert out["task_status"] == status and out["dms_reports"] == says
    assert out["reached"] is stopped
    if fields.get("LastFailureMessage"):
        assert out["last_failure"] == FAILURE["LastFailureMessage"]
        assert out["stop_reason"] == FAILURE["StopReason"]


def test_a_gone_task_is_named_as_gone():
    cred = StubbedCredMgr()
    _stack(cred, status="DELETE_FAILED")
    _task_rows(cred, task_status="DELETE_COMPLETE")
    out = script.wait_status(_clients(cred), STACK, "task_stopped", 0, 30, sleep=lambda _s: None)
    assert out["task_status"] == "deleted" and out["dms_reports"] == GONE


@pytest.mark.parametrize("status", ["stopped", "failed", "ready"])
def test_stop_names_what_dms_reports_for_a_task_it_leaves_as_is(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, status, **(FAILURE if status == "failed" else {}))
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["already_stopped"] is True
    assert out["dms_reports"] == script.dms_reports(
        {
            "Status": status,
            **(FAILURE if status == "failed" else {"StopReason": "Stop Reason NORMAL"}),
        }
    )
    assert out["dms_reports"].startswith(
        f"DMS reports the task {'never started' if status == 'ready' else status}"
    )
    if status == "failed":
        assert (
            out["last_failure"] == FAILURE["LastFailureMessage"]
            and FAILURE["LastFailureMessage"] in out["dms_reports"]
        )


@pytest.mark.parametrize("status", ["stopped", "failed", "ready"])
def test_teardown_names_what_dms_reported_when_it_deleted(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, status, **(FAILURE if status == "failed" else {}))
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "deleted" and out["task_status"] == status
    assert out["dms_reports"].startswith("DMS reports the task ")
    if status == "failed":
        assert FAILURE["LastFailureMessage"] in out["dms_reports"]


def test_status_names_what_dms_reports(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred)
    _task(cred, status="ready")
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert (
        code == 0
        and out["dms_reports"] == "DMS reports the task never started (ready, not running)"
    )


# --- Prove: stalled validation, --until running, progress lines (#1995, #1996, #1997) ---------

HOUR_AGO = datetime.now(UTC) - timedelta(hours=1)


def _prove_task(cred, status="running", percent=100, validation=True, finished=HOUR_AGO,
                settings=True, **stats):  # fmt: skip
    """A task past its start, with its settings (validation on or off) and full-load dates."""
    task_stats = {"FullLoadProgressPercent": percent, "TablesLoaded": 305, **stats}
    if finished is not None and percent == 100:
        task_stats["FullLoadFinishDate"] = finished
    task = {"ReplicationTaskArn": TASK, "ReplicationTaskIdentifier": STACK, "Status": status,
            "ReplicationTaskStats": task_stats}  # fmt: skip
    if settings:
        task["ReplicationTaskSettings"] = json.dumps(
            {"ValidationSettings": {"EnableValidation": validation, "ValidationMode": "ROW_LEVEL"}}
        )
    cred.stub("dms").add_response("describe_replication_tasks", {"ReplicationTasks": [task]})


def _rows(not_enabled=0, validated=0, pending=0, no_key=0, pending_records=1000, applied=(0, 0)):
    """Table statistics shaped like a real step 1 snapshot (one row per table)."""
    rows = []
    for state, count in (("Not enabled", not_enabled), ("Validated", validated),
                         ("Pending validation", pending), ("No primary Key", no_key)):  # fmt: skip
        for i in range(count):
            rows.append({"SchemaName": "public", "TableName": f"{state[:3].lower()}_{i}",
                         "TableState": "Table completed", "FullLoadRows": 0 if state == "Not enabled" else 10,
                         "AppliedInserts": applied[0], "AppliedUpdates": applied[1], "AppliedDeletes": 0,
                         "ValidationState": state,
                         "ValidationPendingRecords": pending_records if state == "Pending validation" else 0,
                         "ValidationFailedRecords": 0, "ValidationSuspendedRecords": 0})  # fmt: skip
    return rows


def _prove_poll(cred, rows, **task):
    _stack(cred)
    _connections(cred)
    _prove_task(cred, **task)
    # A running task's validation, or a stopped or failed one's recovery (#2061), reads them.
    if task.get("status", "running") not in ("ready", "failed-move"):
        cred.stub("dms").add_response(
            "describe_table_statistics", {"ReplicationTaskArn": TASK, "TableStatistics": rows}
        )


# The snapshot of #1995: full load 100% (305 tables, 0 errors), validation on, and an hour later
# 139 tables Not enabled and 64 pending validation, with the validator unable to connect.
SNAPSHOT = {"not_enabled": 139, "validated": 98, "pending": 64, "no_key": 4}


def _wait(cred, until="replicating", seconds=540, interval=60, polls=10, on_poll=None, lag=None):
    clock = iter([0, *(i * interval for i in range(polls + 2))])
    return script.wait_status(_clients(cred), STACK, until, seconds, interval, sleep=lambda _s: None,
                              clock=lambda: next(clock), on_poll=on_poll, lag=lag)  # fmt: skip


def test_tables_not_enabled_long_after_the_full_load_with_validation_on_are_a_stalled_validation(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(**SNAPSHOT))
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["phase"] == "validation_stalled"
    assert out["validation_enabled"] is True and out["validation_running"] is False
    assert out["validation"]["not_enabled_tables"] == 139
    assert out["validation"]["pending_tables"] == 64 and out["validation"]["validated"] == 98
    check = out["validation_check"]
    assert "139 tables Not enabled and 64 not validated yet" in check and "secrets" in check
    _no_pending(cred)


_MIXED_CASE = {"Not enabled": "Not Enabled", "Validated": "VALIDATED",
               "Pending validation": "Pending Validation", "No primary Key": "no primary key"}  # fmt: skip


@pytest.mark.parametrize("snapshot", [SNAPSHOT, {"validated": 3, "pending": 2, "no_key": 1}])
def test_status_classifies_a_mixed_case_state_as_the_canonical_one(tmp_path, capsys, snapshot):
    # DMS 3.6.1 spells some states its own way ("No primary Key"): status compares states
    # case-folded, through the gate's validation_state (#2042).
    def status(rows):
        cred = StubbedCredMgr()
        _prove_poll(cred, rows)
        _code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
        return out

    _job(tmp_path)
    canonical = status(_rows(**snapshot))
    mixed_rows = [
        {**r, "ValidationState": _MIXED_CASE[r["ValidationState"]]} for r in _rows(**snapshot)
    ]
    mixed = status(mixed_rows)
    for out in (canonical, mixed):
        out["validation"].pop("states")
    assert mixed["validation"] == canonical["validation"] and mixed["phase"] == canonical["phase"]
    assert mixed["validation"]["validated"] == snapshot["validated"]
    assert mixed["validation"]["pending_tables"] == snapshot["pending"]
    assert mixed["validation"]["not_enabled_tables"] == snapshot.get("not_enabled", 0)


@pytest.mark.parametrize("state", ["Pending Records", "pending records", "PENDING RECORDS"])
def test_status_counts_a_pending_records_table_in_any_letter_case(tmp_path, capsys, state):
    _job(tmp_path)
    cred = StubbedCredMgr()
    rows = [{**r, "ValidationState": state} for r in _rows(pending=2)]
    _prove_poll(cred, rows)
    _code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert out["validation"]["pending_tables"] == 2 and out["phase"] == "validating"


def test_tables_not_enabled_right_after_the_full_load_are_not_yet_judged_stalled(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(**SNAPSHOT), finished=datetime.now(UTC) - timedelta(seconds=60))
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert out["phase"] == "validating" and out["validation_running"] is True
    assert "validation_check" not in out


def test_a_task_whose_finish_date_is_unknown_is_not_judged_on_not_enabled_tables(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(**SNAPSHOT), finished=None)
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert out["phase"] == "validating" and out["validation_running"] is True


def test_a_task_without_settings_is_not_judged_on_not_enabled_tables(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(**SNAPSHOT), settings=False)
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert out["phase"] == "validating" and out["validation_enabled"] is None


def test_validation_turned_off_in_the_task_settings_is_never_reported_as_stalled(tmp_path, capsys):
    """Every table reads Not enabled when the task turns validation off: that is no stall."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(not_enabled=305), validation=False)
    code, out = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert out["phase"] == "replicating" and out["validation_enabled"] is False
    assert out["validation_running"] is None and "validation_check" not in out


def test_validation_turned_off_is_never_stalled_however_long_the_wait():
    cred = StubbedCredMgr()
    for _ in range(10):
        _prove_poll(cred, _rows(not_enabled=300, pending=5), validation=False)
    out = _wait(cred)
    assert out["phase"] == "validating" and out["reached"] is False
    assert out["validation_running"] is None
    _no_pending(cred)


def test_pending_validation_with_no_progress_over_the_window_is_a_stalled_validation():
    """No table is Not enabled; 64 stay pending, their pending records only grow (changes
    keep arriving, nothing gets validated): after the window, the wait stops on it."""
    cred = StubbedCredMgr()
    for i in range(6):  # 0, 60, ..., 300 s: six polls with no progress
        _prove_poll(cred, _rows(validated=98, pending=64, pending_records=1000 + i))
    out = _wait(cred)
    assert out["reached"] is True and out["phase"] == "validation_stalled"
    assert out["validation_running"] is False
    assert (
        "64 tables not validated yet, with no validation progress for 300 s"
        in out["validation_check"]
    )
    _no_pending(cred)  # it stopped polling once it judged the stall


def test_pending_validation_that_moves_is_not_stalled():
    cred = StubbedCredMgr()
    for i in range(10):
        _prove_poll(cred, _rows(validated=98, pending=64, pending_records=1000 - i))
    out = _wait(cred)
    assert out["reached"] is False and out["phase"] == "validating"
    assert out["validation_running"] is True


# --- replicating: the full load is complete and replication is in progress (#2062) ----------


def _table(name, state, pending=0, failed=0):
    return {"SchemaName": "public", "TableName": name, "TableState": "Table completed",
            "FullLoadRows": 10, "FullLoadEndTime": HOUR_AGO, "ValidationState": state,
            "ValidationPendingRecords": pending, "ValidationFailedRecords": failed,
            "ValidationSuspendedRecords": 0}  # fmt: skip


def test_tables_validated_once_then_back_to_pending_records_under_writes_are_replicating():
    """A table DMS validated, now holding records it validates as changes arrive, is never
    waited for: the wait ends at once, with a non-zero backlog."""
    cred = StubbedCredMgr()
    _prove_poll(cred, [_table("a", "Validated"), _table("b", "Validated"),
                       _table("c", "Pending records", pending=936_599)])  # fmt: skip
    _prove_poll(cred, [_table("a", "Pending records", pending=15), _table("b", "Pending records", pending=2),
                       _table("c", "Validated")])  # fmt: skip
    _prove_poll(cred, [_table("a", "Pending records", pending=3944),
                       _table("b", "Pending records", pending=7), _table("c", "Validated")])  # fmt: skip
    out = _wait(cred, polls=3)
    assert out["reached"] is True and out["phase"] == "replicating"
    assert out["validation"]["validated"] == 3 and out["validation"]["pending_tables"] == 0
    assert "pending_records" not in out["validation"]


def test_a_first_validation_still_running_is_waited_for():
    cred = StubbedCredMgr()
    for _ in range(3):
        _prove_poll(cred, [_table("a", "Validated"), _table("b", "Pending records", pending=50)])
    out = _wait(cred, seconds=120, polls=3)
    assert out["reached"] is False and out["phase"] == "validating"
    assert (
        out["validation"]["pending_tables"] == 1 and out["validation"]["first_pass_records"] == 50
    )


def test_a_validated_table_whose_backlog_rises_and_never_drains_is_never_a_stall():
    """Only a first validation that does not progress is a stall, never the records DMS
    validates as changes arrive on a table it already validated (#2062)."""
    cred = StubbedCredMgr()
    _prove_poll(cred, [_table("a", "Validated"), _table("b", "Pending records", pending=50)])
    for i in range(9):  # a's backlog grows faster than b's first validation shrinks
        _prove_poll(cred, [_table("a", "Pending records", pending=100 + 50 * i),
                           _table("b", "Pending records", pending=49 - i)])  # fmt: skip
    out = _wait(cred)
    assert out["reached"] is False and out["phase"] == "validating"
    assert out["validation_running"] is True and "validation_check" not in out
    _no_pending(cred)


def test_a_task_that_is_not_running_is_never_replicating():
    cred = StubbedCredMgr()
    _prove_poll(cred, [_table("a", "Validated")], status="stopping")
    out = script.wait_status(_clients(cred), STACK, None, 0, 60)
    assert out["phase"] == "validating"


def test_the_tables_validated_in_one_run_still_count_in_the_next(tmp_path, capsys):
    """Prove records the tables DMS reported Validated; the next run, under writes, finds
    them pending records again and still ends replicating."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, [_table("a", "Validated"), _table("b", "Pending records", pending=9)])
    code, first = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert code == 0 and first["phase"] == "validating"
    saved = json.loads(
        (tmp_path / "shop/job1/data-movement/step-1/validation-seen.json").read_text()
    )
    assert list(saved["tables"]) == ["public.a"]
    cred = StubbedCredMgr()
    _prove_poll(cred, [_table("a", "Pending records", pending=12), _table("b", "Validated")])
    code, second = _run(tmp_path, "status", clients=_clients(cred), capsys=capsys)
    assert code == 0 and second["phase"] == "replicating"


def test_revalidate_forgets_the_tables_it_asks_dms_to_validate_again(tmp_path, capsys):
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_gate import ValidatedTables, load_validated, save_validated

    _job(tmp_path)
    store = LocalArtifactStore(str(tmp_path))
    save_validated(store, "shop", "job1", ValidatedTables({"shop.orders": "x", "shop.ok": "y"}))
    cred = StubbedCredMgr()
    script_revalidate = script.revalidate
    try:
        script.revalidate = lambda *_a, **_k: {"status": "revalidating",
                                               "tables_revalidated": ["shop.orders"]}  # fmt: skip
        code, out = _run(tmp_path, "revalidate", "--approve", clients=_clients(cred), capsys=capsys)
    finally:
        script.revalidate = script_revalidate
    assert code == 0 and out["status"] == "revalidating"
    assert load_validated(store, "shop", "job1").marks == {"shop.ok": "y"}


def test_retry_forgets_every_validated_table_before_it_reloads(tmp_path, capsys, monkeypatch):
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_gate import ValidatedTables, load_validated, save_validated

    _job(tmp_path)
    store = LocalArtifactStore(str(tmp_path))
    save_validated(store, "shop", "job1", ValidatedTables({"shop.orders": "x"}))
    seen_at_call = []

    def fake_retry(*_a, **_k):
        seen_at_call.append(load_validated(store, "shop", "job1").marks)
        return {"status": "retried"}

    monkeypatch.setattr(script, "retry", fake_retry)
    code, _ = _run(
        tmp_path, "retry", "--approve", clients=_clients(StubbedCredMgr()), capsys=capsys
    )
    assert code == 0 and seen_at_call == [{}]  # cleared before the reload is sent


def test_the_earlier_until_value_still_works_and_reads_as_replicating():
    cred = StubbedCredMgr()
    _prove_poll(cred, [_table("a", "Validated")])
    out = _wait(cred, until="caught_up", polls=1)
    assert out["until"] == "replicating" and out["reached"] is True
    assert out["phase"] == "replicating"


def test_a_status_line_saved_before_the_rename_reads_as_replicating():
    saved = json.loads(
        '{"status": "complete", "phase": "caught_up", "until": "caught_up", "reached": true}'
    )
    assert script.current_phase(saved["phase"]) == "replicating"
    assert script.current_phase(saved["until"]) == "replicating"
    assert script.UNTIL[script.current_phase(saved["until"])]({"phase": "replicating"})
    assert script.current_phase("validating") == "validating" and script.current_phase(None) is None


def test_the_cli_accepts_the_earlier_until_value(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, [_table("a", "Validated")])
    code, out = _run(tmp_path, "status", "--until", "caught_up", "--wait-seconds", "0",
                     clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["until"] == "replicating" and out["phase"] == "replicating"
    _no_pending(cred)


def test_a_table_that_gets_validated_resets_the_window():
    cred = StubbedCredMgr()
    for i in range(9):  # 0 to 480 s: one more table validated at 240 s, then nothing moves
        done = 1 if i >= 4 else 0
        _prove_poll(cred, _rows(validated=98 + done, pending=64 - done))
    out = _wait(cred, seconds=480, polls=9)
    assert out["reached"] is False and out["phase"] == "validating"
    _no_pending(cred)


def test_no_progress_shortly_after_the_full_load_is_not_yet_a_stall():
    cred = StubbedCredMgr()
    recent = datetime.now(UTC) - timedelta(seconds=120)
    for _ in range(10):
        _prove_poll(cred, _rows(validated=98, pending=64), finished=recent)
    out = _wait(cred)
    assert out["reached"] is False and out["phase"] == "validating"


def test_a_stalled_validation_through_the_cli_ends_the_wait_with_its_check(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(**SNAPSHOT))
    code, out = _run(tmp_path, "status", "--until", "replicating", "--wait-seconds", "540",
                     "--interval-seconds", "60", clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["reached"] is True and out["phase"] == "validation_stalled"
    _no_pending(cred)


# --- --until running: /move --until moving (#1996)


def test_until_running_is_reached_once_the_task_reads_running(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, [], status="starting", percent=0)
    _prove_poll(cred, _rows(pending=3), status="running", percent=5)
    code, out = _run(tmp_path, "status", "--until", "running", "--wait-seconds", "300",
                     "--interval-seconds", "15", clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["reached"] is True and out["until"] == "running"
    assert out["task_status"] == "running" and out["phase"] == "loading"
    _no_pending(cred)


def test_until_running_is_not_reached_while_the_task_is_still_starting():
    cred = StubbedCredMgr()
    for _ in range(3):
        _prove_poll(cred, [], status="starting", percent=0)
    out = _wait(cred, until="running", seconds=60, interval=30, polls=3)
    assert out["reached"] is False and out["task_status"] == "starting"
    _no_pending(cred)


@pytest.mark.parametrize("status", ["stopped", "failed"])
def test_until_running_ends_on_a_task_that_stopped(status):
    cred = StubbedCredMgr()
    _prove_poll(cred, [], status=status, percent=0)
    out = _wait(cred, until="running")
    assert out["reached"] is True and out["phase"] == "task_stopped"
    assert out["task_status"] == status


def test_until_running_on_a_task_already_proving_is_reached_at_once(tmp_path, capsys):
    """A later /move step-1 --until moving finds the task past its start: nothing to wait for."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(validated=3))
    code, out = _run(tmp_path, "status", "--until", "running", "--wait-seconds", "300",
                     clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert out["reached"] is True and out["phase"] == "replicating"


@pytest.mark.parametrize("command", ["preflight", "gate", "plan", "summary", "start", "stop"])
def test_until_is_refused_on_every_command_but_status(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    code, out = _run(tmp_path, command, "--until", "running", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    assert code == 1 and "--until is a status option" in out["message"]
    _no_pending(cred)  # refused before any AWS call


# --- progress lines while proving (#1997)


def _lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def _status_cli(tmp_path, cred, *extra, cloudwatch=None):
    return script.main(["status", "--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path),
                        *extra], clients=_clients(cred), sleep=lambda _s: None,
                       **({"cloudwatch": cloudwatch} if cloudwatch is not None else {}))  # fmt: skip


def test_the_replicating_wait_prints_one_progress_line_per_poll_then_its_result_line(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(pending=2, pending_records=0), percent=40, finished=None,
                TablesLoading=3)  # fmt: skip
    _prove_poll(cred, _rows(validated=1, pending=1, applied=(7, 2)), percent=90, finished=None)
    _prove_poll(cred, _rows(validated=2, applied=(9, 4)))
    code = _status_cli(tmp_path, cred, "--until", "replicating", "--wait-seconds", "540",
                       "--interval-seconds", "60")  # fmt: skip
    lines = _lines(capsys)
    assert code == 0 and len(lines) == 3
    progress, result = lines[:-1], lines[-1]
    assert [p["status"] for p in progress] == ["progress", "progress"]
    assert result["status"] == "complete" and result["reached"] is True
    for line in progress:
        assert set(line) == {"status", *script.PROGRESS_FIELDS}
        assert datetime.fromisoformat(line["time"]).tzinfo is not None
    first, second = progress
    assert (first["full_load_percent"], first["tables_loaded"], first["pending_tables"]) == (
        40,
        305,
        2,
    )
    assert (second["full_load_percent"], second["validated_tables"]) == (90, 1)
    assert (second["inserts_applied"], second["updates_applied"]) == (14, 4)  # summed over tables
    assert second["capture_lag_seconds"] is None  # no CloudWatch reader: no lag
    assert first["not_enabled_tables"] == 0
    _no_pending(cred)


@pytest.mark.passes_on_main  # the one-line output the other waits keep
@pytest.mark.parametrize("until", [None, "stack", "connections"])
def test_every_other_status_call_prints_its_one_line(tmp_path, capsys, until):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(validated=2))
    _status_cli(tmp_path, cred, *(["--until", until, "--wait-seconds", "60"] if until else []))
    lines = _lines(capsys)
    assert len(lines) == 1 and lines[0]["status"] == "complete"


def test_the_running_wait_prints_only_its_result_line(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, [], status="starting", percent=0)
    _prove_poll(cred, _rows(pending=3), percent=5)
    _status_cli(
        tmp_path, cred, "--until", "running", "--wait-seconds", "60", "--interval-seconds", "15"
    )
    lines = _lines(capsys)
    assert len(lines) == 1 and lines[0]["reached"] is True


def _lag(cred, source=4.0, target=9.0):
    cw = cred.stub("cloudwatch")
    for metric in ("CDCLatencySource", "CDCLatencyTarget"):
        cw.add_response("list_metrics", {"Metrics": [{"Namespace": "AWS/DMS", "MetricName": metric,
                        "Dimensions": [{"Name": "ReplicationInstanceIdentifier", "Value": "ri"},
                                       {"Name": "ReplicationTaskIdentifier", "Value": STACK}]}]})  # fmt: skip
    now = datetime.now(UTC)
    cw.add_response("get_metric_data", {"MetricDataResults": [
        {"Id": "lag0", "Label": "CDCLatencySource", "StatusCode": "Complete",
         "Timestamps": [now - timedelta(minutes=2), now - timedelta(minutes=1)], "Values": [1.0, source]},
        {"Id": "lag1", "Label": "CDCLatencyTarget", "StatusCode": "Complete",
         "Timestamps": [now - timedelta(minutes=1)], "Values": [target]}]})  # fmt: skip


def _reader(cred) -> script.LagReader:
    return script.LagReader(cred.client("cloudwatch"))


def test_status_reads_the_latest_capture_lag_from_cloudwatch():
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(validated=2))
    _lag(cred)
    out = script.status(_clients(cred), STACK, _reader(cred))
    assert out["capture_lag_seconds"] == 4.0 and out["apply_lag_seconds"] == 9.0
    _no_pending(cred)


def test_a_wait_lists_the_lag_metrics_once_and_reads_their_data_every_poll():
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(pending=2))
    _lag(cred, source=6.0)
    _prove_poll(cred, _rows(validated=2))
    cred.stub("cloudwatch").add_response("get_metric_data", {"MetricDataResults": [
        {"Id": "lag0", "Label": "CDCLatencySource", "StatusCode": "Complete",
         "Timestamps": [datetime.now(UTC)], "Values": [2.0]}]})  # fmt: skip
    seen = []
    out = _wait(cred, on_poll=seen.append, lag=_reader(cred))
    assert [p["capture_lag_seconds"] for p in seen] == [6.0]
    assert out["reached"] is True and out["capture_lag_seconds"] == 2.0
    assert out["apply_lag_seconds"] is None  # no CDCLatencyTarget datapoint this time
    _no_pending(cred)


def test_the_cli_status_passes_its_cloudwatch_client_to_the_lag_reader(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(validated=2))
    _lag(cred, source=3.0)
    _status_cli(tmp_path, cred, cloudwatch=cred.client("cloudwatch"))
    assert _lines(capsys)[-1]["capture_lag_seconds"] == 3.0
    _no_pending(cred)


def test_a_lag_cloudwatch_refuses_to_read_leaves_the_status_as_is():
    """Lag is progress, not proof: a read profile without CloudWatch access still polls."""
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(validated=2))
    cred.stub("cloudwatch").add_client_error("list_metrics", "AccessDenied", "not allowed")
    out = script.status(_clients(cred), STACK, _reader(cred))
    assert out["phase"] == "replicating" and out["capture_lag_seconds"] is None


def test_no_lag_is_read_before_the_task_runs():
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred)
    _task(cred, status="ready")
    out = script.status(_clients(cred), STACK, _reader(cred))
    assert out["phase"] == "ready_to_start" and "capture_lag_seconds" not in out
    _no_pending(cred)  # no CloudWatch call was stubbed, and none was made


# --- source change-log retention while proving, and what ending the step leaves (#2060) -------

GIB = 1024**3


def _pg_plan(tmp_path: Path) -> None:
    """Make the job's step plan a PostgreSQL source's (``_job`` writes a MySQL one)."""
    path = tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "plan.json"
    path.write_text(json.dumps(make_bundle("aurora_postgresql").plan.model_dump(mode="json")))


def _source_metrics(cred, values: dict[str, list[float]], source_id="shop-source"):
    now = datetime.now(UTC)
    results = [{"Id": f"m{i}", "Label": key, "StatusCode": "Complete",
                "Timestamps": [now - timedelta(minutes=len(v) - n) for n in range(len(v))],
                "Values": v} for i, (key, v) in enumerate(values.items())]  # fmt: skip
    cred.stub("cloudwatch").add_response("get_metric_data", {"MetricDataResults": results})


HEALTHY_PG = {"slot_lag_bytes": [5e6], "transaction_logs_bytes": [2e8],
              "free_storage_bytes": [80.0 * GIB]}  # fmt: skip
CRITICAL_PG = {"slot_lag_bytes": [30.0 * GIB], "slot_disk_bytes": [30.0 * GIB],
               "transaction_logs_bytes": [30.0 * GIB],
               "free_storage_bytes": [40.0 * GIB]}  # fmt: skip


def _retention(cred, kind="rds_instance", family="postgresql", source_id="shop-source"):
    return script.RetentionReader(cred.client("cloudwatch"), kind, source_id, family)


def test_status_reports_the_sources_retention_once_the_task_runs():
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(pending=2))
    _source_metrics(cred, HEALTHY_PG)
    out = script.status(_clients(cred), STACK, retention=_retention(cred))
    retention = out["source_retention"]
    assert retention["level"] == "ok" and retention["source"] == "shop-source"
    assert retention["slot_lag_bytes"] == 5e6 and retention["free_storage_bytes"] == 80.0 * GIB
    _no_pending(cred)


def test_a_stopped_task_names_what_the_stop_costs_a_postgresql_source():
    cred = StubbedCredMgr()
    _prove_poll(cred, [], status="stopped")
    _source_metrics(cred, {"slot_lag_bytes": [2.0 * GIB], "transaction_logs_bytes": [2.0 * GIB],
                           "free_storage_bytes": [100.0 * GIB]})  # fmt: skip
    out = script.status(_clients(cred), STACK, retention=_retention(cred))
    assert out["phase"] == "task_stopped"
    retention = out["source_retention"]
    assert retention["level"] == "warning" and "2.0 GiB behind" in retention["message"]
    assert "keeps WAL" in retention["stopped_task"] and "drop the slot" in retention["stopped_task"]
    _no_pending(cred)


def test_a_stopped_task_on_a_mysql_source_names_the_binlog_retention():
    cred = StubbedCredMgr()
    _prove_poll(cred, [], status="failed")
    _source_metrics(cred, {"binlog_bytes": [1e9], "free_storage_bytes": [100.0 * GIB]})
    out = script.status(_clients(cred), STACK, retention=_retention(cred, family="mysql"))
    assert "binlog retention" in out["source_retention"]["stopped_task"]
    assert "slot" not in out["source_retention"]["stopped_task"]


def test_a_self_managed_source_reads_not_measured_and_still_names_the_stop_risk():
    cred = StubbedCredMgr()
    _prove_poll(cred, [], status="stopped")
    out = script.status(_clients(cred), STACK,
                        retention=_retention(cred, kind="self_managed", source_id=None))  # fmt: skip
    retention = out["source_retention"]
    assert retention["level"] == "not_measured" and "self-managed" in retention["reason"]
    assert "pg_replication_slots" in retention["check_on_source"]
    assert "keeps WAL" in retention["stopped_task"]
    _no_pending(cred)  # no CloudWatch call for a self-managed source


def test_no_source_retention_is_read_before_the_task_runs():
    cred = StubbedCredMgr()
    _stack(cred)
    _connections(cred)
    _task(cred, status="ready")
    out = script.status(_clients(cred), STACK, retention=_retention(cred))
    assert out["phase"] == "ready_to_start" and "source_retention" not in out
    _no_pending(cred)


def test_a_critical_source_retention_ends_the_replicating_wait_before_it_is_reached():
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(pending=2))
    _source_metrics(cred, HEALTHY_PG)
    _prove_poll(cred, _rows(pending=2))
    _source_metrics(cred, CRITICAL_PG)
    seen: list[dict] = []
    clock = iter([0, *(i * 60 for i in range(12))])
    out = script.wait_status(_clients(cred), STACK, "replicating", 540, 60, sleep=lambda _s: None,
                             clock=lambda: next(clock), on_poll=seen.append,
                             retention=_retention(cred))  # fmt: skip
    assert out["reached"] is False and out["stopped_for"] == "source_retention"
    assert out["phase"] == "validating" and out["source_retention"]["level"] == "critical"
    assert [p["source_retention"] for p in seen] == ["ok"]
    _no_pending(cred)  # the wait stopped at once: no third poll


def test_a_warning_source_retention_keeps_polling_and_each_progress_line_carries_it():
    cred = StubbedCredMgr()
    warning = {"slot_lag_bytes": [3.0 * GIB], "transaction_logs_bytes": [3.0 * GIB],
               "free_storage_bytes": [100.0 * GIB]}  # fmt: skip
    _prove_poll(cred, _rows(pending=2))
    _source_metrics(cred, warning)
    _prove_poll(cred, _rows(validated=2))
    _source_metrics(cred, warning)
    seen: list[dict] = []
    clock = iter([0, *(i * 60 for i in range(12))])
    out = script.wait_status(_clients(cred), STACK, "replicating", 540, 60, sleep=lambda _s: None,
                             clock=lambda: next(clock), on_poll=seen.append,
                             retention=_retention(cred))  # fmt: skip
    assert out["reached"] is True and "stopped_for" not in out
    assert (
        seen[0]["source_retention"] == "warning" and "3.0 GiB behind" in seen[0]["source_warning"]
    )


def test_a_critical_reading_on_a_replicating_poll_is_never_lost():
    """Replicating and critical in the same poll: reached, and stopped_for still says why."""
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(validated=2))
    _source_metrics(cred, CRITICAL_PG)
    clock = iter([0, *(i * 60 for i in range(12))])
    out = script.wait_status(_clients(cred), STACK, "replicating", 540, 60, sleep=lambda _s: None,
                             clock=lambda: next(clock), retention=_retention(cred))  # fmt: skip
    assert out["phase"] == "replicating" and out["reached"] is True
    assert out["stopped_for"] == "source_retention"
    assert out["source_retention"]["level"] == "critical"


@pytest.mark.passes_on_main  # renamed only (#2062): guards existing behaviour
@pytest.mark.parametrize("until", ["stack", "running"])
def test_a_critical_source_retention_ends_only_the_replicating_wait(until):
    cred = StubbedCredMgr()
    _prove_poll(cred, [], status="starting", percent=0)
    _source_metrics(cred, CRITICAL_PG)
    out = script.wait_status(_clients(cred), STACK, until, 0, 60, sleep=lambda _s: None,
                             clock=lambda: 0, retention=_retention(cred))  # fmt: skip
    assert "stopped_for" not in out


def test_the_cli_status_reads_the_inputs_source_with_the_plans_engine_family(tmp_path, capsys):
    _job(tmp_path)
    _pg_plan(tmp_path)
    cred = StubbedCredMgr()
    _prove_poll(cred, _rows(validated=2))
    cred.stub("cloudwatch").add_client_error("list_metrics", "AccessDenied", "no lag here")
    cred.stub("cloudwatch").add_response("get_metric_data", {"MetricDataResults": []}, {
        "MetricDataQueries": script_queries("shop-source"), "StartTime": ANY, "EndTime": ANY})  # fmt: skip
    _status_cli(tmp_path, cred, cloudwatch=cred.client("cloudwatch"))
    retention = _lines(capsys)[-1]["source_retention"]
    assert retention["engine_family"] == "postgresql" and retention["source"] == "shop-source"
    assert retention["level"] == "not_measured"  # no datapoint yet: never guessed
    _no_pending(cred)


def script_queries(source_id):
    from src.tools.data_movement.source_retention import _queries

    return _queries("rds_instance", source_id, "postgresql")


def test_the_source_family_is_none_without_a_readable_step_plan(tmp_path):
    assert script.source_family(str(tmp_path), "shop", "job1") is None
    _job(tmp_path)
    assert script.source_family(str(tmp_path), "shop", "job1") == "mysql"
    _pg_plan(tmp_path)
    assert script.source_family(str(tmp_path), "shop", "job1") == "postgresql"


def test_teardown_reminds_to_drop_the_replication_slot_on_a_postgresql_source(tmp_path, capsys):
    _job(tmp_path)
    _pg_plan(tmp_path)
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "deleted"
    assert "replication slot" in out["source_reminder"]
    assert any("pg_drop_replication_slot" in i for i in out["source_followups"])
    assert any("FROM pg_replication_slots WHERE NOT active" in i for i in out["source_followups"])


def test_teardown_names_the_binlog_retention_on_a_mysql_source(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped")
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "teardown", "--approve", "--wait-seconds", "30",
                     "--interval-seconds", "30", clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["status"] == "deleted", out
    assert "binary log retention" in out["source_reminder"]
    assert not any("slot" in i for i in out["source_followups"])


# --- A stopped or failed task: resume, retry, fix hints, credential errors (#2061) -------------

# A task whose full load finished (DMS's own statistics), and one stopped part way through it.
LOADED = {
    "ReplicationTaskStats": {
        "FullLoadProgressPercent": 100,
        "TablesLoaded": 2,
        "FullLoadStartDate": HOUR_AGO,
        "FullLoadFinishDate": HOUR_AGO,
    }
}
PART_LOADED = {
    "ReplicationTaskStats": {
        "FullLoadProgressPercent": 40,
        "TablesLoaded": 1,
        "TablesLoading": 1,
        "FullLoadStartDate": HOUR_AGO,
    }
}
NEVER_LOADED = {"ReplicationTaskStats": {"FullLoadProgressPercent": 0, "TablesQueued": 2}}


def _start(cred, kind):
    cred.stub("dms").add_response(
        "start_replication_task", {},
        {"ReplicationTaskArn": TASK, "StartReplicationTaskType": kind},
    )  # fmt: skip


def _cli(tmp_path, capsys, cred, command, *extra):
    code = script.main([command, "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(tmp_path), "--approve", "--interval-seconds", "30", *extra],
                       clients=_clients(cred), sleep=lambda _s: None)  # fmt: skip
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1, lines  # one compact JSON status line; progress goes to stderr
    return code, json.loads(lines[0])


def _owned_task(cred, status, **fields):
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, status, **fields)


def _loaded(cred, *rows, pages=1, state="Table error", **counters):
    """describe_table_statistics: (schema, table, full load rows, inserts) per table."""
    stats = [{"SchemaName": s, "TableName": t, "FullLoadRows": n, "Inserts": i,
              "AppliedInserts": i, "TableState": state, **counters} for s, t, n, i in rows]  # fmt: skip
    size = -(-len(stats) // pages) if stats else 0
    chunks = [stats[k * size : (k + 1) * size] for k in range(pages)] if stats else [[]]
    for k, chunk in enumerate(chunks):
        page: dict = {"ReplicationTaskArn": TASK, "TableStatistics": chunk}
        params: dict = {"ReplicationTaskArn": TASK, **({"Marker": f"m{k}"} if k else {})}
        if k + 1 < len(chunks):
            page["Marker"] = f"m{k + 1}"
        cred.stub("dms").add_response("describe_table_statistics", page, params)


def _completed(cred):
    _loaded(cred, ("shop", "orders", 10, 0), ("shop", "customers", 5, 0), state="Table completed")


GRANTS = {"LastFailureMessage": "Access denied; you need (at least one of) the REPLICATION SLAVE "
                                "privilege(s) for this operation",
          "StopReason": "Stop Reason FATAL_ERROR"}  # fmt: skip


def test_resume_starts_a_stopped_task_whose_full_load_finished_and_waits_until_it_runs(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **LOADED)
    _completed(cred)
    _start(cred, "resume-processing")
    _poll_task(cred, "starting")
    _poll_task(cred, "running")
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 0 and out["status"] == "resumed" and out["reached"] is True
    assert out["task_status"] == "running" and out["waited_seconds"] == 60
    assert out["stack_name"] == STACK and out["task_arn"] == TASK
    _no_pending(cred)  # one resume-processing start, no stop, no reload


@pytest.mark.parametrize(("stats", "state", "why"), [
    (PART_LOADED, "Table completed", "its full load started"),
    ({"ReplicationTaskStats": {"FullLoadProgressPercent": 100}}, "Table completed",
     "its full load made progress"),  # no FullLoadFinishDate: not proven finished
    (LOADED, "Full load", "shop.orders is Full load"),
    (LOADED, "Table error", "shop.orders is Table error"),
    (LOADED, "Table is being reloaded", "shop.orders is Table is being reloaded"),
    (LOADED, "Table cancelled", "shop.orders is Table cancelled"),
])  # fmt: skip
def test_resume_refuses_a_stopped_task_whose_full_load_did_not_finish_with_the_customers_steps(
    tmp_path, capsys, stats, state, why
):
    """resume-processing reloads partly loaded tables: over DO_NOTHING that duplicates rows. When
    retry cannot reload either, resume gives the customer's steps, never a pointer to retry."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **stats)
    _loaded(cred, ("shop", "orders", 10, 0), state=state)
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 1 and out["kind"] == "target_may_hold_rows" and why in out["message"]
    assert "run retry" not in out["message"] and out["tables_to_empty"] == ["shop.orders"]
    assert any("reload-target" in s for s in out["customer_steps"])
    _no_pending(cred)  # no start of any kind


def test_resume_points_at_retry_only_when_retry_would_reload(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **NEVER_LOADED)
    _loaded(cred, ("shop", "orders", 0, 0), state="Before load")
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 1 and "run retry" in out["message"] and "kind" not in out
    _no_pending(cred)


def test_a_finished_load_stuck_on_a_table_is_no_dead_end(tmp_path, capsys):
    """Re-review #2074: 100% with a finish date but a table not completed. The status line
    offers nothing, and resume and retry both give the same customer's steps."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_status(cred, status="stopped", stats=LOADED["ReplicationTaskStats"])
    _loaded(cred, ("shop", "orders", 10, 0), state="Table is being reloaded")
    _status_cli(tmp_path, cred)
    line = _lines(capsys)[-1]
    assert line["recovery"] is None and "Table is being reloaded" in line["recovery_refused"]
    outs = []
    for command in ("resume", "retry"):
        _owned_task(cred, "stopped", **LOADED)
        _loaded(cred, ("shop", "orders", 10, 0), state="Table is being reloaded")
        code, out = _cli(tmp_path, capsys, cred, command)
        assert code == 1 and out["kind"] == "target_may_hold_rows", (command, out)
        assert "run resume" not in out["message"] and "run retry" not in out["message"]
        outs.append(out)
    assert outs[0]["customer_steps"] == outs[1]["customer_steps"] == line["customer_steps"]
    assert outs[0]["tables_to_empty"] == line["tables_to_empty"] == ["shop.orders"]
    _no_pending(cred)


def test_resume_reports_a_task_still_starting_at_the_deadline(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **LOADED)
    _completed(cred)
    _start(cred, "resume-processing")
    for _ in range(2):
        _poll_task(cred, "starting")
    code, out = _cli(tmp_path, capsys, cred, "resume", "--wait-seconds", "60")
    assert code == 0 and out["status"] == "starting" and out["reached"] is False
    _no_pending(cred)


def test_a_resume_that_stops_again_after_starting_is_reported_failed(tmp_path, capsys):
    """stopped -> starting -> stopped: the task went back, it is not still starting."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **LOADED)
    _completed(cred)
    _start(cred, "resume-processing")
    _poll_task(cred, "starting")
    _poll_task(cred, "stopped")
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 0 and out["status"] == "resume_failed" and out["waited_seconds"] == 60
    _no_pending(cred)


def test_a_resume_that_fails_again_carries_the_failures_hint(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **LOADED)
    _completed(cred)
    _start(cred, "resume-processing")
    _poll_task(cred, "starting")
    _poll_task(cred, "failed", **GRANTS)
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 0 and out["status"] == "resume_failed" and out["task_status"] == "failed"
    assert "REPLICATION CLIENT" in out["hint"] and "REPLICATION SLAVE" in out["dms_reports"]
    _no_pending(cred)


@pytest.mark.parametrize(("status", "pointer"), [("ready", "start"),
                                                 ("stopping", "wait until it has stopped"),
                                                 ("running", "nothing to resume"),
                                                 ("starting", "nothing to resume"),
                                                 ("modifying", "status shows")])  # fmt: skip
def test_resume_refuses_a_task_that_is_not_stopped(tmp_path, capsys, status, pointer):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, status)
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 1 and out["message"].startswith(script.dms_reports(
        {"Status": status, "StopReason": "Stop Reason NORMAL"}))  # fmt: skip
    assert "resume acts only on a stopped or failed task" in out["message"]
    assert pointer in out["message"]
    _no_pending(cred)  # no table statistics read, no start of any kind


@pytest.mark.parametrize("command", ["resume", "retry"])
def test_resume_and_retry_without_a_stack_or_a_task_are_an_error(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _no_stack(cred)
    code, out = _cli(tmp_path, capsys, cred, command)
    assert code == 1 and "no stack" in out["message"] and "nothing to" in out["message"]
    _stack(cred, status="DELETE_FAILED")
    _resources(cred, task_status="DELETE_COMPLETE")
    code, out = _cli(tmp_path, capsys, cred, command)
    assert code == 1 and "no task" in out["message"]
    _no_pending(cred)


@pytest.mark.parametrize(("status", "stats"), [("failed", {}), ("failed", NEVER_LOADED),
                                               ("stopped", NEVER_LOADED)])  # fmt: skip
def test_retry_reloads_a_task_whose_full_load_never_started_and_waits_until_it_runs(
    tmp_path, capsys, status, stats
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, status, **GRANTS, **stats)
    _loaded(cred, ("shop", "orders", 0, 0), ("shop", "customers", 0, 0), pages=2)
    _start(cred, "reload-target")
    _poll_task(cred, "starting")
    _poll_task(cred, "running")
    code, out = _cli(tmp_path, capsys, cred, "retry")
    assert code == 0 and out["status"] == "retried" and out["reached"] is True
    assert out["task_status"] == "running" and out["tables_checked"] == 2
    _no_pending(cred)  # every page of statistics read before the reload


def test_retry_reloads_a_task_that_failed_before_it_had_table_statistics(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "failed", **GRANTS)
    _loaded(cred)
    _start(cred, "reload-target")
    _poll_task(cred, "running")
    code, out = _cli(tmp_path, capsys, cred, "retry")
    assert code == 0 and out["status"] == "retried" and out["tables_checked"] == 0
    _no_pending(cred)


def test_a_retry_that_reads_failed_until_the_deadline_is_reported_failed(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "failed", **GRANTS)
    _loaded(cred, ("shop", "orders", 0, 0))
    _start(cred, "reload-target")
    for _ in range(2):
        _poll_task(cred, "failed", **GRANTS)
    code, out = _cli(tmp_path, capsys, cred, "retry", "--wait-seconds", "60")
    assert code == 0 and out["status"] == "retry_failed" and out["reached"] is False
    assert "REPLICATION CLIENT" in out["hint"]
    _no_pending(cred)


@pytest.mark.parametrize(("status", "stats", "rows", "counters", "why"), [
    # DMS restarts its table statistics on a reload: a started full load refuses at 0 rows.
    ("failed", LOADED, (("shop", "orders", 0, 0),), {}, "its full load started"),
    ("stopped", PART_LOADED, (("shop", "orders", 0, 0),), {}, "its full load started"),
    ("failed", {}, (("shop", "orders", 10, 0),), {}, "shop.orders FullLoadRows is 10"),
    ("failed", {}, (("shop", "orders", 0, 3),), {}, "shop.orders Inserts is 3"),
    ("failed", {}, (("shop", "orders", 0, 0),), {"Updates": 2}, "shop.orders Updates is 2"),
    ("failed", {}, (("shop", "orders", 0, 0),), {"AppliedDeletes": 1}, "AppliedDeletes is 1"),
    ("failed", {}, (("shop", "orders", 0, 0),), {"Ddls": 1}, "shop.orders Ddls is 1"),
    ("failed", {}, (("shop", "orders", 0, 0),), {"FullLoadErrorRows": 4}, "FullLoadErrorRows"),
    ("failed", {}, (("shop", "orders", 0, 0),), {"FullLoadReloaded": True}, "shop.orders was loaded"),
    ("failed", {}, (("shop", "orders", 0, 0),), {"FullLoadStartTime": HOUR_AGO}, "was loaded"),
    ("failed", {"ReplicationTaskStats": {"TablesLoaded": 1}}, (("shop", "orders", 0, 0),), {},
     "TablesLoaded is 1"),
])  # fmt: skip
def test_retry_refuses_a_task_that_may_have_written_to_aurora_and_prints_the_customers_steps(
    tmp_path, capsys, status, stats, rows, counters, why
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, status, **GRANTS, **stats)
    _loaded(cred, *rows, ("shop", "customers", 0, 0), **counters)
    code, out = _cli(tmp_path, capsys, cred, "retry")
    assert code == 1 and out["status"] == "error" and out["kind"] == "target_may_hold_rows"
    assert why in out["message"] and "never connects to your databases" in out["message"]
    assert out["tables_to_empty"] == ["shop.orders", "shop.customers"]  # every table of the step
    steps = out["customer_steps"]
    assert (
        "SET FOREIGN_KEY_CHECKS = 0; TRUNCATE TABLE `shop`.`orders`; TRUNCATE TABLE "
        "`shop`.`customers`; SET FOREIGN_KEY_CHECKS = 1;"
    ) in steps  # SQL only, one entry
    text = "\n".join(steps)
    assert "On the Aurora cluster (the target), never on the source" in text
    assert (f"aws dms start-replication-task --replication-task-arn {TASK} "
            f"--start-replication-task-type reload-target --region {REGION}") in text  # fmt: skip
    assert "REPLICATION CLIENT" in out["hint"]  # fix the cause first
    _no_pending(cred)  # no start of any kind


def test_retry_with_no_table_statistics_names_the_steps_tables_in_words(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "failed", **LOADED)
    _loaded(cred)
    code, out = _cli(tmp_path, capsys, cred, "retry")
    assert code == 1 and out["kind"] == "target_may_hold_rows" and out["tables_to_empty"] == []
    assert any("every table step-1.ddl.sql created" in s for s in out["customer_steps"])
    assert not any("TRUNCATE" in s for s in out["customer_steps"])
    _no_pending(cred)


def test_retry_quotes_postgresql_tables_and_keeps_the_sql_apart_from_the_prose(tmp_path, capsys):
    _job(tmp_path)
    _pg_plan(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "failed", **LOADED)
    _loaded(cred, ("public", 'odd"name', 4, 0), ("public", "posts", 9, 0))
    code, out = _cli(tmp_path, capsys, cred, "retry")
    assert code == 1 and out["kind"] == "target_may_hold_rows"
    steps = out["customer_steps"]
    assert 'TRUNCATE TABLE "public"."odd""name", "public"."posts";' in steps  # the whole entry
    assert any(s.startswith("If PostgreSQL refuses") for s in steps)
    assert not any("FOREIGN_KEY_CHECKS" in s for s in steps)
    _no_pending(cred)


@pytest.mark.parametrize(("status", "pointer"), [
    ("ready", "run start"), ("running", "nothing to retry"), ("stopping", "run retry again"),
    ("failed-move", "status shows"),
])  # fmt: skip
def test_retry_refuses_a_task_it_does_not_cover(tmp_path, capsys, status, pointer):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, status)
    code, out = _cli(tmp_path, capsys, cred, "retry")
    assert code == 1 and "retry acts only on a stopped or failed task" in out["message"]
    assert pointer in out["message"]
    _no_pending(cred)  # no table statistics read, no start


def test_retry_points_at_resume_only_when_resume_would_run(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **LOADED)
    _completed(cred)
    code, out = _cli(tmp_path, capsys, cred, "retry")
    assert code == 1 and "run resume" in out["message"] and "kind" not in out
    _no_pending(cred)


# Property: recovery names a command only when that command would write, and when it names
# none, neither writes; a command points at the other only when the other is the one named.
_STATUSES = ["stopped", "failed", "ready", "running", "stopping", "failed-move"]
_STATS = {"loaded": LOADED, "part": PART_LOADED, "never": NEVER_LOADED, "none": {},
          "no_finish": {"ReplicationTaskStats": {"FullLoadProgressPercent": 100}}}  # fmt: skip
_TABLES = {"completed": ("Table completed", 0), "reloading": ("Table is being reloaded", 0),
           "before": ("Before load", 0), "error_rows": ("Table error", 5), "empty": None}  # fmt: skip


@pytest.mark.parametrize("status", _STATUSES)
@pytest.mark.parametrize("stats", list(_STATS))
@pytest.mark.parametrize("tables", list(_TABLES))
def test_recovery_always_names_a_command_that_would_run(tmp_path, capsys, status, stats, tables):
    _job(tmp_path)
    fields = _STATS[stats]
    table = _TABLES[tables]
    rows = [] if table is None else [("shop", "orders", table[1], 0)]
    state = "Table completed" if table is None else table[0]
    task = {"Status": status, "ReplicationTaskArn": TASK, **fields}
    stat_rows = [{"SchemaName": s, "TableName": n, "FullLoadRows": c, "Inserts": i,
                  "AppliedInserts": i, "TableState": state} for s, n, c, i in rows]  # fmt: skip
    named = script.recovery(task, stat_rows)
    reads = status in ("stopped", "failed")
    for command, start_type in (("resume", "resume-processing"), ("retry", "reload-target")):
        cred = StubbedCredMgr()
        _owned_task(cred, status, **fields)
        if reads:
            _loaded(cred, *rows, state=state)
        if command == named:
            _start(cred, start_type)
            _poll_task(cred, "running")
        code, out = _cli(tmp_path, capsys, cred, command)
        if command == named:
            assert code == 0 and out["status"] in ("resumed", "retried"), out
        else:
            assert code == 1, out
            other = "retry" if command == "resume" else "resume"
            assert (f"run {other}" in out.get("message", "")) == (named == other), out
        _no_pending(cred)


def test_restart_validation_keeps_its_result_lines_through_the_shared_resume(tmp_path, capsys):
    """restart-validation's second half is the one resume uses: its line is unchanged."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped")
    _start(cred, "resume-processing")
    _poll_task(cred, "running")
    code, out, _ = _restart(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "restarted" and out["resumed"] is True
    assert out["hint"] is None
    _no_pending(cred)


def test_restart_validation_reports_a_task_that_stopped_again_after_starting(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped")
    _start(cred, "resume-processing")
    _poll_task(cred, "starting")
    _poll_task(cred, "stopped")
    code, out, _ = _restart(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "resume_failed" and out["waited_seconds"] == 60
    _no_pending(cred)


# Fix hints and the recovery Prove may offer, on every task_stopped line.


def _failed_status(cred, status="failed", stats=None, tables=None, **fields):
    """A Prove status poll of a task that is not running; ``tables`` None: no statistics read."""
    _stack(cred)
    _connections(cred)
    cred.stub("dms").add_response("describe_replication_tasks", {"ReplicationTasks": [{
        "ReplicationTaskArn": TASK, "Status": status, **fields,
        "ReplicationTaskStats": stats or {"FullLoadProgressPercent": 40, "TablesLoaded": 2}}]})  # fmt: skip
    if tables is not None:
        cred.stub("dms").add_response(
            "describe_table_statistics", {"ReplicationTaskArn": TASK, "TableStatistics": tables}
        )


def test_the_prove_task_stopped_line_carries_the_failures_hint(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_status(cred, LastFailureMessage="Failed to run load command. Cannot allocate memory",
                   StopReason="Stop Reason FATAL_ERROR", tables=[])  # fmt: skip
    code = _status_cli(tmp_path, cred, "--until", "replicating", "--wait-seconds", "0")
    out = _lines(capsys)[-1]
    assert code == 0 and out["phase"] == "task_stopped" and "instance_class" in out["hint"]
    # 40% loaded: neither command may reload, so the line carries the customer's steps.
    assert out["recovery"] is None and "TablesLoaded is 2" in out["recovery_refused"]
    assert out["tables_to_empty"] == [] and any(
        "reload-target --region us-east-1" in s for s in out["customer_steps"]
    )


@pytest.mark.parametrize(("status", "stats", "tables", "offer"), [
    ("stopped", LOADED["ReplicationTaskStats"], [], "resume"),
    ("stopped", NEVER_LOADED["ReplicationTaskStats"], [], "retry"),
    ("failed", {"FullLoadProgressPercent": 0}, [], "retry"),
    ("stopped", PART_LOADED["ReplicationTaskStats"], [], None),
    ("stopped", {"FullLoadProgressPercent": 100}, [], None),  # no finish date
    ("failed", LOADED["ReplicationTaskStats"], [], None),
    ("failed-move", LOADED["ReplicationTaskStats"], None, None),  # no statistics read
])  # fmt: skip
def test_the_task_stopped_line_names_the_recovery_prove_may_offer(
    tmp_path, capsys, status, stats, tables, offer
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_status(cred, status=status, stats=stats, tables=tables)
    _status_cli(tmp_path, cred)
    out = _lines(capsys)[-1]
    assert out["phase"] == "task_stopped" and out["recovery"] == offer
    _no_pending(cred)


@pytest.mark.parametrize("fields", [{"LastFailureMessage": "Something DMS has never said"},
                                    {"StopReason": "Stop Reason NORMAL"}, {}])  # fmt: skip
def test_a_task_stopped_line_with_an_unknown_failure_carries_no_hint(tmp_path, capsys, fields):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_status(cred, tables=[], **fields)
    _status_cli(tmp_path, cred)
    out = _lines(capsys)[-1]
    assert out["phase"] == "task_stopped" and "hint" in out and out["hint"] is None


def test_the_task_stopped_wait_and_stop_lines_carry_the_hint():
    cred = StubbedCredMgr()
    _stack(cred)
    _task_rows(cred)
    _poll_task(cred, "failed", **GRANTS)
    out = script.wait_status(_clients(cred), STACK, "task_stopped", 0, 30, sleep=lambda _s: None)
    assert out["reached"] is True and "REPLICATION CLIENT" in out["hint"]


def test_a_running_task_line_carries_no_hint_from_an_old_failure(tmp_path, capsys):
    """DMS keeps the last failure message after a resume; the task runs, so no hint."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_status(cred, status="running", LastFailureMessage=GRANTS["LastFailureMessage"])
    cred.stub("dms").add_response(
        "describe_table_statistics",
        {"ReplicationTaskArn": TASK, "TableStatistics": _rows(pending=1)},
    )
    code = _status_cli(tmp_path, cred)
    out = _lines(capsys)[-1]
    assert code == 0 and out["phase"] == "loading" and "hint" in out and out["hint"] is None
    assert out["recovery"] is None


# Credential errors: refresh and run again, and what this run already changed.


def _raise_on(cred, service, operation, exc, call=1):
    """Raise ``exc`` on the ``call``-th ``operation``, before the Stubber reads its queue."""
    client = cred.clients[service] if service in cred.clients else cred.client(service)
    seen = {"n": 0}

    def boom(**_kwargs):
        seen["n"] += 1
        if seen["n"] == call:
            raise exc

    service_id = client.meta.service_model.service_id.hyphenize()
    client.meta.events.register(f"provide-client-params.{service_id}.{operation}", boom)


@pytest.mark.parametrize("exc", ["NoCredentialsError", "SSOTokenLoadError", "LoginTokenLoadError"])
def test_a_missing_or_expired_session_is_a_credentials_error_on_a_read(tmp_path, capsys, exc):
    from botocore import exceptions

    _job(tmp_path)
    cred = StubbedCredMgr()
    cls = getattr(exceptions, exc)
    _raise_on(cred, "cloudformation", "DescribeStacks",
              cls() if exc == "NoCredentialsError" else cls(error_msg="expired"))  # fmt: skip
    code = _status_cli(tmp_path, cred)
    out = _lines(capsys)[-1]
    assert code == 1 and out["status"] == "error" and out["kind"] == "credentials"
    assert out["message"].endswith(
        "refresh your credentials and run /move step-1 again; nothing else changed"
    )
    assert exc in out["message"]
    _no_pending(cred)


def test_an_expired_token_on_the_resume_itself_changed_nothing(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **LOADED)
    _completed(cred)
    cred.stub("dms").add_client_error("start_replication_task", "ExpiredTokenException",
                                      "The security token included in the request is expired")  # fmt: skip
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 1 and out["kind"] == "credentials"
    assert out["message"].endswith("nothing else changed")
    _no_pending(cred)


def test_an_expired_token_after_the_resume_says_the_task_was_started(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped", **LOADED)
    _completed(cred)
    _start(cred, "resume-processing")
    cred.stub("dms").add_client_error("describe_replication_tasks", "ExpiredToken", "expired")
    code, out = _cli(tmp_path, capsys, cred, "resume")
    assert code == 1 and out["kind"] == "credentials"
    assert "nothing else changed" not in out["message"]
    assert "this run already called StartReplicationTask" in out["message"]
    assert "status shows where the task stands" in out["message"]
    _no_pending(cred)


def test_an_expired_token_between_the_stop_and_the_resume_says_the_task_was_stopped(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "running")
    cred.stub("dms").add_response("stop_replication_task", {}, {"ReplicationTaskArn": TASK})
    _poll_task(cred, "stopped")
    cred.stub("dms").add_client_error("start_replication_task", "ExpiredTokenException", "expired")
    code, out, _ = _restart(tmp_path, capsys, cred)
    assert code == 1 and out["kind"] == "credentials"
    assert "this run already called StopReplicationTask before" in out["message"]
    assert "StartReplicationTask before" not in out["message"]  # the start never happened
    _no_pending(cred)


@pytest.mark.passes_on_main  # an AWS error that is not about credentials stays as it was
def test_an_aws_error_that_is_not_about_credentials_has_no_kind(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned_task(cred, "stopped")
    cred.stub("dms").add_client_error("start_replication_task", "InvalidResourceStateFault", "no")
    code, out = _cli(tmp_path, capsys, cred, "restart-validation")
    assert code == 1 and "kind" not in out and "InvalidResourceStateFault" in out["message"]
    _no_pending(cred)


# --- A stack that rolled back or is stuck: deploy, status and teardown (#2063) ----------------

CAPACITY = ("Resource handler returned message: \"There are not enough resources available to "
            "create a replication instance of class dms.r6i.large in us-east-1a\" "
            "(HandlerErrorCode: InsufficientResourceCapacityFault)")  # fmt: skip
NOT_UPDATABLE = "delete the failed stack first (`/move step-1` can end it)"
# Deploy and status per CloudFormation status, as #2063 changes them. Every other status keeps
# what it did: ``UNCHANGED`` below.
FAILED_CREATE = ("CREATE_FAILED", "ROLLBACK_FAILED", "ROLLBACK_COMPLETE")
STUCK = ("DELETE_FAILED", "UPDATE_FAILED", "UPDATE_ROLLBACK_FAILED", "IMPORT_ROLLBACK_FAILED")
UNCHANGED_DEPLOYED = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE")
ROLLED_BACK_UPDATE = ("UPDATE_ROLLBACK_COMPLETE", "IMPORT_ROLLBACK_COMPLETE")


def _all_statuses():
    from tests.unit.tools.aws.aws_stubs import make_client

    return make_client("cloudformation").meta.service_model.shape_for("StackStatus").enum


def _changing():
    return [x for x in _all_statuses() if x.endswith("_IN_PROGRESS") and x != "REVIEW_IN_PROGRESS"]


def test_the_status_groups_below_cover_every_cloudformation_status():
    groups = [*FAILED_CREATE, *STUCK, *UNCHANGED_DEPLOYED, *ROLLED_BACK_UPDATE,
              "REVIEW_IN_PROGRESS", "DELETE_COMPLETE", *_changing()]  # fmt: skip
    assert sorted(groups) == sorted(_all_statuses())
    from src.tools.data_movement.stack_states import stack_kind

    assert {x: stack_kind(x) for x in FAILED_CREATE} == dict.fromkeys(
        FAILED_CREATE, "create_failed"
    )


def _failed_stack(cred, status, *, job_tag="job1", outputs=None, protected=None, reason=None):
    stack = {"StackName": STACK, "StackId": STACK_ID, "CreationTime": "2026-11-04T00:00:00Z",
             "StackStatus": status,
             "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in (outputs or {}).items()]}  # fmt: skip
    if job_tag is not None:
        stack["Tags"] = [{"Key": "modernizer:job-id", "Value": job_tag}]
    if protected is not None:
        stack["EnableTerminationProtection"] = protected
    if reason:
        stack["StackStatusReason"] = reason
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [stack]},
                                             {"StackName": STACK})  # fmt: skip


def _cfn_event(logical, status, reason=None, seconds=0):
    kind = ("AWS::CloudFormation::Stack" if logical == STACK
            else STEP_RESOURCES.get(logical, "AWS::DMS::ReplicationInstance"))  # fmt: skip
    return {"StackId": STACK_ID, "EventId": f"{logical}-{status}", "StackName": STACK,
            "LogicalResourceId": logical, "ResourceType": kind, "ResourceStatus": status,
            "Timestamp": datetime(2026, 11, 9, tzinfo=UTC) + timedelta(seconds=seconds),
            **({"ResourceStatusReason": reason} if reason else {})}  # fmt: skip


def _events(cred, *events, pages=1):
    """describe_stack_events by the stack id, newest first, in ``pages`` pages."""
    rows = list(events)
    size = -(-len(rows) // pages) if rows else 0
    chunks = [rows[i * size : (i + 1) * size] for i in range(pages)] if rows else [[]]
    for i, chunk in enumerate(chunks):
        response: dict = {"StackEvents": chunk}
        if i + 1 < len(chunks):
            response["NextToken"] = f"e{i + 1}"
        cred.stub("cloudformation").add_response(
            "describe_stack_events", response,
            {"StackName": STACK_ID, **({"NextToken": f"e{i}"} if i else {})})  # fmt: skip


def _capacity_events(cred, operation="CREATE", pages=1):
    _events(cred, _cfn_event(STACK, "ROLLBACK_COMPLETE", seconds=40),
            _cfn_event("ReplicationInstance", f"{operation}_FAILED", CAPACITY, seconds=20),
            _cfn_event("SubnetGroup", f"{operation}_COMPLETE", seconds=10),
            _cfn_event(STACK, f"{operation}_IN_PROGRESS", "User Initiated", seconds=0),
            pages=pages)  # fmt: skip


@pytest.mark.parametrize("status", FAILED_CREATE)
def test_deploy_after_a_failed_create_says_delete_it_first_and_sends_no_change_set(
    tmp_path, capsys, status
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, status)
    _capacity_events(cred)
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and out["status"] == "error" and out["kind"] == "stack_not_updatable"
    assert NOT_UPDATABLE in out["message"] and CAPACITY in out["message"]
    assert out["stack_status"] == status
    assert out["stack_failure"]["logical_id"] == "ReplicationInstance"
    assert "instance_class" in out["stack_failure"]["hint"]
    _no_pending(cred)  # and no create_change_set was queued or made


@pytest.mark.parametrize("status", STUCK)
def test_deploy_on_a_stuck_stack_says_what_the_customer_can_do_and_sends_nothing(
    tmp_path, capsys, status
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, status, outputs=OUTPUTS)
    _events(cred)  # no events at all: the line still says what to do
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and out["kind"] == "stack_not_updatable" and out["stack_failure"] is None
    assert out["stack_action"] and out["stack_action"] in out["message"]
    _no_pending(cred)


def _change_set(cred, kind):
    cfn = cred.stub("cloudformation")
    cfn.add_response("create_change_set", {"Id": "cs"}, {"StackName": STACK, "ChangeSetName": ANY,
        "ChangeSetType": kind, "TemplateBody": ANY, "Capabilities": ["CAPABILITY_IAM"],
        "Parameters": ANY, "Tags": script.stack_tags("job1")})  # fmt: skip
    cfn.add_response("describe_change_set", {"Status": "CREATE_COMPLETE", "Changes": []},
                     {"StackName": STACK, "ChangeSetName": ANY})  # fmt: skip


@pytest.mark.passes_on_main  # a rolled-back update keeps taking updates, as before
@pytest.mark.parametrize("status", ROLLED_BACK_UPDATE)
def test_deploy_after_an_update_rolled_back_updates_the_stack_again(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, status, outputs=OUTPUTS)
    _change_set(cred, "UPDATE")
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "change_set_ready" and out["change_set_type"] == "UPDATE"
    _no_pending(cred)


@pytest.mark.passes_on_main  # a deployed, missing or never-executed stack deploys as before
@pytest.mark.parametrize("status, kind", [*((x, "UPDATE") for x in UNCHANGED_DEPLOYED),
                                          ("REVIEW_IN_PROGRESS", "CREATE")])  # fmt: skip
def test_deploy_on_a_deployed_or_never_executed_stack_is_unchanged(tmp_path, capsys, status, kind):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, status, outputs=OUTPUTS if kind == "UPDATE" else None)
    _change_set(cred, kind)
    code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["change_set_type"] == kind
    _no_pending(cred)


@pytest.mark.passes_on_main  # a stack still changing is waited on, as before
def test_deploy_refuses_every_status_still_changing(tmp_path, capsys):
    for status in _changing():
        _job(tmp_path)
        cred = StubbedCredMgr()
        _failed_stack(cred, status)
        code, out = _run(tmp_path, "deploy", "--approve", clients=_clients(cred), capsys=capsys)
        assert code == 1 and status in out["message"] and "wait for it" in out["message"]
        _no_pending(cred)


@pytest.mark.parametrize("status", FAILED_CREATE)
def test_status_after_a_failed_create_names_the_failed_resource_and_the_way_on(status):
    cred = StubbedCredMgr()
    _failed_stack(
        cred, status, reason="The following resource(s) failed to create: [ReplicationInstance]."
    )
    _capacity_events(cred, pages=2)
    out = script.status(_clients(cred), STACK)
    assert out["phase"] == "stack_create_failed"
    assert out["stack_failure"] == {"logical_id": "ReplicationInstance",
                                    "resource_type": "AWS::DMS::ReplicationInstance",
                                    "status": "CREATE_FAILED", "reason": CAPACITY,
                                    "hint": out["stack_failure"]["hint"]}  # fmt: skip
    assert "another instance_class" in out["stack_failure"]["hint"]
    assert NOT_UPDATABLE in out["stack_action"]
    _no_pending(cred)  # read-only: describe_stacks and describe_stack_events only


def test_status_after_a_failed_create_with_no_events_keeps_cloudformations_reason():
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_COMPLETE", reason="The following resource(s) failed to create")
    _events(cred)
    out = script.status(_clients(cred), STACK)
    assert out["phase"] == "stack_create_failed" and out["stack_failure"] is None
    assert out["stack_reason"] == "The following resource(s) failed to create"
    assert NOT_UPDATABLE in out["stack_action"]
    _no_pending(cred)


@pytest.mark.parametrize("status", STUCK)
def test_status_of_a_stuck_stack_is_stack_failed_with_what_to_do(status):
    cred = StubbedCredMgr()
    _failed_stack(cred, status, outputs=OUTPUTS)
    _capacity_events(cred, operation="UPDATE")
    out = script.status(_clients(cred), STACK)
    assert out["phase"] == "stack_failed" and STACK in out["stack_action"]
    assert out["stack_failure"]["status"] == "UPDATE_FAILED"
    _no_pending(cred)


def test_a_change_set_never_executed_is_not_a_stack_still_changing():
    """A declined change set leaves an empty REVIEW_IN_PROGRESS stack: the wait ends at once."""
    cred = StubbedCredMgr()
    _failed_stack(cred, "REVIEW_IN_PROGRESS")
    _change_sets(cred, "AVAILABLE")  # created, never executed
    clock = iter([0, 0, 30])
    out = script.wait_status(_clients(cred), STACK, "stack", 540, 30, sleep=lambda _s: None,
                             clock=lambda: next(clock))  # fmt: skip
    assert out["phase"] == "stack_in_review" and out["reached"] is True
    assert "never executed" in out["stack_action"]
    _no_pending(cred)


@pytest.mark.parametrize("status", ROLLED_BACK_UPDATE)
def test_an_update_that_rolled_back_still_judges_the_task_so_prove_stays_reachable(status):
    cred = StubbedCredMgr()
    _failed_stack(cred, status, outputs=OUTPUTS)
    _capacity_events(cred, operation="UPDATE")
    _connections(cred)
    _prove_task(cred)
    cred.stub("dms").add_response("describe_table_statistics",
                                  {"TableStatistics": _rows(validated=2, pending_records=0)})  # fmt: skip
    out = script.status(_clients(cred), STACK)
    assert out["phase"] == "replicating" and out["task_status"] == "running"
    assert out["stack_failure"]["status"] == "UPDATE_FAILED"
    assert "earlier settings" in out["stack_action"]
    _no_pending(cred)


@pytest.mark.passes_on_main  # a stack that deployed cleanly reads no events
@pytest.mark.parametrize("status", UNCHANGED_DEPLOYED)
def test_a_cleanly_deployed_stack_reads_no_events_and_names_no_action(status):
    cred = StubbedCredMgr()
    _failed_stack(cred, status, outputs=OUTPUTS)
    _connections(cred)
    _task(cred)
    out = script.status(_clients(cred), STACK)
    assert out["phase"] == "ready_to_start" and "stack_action" not in out
    _no_pending(cred)


def test_test_and_start_accept_a_stack_whose_update_rolled_back(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "UPDATE_ROLLBACK_COMPLETE", outputs=OUTPUTS)
    for ep in (SOURCE_EP, TARGET_EP):
        cred.stub("dms").add_response(
            "test_connection", {}, {"ReplicationInstanceArn": INSTANCE, "EndpointArn": ep}
        )
    code, out = _run(tmp_path, "test", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "testing"
    _no_pending(cred)


@pytest.mark.parametrize("status", FAILED_CREATE)
def test_the_until_waits_end_on_a_failed_create(status):
    for until in ("running", "replicating"):
        cred = StubbedCredMgr()
        _failed_stack(cred, status)
        _events(cred)
        ticks = iter([0, 0, 30])
        out = script.wait_status(_clients(cred), STACK, until, 540, 30, sleep=lambda _s: None,
                                 clock=functools.partial(next, ticks))  # fmt: skip
        assert out["reached"] is True and out["phase"] == "stack_create_failed", until
        _no_pending(cred)


# teardown of this job's own stack whose create failed or never ran


def _rolled_back_rows(cred, live=()):
    """list_stack_resources of a failed create: every resource deleted but those in ``live``."""
    rows = [_summary(k, v, "DELETE_FAILED" if k in live else "DELETE_COMPLETE")
            for k, v in STEP_RESOURCES.items() if k != "ReplicationTask"]  # fmt: skip
    cred.stub("cloudformation").add_response(
        "list_stack_resources", {"StackResourceSummaries": rows}, {"StackName": STACK}
    )


def _teardown(tmp_path, capsys, cred):
    return _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)


def _deleted(cred):
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")


@pytest.mark.parametrize("status", FAILED_CREATE)
def test_teardown_ends_this_jobs_stack_whose_create_failed(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, status)
    _rolled_back_rows(cred)
    _deleted(cred)
    code, out = _teardown(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "deleted" and out["reached"] is True
    assert out["dms_reports"] == GONE
    _no_pending(cred)


def test_teardown_ends_a_failed_create_whose_rollback_left_this_jobs_instance(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_FAILED")
    _rolled_back_rows(cred, live=("ReplicationInstance", "SecretsAccessRole"))
    _dms_tags(cred, arns=(INSTANCE,))  # the instance still exists, and carries this job's tag
    _deleted(cred)
    code, out = _teardown(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "deleted"
    _no_pending(cred)


def test_teardown_ends_an_empty_stack_a_declined_change_set_left(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "REVIEW_IN_PROGRESS")
    _resources(cred, resources={})
    _deleted(cred)
    code, out = _teardown(tmp_path, capsys, cred)
    assert code == 0 and out["status"] == "deleted"
    _no_pending(cred)


def test_stop_on_this_jobs_failed_create_has_no_task_to_stop(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_COMPLETE")
    _rolled_back_rows(cred)
    code, out = _run(tmp_path, "stop", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 0 and out["status"] == "no_task" and out["dms_reports"] == GONE
    _no_pending(cred)


@pytest.mark.parametrize("command", OWNED)
@pytest.mark.parametrize("status", [*FAILED_CREATE, "REVIEW_IN_PROGRESS"])
def test_a_failed_create_without_this_jobs_stack_tag_is_refused(tmp_path, capsys, command, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, status, job_tag=None)
    _refused(tmp_path, capsys, cred, command, f"its own {script.JOB_TAG} tag names this job")


@pytest.mark.passes_on_main  # another job's failed create stays refused
@pytest.mark.parametrize("command", OWNED)
def test_a_failed_create_of_another_job_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_COMPLETE", job_tag="otherjob")
    _refused(tmp_path, capsys, cred, command, "['otherjob']")


@pytest.mark.passes_on_main  # a foreign resource still refuses a failed create
@pytest.mark.parametrize("command", OWNED)
def test_a_failed_create_holding_a_resource_the_step_never_makes_is_refused(
    tmp_path, capsys, command
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_FAILED")
    _resources(cred, resources={"SubnetGroup": STEP_RESOURCES["SubnetGroup"],
                                "Cluster": "AWS::RDS::DBCluster"})  # fmt: skip
    _refused(tmp_path, capsys, cred, command, "Cluster (AWS::RDS::DBCluster)")


def test_a_failed_create_whose_leftover_instance_is_another_jobs_is_refused(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_FAILED")
    _rolled_back_rows(cred, live=("ReplicationInstance",))
    _dms_tags(cred, job_tag="otherjob", arns=(INSTANCE,))
    _refused(tmp_path, capsys, cred, "teardown", "ReplicationInstance's modernizer:job-id tag")


@pytest.mark.parametrize("status", ["ROLLBACK_COMPLETE", "CREATE_COMPLETE"])
def test_teardown_respects_termination_protection_and_never_turns_it_off(tmp_path, capsys, status):
    _job(tmp_path)
    cred = StubbedCredMgr()
    if status == "ROLLBACK_COMPLETE":
        _failed_stack(cred, status, protected=True)
        _rolled_back_rows(cred)
    else:
        _failed_stack(cred, status, outputs=OUTPUTS, protected=True)
        _resources(cred)
        _task_by_arn(cred, "stopped")
    seen = []
    for client in (cred.client("cloudformation"), cred.client("dms")):
        client.meta.events.register("before-call", lambda model, **_k: seen.append(model.name))
    code, out = _teardown(tmp_path, capsys, cred)
    assert code == 1 and "termination protection" in out["message"]
    assert "never turns it off" in out["message"]
    assert "DeleteStack" not in seen and "UpdateTerminationProtection" not in seen
    _no_pending(cred)


def test_credentials_that_expire_mid_teardown_of_a_failed_create_say_the_delete_was_sent(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_COMPLETE")
    _rolled_back_rows(cred)
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    cred.stub("cloudformation").add_client_error("describe_stacks", "ExpiredToken", "expired")
    code, out = _teardown(tmp_path, capsys, cred)
    assert code == 1 and out["kind"] == "credentials"
    assert "this run already called DeleteStack" in out["message"]
    _no_pending(cred)


# A change set just executed: the stack may still read REVIEW_IN_PROGRESS (#2063 review)


def _change_sets(cred, *statuses):
    cred.stub("cloudformation").add_response("list_change_sets", {"Summaries": [
        {"ChangeSetName": f"cs{i}", "ExecutionStatus": s, "Status": "CREATE_COMPLETE"}
        for i, s in enumerate(statuses)]}, {"StackName": STACK_ID})  # fmt: skip


def _named_change_set(cred, execution):
    cred.stub("cloudformation").add_response("describe_change_set", {
        "ChangeSetName": "move-step-1-x", "Status": "CREATE_COMPLETE", "ExecutionStatus": execution},
        {"StackName": STACK_ID, "ChangeSetName": "move-step-1-x"})  # fmt: skip


@pytest.mark.parametrize("execution", ["AVAILABLE", "EXECUTE_IN_PROGRESS"])
def test_the_stack_wait_after_execute_keeps_waiting_while_the_create_starts(execution):
    cred = StubbedCredMgr()
    for _ in range(3):
        _failed_stack(cred, "REVIEW_IN_PROGRESS")
        _named_change_set(cred, execution)
    ticks = iter([0, 0, 30, 60])
    out = script.wait_status(_clients(cred), STACK, "stack", 60, 30, sleep=lambda _s: None,
                             clock=functools.partial(next, ticks), change_set="move-step-1-x")  # fmt: skip
    assert out["phase"] == "stack_in_progress" and out["reached"] is False
    _no_pending(cred)  # the executed change set was read on every poll


def test_the_cli_stack_wait_reads_the_change_set_it_is_given(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "REVIEW_IN_PROGRESS")
    _named_change_set(cred, "EXECUTE_IN_PROGRESS")
    code, out = _run(tmp_path, "status", "--until", "stack", "--change-set", "move-step-1-x",
                     "--wait-seconds", "0", clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["phase"] == "stack_in_progress"
    _no_pending(cred)


def test_the_stack_wait_after_execute_ends_once_the_change_set_failed_to_execute(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "REVIEW_IN_PROGRESS")
    _named_change_set(cred, "EXECUTE_FAILED")
    code, out = _run(tmp_path, "status", "--until", "stack", "--change-set", "move-step-1-x",
                     "--wait-seconds", "60", clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["phase"] == "stack_in_review" and out["reached"] is True
    _no_pending(cred)


def test_without_a_change_set_name_any_change_set_executing_keeps_the_wait():
    cred = StubbedCredMgr()
    _failed_stack(cred, "REVIEW_IN_PROGRESS")
    _change_sets(cred, "AVAILABLE", "EXECUTE_IN_PROGRESS")
    assert script.status(_clients(cred), STACK)["phase"] == "stack_in_progress"
    _no_pending(cred)


def test_a_declined_change_set_reads_stack_in_review_at_once(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "REVIEW_IN_PROGRESS")
    _change_sets(cred, "AVAILABLE")
    code, out = _run(tmp_path, "status", "--until", "stack", "--wait-seconds", "540",
                     clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["phase"] == "stack_in_review" and out["reached"] is True
    _no_pending(cred)


# A delete that failed after a failed create (#2063 review)


def _history(cred, *, completed: bool, pages=1):
    events = [_cfn_event(STACK, "DELETE_FAILED", "The following resource(s) failed to delete: "
                         "[ReplicationInstance].", seconds=90),
              _cfn_event("ReplicationInstance", "DELETE_FAILED", "still in use", seconds=80),
              _cfn_event(STACK, "DELETE_IN_PROGRESS", "User Initiated", seconds=70)]  # fmt: skip
    if completed:
        events += [_cfn_event(STACK, "CREATE_COMPLETE", seconds=20)]
    else:
        events += [_cfn_event(STACK, "ROLLBACK_FAILED", seconds=60),
                   _cfn_event("ReplicationInstance", "CREATE_FAILED", CAPACITY, seconds=20)]  # fmt: skip
    events += [_cfn_event(STACK, "CREATE_IN_PROGRESS", "User Initiated", seconds=0)]
    _events(cred, *events, pages=pages)


def test_a_failed_create_whose_delete_failed_can_be_ended_again(tmp_path, capsys):
    """ROLLBACK_FAILED, teardown, DELETE_FAILED, then a second teardown deletes it."""
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "ROLLBACK_FAILED")
    _rolled_back_rows(cred, live=("ReplicationInstance",))
    _dms_tags(cred, arns=(INSTANCE,))
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(
        cred, "DELETE_FAILED", "The following resource(s) failed to delete: [ReplicationInstance]."
    )
    code, first = _teardown(tmp_path, capsys, cred)
    assert code == 0 and first["status"] == "delete_failed"
    _no_pending(cred)

    _failed_stack(cred, "DELETE_FAILED")
    _rolled_back_rows(cred, live=("ReplicationInstance",))
    _history(cred, completed=False, pages=2)
    _dms_tags(cred, arns=(INSTANCE,))
    _deleted(cred)
    code, second = _teardown(tmp_path, capsys, cred)
    assert code == 0 and second["status"] == "deleted" and second["reached"] is True
    _no_pending(cred)
    # The DELETE_FAILED way on points at a step that runs: ending the step again.
    from src.tools.data_movement.stack_states import stack_action

    assert "end the step again" in stack_action("DELETE_FAILED", STACK, protected=False)


@pytest.mark.parametrize("command", OWNED)
def test_a_delete_failed_stack_whose_create_completed_must_still_hold_all_six(
    tmp_path, capsys, command
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "DELETE_FAILED")
    _rolled_back_rows(cred, live=("ReplicationInstance",))
    _history(cred, completed=True)
    _refused(tmp_path, capsys, cred, command, "missing: ReplicationTask")


def test_a_delete_failed_failed_create_without_this_jobs_tag_is_refused(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "DELETE_FAILED", job_tag=None)
    _rolled_back_rows(cred, live=("ReplicationInstance",))
    _history(cred, completed=False)
    _refused(tmp_path, capsys, cred, "teardown", f"its own {script.JOB_TAG} tag names this job")


def test_a_delete_failed_stack_with_a_history_too_long_to_read_counts_as_created(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(script, "MAX_EVENT_PAGES", 2)
    _job(tmp_path)
    cred = StubbedCredMgr()
    _failed_stack(cred, "DELETE_FAILED")
    _rolled_back_rows(cred, live=("ReplicationInstance",))
    _events(cred, *(_cfn_event("SubnetGroup", "DELETE_FAILED", "x", seconds=i) for i in range(3)),
            pages=3)  # fmt: skip
    code, out = _run(tmp_path, "teardown", "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "missing: ReplicationTask" in out["message"]
    # the third page was never read
    with pytest.raises(AssertionError):
        cred.stub("cloudformation").assert_no_pending_responses()
