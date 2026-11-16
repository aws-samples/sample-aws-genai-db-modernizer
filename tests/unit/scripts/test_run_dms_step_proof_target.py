# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py proof-target-*: the routine-proof test cluster (#1946).

Plan, deploy, status and teardown of a throwaway Aurora Serverless v2 stack, at the planned
target's engine and version line. Every AWS call goes to a botocore Stubber: nothing is sent."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import ANY

from scripts import run_dms_step as script
from tests.unit.tools.aws.aws_stubs import REGION, StubbedCredMgr
from tests.unit.tools.data_movement.dms_step_fixtures import write_job, write_pg_job

REPO_ROOT = Path(__file__).resolve().parents[3]
STACK = "dm-job1-routine-proof"
STACK_ID = f"arn:aws:cloudformation:{REGION}:111122223333:stack/{STACK}/1"
CLUSTER = "dm-job1-routine-proof-cluster-abc"
COMMANDS = ("proof-target-plan", "proof-target-deploy", "proof-target-status",
            "proof-target-teardown")  # fmt: skip
WRITES = ("proof-target-deploy", "proof-target-teardown")
INPUTS = {"region": REGION, "vpc_id": "vpc-0abc", "subnet_ids": ["subnet-0aa", "subnet-0bb"],
          "read_profile": "team-ReadOnly", "write_profile": "team-Admin"}  # fmt: skip
THIS_JOB = [{"Key": "modernizer:job-id", "Value": "job1"},
            {"Key": "modernizer:purpose", "Value": "routine-proof"}]  # fmt: skip
RESOURCES = {"SubnetGroup": "AWS::RDS::DBSubnetGroup", "SecurityGroup": "AWS::EC2::SecurityGroup",
             "Cluster": "AWS::RDS::DBCluster", "Writer": "AWS::RDS::DBInstance"}  # fmt: skip
MYSQL_VERSIONS = [
    ("5.7.mysql_aurora.2.12.0", None),
    ("8.0.mysql_aurora.3.04.0", 0.5),
    ("8.0.mysql_aurora.3.06.0", 0.5),
    ("8.0.mysql_aurora.3.08.0", 0.0),
    ("8.0.mysql_aurora.3.10.0", 0.0),
    ("8.4.mysql_aurora.8.4.10", 0.0),
]


@pytest.fixture(autouse=True)
def _restore_aws_profile(monkeypatch):
    """The commands set AWS_PROFILE from the inputs file: put the caller's back afterwards."""
    monkeypatch.setenv("AWS_PROFILE", "unset-by-test")
    monkeypatch.delenv("AWS_PROFILE")


@pytest.fixture
def cred(monkeypatch) -> StubbedCredMgr:
    """The one credential manager main() builds: every client it asks for is stubbed."""
    stubbed = StubbedCredMgr()
    for service in ("cloudformation", "rds"):  # made now: main() sets AWS_PROFILE before asking
        stubbed.stub(service)
    regions: list[str] = []

    def make(region: str | None = None, **_kw: Any) -> StubbedCredMgr:
        regions.append(region or "")
        return stubbed

    monkeypatch.setattr("src.tools.aws.credentials.AWSCredentialManager", make)
    stubbed.regions = regions  # type: ignore[attr-defined]
    return stubbed


def _job(tmp_path: Path, engine: str = "mysql", **inputs: Any) -> Path:
    (write_pg_job if engine == "postgresql" else write_job)(tmp_path)
    path = tmp_path / "shop" / "job1" / "data-movement" / "routine-proof" / "inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**INPUTS, **inputs}))
    return path


def _run(root: Path, command: str, *extra: str, capsys) -> tuple[int, dict[str, Any]]:
    try:
        code = script.main([command, "--job-id", "job1", "--db", "shop", "--artifact-root",
                            str(root), *extra], sleep=lambda _s: None)  # fmt: skip
    except SystemExit as exc:  # the parser refuses a command it does not know
        code = int(exc.code or 0)
    lines = capsys.readouterr().out.splitlines()
    return code, (json.loads(lines[-1]) if lines else {})


