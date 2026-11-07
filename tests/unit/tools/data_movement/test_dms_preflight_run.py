# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 preflight checks 4 to 8 and the run (#1799): botocore Stubbers, nothing is sent."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from botocore.exceptions import BotoCoreError

import src.tools.data_movement.dms_preflight as module
from src.contracts.dms_preflight import CHECK_ORDER
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_preflight import run_preflight, write_preflight
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

from . import preflight_stubs as s
from .dms_step_fixtures import make_bundle, make_step1_inputs

NOW = dt.datetime(2026, 11, 3, 12, tzinfo=dt.UTC)


def _run(cred, engine="aurora_mysql", confirmed=frozenset(), **request):
    return run_preflight(make_step1_inputs(engine), s.request(**request), s.clients(cred), now=NOW,
                         confirmed=confirmed)  # fmt: skip


def _row(result, check_id):
    return next(c for c in result.checks if c.id == check_id)


def _done(cred):
    for stub in cred.stubbers.values():
        stub.assert_no_pending_responses()


def test_a_ready_cluster_passes_and_a_self_managed_source_needs_confirmation():
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    result = _run(cred)
    assert [c.id for c in result.checks] == list(CHECK_ORDER)
    assert [c.status for c in result.checks] == ["pass"] * 5 + ["confirm", "pass", "pass"]
    assert result.status == "needs_confirmation" and result.instance_class == "dms.t3.medium"
    assert result.contract_sha256 == make_step1_inputs().contract_sha256
    _done(cred)


def test_a_self_managed_source_needs_confirmation():
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    row = _row(_run(cred), "source_cdc")
    assert row.status == "confirm" and "--confirm source_cdc" in (row.message or "")
    assert "--confirm-source-checklist" in (row.message or "")
    assert any("binlog_format" in item for item in row.checklist)
    assert any("importable" in item or "import into DMS" in item for item in row.checklist)
    assert any("step-1.ddl.sql" in item for item in row.checklist)
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    done = _run(cred, confirmed={"source_cdc"})
    assert done.status == "pass"
    confirmed = _row(done, "source_cdc")
    assert confirmed.status == "pass" and "confirmed by the operator" in confirmed.summary
    assert confirmed.evidence["confirmed_by"] == "the operator"


def test_a_postgresql_target_checklist_names_session_replication_role():
    cred = StubbedCredMgr()
    s.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)
    s.queue_certs(cred)
    s.queue_network(cred)
    row = _row(_run(cred, "aurora_postgresql"), "source_cdc")
    assert row.status == "confirm"
    assert any("session_replication_role" in i for i in row.checklist)
    assert any("wal_level" in i for i in row.checklist)


def test_a_confirmation_never_turns_a_fail_or_another_checks_row_into_a_pass():
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    result = _run(cred, confirmed={"aurora_cluster", "secrets"})
    assert _row(result, "source_cdc").status == "confirm" and result.status == "needs_confirmation"
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred, ["dms.r6i.large"])
    result = _run(cred, confirmed=set(CHECK_ORDER))
    assert result.status == "fail" and _row(result, "instance_class_offered").status == "fail"


def test_a_missing_cluster_fails_and_stops():
    cred = StubbedCredMgr()
    cred.stub("rds").add_client_error("describe_db_clusters", service_error_code="DBClusterNotFoundFault",
                                      http_status_code=404)  # fmt: skip
    result = _run(cred)  # nothing else is queued: any later call would raise
    assert result.status == "fail"
    assert [c.status for c in result.checks] == ["fail"] + ["not_run"] * 7
    assert "aurora-target.yaml" in (result.checks[0].message or "")


def test_a_check_that_fails_in_the_middle_leaves_the_rest_not_run():
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred, missing="dms-vpc-role")
    result = _run(cred)
    assert [c.status for c in result.checks] == ["pass"] * 4 + ["fail"] + ["not_run"] * 3
    _done(cred)


def test_an_unoffered_instance_class_fails():
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred, ["dms.r6i.large"])
    row = _row(_run(cred), "instance_class_offered")
    assert (
        row.status == "fail"
        and "dms.t3.medium" in row.summary
        and "--instance-class" in (row.message or "")
    )


