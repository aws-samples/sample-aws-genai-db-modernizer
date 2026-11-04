# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 preflight checks 1 to 3 (#1799): botocore Stubbers, nothing is sent."""

from __future__ import annotations

import json

import pytest

from src.tools.data_movement.dms_preflight import (
    check_aurora_cluster,
    check_aurora_parameters,
    check_secrets,
)
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

from . import preflight_stubs as st
from .dms_step_fixtures import make_step1_inputs, with_zero_dates


def _run(check, cred, inputs=None, req=None, ctx=None):
    cl = st.clients(cred)
    return check(inputs or make_step1_inputs(), req or st.request(), cl, {} if ctx is None else ctx)


def _ctx(cred, **kw):
    """Run check 1 on the queued responses; return its context for check 2."""
    ctx: dict = {}
    row = _run(check_aurora_cluster, cred, ctx=ctx, **kw)
    assert row.status == "pass", row
    return ctx


def _done(cred):
    for s in cred.stubbers.values():
        s.assert_no_pending_responses()


def _policy(*statements):
    return json.dumps({"Version": "2012-10-17", "Statement": list(statements)})


def _deny(action="secretsmanager:GetSecretValue", **extra):
    return {"Effect": "Deny", "Principal": "*", "Resource": "*", "Action": action, **extra}


# --- check 1 ---------------------------------------------------------------------------


def test_a_cluster_on_the_designs_line_passes():
    cred = StubbedCredMgr()
    st.queue_aurora(cred)
    row = _run(check_aurora_cluster, cred)
    assert (row.id, row.status) == ("aurora_cluster", "pass")
    assert row.evidence["engine_version"] == "8.0.mysql_aurora.3.08.0"
    assert row.evidence["writer_status"] == "available"


def test_a_missing_cluster_fails():
    cred = StubbedCredMgr()
    cred.stub("rds").add_client_error("describe_db_clusters", service_error_code="DBClusterNotFoundFault",
                                      http_status_code=404, expected_params={"DBClusterIdentifier": st.CLUSTER})  # fmt: skip
    row = _run(check_aurora_cluster, cred)
    assert row.status == "fail" and st.CLUSTER in row.summary


def test_the_wrong_engine_fails():
    cred = StubbedCredMgr()
    st.queue_aurora(cred, engine="aurora-postgresql", version="16.4")
    row = _run(check_aurora_cluster, cred)
    assert row.status == "fail" and "aurora-mysql" in row.summary


@pytest.mark.parametrize(
    ("engine", "version", "inputs_engine", "passes"),
    [
        ("aurora-mysql", "8.0.mysql_aurora.3.08.0", "aurora_mysql", True),
        ("aurora-mysql", "8.4.mysql_aurora.4.01.0", "aurora_mysql", True),
        ("aurora-mysql", "5.7.mysql_aurora.2.12.0", "aurora_mysql", False),
        ("aurora-postgresql", "16.4", "aurora_postgresql", True),
        ("aurora-postgresql", "17.2", "aurora_postgresql", True),
        ("aurora-postgresql", "13.12", "aurora_postgresql", False),
    ],
)
def test_the_version_must_be_the_designs_line_or_later(engine, version, inputs_engine, passes):
    cred = StubbedCredMgr()
    st.queue_aurora(cred, engine=engine, version=version)
    row = _run(check_aurora_cluster, cred, make_step1_inputs(inputs_engine))
    assert (row.status == "pass") is passes
    if not passes:
        assert version in row.summary


@pytest.mark.parametrize("cluster_status", ["stopped", "modifying", "creating", "failed"])
def test_a_cluster_that_is_not_available_fails(cluster_status):
    cred = StubbedCredMgr()
    st.queue_aurora(cred, cluster_status=cluster_status)
    row = _run(check_aurora_cluster, cred)
    assert row.status == "fail" and cluster_status in row.summary


@pytest.mark.parametrize("writer_status", ["rebooting", "stopped", "backing-up", "failed"])
def test_a_writer_that_is_not_available_fails(writer_status):
    cred = StubbedCredMgr()
    st.queue_aurora(cred, writer_status=writer_status)
    row = _run(check_aurora_cluster, cred)
    assert row.status == "fail" and writer_status in row.summary and st.WRITER in row.summary