def _no_pending(cred: StubbedCredMgr) -> None:
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()


def _versions(cred, engine="aurora-mysql", versions=MYSQL_VERSIONS):
    rows = []
    for version, min_capacity in versions:
        row: dict[str, Any] = {"Engine": engine, "EngineVersion": version, "Status": "available",
                               "SupportedEngineModes": ["provisioned"]}  # fmt: skip
        if min_capacity is not None:
            row["ServerlessV2FeaturesSupport"] = {"MinCapacity": min_capacity, "MaxCapacity": 256.0}
        rows.append(row)
    cred.stub("rds").add_response("describe_db_engine_versions", {"DBEngineVersions": rows},
                                  {"Engine": engine})  # fmt: skip


def _stack(cred, status="CREATE_COMPLETE", tags=THIS_JOB, protected=False, by=STACK):
    stack = {"StackName": STACK, "StackId": STACK_ID, "CreationTime": "2026-11-14T00:00:00Z",
             "StackStatus": status, "Tags": tags, "EnableTerminationProtection": protected,
             "Outputs": [{"OutputKey": "ClusterIdentifier", "OutputValue": CLUSTER}]}  # fmt: skip
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [stack]},
                                             {"StackName": by})  # fmt: skip


def _no_stack(cred):
    cred.stub("cloudformation").add_client_error(
        "describe_stacks", "ValidationError", f"Stack with id {STACK} does not exist"
    )


def _resources(cred, resources=RESOURCES, cluster_status="CREATE_COMPLETE"):
    rows = [{"LogicalResourceId": k, "ResourceType": v,
             "PhysicalResourceId": CLUSTER if k == "Cluster" else f"{k.lower()}-1",
             "ResourceStatus": cluster_status if k == "Cluster" else "CREATE_COMPLETE",
             "LastUpdatedTimestamp": "2026-11-14T00:00:00Z"} for k, v in resources.items()]  # fmt: skip
    cred.stub("cloudformation").add_response("list_stack_resources",
                                             {"StackResourceSummaries": rows}, {"StackName": STACK})  # fmt: skip


def _cluster(cred, tags=THIS_JOB, **extra):
    cluster = {"DBClusterIdentifier": CLUSTER, "Status": "available",
               "Engine": "aurora-mysql", "EngineVersion": "8.0.mysql_aurora.3.10.0",
               "HttpEndpointEnabled": True, "StorageEncrypted": True,
               "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.0, "MaxCapacity": 2.0},
               "MasterUserSecret": {"SecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:rds-x"},  # pragma: allowlist secret
               "DBClusterArn": f"arn:aws:rds:{REGION}:111122223333:cluster:{CLUSTER}",
               "TagList": tags, **extra}  # fmt: skip
    cred.stub("rds").add_response("describe_db_clusters", {"DBClusters": [cluster]},
                                  {"DBClusterIdentifier": CLUSTER})  # fmt: skip


def _no_cluster(cred):
    cred.stub("rds").add_client_error("describe_db_clusters", "DBClusterNotFoundFault", "gone")


def _poll(cred, status, reason=None):
    stack = {"StackName": STACK, "StackId": STACK_ID, "CreationTime": "2026-11-14T00:00:00Z",
             "StackStatus": status, **({"StackStatusReason": reason} if reason else {})}  # fmt: skip
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [stack]},
                                             {"StackName": STACK_ID})  # fmt: skip


# --- Refusals before any AWS call ------------------------------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_every_proof_target_command_is_refused_under_the_ci_sandbox(
    tmp_path, capsys, monkeypatch, cred, command
):
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    code, out = _run(REPO_ROOT / "artifacts", command, "--approve", capsys=capsys)
    assert code == 1 and "refused under MODERNIZER_CI_SANDBOX=1" in out.get("message", "")
    assert cred.asked_for == []