def test_an_instance_class_override_is_the_one_checked():
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    result = _run(cred, instance_class="dms.r6i.large")
    assert (
        result.instance_class == "dms.r6i.large"
        and _row(result, "instance_class_offered").status == "pass"
    )
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred, ["dms.r6i.large"])
    assert _row(_run(cred), "instance_class_offered").status == "fail"  # the default is not offered


def test_a_missing_dms_service_role_fails():
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred, missing="dms-vpc-role")
    row = _row(_run(cred), "dms_service_roles")
    assert (
        row.status == "fail"
        and "dms-vpc-role" in row.summary
        and "AmazonDMSVPCManagementRole" in (row.message or "")
    )
    assert row.evidence["dms-cloudwatch-logs-role"] == "ok"


def test_an_access_denied_dms_call_fails_its_check_naming_the_permission():
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    cred.stub("dms").add_client_error("describe_orderable_replication_instances",
                                      service_error_code="AccessDeniedException", http_status_code=400)  # fmt: skip
    row = _row(_run(cred), "instance_class_offered")
    assert row.status == "fail" and "dms:DescribeOrderableReplicationInstances" in (
        row.message or ""
    )


# --- check 6 ----------------------------------------------------------------------------


def _rds_source(cred, *, retention=7, binlog="ROW", status="in-sync", image="FULL", engine="mysql"):
    rds = cred.stub("rds")
    rds.add_response("describe_db_instances", {"DBInstances": [{
        "DBInstanceIdentifier": "shop-db", "Engine": engine, "BackupRetentionPeriod": retention,
        "DBParameterGroups": [{"DBParameterGroupName": "shop-db-params", "ParameterApplyStatus": status}]}]},
        {"DBInstanceIdentifier": "shop-db"})  # fmt: skip
    rds.add_response("describe_db_parameters", s.parameters(**{k: v for k, v in {"binlog_format": binlog, "binlog_row_image": image}.items() if v}),
                     {"DBParameterGroupName": "shop-db-params"})  # fmt: skip


def _up_to_source(cred):
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)


@pytest.mark.parametrize(("kwargs", "words"), [({"retention": 0}, "backup"), ({"binlog": "MIXED"}, "binlog_format"),
                                              ({"image": "MINIMAL"}, "binlog_row_image"),
                                              ({"status": "pending-reboot"}, "Reboot the source"),
                                              ({"status": "applying"}, "wait until")])  # fmt: skip
def test_an_rds_source_without_its_cdc_prerequisites_fails(kwargs, words):
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _rds_source(cred, **kwargs)
    row = _row(_run(cred, source_kind="rds_instance", source_id="shop-db"), "source_cdc")
    assert row.status == "fail" and words in (row.message or "")


def test_a_ready_rds_source_leaves_only_what_the_api_cannot_read():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _rds_source(cred)
    s.queue_certs(cred)
    s.queue_network(cred)
    result = _run(cred, source_kind="rds_instance", source_id="shop-db")
    row = _row(result, "source_cdc")
    assert row.status == "confirm" and [i for i in row.checklist if "binlog retention hours" in i]
    assert row.evidence["backup_retention_days"] == "7"
    _done(cred)


def test_a_missing_rds_source_fails():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    cred.stub("rds").add_client_error("describe_db_instances", service_error_code="DBInstanceNotFound",
                                      http_status_code=404)  # fmt: skip
    row = _row(_run(cred, source_kind="rds_instance", source_id="shop-db"), "source_cdc")
    assert row.status == "fail" and "shop-db" in row.summary


def _aurora_source(cred, *, status="in-sync", binlog="ROW"):
    rds = cred.stub("rds")
    rds.add_response("describe_db_clusters", {"DBClusters": [{
        "DBClusterIdentifier": "src-cluster", "Engine": "aurora-mysql", "DBClusterParameterGroup": "src-params",
        "DBClusterMembers": [{"DBInstanceIdentifier": "src-1", "IsClusterWriter": True,
                              "DBClusterParameterGroupStatus": status}]}]},
        {"DBClusterIdentifier": "src-cluster"})  # fmt: skip
    rds.add_response("describe_db_cluster_parameters", s.parameters(binlog_format=binlog, binlog_row_image="FULL"),
                     {"DBClusterParameterGroupName": "src-params"})  # fmt: skip