def test_a_refused_describe_becomes_a_failed_row_naming_the_permission():
    cred = StubbedCredMgr()
    cred.stub("rds").add_client_error("describe_db_clusters", service_error_code="AccessDenied",
                                      http_status_code=403, expected_params={"DBClusterIdentifier": st.CLUSTER})  # fmt: skip
    row = _run(check_aurora_cluster, cred)
    assert row.status == "fail" and "rds:DescribeDBClusters" in (row.message or "")
    assert (
        row.evidence["error_code"] == "AccessDenied" and row.evidence["api"] == "DescribeDBClusters"
    )


# --- check 2 ---------------------------------------------------------------------------


def test_applied_cluster_and_instance_parameters_pass():
    cred = StubbedCredMgr()
    st.queue_aurora(cred)
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "pass"
    assert row.evidence["local_infile"] == "1" and row.evidence["binlog_format"] == "ROW"
    _done(cred)


def test_an_unset_target_binlog_row_image_is_the_engine_default_full():
    cred = StubbedCredMgr()
    st.queue_aurora(cred, params={"binlog_format": "ROW"})
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "pass" and row.evidence["binlog_row_image"] == "FULL"
    _done(cred)


def test_the_passing_parameters_row_notes_every_instance_must_use_the_group():
    cred = StubbedCredMgr()
    st.queue_aurora(cred)
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "pass" and any("failover" in i for i in row.checklist)


def test_a_wrong_cluster_value_fails_and_skips_the_instance_parameter_read():
    cred = StubbedCredMgr()
    st.queue_aurora(
        cred,
        params={"binlog_format": "MIXED", "binlog_row_image": "FULL"},
        reads_instance_params=False,
    )
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "fail" and "binlog_format=MIXED" in row.summary
    _done(cred)


@pytest.mark.parametrize(
    "status", ["pending-reboot", "applying", "failed-to-apply", "pending-database-upgrade", None]
)
def test_a_cluster_group_not_in_sync_fails_naming_the_status(status):
    cred = StubbedCredMgr()
    st.queue_aurora(cred, status=status)
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "fail" and st.GROUP in row.summary
    assert (status or "unknown") in row.summary
    assert row.message and "group-wide" in row.message
    assert row.evidence["cluster_parameter_status"] == (status or "unknown")
    assert ("Reboot" in row.message) is (status == "pending-reboot")
    if status == "pending-reboot":
        assert st.WRITER in row.message


@pytest.mark.parametrize(
    "status", ["pending-reboot", "applying", "failed-to-apply", "pending-database-upgrade", None]
)
def test_an_instance_group_not_in_sync_fails_even_when_the_value_is_right(status):
    cred = StubbedCredMgr()
    st.queue_aurora(cred, instance_status=status)
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "fail" and st.INSTANCE_GROUP in row.summary
    assert (status or "unknown") in row.summary
    assert row.message and "group-wide" in row.message
    assert ("Reboot" in row.message) is (status == "pending-reboot")
    assert row.evidence["local_infile"] == "1"


def test_local_infile_off_on_the_writers_group_fails():
    cred = StubbedCredMgr()
    st.queue_aurora(cred, instance_params={"local_infile": "0"})
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "fail" and "local_infile=0" in row.summary
    assert row.message and st.INSTANCE_GROUP in row.message


def test_zero_date_copies_need_a_sql_mode_without_the_zero_date_modes():
    cred = StubbedCredMgr()
    st.queue_aurora(
        cred, instance_params={"local_infile": "1", "sql_mode": "STRICT_TRANS_TABLES,NO_ZERO_DATE"}
    )
    row = _run(check_aurora_parameters, cred, with_zero_dates(), ctx=_ctx(cred))
    assert row.status == "fail" and "sql_mode" in row.summary and "NO_ZERO_DATE" in row.summary


@pytest.mark.parametrize(
    "mode",
    ["TRADITIONAL", "traditional", "NO_ZERO_DATE", "ONLY_FULL_GROUP_BY,NO_ZERO_IN_DATE", None],
)
def test_zero_date_copies_with_a_sql_mode_that_forbids_them_fail(mode):
    cred = StubbedCredMgr()
    values = {"local_infile": "1", **({"sql_mode": mode} if mode else {})}
    st.queue_aurora(cred, instance_params=values)
    row = _run(check_aurora_parameters, cred, with_zero_dates(), ctx=_ctx(cred))
    assert row.status == "fail" and "sql_mode" in row.summary