@pytest.mark.parametrize("command", COMMANDS)
def test_every_proof_target_command_is_refused_under_auto(tmp_path, capsys, cred, command):
    _job(tmp_path)
    code, out = _run(tmp_path, command, "--approve", "--auto", capsys=capsys)
    assert code == 1 and "never runs under --auto" in out.get("message", "")
    assert cred.asked_for == []


@pytest.mark.parametrize("command", WRITES)
def test_the_writes_are_refused_without_approve_and_the_question_names_stack_and_region(
    tmp_path, capsys, cred, command
):
    _job(tmp_path)
    code, out = _run(tmp_path, command, capsys=capsys)
    message = out.get("message", "")
    assert code == 1 and message.startswith(f"{command} is an AWS write: pass --approve")
    assert STACK in message and REGION in message
    assert cred.asked_for == []


def test_an_inputs_file_with_one_subnet_or_a_bad_id_is_refused(tmp_path, capsys, cred):
    _job(tmp_path, subnet_ids=["subnet-0aa"])
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 1 and "subnet_ids" in out.get("message", "")
    _job(tmp_path, vpc_id="sg-0abc")
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 1 and "vpc_id" in out.get("message", "")
    assert cred.asked_for == []


@pytest.mark.parametrize("region", ["il-central-1", "cn-north-1", "ap-newregion-9"])
def test_a_region_without_the_data_api_is_refused_before_any_aws_call(
    tmp_path, capsys, cred, region
):
    """Only the regions the Data API page lists pass: a region added later is refused until
    it is listed, never assumed to have the Data API."""
    _job(tmp_path, region=region)
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 1 and "Data API" in out.get("message", "")
    assert cred.asked_for == []


# --- Plan: the engine and version line of the planned target ---------------------------------


def test_plan_picks_the_newest_version_of_the_targets_line_with_auto_pause(tmp_path, capsys, cred):
    _job(tmp_path)
    _versions(cred)
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 0, out
    assert out["status"] == "plan" and out["stack_name"] == STACK and out["region"] == REGION
    assert out["engine"] == "aurora_mysql" and out["version_line"] == "8.0"
    assert out["engine_version"] == "8.0.mysql_aurora.3.10.0"  # never the 8.4 line
    assert out["min_capacity"] == 0 and out["auto_pause"] is True
    assert out["parameters"] == {"JobId": "job1", "Engine": "aurora-mysql",
                                 "EngineVersion": "8.0.mysql_aurora.3.10.0", "MinCapacity": "0",
                                 "MaxCapacity": "2", "VpcId": "vpc-0abc",
                                 "SubnetIds": "subnet-0aa,subnet-0bb",
                                 "ParameterGroupFamily": "aurora-mysql8.0"}  # fmt: skip
    assert STACK in out["question"] and REGION in out["question"]
    assert STACK in out["teardown_question"] and "no final snapshot" in out["teardown_question"]
    assert cred.asked_for == ["rds"]  # read-only: no CloudFormation call at all
    _no_pending(cred)


def test_aurora_mysql_in_govcloud_is_refused_and_postgresql_is_not(tmp_path, capsys, cred):
    _job(tmp_path, region="us-gov-west-1")
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 1 and "Data API" in out["message"]
    pg = tmp_path / "pg"
    _job(pg, engine="postgresql", region="us-gov-west-1")
    _versions(cred, "aurora-postgresql", [("16.15", 0.0)])
    code, out = _run(pg, "proof-target-plan", capsys=capsys)
    assert code == 0 and out["engine_version"] == "16.15", out


def test_auto_says_it_is_read_only_by_the_proof_target_commands() -> None:
    action = next(a for a in script._parser()._actions if "--auto" in a.option_strings)
    assert "proof-target" in (action.help or "") and "only" in (action.help or "")