def test_an_aurora_source_with_a_writer_pending_reboot_fails_and_a_ready_one_needs_the_checklist():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _aurora_source(cred, status="pending-reboot")
    row = _row(_run(cred, source_kind="aurora_cluster", source_id="src-cluster"), "source_cdc")
    assert row.status == "fail" and "src-1" in (row.message or "")
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _aurora_source(cred)
    s.queue_certs(cred)
    s.queue_network(cred)
    row = _row(_run(cred, source_kind="aurora_cluster", source_id="src-cluster"), "source_cdc")
    assert row.status == "confirm"


def test_a_postgresql_rds_source_needs_logical_replication_and_no_backup_retention():
    cred = StubbedCredMgr()
    s.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)
    rds = cred.stub("rds")
    rds.add_response("describe_db_instances", {"DBInstances": [{
        "DBInstanceIdentifier": "pg", "Engine": "postgres", "BackupRetentionPeriod": 0,
        "DBParameterGroups": [{"DBParameterGroupName": "pg-params", "ParameterApplyStatus": "in-sync"}]}]})  # fmt: skip
    rds.add_response("describe_db_parameters", s.parameters(**{"rds.logical_replication": "0"}))
    row = _row(
        _run(cred, "aurora_postgresql", source_kind="rds_instance", source_id="pg"), "source_cdc"
    )
    assert row.status == "fail" and "rds.logical_replication=1" in (row.message or "")


# --- check 7 ----------------------------------------------------------------------------


def _up_to_certs(cred):
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)


def test_an_expired_certificate_fails():
    cred = StubbedCredMgr()
    _up_to_certs(cred)
    s.queue_cert(cred, s.SOURCE_CERT)
    s.queue_cert(cred, s.TARGET_CERT, valid_to=dt.datetime(2026, 11, 3, 11, tzinfo=dt.UTC))
    row = _row(_run(cred, confirmed={"source_cdc"}), "dms_certificates")
    assert row.status == "fail" and "target" in row.summary and "expired" in row.summary
    assert row.evidence["target_valid_to"].startswith("2026-11-03T11")


def test_a_certificate_valid_until_after_now_passes_and_one_at_now_fails():
    cred = StubbedCredMgr()
    _up_to_certs(cred)
    s.queue_cert(cred, s.SOURCE_CERT, valid_to=NOW + dt.timedelta(seconds=1))
    s.queue_cert(cred, s.TARGET_CERT, valid_to=NOW)
    assert _row(_run(cred, confirmed={"source_cdc"}), "dms_certificates").status == "fail"


def test_an_unknown_certificate_arn_fails():
    cred = StubbedCredMgr()
    _up_to_certs(cred)
    s.queue_cert(cred, s.SOURCE_CERT, found=False)
    row = _row(_run(cred, confirmed={"source_cdc"}), "dms_certificates")
    assert (
        row.status == "fail"
        and s.SOURCE_CERT in row.summary
        and "import-certificate" in (row.message or "")
    )


def test_a_mysql_family_endpoint_without_a_certificate_arn_fails_without_a_call():
    cred = StubbedCredMgr()
    _up_to_certs(cred)
    row = _row(_run(cred, confirmed={"source_cdc"}, source_certificate_arn=""), "dms_certificates")
    assert row.status == "fail" and "--source-certificate-arn" in (row.message or "")
    _done(cred)


def test_postgresql_endpoints_pass_without_a_certificate():
    cred = StubbedCredMgr()
    s.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)
    s.queue_network(cred)
    result = _run(cred, "aurora_postgresql", confirmed={"source_cdc"}, source_certificate_arn="",
                  target_certificate_arn="")  # fmt: skip
    row = _row(result, "dms_certificates")
    assert row.status == "pass" and "require" in row.evidence["source_certificate"]
    _done(cred)


def test_a_postgresql_certificate_that_is_given_is_checked():
    cred = StubbedCredMgr()
    s.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)
    s.queue_cert(cred, s.SOURCE_CERT, found=False)
    row = _row(_run(cred, "aurora_postgresql", confirmed={"source_cdc"}, target_certificate_arn=""),
               "dms_certificates")  # fmt: skip
    assert row.status == "fail"


# --- check 8 ----------------------------------------------------------------------------


def _up_to_network(cred):
    _up_to_certs(cred)
    s.queue_certs(cred)