@pytest.mark.parametrize(
    "mode",
    [
        "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES",
        "strict_trans_tables, only_full_group_by",
        "NO_ENGINE_SUBSTITUTION,STRICT_TRANS_TABLES,ONLY_FULL_GROUP_BY",
    ],
)
def test_zero_date_copies_with_a_permissive_sql_mode_pass_in_any_order_and_case(mode):
    cred = StubbedCredMgr()
    st.queue_aurora(cred, instance_params={"local_infile": "1", "sql_mode": mode})
    row = _run(check_aurora_parameters, cred, with_zero_dates(), ctx=_ctx(cred))
    assert row.status == "pass" and row.evidence["sql_mode"] == mode


def test_no_zero_date_copies_do_not_read_sql_mode():
    cred = StubbedCredMgr()
    st.queue_aurora(cred, instance_params={"local_infile": "1", "sql_mode": "NO_ZERO_DATE"})
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "pass" and "sql_mode" not in row.evidence


def test_postgresql_reads_no_db_parameter_group_but_still_checks_the_apply_status():
    inputs = make_step1_inputs("aurora_postgresql")
    cred = StubbedCredMgr()
    st.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False)
    row = _run(check_aurora_parameters, cred, inputs, ctx=_ctx(cred, inputs=inputs))
    assert row.status == "pass" and row.evidence["rds.logical_replication"] == "1"
    _done(cred)


def test_postgresql_instance_group_pending_reboot_fails():
    inputs = make_step1_inputs("aurora_postgresql")
    cred = StubbedCredMgr()
    st.queue_aurora(cred, engine="aurora-postgresql", version="16.4", reads_instance_params=False,
                    instance_status="pending-reboot")  # fmt: skip
    row = _run(check_aurora_parameters, cred, inputs, ctx=_ctx(cred, inputs=inputs))
    assert row.status == "fail" and "pending-reboot" in row.summary


def test_a_cluster_without_a_writer_fails():
    cred = StubbedCredMgr()
    body = st.cluster()
    body["DBClusters"][0]["DBClusterMembers"][0]["IsClusterWriter"] = False
    cred.stub("rds").add_response("describe_db_clusters", body, {"DBClusterIdentifier": st.CLUSTER})
    row = _run(check_aurora_parameters, cred, ctx=_ctx(cred))
    assert row.status == "fail" and "writer" in row.summary


def test_the_cluster_parameters_are_read_across_pages():
    cred = StubbedCredMgr()
    rds = cred.stub("rds")
    rds.add_response("describe_db_clusters", st.cluster(), {"DBClusterIdentifier": st.CLUSTER})
    rds.add_response("describe_db_instances", st.instance(), {"DBInstanceIdentifier": st.WRITER})
    ctx = _ctx(cred)
    rds.add_response("describe_db_cluster_parameters", {**st.parameters(binlog_format="ROW"), "Marker": "m1"},
                     {"DBClusterParameterGroupName": st.GROUP})  # fmt: skip
    rds.add_response("describe_db_cluster_parameters", st.parameters(binlog_row_image="FULL"),
                     {"DBClusterParameterGroupName": st.GROUP, "Marker": "m1"})  # fmt: skip
    rds.add_response("describe_db_parameters", st.parameters(local_infile="1"),
                     {"DBParameterGroupName": st.INSTANCE_GROUP})  # fmt: skip
    row = _run(check_aurora_parameters, cred, ctx=ctx)
    assert row.status == "pass" and row.evidence["binlog_row_image"] == "FULL"
    _done(cred)


def test_the_instance_parameters_are_read_across_pages():
    cred = StubbedCredMgr()
    rds = cred.stub("rds")
    rds.add_response("describe_db_clusters", st.cluster(), {"DBClusterIdentifier": st.CLUSTER})
    rds.add_response("describe_db_instances", st.instance(), {"DBInstanceIdentifier": st.WRITER})
    ctx = _ctx(cred)
    rds.add_response("describe_db_cluster_parameters", st.parameters(**st.MYSQL_PARAMS),
                     {"DBClusterParameterGroupName": st.GROUP})  # fmt: skip
    rds.add_response("describe_db_parameters", {**st.parameters(max_connections="100"), "Marker": "m1"},
                     {"DBParameterGroupName": st.INSTANCE_GROUP})  # fmt: skip
    rds.add_response("describe_db_parameters", st.parameters(local_infile="1"),
                     {"DBParameterGroupName": st.INSTANCE_GROUP, "Marker": "m1"})  # fmt: skip
    row = _run(check_aurora_parameters, cred, ctx=ctx)
    assert row.status == "pass" and row.evidence["local_infile"] == "1"
    _done(cred)