def test_a_line_without_auto_pause_runs_at_half_an_acu(tmp_path, capsys, cred):
    _job(tmp_path, engine="postgresql")
    _versions(cred, "aurora-postgresql", [("16.1", 0.5), ("16.2", 0.5), ("16.2-limitless", 0.0),
                                          ("17.4", 0.0)])  # fmt: skip
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 0, out
    assert out["engine"] == "aurora_postgresql" and out["version_line"] == "16"
    assert out["engine_version"] == "16.2" and out["min_capacity"] == 0.5
    assert out["auto_pause"] is False and out["parameters"]["MinCapacity"] == "0.5"


def test_a_version_below_the_data_api_minimum_is_never_picked(tmp_path, capsys, cred):
    _job(tmp_path)
    _versions(cred, versions=[("8.0.mysql_aurora.3.06.0", 0.5), ("8.4.mysql_aurora.8.4.10", 0.0)])
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 1 and "Data API" in out["message"] and "8.0" in out["message"]


def test_plan_without_a_step_plan_names_what_to_run(tmp_path, capsys, cred):
    path = tmp_path / "shop" / "job1" / "data-movement" / "routine-proof" / "inputs.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(INPUTS))
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 1 and "step 1 plan" in out.get("message", "")
    assert cred.asked_for == []


# --- Deploy ---------------------------------------------------------------------------------


def test_deploy_creates_the_tagged_stack_and_waits_until_it_is_created(tmp_path, capsys, cred):
    _job(tmp_path)
    _no_stack(cred)
    _versions(cred)
    cred.stub("cloudformation").add_response("create_stack", {"StackId": STACK_ID}, {
        "StackName": STACK, "TemplateBody": ANY, "OnFailure": "ROLLBACK",
        "EnableTerminationProtection": False,
        "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in {
            "JobId": "job1", "Engine": "aurora-mysql", "EngineVersion": "8.0.mysql_aurora.3.10.0",
            "MinCapacity": "0", "MaxCapacity": "2", "VpcId": "vpc-0abc",
            "SubnetIds": "subnet-0aa,subnet-0bb", "ParameterGroupFamily": "aurora-mysql8.0"}.items()],
        "Tags": THIS_JOB})  # fmt: skip
    _poll(cred, "CREATE_IN_PROGRESS")
    _poll(cred, "CREATE_COMPLETE")
    code, out = _run(tmp_path, "proof-target-deploy", "--approve", capsys=capsys)
    assert code == 0, out
    assert out["status"] == "deployed" and out["stack_id"] == STACK_ID and out["reached"] is True
    assert out["waited_seconds"] == 60
    assert os.environ["AWS_PROFILE"] == "team-Admin"  # the write profile
    _no_pending(cred)


def test_the_template_deploy_sends_is_the_committed_one(tmp_path, capsys, cred):
    _job(tmp_path)
    _no_stack(cred)
    _versions(cred)
    sent: list[str] = []
    cred.stub("cloudformation")
    cred.clients["cloudformation"].meta.events.register(
        "provide-client-params.cloudformation.CreateStack",
        lambda params, **_kw: sent.append(params["TemplateBody"]))  # fmt: skip
    cred.stub("cloudformation").add_response("create_stack", {"StackId": STACK_ID})
    _poll(cred, "CREATE_COMPLETE")
    _run(tmp_path, "proof-target-deploy", "--approve", "--wait-seconds", "30", capsys=capsys)
    committed = (REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement"
                 / "routine-proof-target.yaml").read_text()  # fmt: skip
    assert sent == [committed]


def test_deploy_of_this_jobs_stack_already_created_is_a_no_op(tmp_path, capsys, cred):
    """Kept scaled to zero between re-tests: a second deploy writes nothing."""
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred)
    code, out = _run(tmp_path, "proof-target-deploy", "--approve", capsys=capsys)
    assert code == 0 and out["status"] == "already_deployed", out
    _no_pending(cred)


@pytest.mark.parametrize("status", ["ROLLBACK_COMPLETE", "DELETE_FAILED", "UPDATE_ROLLBACK_FAILED"])
def test_deploy_on_a_failed_stack_says_tear_it_down_first(tmp_path, capsys, cred, status):
    _job(tmp_path)
    _stack(cred, status=status)
    _resources(cred, {"SubnetGroup": RESOURCES["SubnetGroup"]})
    code, out = _run(tmp_path, "proof-target-deploy", "--approve", capsys=capsys)
    assert code == 1 and "proof-target-teardown" in out["message"] and status in out["message"]
    _no_pending(cred)