def _network(cred, **request):
    return _row(_run(cred, confirmed={"source_cdc"}, **request), "secrets_manager_reachable")


def test_an_interface_endpoint_with_private_dns_passes():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_network(cred)
    row = _network(cred)
    assert row.status == "pass" and "vpce-1" in row.evidence["secretsmanager_endpoints"]
    _done(cred)


@pytest.mark.parametrize("ep", [{"state": "pending"}, {"private_dns": False}])
def test_an_endpoint_that_is_not_available_or_has_no_private_dns_does_not_count(ep):
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred, [s.endpoint(**ep)])
    for subnet in s.SUBNETS:
        s.queue_routes(cred, [s.igw_table()], subnet=subnet)
    row = _network(cred)
    assert (
        row.status == "fail"
        and "interface endpoint" in (row.message or "")
        and "NAT gateway" in (row.message or "")
    )
    assert "vpce-1" in row.evidence["secretsmanager_endpoints"]


def test_a_nat_route_on_every_subnet_passes():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    for subnet in s.SUBNETS:
        s.queue_routes(cred, [s.nat_table()], subnet=subnet)
    row = _network(cred)
    assert row.status == "pass" and "NAT" in row.summary
    _done(cred)


def test_a_subnet_without_a_table_uses_the_main_route_table():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    s.queue_routes(cred, [s.nat_table()], subnet="subnet-aaa")
    s.queue_routes(cred, [], subnet="subnet-bbb")
    s.queue_routes(cred, [s.nat_table()])
    assert _network(cred).status == "pass"
    _done(cred)


def test_a_blackhole_nat_route_and_a_subnet_with_an_internet_gateway_route_fail():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    s.queue_routes(cred, [s.nat_table()], subnet="subnet-aaa")
    s.queue_routes(cred, [s.nat_table("blackhole")], subnet="subnet-bbb")
    row = _network(cred)
    assert row.status == "fail" and row.evidence["subnets_without_default_route"] == "subnet-bbb"


def test_subnets_in_two_vpcs_fail():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred, vpcs=("vpc-1", "vpc-2"))
    row = _network(cred)
    assert row.status == "fail" and "one VPC" in row.summary


def test_no_subnets_fail_and_an_endpoint_override_passes_without_ec2_calls():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    row = _network(cred, subnet_ids=())
    assert row.status == "fail" and "--subnet-ids" in (row.message or "")
    cred = StubbedCredMgr()
    _up_to_network(cred)
    row = _network(
        cred,
        secrets_manager_endpoint_override="vpce-1.secretsmanager.us-east-1.vpce.amazonaws.com",  # pragma: allowlist secret
    )
    assert row.status == "pass" and "vpce-1.secretsmanager" in row.summary
    _done(cred)


def test_an_access_denied_ec2_call_names_the_permission():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    cred.stub("ec2").add_client_error("describe_subnets", service_error_code="UnauthorizedOperation",
                                      http_status_code=403)  # fmt: skip
    row = _network(cred)
    assert row.status == "fail" and "ec2:DescribeSubnets" in (row.message or "")


# --- the run ----------------------------------------------------------------------------


def test_every_aws_call_is_a_read_and_never_a_secret_value():
    source = Path(module.__file__).read_text()
    calls = set(re.findall(r"clients\.\w+\.(\w+)\(", source)) | set(
        re.findall(r'paginator\("(\w+)"\)', source)
    )
    calls |= set(re.findall(r'_paged\(clients\.\w+, "(\w+)"', source))
    assert calls and all(c.startswith(("describe_", "get_", "list_")) for c in calls), calls
    assert "get_secret_value" not in calls


def test_the_result_is_written_under_the_step(tmp_path):
    b = make_bundle()
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/plan.json", b.plan.model_dump(mode="json"))
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    path, result = write_preflight(store, "shop", "job1", s.request(), s.clients(cred), now=NOW)
    assert path == "shop/job1/data-movement/step-1/preflight.json"
    assert store.read_json(path)["status"] == result.status == "needs_confirmation"
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    _, again = write_preflight(store, "shop", "job1", s.request(), s.clients(cred), now=NOW,
                               confirmed={"source_cdc"})  # fmt: skip
    assert store.read_json(path)["status"] == again.status == "pass"