# --- check 3 ---------------------------------------------------------------------------


def test_two_readable_secrets_pass_with_exactly_the_queued_metadata_reads():
    cred = StubbedCredMgr()
    st.queue_secrets(cred)
    row = _run(check_secrets, cred)
    assert row.status == "pass"
    assert row.summary.startswith(
        "Both secrets exist; nothing denies the stack's role reading these secrets"
    )
    _done(cred)


def test_a_missing_secret_fails():
    cred = StubbedCredMgr()
    cred.stub("secretsmanager").add_client_error("describe_secret", service_error_code="ResourceNotFoundException",
                                                 http_status_code=400, expected_params={"SecretId": st.SOURCE_SECRET})  # fmt: skip
    row = _run(check_secrets, cred)
    assert row.status == "fail" and "source" in row.summary


def test_a_refused_secret_read_names_the_permission():
    cred = StubbedCredMgr()
    cred.stub("secretsmanager").add_client_error("describe_secret", service_error_code="AccessDeniedException",
                                                 http_status_code=400, expected_params={"SecretId": st.SOURCE_SECRET})  # fmt: skip
    row = _run(check_secrets, cred)
    assert row.status == "fail" and "secretsmanager:DescribeSecret" in (row.message or "")


def test_a_secret_scheduled_for_deletion_fails():
    cred = StubbedCredMgr()
    st.queue_secrets(cred, target=st.secret(st.TARGET_SECRET, deleted=True))
    row = _run(check_secrets, cred)
    assert row.status == "fail" and "target" in row.summary and "deletion" in row.summary


def test_a_secret_on_an_unnamed_kms_key_fails():
    cred = StubbedCredMgr()
    st.queue_secrets(cred, source=st.secret(st.SOURCE_SECRET, kms=st.KEY))
    row = _run(check_secrets, cred)
    assert row.status == "fail" and st.KEY in row.summary
    assert row.message and "--source-secret-kms-key-arn" in row.message


def test_a_secret_on_a_named_kms_key_passes():
    cred = StubbedCredMgr()
    st.queue_secrets(cred, source=st.secret(st.SOURCE_SECRET, kms=st.KEY))
    row = _run(check_secrets, cred, req=st.request(source_secret_kms_key_arn=st.KEY))
    assert row.status == "pass"


def _alias_secret(cred, given_key):
    """The source secret on an alias; DescribeKey resolves it to st.KEY."""
    alias = f"arn:aws:kms:{st.REGION}:{st.ACCOUNT}:alias/shop-secrets"
    sm = cred.stub("secretsmanager")
    sm.add_response(
        "describe_secret", st.secret(st.SOURCE_SECRET, kms=alias), {"SecretId": st.SOURCE_SECRET}
    )
    cred.stub("kms").add_response("describe_key", st.key(), {"KeyId": alias})
    sm.add_response("get_resource_policy", {"ARN": st.SOURCE_SECRET, "Name": "shop-source-AbCdEf"},
                    {"SecretId": st.SOURCE_SECRET})  # fmt: skip
    st.queue_secrets_target(cred)
    return st.request(source_secret_kms_key_arn=given_key)


def test_a_secret_on_an_alias_resolves_to_the_key_the_stack_was_told_about():
    cred = StubbedCredMgr()
    row = _run(check_secrets, cred, req=_alias_secret(cred, st.KEY))
    assert row.status == "pass" and row.evidence["source_kms_key_resolved"] == st.KEY
    _done(cred)


def test_a_secret_on_an_alias_fails_when_its_key_is_not_named():
    cred = StubbedCredMgr()
    row = _run(check_secrets, cred, req=_alias_secret(cred, ""))
    assert row.status == "fail" and st.KEY in row.summary and row.message and st.KEY in row.message


def test_naming_the_alias_does_not_count_as_naming_the_key():
    cred = StubbedCredMgr()
    alias = f"arn:aws:kms:{st.REGION}:{st.ACCOUNT}:alias/shop-secrets"
    row = _run(check_secrets, cred, req=_alias_secret(cred, alias))
    assert row.status == "fail"