def test_deploy_refuses_a_stack_of_that_name_tagged_for_another_job(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred, tags=[{"Key": "modernizer:job-id", "Value": "job2"},
                       {"Key": "modernizer:purpose", "Value": "routine-proof"}])  # fmt: skip
    code, out = _run(tmp_path, "proof-target-deploy", "--approve", capsys=capsys)
    assert code == 1 and "job2" in out["message"] and "not this job's" in out["message"]
    _no_pending(cred)


# --- Status (read-only) ---------------------------------------------------------------------


def test_status_reads_the_cluster_and_uses_the_read_profile(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred)
    code, out = _run(tmp_path, "proof-target-status", capsys=capsys)
    assert code == 0, out
    assert out["status"] == "deployed" and out["stack_status"] == "CREATE_COMPLETE"
    assert out["cluster"]["identifier"] == CLUSTER and out["cluster"]["data_api"] is True
    assert out["cluster"]["min_capacity"] == 0.0 and out["cluster"]["status"] == "available"
    assert out["cluster"]["master_user_secret_arn"].startswith("arn:aws:secretsmanager:")
    assert STACK in out["teardown_question"] and REGION in out["teardown_question"]
    assert os.environ["AWS_PROFILE"] == "team-ReadOnly"
    _no_pending(cred)


def test_status_of_no_stack_is_not_deployed(tmp_path, capsys, cred):
    _job(tmp_path)
    _no_stack(cred)
    code, out = _run(tmp_path, "proof-target-status", capsys=capsys)
    assert code == 0 and out["status"] == "not_deployed"


def test_status_refuses_a_stack_that_is_not_the_proof_stack(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred, tags=[{"Key": "modernizer:job-id", "Value": "job1"},
                       {"Key": "modernizer:step", "Value": "1"}])  # fmt: skip
    code, out = _run(tmp_path, "proof-target-status", capsys=capsys)
    assert code == 1 and "modernizer:purpose" in out["message"]


# --- Teardown -------------------------------------------------------------------------------


def test_teardown_deletes_this_jobs_stack_by_its_id_and_waits(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred)
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_IN_PROGRESS")
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 0, out
    assert out["status"] == "deleted" and out["already_deleted"] is False and out["reached"]
    assert out["stack_id"] == STACK_ID and "no final snapshot" in out["deleted"]
    assert os.environ["AWS_PROFILE"] == "team-Admin"
    _no_pending(cred)


def test_teardown_of_a_stack_that_is_gone_is_a_no_op(tmp_path, capsys, cred):
    _job(tmp_path)
    _no_stack(cred)
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 0 and out["status"] == "deleted" and out["already_deleted"] is True
    _no_pending(cred)


@pytest.mark.parametrize("tags", [
    [],  # no tag: never assumed to be this job's
    [{"Key": "modernizer:job-id", "Value": "job2"}, {"Key": "modernizer:purpose", "Value": "routine-proof"}],
    [{"Key": "modernizer:job-id", "Value": "job1"}],  # this job's, but not the proof stack
    [{"Key": "modernizer:job-id", "Value": "job1"}, {"Key": "modernizer:purpose", "Value": "other"}],
])  # fmt: skip
def test_teardown_refuses_a_stack_without_this_jobs_proof_tags(tmp_path, capsys, cred, tags):
    _job(tmp_path)
    _stack(cred, tags=tags)
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 1 and "not this job's routine-proof test stack" in out["message"]
    _no_pending(cred)  # no delete_stack was queued, so none could be sent