# --- fix round 1 -------------------------------------------------------------------------


def _roles_row(**kw):
    cred = StubbedCredMgr()
    _up_to_roles(cred)
    s.queue_roles(cred, **kw)
    return _row(_run(cred), "dms_service_roles")


def _up_to_roles(cred):
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)


def test_a_role_that_dms_cannot_assume_fails():
    row = _roles_row(trust_docs={"dms-vpc-role": s.trust(service="ec2.amazonaws.com")})
    assert (
        row.status == "fail"
        and "dms-vpc-role" in row.summary
        and "(trusting dms" in (row.message or "")
    )
    row = _roles_row(trust_docs={"dms-vpc-role": s.trust(action="sts:TagSession")})
    assert row.status == "fail"
    row = _roles_row(trust_docs={"dms-vpc-role": s.trust(effect="Deny")})
    assert row.status == "fail"


def test_a_regional_dms_principal_and_a_url_encoded_trust_document_pass():
    regional = s.trust(service=["ec2.amazonaws.com", f"dms.{s.REGION}.amazonaws.com"])
    encoded = quote(json.dumps(s.trust()))
    row = _roles_row(trust_docs={"dms-vpc-role": regional, "dms-cloudwatch-logs-role": encoded})
    assert row.status == "pass"


def test_a_role_without_its_managed_policy_fails():
    row = _roles_row(policies={"dms-cloudwatch-logs-role": ["SomethingElse"]})
    assert row.status == "fail" and "AmazonDMSCloudWatchLogsRole" in row.summary + (
        row.message or ""
    )
    assert row.evidence["dms-vpc-role"] == "ok"


def test_attached_policies_are_read_across_pages():
    cred = StubbedCredMgr()
    _up_to_roles(cred)
    iam = cred.stub("iam")
    iam.add_response("get_role", s.role("dms-vpc-role"), {"RoleName": "dms-vpc-role"})
    iam.add_response("list_attached_role_policies", {"AttachedPolicies": [{"PolicyName": "Other"}], "IsTruncated": True,
                                                     "Marker": "m1"}, {"RoleName": "dms-vpc-role"})  # fmt: skip
    iam.add_response("list_attached_role_policies", {"AttachedPolicies": [{"PolicyName": "AmazonDMSVPCManagementRole"}]},
                     {"RoleName": "dms-vpc-role", "Marker": "m1"})  # fmt: skip
    iam.add_response(
        "get_role", s.role("dms-cloudwatch-logs-role"), {"RoleName": "dms-cloudwatch-logs-role"}
    )
    iam.add_response("list_attached_role_policies", {"AttachedPolicies": [{"PolicyName": "AmazonDMSCloudWatchLogsRole"}]},
                     {"RoleName": "dms-cloudwatch-logs-role"})  # fmt: skip
    assert _row(_run(cred), "dms_service_roles").status == "pass"
    _done(cred)


def test_an_unset_binlog_row_image_is_the_engine_default_but_an_unset_binlog_format_fails():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _rds_source(cred, image=None)
    s.queue_certs(cred)
    s.queue_network(cred)
    row = _row(_run(cred, source_kind="rds_instance", source_id="shop-db"), "source_cdc")
    assert row.status == "confirm" and "engine default FULL" in row.summary
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _rds_source(cred, binlog=None)
    row = _row(_run(cred, source_kind="rds_instance", source_id="shop-db"), "source_cdc")
    assert row.status == "fail" and "binlog_format" in row.summary


def test_a_managed_mysql_source_checklist_has_grants_and_tls():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _rds_source(cred)
    row = _row(_run(cred, source_kind="rds_instance", source_id="shop-db"), "source_cdc")
    assert any("step-1.ddl.sql" in i for i in row.checklist) and any(
        "certificate authority" in i for i in row.checklist
    )
    assert any("binlog retention hours" in i for i in row.checklist)


