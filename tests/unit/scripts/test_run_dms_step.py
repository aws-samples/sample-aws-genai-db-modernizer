# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py: /move's step 1 helper, one JSON line per call (#1886).

Every AWS call goes to a botocore Stubber: nothing is sent."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest
from botocore.stub import ANY

from scripts import check_dms_step_gate, check_dms_step_preflight
from scripts import run_dms_step as script
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_step import CUSTOMER_RESPONSIBILITY, bundle_inputs, render_files
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.aws.aws_stubs import REGION, StubbedCredMgr
from tests.unit.tools.data_movement import preflight_stubs as s
from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle

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
    store = LocalArtifactStore(str(tmp_path))
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
    return code, json.loads(capsys.readouterr().out)


def _clients(cred: StubbedCredMgr) -> script.MoveClients:
    return script.MoveClients(cloudformation=cred.client("cloudformation"), dms=cred.client("dms"))


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
    assert out["monthly_cost_usd"] == b.plan.monthly_cost_usd
    assert set(out["files"]) == {"aurora-target.yaml", "step-1.ddl.sql", "dms-step-1.yaml"}
    assert out["files_missing"] == []


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


def test_only_cloudformation_and_dms_clients_exist_so_no_secret_is_read() -> None:
    assert [f.name for f in dataclasses.fields(script.MoveClients)] == ["cloudformation", "dms"]
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


def test_status_polls_until_the_full_load_is_done_and_validation_caught_up(tmp_path, capsys):
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
    code, out = _run(tmp_path, "status", "--until", "caught_up", "--wait-seconds", "60",
                     "--interval-seconds", "30", clients=_clients(cred), capsys=capsys)  # fmt: skip
    assert code == 0 and out["reached"] is True and out["phase"] == "caught_up"
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
    assert code == 0 and out == {"status": "deleted", "stack_name": STACK, "already_deleted": True}
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


@pytest.mark.parametrize("command", ["stop", "teardown"])
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


# Each stack below is refused by stop and by teardown, before any write.


def _refused(tmp_path, capsys, cred, command, *needles):
    code, out = _run(tmp_path, command, "--approve", clients=_clients(cred), capsys=capsys)
    assert code == 1 and "not this job's step 1 DMS stack" in out["message"], out
    for needle in needles:
        assert needle in out["message"], (needle, out["message"])
    _no_pending(cred)  # no stop_replication_task or delete_stack was queued or made


@pytest.mark.parametrize("command", ["stop", "teardown"])
def test_an_empty_stack_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, status="ROLLBACK_COMPLETE", outputs={})
    _resources(cred, resources={})
    _refused(tmp_path, capsys, cred, command, "missing: ReplicationInstance")


@pytest.mark.parametrize("command", ["stop", "teardown"])
def test_a_stack_holding_only_an_iam_role_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, outputs={})
    _resources(cred, resources={"SecretsAccessRole": "AWS::IAM::Role"})  # pragma: allowlist secret
    _refused(tmp_path, capsys, cred, command, "missing: ReplicationInstance")


@pytest.mark.parametrize("command", ["stop", "teardown"])
def test_another_jobs_stopped_step_stack_is_refused(tmp_path, capsys, command):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped", identifier="dm-otherjob-step-1", tags=False)
    _refused(tmp_path, capsys, cred, command, "'dm-otherjob-step-1'", "'dm-job1-step-1'")


@pytest.mark.parametrize("job_tag", ["otherjob", None])
@pytest.mark.parametrize("command", ["stop", "teardown"])
def test_a_task_whose_job_tag_does_not_name_this_job_is_refused(tmp_path, capsys, command, job_tag):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred)
    _resources(cred)
    _task_by_arn(cred, "stopped", job_tag=job_tag)
    _refused(tmp_path, capsys, cred, command, "modernizer:job-id")


@pytest.mark.parametrize("command", ["stop", "teardown"])
def test_the_customers_aurora_stack_is_refused(tmp_path, capsys, command):
    _job(tmp_path, stack_name="my-aurora")
    cred = StubbedCredMgr()
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [{"StackName": "my-aurora",
        "CreationTime": "2026-11-04T00:00:00Z", "StackStatus": "CREATE_COMPLETE"}]},
        {"StackName": "my-aurora"})  # fmt: skip
    _resources(cred, resources={"Cluster": "AWS::RDS::DBCluster"}, name="my-aurora")
    _refused(tmp_path, capsys, cred, command, "Cluster (AWS::RDS::DBCluster)")


@pytest.mark.parametrize("command", ["stop", "teardown"])
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


@pytest.mark.parametrize("command", ["stop", "teardown"])
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


@pytest.mark.parametrize("command", ["stop", "teardown"])
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


@pytest.mark.parametrize("command", ["stop", "teardown"])
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


@pytest.mark.parametrize("command", ["stop", "teardown"])
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