@pytest.mark.parametrize("resources", [
    {**RESOURCES, "Extra": "AWS::S3::Bucket"},
    {k: v for k, v in RESOURCES.items() if k != "Writer"},
    {"ReplicationTask": "AWS::DMS::ReplicationTask", **RESOURCES},
    {**RESOURCES, "Cluster": "AWS::RDS::DBInstance"},
])  # fmt: skip
def test_teardown_refuses_a_stack_whose_resources_are_not_the_templates(
    tmp_path, capsys, cred, resources
):
    _job(tmp_path)
    _stack(cred)
    _resources(cred, resources)
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 1 and "routine-proof-target.yaml" in out["message"]
    _no_pending(cred)


def test_teardown_refuses_when_the_clusters_tag_names_another_job(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred, tags=[{"Key": "modernizer:job-id", "Value": "job2"}])
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 1 and "Cluster" in out["message"] and "job2" in out["message"]
    _no_pending(cred)


def test_teardown_ends_this_jobs_failed_create_that_holds_fewer_resources(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred, status="ROLLBACK_COMPLETE")
    _resources(cred, {"SubnetGroup": RESOURCES["SubnetGroup"], "Cluster": RESOURCES["Cluster"]},
               cluster_status="DELETE_COMPLETE")  # fmt: skip
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 0 and out["status"] == "deleted", out
    _no_pending(cred)