def test_an_rds_postgresql_source_returns_a_checklist_for_what_the_api_cannot_read():
    cred = StubbedCredMgr()
    s.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)
    rds = cred.stub("rds")
    rds.add_response("describe_db_instances", {"DBInstances": [{
        "DBInstanceIdentifier": "pg", "Engine": "postgres", "BackupRetentionPeriod": 0,
        "DBParameterGroups": [{"DBParameterGroupName": "pg-params", "ParameterApplyStatus": "in-sync"}]}]})  # fmt: skip
    rds.add_response("describe_db_parameters", s.parameters(**{"rds.logical_replication": "1"}))
    row = _row(
        _run(cred, "aurora_postgresql", source_kind="rds_instance", source_id="pg"), "source_cdc"
    )
    assert row.status == "confirm"
    text = " | ".join(row.checklist)
    assert (
        "rds_replication" in text and "REPLICA IDENTITY" in text and "certificate authority" in text
    )
    assert "session_replication_role" in text


HEARTBEAT_GRANT = "GRANT CREATE ON SCHEMA public TO <source user>"
HEARTBEAT_MESSAGE = (
    "On PostgreSQL 15 and later, that includes CREATE on the heartbeat schema public for the "
    "source user."
)


def _pg_source_row(source_kind: str):
    cred = StubbedCredMgr()
    s.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)
    if source_kind == "rds_instance":
        rds = cred.stub("rds")
        rds.add_response("describe_db_instances", {"DBInstances": [{
            "DBInstanceIdentifier": "pg", "Engine": "postgres", "BackupRetentionPeriod": 0,
            "DBParameterGroups": [{"DBParameterGroupName": "pg-params", "ParameterApplyStatus": "in-sync"}]}]})  # fmt: skip
        rds.add_response("describe_db_parameters", s.parameters(**{"rds.logical_replication": "1"}))
        result = _run(cred, "aurora_postgresql", source_kind="rds_instance", source_id="pg")
    else:
        s.queue_certs(cred)
        s.queue_network(cred)
        result = _run(cred, "aurora_postgresql")
    return _row(result, "source_cdc")


@pytest.mark.parametrize("source_kind", ["self_managed", "rds_instance"])
def test_a_postgresql_source_names_the_heartbeat_schemas_create_grant(source_kind):
    """The task's DMS heartbeat creates its table in public, which grants CREATE to its owner
    only from PostgreSQL 15 (#1904): the checklist and the confirm message both say so."""
    row = _pg_source_row(source_kind)
    assert row.status == "confirm"
    assert any(HEARTBEAT_GRANT in item and "PostgreSQL 15" in item for item in row.checklist)
    assert HEARTBEAT_MESSAGE in (row.message or "")


@pytest.mark.passes_on_main
def test_a_mysql_source_has_no_heartbeat_grant():
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    row = _row(_run(cred), "source_cdc")
    assert row.status == "confirm" and "heartbeat" not in (row.message or "")
    assert not any("heartbeat" in item for item in row.checklist)


def test_an_aurora_cluster_source_with_wrong_values_fails():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _aurora_source(cred, binlog="MIXED")
    row = _row(_run(cred, source_kind="aurora_cluster", source_id="src-cluster"), "source_cdc")
    assert row.status == "fail" and "binlog_format=ROW" in (row.message or "")


def test_a_source_of_the_wrong_engine_family_fails():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _rds_source(cred, engine="postgres")
    row = _row(_run(cred, source_kind="rds_instance", source_id="shop-db"), "source_cdc")
    assert row.status == "fail" and "postgres" in row.summary and "mysql" in row.summary
    cred = StubbedCredMgr()
    _up_to_source(cred)
    rds = cred.stub("rds")
    rds.add_response("describe_db_clusters", {"DBClusters": [{"DBClusterIdentifier": "src-cluster",
                                                              "Engine": "aurora-postgresql"}]})  # fmt: skip
    row = _row(_run(cred, source_kind="aurora_cluster", source_id="src-cluster"), "source_cdc")
    assert row.status == "fail" and "aurora-postgresql" in row.summary


@pytest.mark.parametrize("kind", ["rds_instance", "aurora_cluster"])
def test_a_managed_source_without_an_id_fails_early_with_a_usage_message(kind):
    cred = StubbedCredMgr()
    _up_to_source(cred)
    row = _row(_run(cred, source_kind=kind), "source_cdc")  # no RDS call is queued
    assert (
        row.status == "fail"
        and "--source-id" in (row.message or "")
        and "None" not in row.summary + (row.message or "")
    )


