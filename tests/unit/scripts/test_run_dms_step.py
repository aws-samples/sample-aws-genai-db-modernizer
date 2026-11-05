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
STACK = "dm-job1-step-1"
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


def _stack(cred, status="CREATE_COMPLETE", outputs=OUTPUTS):
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [{
        "StackName": STACK, "CreationTime": "2026-11-04T00:00:00Z", "StackStatus": status,
        "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}]},
        {"StackName": STACK})  # fmt: skip


def _no_stack(cred):
    cred.stub("cloudformation").add_client_error(
        "describe_stacks", "ValidationError", f"Stack with id {STACK} does not exist"
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
        "Capabilities": ["CAPABILITY_IAM"], "Parameters": ANY})  # fmt: skip
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
        "ChangeSetType": "UPDATE", "TemplateBody": ANY, "Capabilities": ["CAPABILITY_IAM"], "Parameters": ANY})  # fmt: skip
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