def test_a_complete_stack_with_a_gone_cluster_still_needs_the_stack_tags(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _no_cluster(cred)
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 0 and out["status"] == "deleted", out
    _no_pending(cred)


@pytest.mark.parametrize(
    "status", ["CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_IN_PROGRESS"]
)
def test_teardown_refuses_a_stack_cloudformation_is_still_changing(tmp_path, capsys, cred, status):
    _job(tmp_path)
    _stack(cred, status=status)
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 1 and status in out["message"] and "proof-target-status" in out["message"]
    _no_pending(cred)  # no delete_stack was queued, so none could be sent


def test_teardown_respects_termination_protection_and_never_turns_it_off(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred, protected=True)
    _resources(cred)
    _cluster(cred)
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 1 and "termination protection" in out["message"]
    _no_pending(cred)


def test_teardown_of_a_stack_already_deleting_waits_without_deleting_again(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred, status="DELETE_IN_PROGRESS")
    _resources(cred)
    _cluster(cred)
    _poll(cred, "DELETE_COMPLETE")
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 0 and out["status"] == "deleted", out
    _no_pending(cred)


def test_a_delete_that_fails_is_reported_with_cloudformations_reason(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred)
    cred.stub("cloudformation").add_response("delete_stack", {}, {"StackName": STACK_ID})
    _poll(cred, "DELETE_FAILED", reason="The following resource(s) failed to delete: [Cluster].")
    code, out = _run(tmp_path, "proof-target-teardown", "--approve", capsys=capsys)
    assert code == 0 and out["status"] == "delete_failed" and "[Cluster]" in out["stack_reason"]
    _no_pending(cred)


def test_the_commands_make_no_call_but_cloudformation_and_rds(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred)
    _run(tmp_path, "proof-target-status", capsys=capsys)
    assert set(cred.asked_for) == {"cloudformation", "rds"}
    source = REPO_ROOT / "src" / "tools" / "data_movement" / "proof_target.py"
    assert source.is_file() and "get_secret_value" not in source.read_text()


def _readme_section() -> str:
    text = (
        REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement" / "README.md"
    ).read_text()
    start = text.index("### Routine proof test cluster")
    return text[start : text.index("\n### ", start + 1)]


def _called_actions() -> set[str]:
    """``service:Action`` for every CloudFormation and RDS call proof_target.py makes."""
    import re

    source = (REPO_ROOT / "src" / "tools" / "data_movement" / "proof_target.py").read_text()
    calls = set(
        re.findall(r"\b(cloudformation|rds)\.(?:get_paginator\(\"(\w+)\"\)|(\w+)\()", source)
    )
    actions = {
        f"{svc}:{''.join(w.title() for w in (pag or op).split('_'))}" for svc, pag, op in calls
    }
    return {a.replace("Db", "DB") for a in actions}


def test_the_readme_lists_every_aws_call_the_commands_make() -> None:
    """The least-privilege sets name each call the code makes, with read-only calls in the
    read set (plan and status) and the writes only in the write set."""
    section = _readme_section()
    called = _called_actions()
    assert {"cloudformation:CreateStack", "cloudformation:DeleteStack",
            "rds:DescribeDBEngineVersions", "rds:DescribeDBClusters"} <= called  # fmt: skip
    read = section[section.index("**Read set**") : section.index("**Write set**")]
    write = section[section.index("**Write set**") :]
    for action in called:
        assert f"`{action}`" in write, action
        reads = action.split(":")[1].startswith(("Describe", "List"))
        assert (f"`{action}`" in read) == reads, action
    # What CloudFormation does as the caller for the template's resources, and no IAM role.
    for action in ("rds:CreateDBCluster", "rds:CreateDBInstance", "rds:EnableHttpEndpoint",
                   "rds:DeleteDBCluster", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
                   "secretsmanager:CreateSecret", "kms:DescribeKey",
                   "ec2:DescribeInternetGateways"):  # fmt: skip
        assert f"`{action}`" in write, action
    assert "iam:PassRole" not in section and "logs:" not in write


def test_a_mysql_stack_holds_its_parameter_group_and_still_passes_the_ownership_check():
    """#1945: an Aurora MySQL stack also holds the cluster parameter group that turns the event
    scheduler off; a PostgreSQL stack holding one is not this template's."""
    from src.tools.data_movement import proof_target as pt

    mysql = {"Parameters": [{"ParameterKey": "Engine", "ParameterValue": "aurora-mysql"}]}
    pg = {"Parameters": [{"ParameterKey": "Engine", "ParameterValue": "aurora-postgresql"}]}
    assert pt.resources_for(mysql)["ClusterParameters"] == "AWS::RDS::DBClusterParameterGroup"
    assert "ClusterParameters" not in pt.resources_for(pg)
    assert pt.parameter_group_family("aurora_mysql", "8.4.mysql_aurora.8.4.10") == "aurora-mysql8.4"
    assert pt.parameter_group_family("aurora_mysql", "8.0.mysql_aurora.3.14.0") == "aurora-mysql8.0"


# --- Aurora MySQL (#2298) -----------------------------------------------------------------


def test_the_mysql_proof_is_planned_like_the_postgresql_one(tmp_path, capsys, cred):
    """#2298: the MySQL runner user is created from a hash, so the plan no longer refuses an
    Aurora MySQL target; it reads the engine versions as it does for PostgreSQL."""
    _job(tmp_path)  # an Aurora MySQL target
    _versions(cred)
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 0 and out.get("reason_code") is None, out
    assert out["status"] == "plan" and out["engine"] == "aurora_mysql" and cred.asked_for == ["rds"]


@pytest.mark.passes_on_main  # status was never refused on MySQL
def test_a_mysql_stack_is_read(tmp_path, capsys, cred):
    _job(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred)
    code, out = _run(tmp_path, "proof-target-status", capsys=capsys)
    assert code == 0 and out["status"] == "deployed"


def test_the_mysql_proof_has_no_refusal_switch_left():
    """#2298: the switch and its refusal are gone, so nothing can turn the MySQL proof off
    again by accident."""
    from src.tools.data_movement import proof_target as pt

    assert not hasattr(pt, "MYSQL_PROOF_AVAILABLE") and not hasattr(pt, "mysql_refusal")


def test_proof_target_run_refuses_when_the_planned_target_cannot_be_read(tmp_path, capsys, cred):
    """Review round 3 of #2297: the run's MySQL refusal comes before the status describe
    calls, so a run whose planned target cannot be read is refused before any AWS call
    rather than going on to describe the stack."""
    path = tmp_path / "shop" / "job1" / "data-movement" / "routine-proof" / "inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(INPUTS))  # no step 1 plan: the planned target cannot be read
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "planned target" in out["message"], out
    assert cred.asked_for == [] and cred.regions == []