def test_check_8_confirms_a_transit_gateway_or_instance_default_route_and_fails_with_none():
    other = {
        "RouteTableId": "rtb-3",
        "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "TransitGatewayId": "tgw-1"}],
    }
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    s.queue_routes(cred, [s.nat_table()], subnet="subnet-aaa")
    s.queue_routes(cred, [other], subnet="subnet-bbb")
    row = _network(cred)
    assert row.status == "confirm" and "--confirm secrets_manager_reachable" in (row.message or "")
    assert "transit gateway" in (row.message or "") and "subnet-bbb" in " ".join(row.checklist)
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    s.queue_routes(cred, [other], subnet="subnet-aaa")
    s.queue_routes(cred, [{"RouteTableId": "r", "Routes": []}], subnet="subnet-bbb")
    row = _network(cred)
    assert row.status == "fail" and "NAT gateways and interface endpoints" in (row.message or "")


def test_the_confirmed_check_8_passes():
    other = {
        "RouteTableId": "rtb-3",
        "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "InstanceId": "i-1"}],
    }
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    for subnet in s.SUBNETS:
        s.queue_routes(cred, [other], subnet=subnet)
    result = _run(cred, confirmed={"source_cdc", "secrets_manager_reachable"})
    assert (
        result.status == "pass"
        and "confirmed by the operator" in _row(result, "secrets_manager_reachable").summary
    )


def test_an_endpoint_pass_still_lists_the_security_group_item():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_network(cred)
    row = _network(cred)
    assert row.status == "pass" and any("443" in i and "security group" in i for i in row.checklist)


@pytest.mark.parametrize(("code", "needle"), [("InvalidSubnetID.NotFound", "check the ids and region"),
                                              ("InvalidParameterValue", "check the ids and region"),
                                              ("AccessDenied", "ec2:DescribeSubnets")])  # fmt: skip
def test_a_not_found_error_is_not_reported_as_a_permission(code, needle):
    cred = StubbedCredMgr()
    _up_to_network(cred)
    cred.stub("ec2").add_client_error(
        "describe_subnets", service_error_code=code, http_status_code=400
    )
    row = _network(cred)
    assert row.status == "fail" and needle in (row.message or "")
    if code != "AccessDenied":
        assert "ec2:DescribeSubnets" not in (row.message or "")


def test_a_botocore_error_and_a_bad_json_document_become_failed_rows(monkeypatch):
    def boom(*a, **k):
        raise BotoCoreError()

    cred = StubbedCredMgr()
    clients = s.clients(cred)
    monkeypatch.setattr(clients.rds, "describe_db_clusters", boom)
    result = run_preflight(make_step1_inputs(), s.request(), clients, now=NOW)
    assert result.checks[0].status == "fail" and result.status == "fail"
    cred = StubbedCredMgr()
    _up_to_roles(cred)
    s.queue_roles(cred, trust_docs={"dms-vpc-role": "%7Bnot json"})
    row = _row(_run(cred), "dms_service_roles")
    assert row.status == "fail" and "dms_service_roles" not in row.summary and row.message


def test_unknown_confirmation_ids_are_rejected():
    cred = StubbedCredMgr()
    with pytest.raises(ValueError, match="source_cdcs"):
        _run(cred, confirmed={"source_cdc", "source_cdcs"})


# --- pagination: every list call follows its Marker --------------------------------------


def test_orderable_classes_are_read_across_pages():
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    dms = cred.stub("dms")
    dms.add_response("describe_orderable_replication_instances",
                     {"OrderableReplicationInstances": [{"ReplicationInstanceClass": "dms.r6i.large"}], "Marker": "m"})  # fmt: skip
    dms.add_response("describe_orderable_replication_instances",
                     {"OrderableReplicationInstances": [{"ReplicationInstanceClass": "dms.t3.medium"}]}, {"Marker": "m"})  # fmt: skip
    assert _row(_run(cred), "instance_class_offered").status == "pass"


def test_certificates_are_read_across_pages():
    cred = StubbedCredMgr()
    _up_to_certs(cred)
    dms = cred.stub("dms")
    flt = {"Filters": [{"Name": "certificate-arn", "Values": [s.SOURCE_CERT]}]}
    dms.add_response("describe_certificates", {"Certificates": [], "Marker": "m"}, flt)
    dms.add_response(
        "describe_certificates", {"Certificates": [s.cert(s.SOURCE_CERT)]}, {**flt, "Marker": "m"}
    )
    s.queue_cert(cred, s.TARGET_CERT)
    s.queue_network(cred)
    assert _row(_run(cred, confirmed={"source_cdc"}), "dms_certificates").status == "pass"