def test_the_aws_managed_key_needs_no_naming():
    cred = StubbedCredMgr()
    st.queue_secrets(cred, source=st.secret(st.SOURCE_SECRET, kms="alias/aws/secretsmanager"))
    assert _run(check_secrets, cred).status == "pass"


@pytest.mark.parametrize(
    "action", ["secretsmanager:GetSecretValue", "secretsmanager:*", "*", ["secretsmanager:Get*"]]
)
def test_an_unconditional_deny_of_everyone_reading_fails(action):
    cred = StubbedCredMgr()
    st.queue_secrets(cred, policy=_policy({"Sid": "NoRead", **_deny(action)}))
    row = _run(check_secrets, cred)
    assert row.status == "fail" and "NoRead" in row.summary


def test_an_aws_wildcard_principal_deny_fails():
    cred = StubbedCredMgr()
    st.queue_secrets(cred, policy=_policy({"Sid": "NoRead", **_deny(), "Principal": {"AWS": "*"}}))
    assert _run(check_secrets, cred).status == "fail"


def test_an_unrelated_deny_does_not_fail():
    cred = StubbedCredMgr()
    st.queue_secrets(cred, policy=_policy(_deny("secretsmanager:DeleteSecret")))
    assert _run(check_secrets, cred).status == "pass"


def test_a_not_action_deny_hits_only_when_no_listed_pattern_matches():
    cred = StubbedCredMgr()
    # NotAction lists GetSecretValue: it is exempt, so the Deny does not cover it.
    exempt = {
        "Effect": "Deny",
        "Principal": "*",
        "Resource": "*",
        "NotAction": ["secretsmanager:GetSecretValue"],
    }
    st.queue_secrets(cred, policy=_policy(exempt))
    assert _run(check_secrets, cred).status == "pass"
    cred = StubbedCredMgr()
    exempt_by_pattern = {**exempt, "NotAction": "secretsmanager:Get*"}
    st.queue_secrets(cred, policy=_policy(exempt_by_pattern))
    assert _run(check_secrets, cred).status == "pass"
    cred = StubbedCredMgr()
    covers = {**exempt, "Sid": "AllButDescribe", "NotAction": ["secretsmanager:DescribeSecret"]}
    st.queue_secrets(cred, policy=_policy(covers))
    row = _run(check_secrets, cred)
    assert row.status == "fail" and "AllButDescribe" in row.summary


@pytest.mark.parametrize(
    "statement",
    [
        {"Sid": "OnlyOutsideVpc", **_deny(), "Condition": {"StringNotEquals": {"aws:SourceVpc": "vpc-1"}}},
        {"Sid": "ForOneRole", **_deny(), "Principal": {"AWS": f"arn:aws:iam::{st.ACCOUNT}:role/other"}},
        {"Sid": "ForAllButOne", "Effect": "Deny", "NotPrincipal": {"AWS": "arn:aws:iam::1:role/x"},
         "Action": "secretsmanager:GetSecretValue", "Resource": "*"},
    ],
)  # fmt: skip
def test_a_conditional_or_principal_scoped_deny_needs_the_customers_confirmation(statement):
    cred = StubbedCredMgr()
    st.queue_secrets(cred, policy=_policy(statement))
    row = _run(check_secrets, cred)
    assert row.status == "confirm"
    assert statement["Sid"] in row.summary
    assert any(statement["Sid"] in item and "source" in item for item in row.checklist)
    assert row.message and "--confirm secrets" in row.message
    _done(cred)


def test_an_unconditional_deny_wins_over_a_conditional_one():
    cred = StubbedCredMgr()
    cond = {"Sid": "Cond", **_deny(), "Condition": {"Bool": {"aws:SecureTransport": "false"}}}
    st.queue_secrets(cred, policy=_policy(cond, {"Sid": "Hard", **_deny()}))
    row = _run(check_secrets, cred)
    assert row.status == "fail" and "Hard" in row.summary


def test_a_conditional_deny_on_the_target_secret_is_found_too():
    cred = StubbedCredMgr()
    cond = {"Sid": "TargetCond", **_deny(), "Condition": {"Bool": {"aws:SecureTransport": "false"}}}
    st.queue_secrets(cred, target_policy=_policy(cond))
    row = _run(check_secrets, cred)
    assert row.status == "confirm" and any("target" in item for item in row.checklist)