def test_subnets_endpoints_and_route_tables_are_read_across_pages():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    ec2 = cred.stub("ec2")
    ids = {"SubnetIds": list(s.SUBNETS)}
    ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": "subnet-aaa", "VpcId": s.VPC}], "NextToken": "n"},
        ids,
    )
    ec2.add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": "subnet-bbb", "VpcId": s.VPC}]},
        {**ids, "NextToken": "n"},
    )
    flt = {"Filters": [{"Name": "vpc-id", "Values": [s.VPC]},
                       {"Name": "service-name", "Values": [f"com.amazonaws.{s.REGION}.secretsmanager"]},
                       {"Name": "vpc-endpoint-type", "Values": ["Interface"]}]}  # fmt: skip
    ec2.add_response(
        "describe_vpc_endpoints",
        {"VpcEndpoints": [s.endpoint(state="pending")], "NextToken": "e"},
        flt,
    )
    ec2.add_response("describe_vpc_endpoints", {"VpcEndpoints": [{**s.endpoint(), "VpcEndpointId": "vpce-2"}]},
                     {**flt, "NextToken": "e"})  # fmt: skip
    row = _network(cred)
    assert row.status == "pass" and "vpce-2" in row.evidence["secretsmanager_endpoints"]
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    for subnet in s.SUBNETS:
        sub = {"Filters": [{"Name": "association.subnet-id", "Values": [subnet]}]}
        ec2 = cred.stub("ec2")
        ec2.add_response(
            "describe_route_tables", {"RouteTables": [s.igw_table()], "NextToken": subnet}, sub
        )
        ec2.add_response(
            "describe_route_tables", {"RouteTables": [s.nat_table()]}, {**sub, "NextToken": subnet}
        )
    assert _network(cred).status == "pass"
    _done(cred)


# --- fix round 2 -------------------------------------------------------------------------


def test_a_virtual_private_gateway_default_route_needs_confirmation():
    vgw = {
        "RouteTableId": "rtb-4",
        "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "vgw-1"}],
    }
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    for subnet in s.SUBNETS:
        s.queue_routes(cred, [vgw], subnet=subnet)
    row = _network(cred)
    assert row.status == "confirm" and "--confirm secrets_manager_reachable" in (row.message or "")


def test_an_internet_gateway_default_route_alone_still_fails():
    cred = StubbedCredMgr()
    _up_to_network(cred)
    s.queue_subnets(cred)
    s.queue_endpoints(cred)
    for subnet in s.SUBNETS:
        s.queue_routes(cred, [s.igw_table()], subnet=subnet)
    assert _network(cred).status == "fail"


def test_a_source_kind_that_does_not_fit_the_engine_names_the_right_kind():
    cred = StubbedCredMgr()
    _up_to_source(cred)
    _rds_source(cred, engine="aurora-mysql")
    row = _row(_run(cred, source_kind="rds_instance", source_id="shop-db"), "source_cdc")
    assert row.status == "fail" and "--source-kind aurora_cluster" in (row.message or "")
    cred = StubbedCredMgr()
    _up_to_source(cred)
    cred.stub("rds").add_response("describe_db_clusters", {"DBClusters": [{
        "DBClusterIdentifier": "src-cluster", "Engine": "mysql"}]})  # fmt: skip
    row = _row(_run(cred, source_kind="aurora_cluster", source_id="src-cluster"), "source_cdc")
    assert row.status == "fail" and "--source-kind rds_instance" in (row.message or "")


@pytest.mark.parametrize(
    "code", ["NoSuchEntity", "InvalidVpcEndpointId.Malformed", "InvalidSubnetID.Malformed"]
)
def test_no_such_entity_and_malformed_ids_are_lookup_errors(code):
    cred = StubbedCredMgr()
    _up_to_network(cred)
    cred.stub("ec2").add_client_error(
        "describe_subnets", service_error_code=code, http_status_code=400
    )
    row = _network(cred)
    assert row.status == "fail" and "check the ids and region" in (row.message or "")
