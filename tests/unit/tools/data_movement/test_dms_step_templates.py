# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The rendered DMS stack and the suggested Aurora template (#1798)."""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from src.contracts.data_movement_plan import UnboundedVarchar
from src.contracts.lineage_ops import UnsupportedOp
from src.contracts.transformation_contract import Finding
from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.data_movement.debezium_connector import dump_template
from src.tools.data_movement.dms_step import (
    AURORA_TEMPLATE,
    CONTRACT_TAG,
    CUSTOMER_RESPONSIBILITY,
    DDL_FILE,
    DMS_TEMPLATE,
    MYSQL_80_DEFAULT_SQL_MODE,
    REQUIRED_CLUSTER_PARAMETERS,
    REQUIRED_INSTANCE_PARAMETERS,
    SOURCE_ENDPOINTS,
    STACK_OUTPUTS,
    TARGET_ENDPOINTS,
    VERIFY_SSL_MODE,
    ZERO_DATE_SQL_MODE,
    DmsStepError,
    Step1Inputs,
    load_step1,
    render_aurora_template,
    render_ddl,
    render_dms_template,
    render_files,
    required_settings,
    table_mappings,
    task_identifier,
    task_settings,
    zero_date_columns,
)

from .dms_step_fixtures import (
    edge_field,
    make_bundle,
    make_step1_inputs,
    rebuilt,
    with_zero_dates,
    write_job,
)

ENGINES = ["aurora_mysql", "aurora_postgresql"]
# Parameters that name an account, a region, an endpoint, the network or a credential.
DMS_DEPLOYMENT_PARAMETERS = {"SubnetIds", "SecurityGroupIds", "SourceSecretArn", "TargetSecretArn"}


def _no_account_region_or_endpoint(text: str) -> None:
    assert not re.search(r"\b\d{12}\b", text), "an account id is in the template"
    assert not re.search(r"\b[a-z]{2}(-gov)?-[a-z]+-\d\b", text), "a region is in the template"
    assert ".rds.amazonaws.com" not in text and "amazonaws.com:" not in text


def _settings_keys(inputs: Step1Inputs) -> dict[str, str]:
    return {
        "Source": SOURCE_ENDPOINTS[inputs.plan.source.engine][1],
        "Target": TARGET_ENDPOINTS[inputs.contract.target.engine][1],
    }


OVERRIDE = "vpce-0abc.secretsmanager.example.vpce.amazonaws.com"


def _eca(props: dict, override: bool) -> str | None:
    """The endpoint's ExtraConnectionAttributes as deployed, with SecretsManagerEndpointOverride
    set (``override``) or left empty."""
    value = props.get("ExtraConnectionAttributes")
    if isinstance(value, dict):
        cond, when_set, when_empty = value["Fn::If"]
        assert cond == "HasSecretsManagerEndpointOverride"
        value = when_set if override else when_empty
    if isinstance(value, dict) and "Fn::Sub" in value:
        value = value["Fn::Sub"].replace("${SecretsManagerEndpointOverride}", OVERRIDE)
    if value == {"Ref": "AWS::NoValue"}:
        value = None
    return value  # type: ignore[no-any-return]


def _with_database(inputs: Step1Inputs, database: str) -> Step1Inputs:
    source = inputs.plan.source.model_copy(update={"database": database})
    return dataclasses.replace(inputs, plan=inputs.plan.model_copy(update={"source": source}))


def _with_unbounded_varchar(inputs: Step1Inputs, max_bytes: int | None) -> Step1Inputs:
    assert inputs.plan.replication is not None
    replication = inputs.plan.replication.model_copy(
        update={"unbounded_varchar": UnboundedVarchar(max_bytes=max_bytes, columns=1)}
    )
    return dataclasses.replace(
        inputs, plan=inputs.plan.model_copy(update={"replication": replication})
    )


@pytest.mark.parametrize("engine", ENGINES)
def test_no_account_region_or_endpoint_defaults(engine):
    inputs = make_step1_inputs(engine)
    template = render_dms_template(inputs)
    params = template["Parameters"]
    assert DMS_DEPLOYMENT_PARAMETERS <= set(params)
    assert not [p for p in DMS_DEPLOYMENT_PARAMETERS if "Default" in params[p]]
    assert all(params[p]["Default"] == "" for p in params if p.endswith("KmsKeyArn"))
    for role, key in _settings_keys(inputs).items():
        cert = params[f"{role}CertificateArn"]
        # MySQL-compatible endpoints support no encrypted-only mode: the CA is required.
        assert ("Default" not in cert) if key == "MySqlSettings" else (cert["Default"] == "")
    _no_account_region_or_endpoint(dump_template(template))


@pytest.mark.parametrize("engine", ENGINES)
def test_endpoints_take_only_secret_arns(engine):
    template = render_dms_template(make_step1_inputs(engine))
    for name in ("SourceEndpoint", "TargetEndpoint"):
        props = template["Resources"][name]["Properties"]
        assert not {"Username", "Password", "ServerName", "Port"} & set(props)
        settings = next(v for k, v in props.items() if k.endswith("Settings"))
        role = name.removesuffix("Endpoint")
        assert settings["SecretsManagerSecretId"] == {"Ref": f"{role}SecretArn"}
        assert settings["SecretsManagerAccessRoleArn"] == {
            "Fn::GetAtt": ["SecretsAccessRole", "Arn"]
        }
        for override in (False, True):
            eca = _eca(props, override) or ""
            assert not re.search(r"(?i)password|username|servername|host=|port=", eca)
    assert not [
        p for p in template["Parameters"] if re.search(r"(?i)password|username|host|port", p)
    ]


@pytest.mark.parametrize("engine", ENGINES)
def test_tls_is_always_on_and_verified_when_a_certificate_is_given(engine):
    inputs = make_step1_inputs(engine)
    resources = render_dms_template(inputs)["Resources"]
    for role, key in _settings_keys(inputs).items():
        props = resources[f"{role}Endpoint"]["Properties"]
        if key == "MySqlSettings":
            # MySQL, MariaDB and Aurora MySQL take no "require": verified TLS only.
            assert props["SslMode"] == VERIFY_SSL_MODE[key] == "verify-ca"
            assert props["CertificateArn"] == {"Ref": f"{role}CertificateArn"}
        else:
            mode = props["SslMode"]["Fn::If"]
            assert mode == [f"Has{role}Certificate", "verify-full", "require"]
            assert props["CertificateArn"]["Fn::If"][0] == f"Has{role}Certificate"
        assert props["SslMode"] != "none"


def test_the_aurora_mysql_target_disables_foreign_key_checks():
    props = render_dms_template(make_step1_inputs("aurora_mysql"))["Resources"]["TargetEndpoint"][
        "Properties"
    ]
    assert props["EngineName"] == "aurora"
    assert props["MySqlSettings"]["AfterConnectScript"] == "SET FOREIGN_KEY_CHECKS=0"
    assert props["MySqlSettings"]["TargetDbType"] == "specific-database"
    assert props["DatabaseName"] == {"Ref": "TargetDatabaseName"}
    source = render_dms_template(make_step1_inputs("aurora_mysql"))["Resources"]["SourceEndpoint"][
        "Properties"
    ]
    assert "AfterConnectScript" not in source["MySqlSettings"] and "DatabaseName" not in source


def test_the_aurora_postgresql_target_bypasses_foreign_keys_and_triggers():
    resources = render_dms_template(make_step1_inputs("aurora_postgresql"))["Resources"]
    target = resources["TargetEndpoint"]["Properties"]
    assert target["EngineName"] == "aurora-postgresql"
    assert (
        target["PostgreSqlSettings"]["AfterConnectScript"]
        == "SET session_replication_role = replica"
    )
    assert (
        "AfterConnectScript" not in resources["SourceEndpoint"]["Properties"]["PostgreSqlSettings"]
    )


def test_a_postgresql_source_raises_the_unbounded_varchar_limit_only_when_the_plan_says_so():
    inputs = make_step1_inputs("aurora_postgresql")
    source = render_dms_template(_with_unbounded_varchar(inputs, 65536))["Resources"][
        "SourceEndpoint"
    ]
    assert _eca(source["Properties"], False) == "unboundedVarcharMaxSize=65536"
    assert source["Properties"]["DatabaseName"] == {"Ref": "SourceDatabaseName"}
    unset = render_dms_template(_with_unbounded_varchar(inputs, None))["Resources"][
        "SourceEndpoint"
    ]
    assert _eca(unset["Properties"], False) is None
    target = render_dms_template(_with_unbounded_varchar(inputs, 65536))["Resources"][
        "TargetEndpoint"
    ]
    assert _eca(target["Properties"], False) is None
    mysql = render_dms_template(_with_unbounded_varchar(make_step1_inputs("aurora_mysql"), 65536))
    assert _eca(mysql["Resources"]["SourceEndpoint"]["Properties"], False) is None


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("max_bytes", [None, 65536])
def test_the_secrets_manager_endpoint_override_is_appended_to_both_endpoints_only_when_set(
    engine, max_bytes
):
    inputs = _with_unbounded_varchar(make_step1_inputs(engine), max_bytes)
    template = render_dms_template(inputs)
    param = template["Parameters"]["SecretsManagerEndpointOverride"]
    assert param["Default"] == "" and re.fullmatch(param["AllowedPattern"], OVERRIDE)
    assert template["Conditions"]["HasSecretsManagerEndpointOverride"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "SecretsManagerEndpointOverride"}, ""]}]
    }
    raised = engine == "aurora_postgresql" and max_bytes is not None
    for name in ("SourceEndpoint", "TargetEndpoint"):
        props = template["Resources"][name]["Properties"]
        base = ["unboundedVarcharMaxSize=65536"] if raised and name == "SourceEndpoint" else []
        assert _eca(props, False) == (";".join(base) or None), name
        assert _eca(props, True) == ";".join([*base, f"secretsManagerEndpointOverride={OVERRIDE}"])


def test_secrets_manager_reachability_is_in_the_network_parameters():
    params = render_dms_template(make_step1_inputs())["Parameters"]
    assert (
        "Secrets Manager interface endpoint with private DNS, or a NAT route"
        in params["SubnetIds"]["Description"]
    )
    assert "egress on 443" in params["SecurityGroupIds"]["Description"]


def test_a_postgresql_source_captures_no_ddl_and_sends_heartbeats():
    resources = render_dms_template(make_step1_inputs("aurora_postgresql"))["Resources"]
    settings = resources["SourceEndpoint"]["Properties"]["PostgreSqlSettings"]
    assert settings["CaptureDdls"] is False and settings["HeartbeatEnable"] is True
    target = resources["TargetEndpoint"]["Properties"]["PostgreSqlSettings"]
    assert "CaptureDdls" not in target and "HeartbeatEnable" not in target
    mysql = render_dms_template(make_step1_inputs("aurora_mysql"))["Resources"]
    assert "CaptureDdls" not in mysql["SourceEndpoint"]["Properties"]["MySqlSettings"]


def test_the_task_identifier_pattern_is_dms_s_and_the_default_matches_it():
    param = render_dms_template(make_step1_inputs())["Parameters"]["TaskIdentifier"]
    assert param["AllowedPattern"] == "^[a-zA-Z][a-zA-Z0-9]*(?:-[a-zA-Z0-9]+)*$"
    assert param["MaxLength"] == 255
    for bad in ("dm--x", "dm-x-", "1dm", "dm_x"):
        assert not re.fullmatch(param["AllowedPattern"], bad)
    assert re.fullmatch(param["AllowedPattern"], param["Default"])


def test_a_hyphenated_mysql_database_name_renders_and_every_default_matches_its_pattern():
    params = render_dms_template(_with_database(make_step1_inputs("aurora_mysql"), "my-shop"))[
        "Parameters"
    ]
    assert params["TargetDatabaseName"]["Default"] == "my-shop"
    for name, p in params.items():
        if "AllowedPattern" in p and "Default" in p:
            assert re.fullmatch(p["AllowedPattern"], str(p["Default"])), name


def test_a_default_that_breaks_its_own_pattern_is_refused_at_render_time():
    with pytest.raises(DmsStepError, match="my-shop"):
        render_dms_template(_with_database(make_step1_inputs("aurora_postgresql"), "my-shop"))


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


@pytest.mark.parametrize("engine", ENGINES)
def test_the_stack_is_exactly_the_six_resources_and_no_iam_wildcard(engine):
    resources = render_dms_template(make_step1_inputs(engine))["Resources"]
    assert {n: r["Type"] for n, r in resources.items()} == {
        "SecretsAccessRole": "AWS::IAM::Role",  # pragma: allowlist secret
        "SubnetGroup": "AWS::DMS::ReplicationSubnetGroup",
        "ReplicationInstance": "AWS::DMS::ReplicationInstance",
        "SourceEndpoint": "AWS::DMS::Endpoint",
        "TargetEndpoint": "AWS::DMS::Endpoint",
        "ReplicationTask": "AWS::DMS::ReplicationTask",
    }
    assert not [t for t in (r["Type"] for r in resources.values())
                if t.startswith("Custom::") or "CustomResource" in t or "Lambda" in t]  # fmt: skip
    role = resources["SecretsAccessRole"]["Properties"]
    statements = [*role["AssumeRolePolicyDocument"]["Statement"],
                  *role["Policies"][0]["PolicyDocument"]["Statement"]]  # fmt: skip
    for statement in statements:
        if "Fn::If" in statement:
            statement = statement["Fn::If"][1]
        for field in ("Action", "Resource"):
            assert not [s for s in _strings(statement.get(field)) if "*" in s], statement


def test_the_role_carries_the_cfn_nag_w76_reason():
    role = render_dms_template(make_step1_inputs())["Resources"]["SecretsAccessRole"]
    rules = role["Metadata"]["cfn_nag"]["rules_to_suppress"]
    assert [r["id"] for r in rules] == ["W76"] and all(r["reason"] for r in rules)


def test_the_role_reads_only_the_two_secrets():
    role = render_dms_template(make_step1_inputs())["Resources"]["SecretsAccessRole"]["Properties"]
    assert "RoleName" not in role
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    secrets = statements[0]
    assert sorted(secrets["Action"]) == [
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    ]
    assert secrets["Resource"] == [{"Ref": "SourceSecretArn"}, {"Ref": "TargetSecretArn"}]
    keys = [s["Fn::If"] for s in statements[1:]]
    assert [(k[0], k[1]["Resource"], k[2]) for k in keys] == [
        (cond, {"Ref": param}, {"Ref": "AWS::NoValue"})
        for cond, param in [("HasSourceSecretKey", "SourceSecretKmsKeyArn"),
                            ("HasSourceSecretKey", "SourceSecretKmsKeyArn"),
                            ("HasTargetSecretKey", "TargetSecretKmsKeyArn"),
                            ("HasTargetSecretKey", "TargetSecretKmsKeyArn")]
    ]  # fmt: skip
    decrypts = [k[1] for k in keys if k[1]["Action"] == "kms:Decrypt"]
    assert len(decrypts) == 2 and all(
        d["Condition"]["StringEquals"]["kms:ViaService"]
        == {"Fn::Sub": "secretsmanager.${AWS::Region}.amazonaws.com"}
        for d in decrypts
    )
    assert {k[1]["Action"] for k in keys} == {"kms:Decrypt", "kms:DescribeKey"}
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": {"Fn::Sub": "dms.${AWS::Region}.amazonaws.com"}}
    # The DMS guide's secret access role trusts the regional principal with no condition.
    assert "Condition" not in trust


def test_the_account_wide_dms_roles_are_named_not_created():
    template = render_dms_template(make_step1_inputs())
    roles = [r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1 and "RoleName" not in roles[0]["Properties"]
    for name in ("dms-vpc-role", "dms-cloudwatch-logs-role"):
        assert name in template["Description"]
        assert name in template["Metadata"]["PrerequisiteRoles"]
    assert "dms-vpc-role" not in json.dumps(template["Resources"])


def test_the_task_is_full_load_and_cdc_with_the_compiled_mappings_and_settings():
    inputs = make_step1_inputs()
    task = render_dms_template(inputs)["Resources"]["ReplicationTask"]["Properties"]
    assert task["MigrationType"] == "full-load-and-cdc"
    assert json.loads(task["TableMappings"]) == table_mappings(inputs)
    assert json.loads(task["ReplicationTaskSettings"]) == task_settings(inputs)
    assert {"Key": CONTRACT_TAG, "Value": inputs.contract_sha256} in task["Tags"]


def test_the_replication_instance_runs_the_catalogs_engine_version():
    template = render_dms_template(make_step1_inputs())
    param = template["Parameters"]["EngineVersion"]
    engine = dms_sizing().engine
    assert param["Default"] == engine.version == "3.6.1"
    assert engine.why in param["Description"]
    assert template["Resources"]["ReplicationInstance"]["Properties"]["EngineVersion"] == {
        "Ref": "EngineVersion"
    }


def test_a_plan_written_before_the_engine_version_falls_back_to_the_catalog():
    inputs = make_step1_inputs()
    old = inputs.plan.replication.model_copy(
        update={"engine_version": None, "engine_version_why": None}
    )
    inputs = dataclasses.replace(inputs, plan=inputs.plan.model_copy(update={"replication": old}))
    assert render_dms_template(inputs)["Parameters"]["EngineVersion"]["Default"] == "3.6.1"


@pytest.mark.parametrize(
    "value, ok",
    [
        ("3.6.1", True),
        ("3.5.4", True),
        ("3.10.12", True),
        ("3.6", False),
        ("3.6.1\n", False),
        ("latest", False),
        ("3.6.1; x", False),
        ("", False),
    ],
)
def test_the_engine_version_pattern_full_matches_a_version_only(value, ok):
    pattern = render_dms_template(make_step1_inputs())["Parameters"]["EngineVersion"][
        "AllowedPattern"
    ]
    assert bool(re.fullmatch(pattern, value)) is ok


def test_the_compiled_json_is_byte_stable():
    first = render_dms_template(make_step1_inputs())["Resources"]["ReplicationTask"]["Properties"]
    again = render_dms_template(make_step1_inputs())["Resources"]["ReplicationTask"]["Properties"]
    assert first["TableMappings"] == again["TableMappings"]
    assert first["ReplicationTaskSettings"] == again["ReplicationTaskSettings"]
    assert dump_template(render_dms_template(make_step1_inputs())) == dump_template(
        render_dms_template(make_step1_inputs())
    )


def test_the_sized_instance_is_the_default_and_multi_az_defaults_to_true():
    inputs = make_step1_inputs()
    template = render_dms_template(inputs)
    params, instance = (
        template["Parameters"],
        template["Resources"]["ReplicationInstance"]["Properties"],
    )
    assert params["InstanceClass"]["Default"] == inputs.plan.replication.sizing.instance_class
    assert inputs.plan.replication.sizing.reason in params["InstanceClass"]["Description"]
    assert (
        params["AllocatedStorage"]["Default"] == inputs.plan.replication.sizing.allocated_storage_gb
    )
    assert params["MultiAZ"] == {"Type": "String", "Description": params["MultiAZ"]["Description"],
                                 "Default": "true", "AllowedValues": ["true", "false"]}  # fmt: skip
    assert instance["PubliclyAccessible"] is False
    assert instance["MultiAZ"] == {"Fn::If": ["IsMultiAZ", True, False]}
    assert instance["KmsKeyId"] == {"Fn::If": ["HasReplicationKey", {"Ref": "ReplicationKmsKeyArn"},
                                               {"Ref": "AWS::NoValue"}]}  # fmt: skip
    assert instance["EngineVersion"] == {"Ref": "EngineVersion"}


def test_every_taggable_resource_is_tagged_and_the_outputs_are_the_named_ones():
    inputs = make_step1_inputs()
    template = render_dms_template(inputs)
    for name, resource in template["Resources"].items():
        tags = {t["Key"]: t["Value"] for t in resource["Properties"].get("Tags", [])}
        assert tags.get("modernizer:job-id") == "job1" and tags.get("modernizer:step") == "1", name
        assert tags.get(CONTRACT_TAG) == inputs.contract_sha256, name
    assert tuple(template["Outputs"]) == STACK_OUTPUTS


def test_the_template_carries_the_generator_marker():
    template = render_dms_template(make_step1_inputs())
    assert "scripts/render_dms_step.py" in template["Description"]
    assert len(template["Description"]) <= 1024


@pytest.mark.parametrize(
    ("job_id", "expected"),
    [("job1", "dm-job1-step-1"), ("a_b..c", "dm-a-b-c-step-1"), ("x--y-", "dm-x-y-step-1")],
)
def test_task_identifier_is_letters_digits_and_single_hyphens(job_id, expected):
    assert task_identifier(job_id) == expected


# --- The suggested Aurora template and the DDL file (Task 7) ---------------------------------

AURORA_DEPLOYMENT_PARAMETERS = {"SubnetIds", "SecurityGroupIds", "EngineVersion"}
ZERO_DATE_PHRASE = (
    "{n} date and time columns can hold zero dates (MySQL accepts them unless its sql_mode "
    "forbids them); {m} of them default to `0000-00-00`"
)


def _flat(text: str, marker: str) -> str:
    """The comment block's words without their wrap markers."""
    return " ".join(w for w in text.split() if w != marker)


@pytest.mark.parametrize("engine", ENGINES)
def test_aurora_template_lists_every_required_setting(engine):
    inputs = make_step1_inputs(engine)
    text = render_files(inputs)[AURORA_TEMPLATE]
    head = text.split("AWSTemplateFormatVersion")[0]
    assert "YOUR RESPONSIBILITY" in head
    assert CUSTOMER_RESPONSIBILITY.split(". ")[0] in _flat(head, "#")
    settings = required_settings(engine, inputs.plan.replication.target_engine_line)
    names = {s.setting for s in settings}
    assert set(REQUIRED_CLUSTER_PARAMETERS[engine]) <= names
    assert set(REQUIRED_INSTANCE_PARAMETERS[engine]) <= names
    assert {"Engine and version", "Encryption at rest", "Writer endpoint", "DMS user"} <= names
    template = render_aurora_template(inputs)
    meta = template["Metadata"]["Modernizer::CustomerResponsibility"]
    assert meta["Disclaimer"] == CUSTOMER_RESPONSIBILITY
    assert [r["Setting"] for r in meta["RequiredSettings"]] == [s.setting for s in settings]
    assert all(r["Reason"] for r in meta["RequiredSettings"])
    flat = _flat(head, "#")
    for s in settings:
        assert s.setting in head and s.reason in flat, s.setting


@pytest.mark.parametrize("engine", ENGINES)
def test_the_suggested_cluster_has_the_settings_it_lists(engine):
    template = render_aurora_template(make_step1_inputs(engine))
    resources = template["Resources"]
    assert (
        resources["ClusterParameterGroup"]["Properties"]["Parameters"]
        == REQUIRED_CLUSTER_PARAMETERS[engine]
    )
    cluster = resources["Cluster"]["Properties"]
    assert cluster["StorageEncrypted"] is True and cluster["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in cluster and cluster["DeletionProtection"] is True
    assert cluster["BackupRetentionPeriod"] == {"Ref": "BackupRetentionDays"}
    retention = template["Parameters"]["BackupRetentionDays"]
    assert retention["Default"] >= 7 and retention["MinValue"] >= 7
    assert resources["Cluster"]["DeletionPolicy"] == "Snapshot"
    writer = resources["Writer"]["Properties"]
    assert writer["PubliclyAccessible"] is False
    if REQUIRED_INSTANCE_PARAMETERS[engine]:
        assert writer["DBParameterGroupName"] == {"Ref": "InstanceParameterGroup"}
        group = resources["InstanceParameterGroup"]
        assert group["Type"] == "AWS::RDS::DBParameterGroup"
        assert group["Properties"]["Parameters"] == REQUIRED_INSTANCE_PARAMETERS[engine]
        assert group["Properties"]["Family"] == {"Ref": "ParameterGroupFamily"}
    else:
        assert "InstanceParameterGroup" not in resources and "DBParameterGroupName" not in writer


def test_the_aurora_mysql_target_turns_local_infile_on_in_the_instance_parameters():
    # local_infile is an instance-level Aurora MySQL parameter: a cluster parameter group refuses it.
    assert REQUIRED_INSTANCE_PARAMETERS["aurora_mysql"] == {"local_infile": "1"}
    assert "local_infile" not in REQUIRED_CLUSTER_PARAMETERS["aurora_mysql"]
    assert REQUIRED_INSTANCE_PARAMETERS["aurora_postgresql"] == {}
    setting = next(
        s for s in required_settings("aurora_mysql", "8.0") if s.setting == "local_infile"
    )
    assert "LOAD DATA" in setting.reason and setting.value == "1"


@pytest.mark.parametrize("engine", ENGINES)
def test_no_account_region_or_endpoint_defaults_in_the_aurora_template(engine):
    template = render_aurora_template(make_step1_inputs(engine))
    params = template["Parameters"]
    assert AURORA_DEPLOYMENT_PARAMETERS <= set(params)
    assert not [p for p in AURORA_DEPLOYMENT_PARAMETERS if "Default" in params[p]]
    assert not [p for p in params if re.search(r"(?i)password", p)]
    for name, p in params.items():
        if "AllowedPattern" in p and "Default" in p:
            assert re.fullmatch(p["AllowedPattern"], str(p["Default"])), name
    _no_account_region_or_endpoint(dump_template(template))
    _no_account_region_or_endpoint(render_files(make_step1_inputs(engine))[AURORA_TEMPLATE])


@pytest.mark.parametrize(
    ("engine", "pattern", "family"),
    [
        ("aurora_postgresql", r"^16\..+$", "aurora-postgresql16"),
        ("aurora_mysql", r"^8\.0\..+$", "aurora-mysql8.0"),
    ],
)
def test_the_engine_version_must_be_on_the_design_line(engine, pattern, family):
    params = render_aurora_template(make_step1_inputs(engine))["Parameters"]
    assert params["EngineVersion"]["AllowedPattern"] == pattern
    assert params["ParameterGroupFamily"]["Default"] == family
    assert family in params["ParameterGroupFamily"]["AllowedValues"]


def test_an_unknown_line_leaves_the_family_to_the_customer():
    inputs = make_step1_inputs("aurora_mysql")
    replication = inputs.plan.replication.model_copy(update={"target_engine_line": None})
    inputs = dataclasses.replace(
        inputs, plan=inputs.plan.model_copy(update={"replication": replication})
    )
    params = render_aurora_template(inputs)["Parameters"]
    assert "Default" not in params["ParameterGroupFamily"]
    assert params["ParameterGroupFamily"]["AllowedValues"] == ["aurora-mysql8.0", "aurora-mysql8.4"]


def test_the_mysql_cluster_creates_no_database_and_the_ddl_does():
    inputs = _with_database(make_step1_inputs("aurora_mysql"), "my-shop")
    template = render_aurora_template(inputs)
    assert "DatabaseName" not in template["Parameters"]
    assert "DatabaseName" not in template["Resources"]["Cluster"]["Properties"]
    ddl = render_ddl(inputs)
    assert "CREATE DATABASE IF NOT EXISTS `my-shop`;\nUSE `my-shop`;\n" in ddl
    assert ddl.index("USE `my-shop`;") < ddl.index("CREATE TABLE")


def test_the_postgresql_cluster_creates_the_source_named_database():
    template = render_aurora_template(make_step1_inputs("aurora_postgresql"))
    assert template["Parameters"]["DatabaseName"]["Default"] == "shop"
    assert template["Resources"]["Cluster"]["Properties"]["DatabaseName"] == {"Ref": "DatabaseName"}


@pytest.mark.parametrize("engine", ENGINES)
def test_the_ddl_file_is_the_design_ddl_with_instructions(engine):
    inputs = make_step1_inputs(engine)
    ddl = render_ddl(inputs)
    assert ddl.startswith("-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
                          "-- SPDX-License-Identifier: MIT-0\n")  # fmt: skip
    assert inputs.ddl.strip() in ddl and "YOUR RESPONSIBILITY" in ddl and "DO_NOTHING" in ddl
    flat = _flat(ddl, "--")
    assert "Keep the declared column names" in flat
    assert "no character set or collation" in flat
    assert "indexes of a saved schema design are not in this file" in flat
    assert "Start the DMS task only after this file has run" in flat
    assert "scripts/render_dms_step.py" in flat
    head = ddl[: ddl.index("CREATE TABLE")]
    assert all(line.startswith("--") or not line.strip() for line in head.splitlines()
               if not line.startswith(("CREATE DATABASE", "USE ", "CREATE SCHEMA", "SET search_path")))  # fmt: skip


def _last_create_table(ddl: str) -> int:
    return ddl.rindex("CREATE TABLE")


def test_the_mysql_ddl_gives_the_dms_user_its_grants_commented_out():
    ddl = render_ddl(make_step1_inputs("aurora_mysql"))
    assert "-- CREATE USER 'dms_user'@'%' IDENTIFIED BY '<password>';" in ddl
    assert "mysql_native_password" not in ddl
    assert ddl.index("-- CREATE USER") > _last_create_table(ddl)
    assert ("-- GRANT ALTER, CREATE, DROP, INDEX, INSERT, UPDATE, DELETE, SELECT, CREATE TEMPORARY TABLES "
            "ON `shop`.* TO 'dms_user'@'%';") in ddl  # fmt: skip
    assert "-- GRANT ALL PRIVILEGES ON `awsdms_control`.* TO 'dms_user'@'%';" in ddl
    assert ddl.index("YOUR RESPONSIBILITY") < ddl.index("-- CREATE USER")
    assert "\nCREATE USER" not in ddl and "\nGRANT" not in ddl


def test_the_postgresql_ddl_gives_the_dms_user_its_grants_commented_out():
    ddl = render_ddl(make_step1_inputs("aurora_postgresql"))
    assert "-- CREATE USER dms_user WITH PASSWORD '<password>';" in ddl
    assert (
        '-- GRANT UPDATE, INSERT, SELECT, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA "public" TO dms_user;'
        in ddl
    )
    # The grants follow the tables and the schema they name: ON ALL TABLES covers existing tables only.
    assert ddl.index("ON ALL TABLES IN SCHEMA") > _last_create_table(ddl)
    assert ddl.index('-- GRANT USAGE, CREATE ON SCHEMA "public"') > ddl.index(
        "CREATE SCHEMA IF NOT EXISTS"
    )
    assert ('-- ALTER DEFAULT PRIVILEGES IN SCHEMA "public" GRANT UPDATE, INSERT, SELECT, DELETE, TRUNCATE '
            "ON TABLES TO dms_user;") in ddl  # fmt: skip
    assert "-- GRANT rds_superuser TO dms_user;" in ddl
    assert "-- GRANT SET ON PARAMETER session_replication_role TO dms_user;" in ddl
    assert "if your Aurora version allows it" in _flat(ddl, "--")
    # Uncommenting the statements leaves the explanation a comment.
    assert "-- -- Narrower, if your Aurora version allows it" in ddl
    assert ddl.index("-- GRANT rds_superuser TO dms_user;") < ddl.index(
        "-- GRANT SET ON PARAMETER session_replication_role TO dms_user;"
    )
    assert "session_replication_role" in _flat(ddl, "--")
    assert "\nCREATE USER" not in ddl and "\nGRANT" not in ddl and "\nALTER DEFAULT" not in ddl


def test_on_the_8_4_line_the_dms_user_uses_mysql_native_password():
    inputs = make_step1_inputs("aurora_mysql")
    replication = inputs.plan.replication.model_copy(update={"target_engine_line": "8.4"})
    inputs = dataclasses.replace(
        inputs, plan=inputs.plan.model_copy(update={"replication": replication})
    )
    ddl = render_ddl(inputs)
    assert (
        "-- CREATE USER 'dms_user'@'%' IDENTIFIED WITH mysql_native_password BY '<password>';"
        in ddl
    )
    assert "IDENTIFIED BY '<password>'" not in ddl.replace("mysql_native_password BY", "")


def test_postgresql_ddl_recommends_replica_identity_full_per_table():
    inputs = make_step1_inputs("aurora_postgresql")
    ddl = render_ddl(inputs)
    for name in ("audit_log", "customers", "orders"):
        assert f'-- ALTER TABLE "public"."{name}" REPLICA IDENTITY FULL;' in ddl
    flat = _flat(ddl, "--")
    assert "WAL" in flat and "updates and deletes" in flat
    assert ddl.index("YOUR RESPONSIBILITY") < ddl.index("REPLICA IDENTITY FULL")
    assert 'SET search_path TO "public";' in ddl
    assert '\nALTER TABLE "public"."audit_log" REPLICA' not in ddl
    assert "REPLICA IDENTITY" not in render_ddl(make_step1_inputs("aurora_mysql"))


@pytest.mark.passes_on_main
def test_the_ddl_lists_the_skipped_tables_and_unsupported_fields_with_their_reasons():
    b = make_bundle()
    hook = UnsupportedOp(op="unsupported", description="no Aurora type for geometry")
    error = Finding(
        code="unknown_table", severity="error", message="shop.audit_log is not in the collection"
    )
    rules = []
    for r in b.contract.rules:
        if r.target_table == "orders":
            r = r.model_copy(
                update={"fields": [r.fields[0], r.fields[1].model_copy(update={"op": hook})]}
            )
        if r.target_table == "audit_log":
            r = r.model_copy(update={"status": "blocked", "findings": [error]})
        rules.append(r)
    inputs = rebuilt(
        b, rules, unsupported_count=1, blocked_rule_count=1, status="has_blocked_rules"
    )
    flat = _flat(render_ddl(inputs), "--")
    assert "shop.audit_log: shop.audit_log is not in the collection" in flat
    assert "shop.orders.note: no Aurora type for geometry" in flat
    none = _flat(render_ddl(make_step1_inputs()), "--")
    assert "Tables left out: none." in none and "Unsupported fields: none." in none


@pytest.mark.passes_on_main
def test_zero_dates_drop_the_no_zero_date_modes_and_say_why():
    inputs = with_zero_dates()
    expected = (
        "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
    )
    assert MYSQL_80_DEFAULT_SQL_MODE == (
        "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
        "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
    )
    template = render_aurora_template(inputs)
    params = template["Resources"]["InstanceParameterGroup"]["Properties"]["Parameters"]
    assert params == {"local_infile": "1", "sql_mode": expected}
    # sql_mode is instance-level on Aurora MySQL: never in the cluster parameter group.
    assert template["Resources"]["ClusterParameterGroup"]["Properties"]["Parameters"] == (
        REQUIRED_CLUSTER_PARAMETERS["aurora_mysql"]
    )
    setting = next(r for r in template["Metadata"]["Modernizer::CustomerResponsibility"]["RequiredSettings"]
                   if r["Setting"] == "sql_mode")  # fmt: skip
    assert setting["Value"] == expected
    phrase = ZERO_DATE_PHRASE.format(n=4, m=1)
    assert phrase in setting["Reason"] and "recorded as NULL" in setting["Reason"]
    assert "every instance of the cluster" in setting["Reason"]
    head = render_files(inputs)[AURORA_TEMPLATE].split("AWSTemplateFormatVersion")[0]
    assert "sql_mode" in head and phrase in _flat(head, "#")
    ddl = _flat(render_ddl(inputs), "--")
    assert phrase in ddl and "NO_ZERO_DATE" in ddl and "recorded as NULL" in ddl
    assert "hold zero dates (0000-00-00) in the source" not in ddl


@pytest.mark.passes_on_main
def test_zero_dates_come_from_date_typed_copy_fields_whatever_the_message():
    one = with_zero_dates(tables=("orders",), columns=(("shipped", "date"),), defaults=())
    group = render_aurora_template(one)["Resources"]["InstanceParameterGroup"]["Properties"]
    assert group["Parameters"]["sql_mode"] == ZERO_DATE_SQL_MODE
    reason = next(s.reason for s in required_settings("aurora_mysql", "8.0", zero_dates=(1, 0))
                  if s.setting == "sql_mode")  # fmt: skip
    assert reason.startswith("1 date and time column can hold zero dates")
    assert "0 of them default to `0000-00-00`" in reason


@pytest.mark.passes_on_main
def test_a_zero_date_looking_message_on_a_varchar_field_is_not_a_zero_date():
    b = make_bundle()
    other = Finding(code="copy_edge_value", severity="warning", table_id="shop.orders", column="code",
                    message="`0000-00-00 00:00:00` in `code` is copied as is. Aurora MySQL rejects zero dates")  # fmt: skip
    rules = [r.model_copy(update={"fields": [*r.fields, edge_field("shop.orders", "code", "VARCHAR(19)")],
                                  "findings": [other]}) if r.target_table == "orders" else r
             for r in b.contract.rules]  # fmt: skip
    group = render_aurora_template(rebuilt(b, rules))["Resources"]["InstanceParameterGroup"]
    assert "sql_mode" not in group["Properties"]["Parameters"]


@pytest.mark.passes_on_main
def test_a_date_field_without_a_copy_edge_value_finding_is_not_counted():
    b = make_bundle()
    rules = [r.model_copy(update={"fields": [*r.fields, edge_field("shop.orders", "made", "DATETIME")]})
             if r.target_table == "orders" else r for r in b.contract.rules]  # fmt: skip
    group = render_aurora_template(rebuilt(b, rules))["Resources"]["InstanceParameterGroup"]
    assert "sql_mode" not in group["Properties"]["Parameters"]


def test_the_loader_counts_the_zero_date_defaults_from_the_collector(tmp_path):
    store = write_job(tmp_path, dates=True)
    inputs = load_step1(store, "shop", "job1")
    assert zero_date_columns(inputs) == [("shop.orders", "created"), ("shop.orders", "shipped")]
    assert inputs.zero_date_defaults == ("shop.orders.created",)
    reason = next(r["Reason"] for r in render_aurora_template(inputs)["Metadata"][
        "Modernizer::CustomerResponsibility"]["RequiredSettings"] if r["Setting"] == "sql_mode")  # fmt: skip
    assert ZERO_DATE_PHRASE.format(n=2, m=1) in reason
    plain = load_step1(write_job(tmp_path / "plain"), "shop", "job1")
    assert plain.zero_date_defaults == () and zero_date_columns(plain) == []


def test_without_zero_dates_sql_mode_is_left_at_the_default():
    inputs = make_step1_inputs("aurora_mysql")
    template = render_aurora_template(inputs)
    assert (
        "sql_mode"
        not in template["Resources"]["InstanceParameterGroup"]["Properties"]["Parameters"]
    )
    assert "sql_mode" not in {r["Setting"] for r in
                              template["Metadata"]["Modernizer::CustomerResponsibility"]["RequiredSettings"]}  # fmt: skip
    assert "NO_ZERO_DATE" not in render_ddl(inputs)


@pytest.mark.passes_on_main
def test_zero_dates_of_a_blocked_table_or_a_postgresql_target_change_nothing():
    blocked = with_zero_dates(tables=("orders",))
    error = Finding(code="unknown_table", severity="error", message="gone")
    b = make_bundle()
    rules = [r.model_copy(update={"status": "blocked", "findings": [*r.findings, error]})
             if r.target_table == "orders" else r for r in blocked.contract.rules]  # fmt: skip
    inputs = rebuilt(b, rules, blocked_rule_count=1, status="has_blocked_rules")
    assert "sql_mode" not in render_aurora_template(inputs)["Resources"]["InstanceParameterGroup"][
        "Properties"]["Parameters"]  # fmt: skip
    pg = render_aurora_template(with_zero_dates("aurora_postgresql"))["Resources"]
    assert "InstanceParameterGroup" not in pg


@pytest.mark.passes_on_main
def test_a_copy_edge_value_on_a_text_field_is_not_a_zero_date():
    b = make_bundle()
    other = Finding(code="copy_edge_value", severity="warning", table_id="shop.orders", column="note",
                    message="`0000-00-00` in `note` is copied as is.")  # fmt: skip
    rules = [r.model_copy(update={"findings": [other]}) if r.target_table == "orders" else r
             for r in b.contract.rules]  # fmt: skip
    group = render_aurora_template(rebuilt(b, rules))["Resources"]["InstanceParameterGroup"]
    assert "sql_mode" not in group["Properties"]["Parameters"]


@pytest.mark.parametrize("engine", ENGINES)
def test_render_files_gives_the_three_files_byte_stable(engine):
    files = render_files(make_step1_inputs(engine))
    assert set(files) == {AURORA_TEMPLATE, DDL_FILE, DMS_TEMPLATE}
    assert files == render_files(make_step1_inputs(engine))
    assert files[DMS_TEMPLATE].startswith("# GENERATED by scripts/render_dms_step.py")
    assert files[AURORA_TEMPLATE].startswith("# GENERATED by scripts/render_dms_step.py")
    assert files[DDL_FILE] == render_ddl(make_step1_inputs(engine))


@pytest.mark.parametrize("engine", ENGINES)
def test_nothing_in_the_aurora_template_runs_inside_the_cluster(engine):
    resources = render_aurora_template(make_step1_inputs(engine))["Resources"]
    types = {r["Type"] for r in resources.values()}
    assert types <= {"AWS::RDS::DBClusterParameterGroup", "AWS::RDS::DBParameterGroup",
                     "AWS::RDS::DBSubnetGroup", "AWS::RDS::DBCluster", "AWS::RDS::DBInstance"}  # fmt: skip


def test_the_writer_carries_the_checkov_enhanced_monitoring_reason():
    writer = render_aurora_template(make_step1_inputs())["Resources"]["Writer"]
    skips = writer["Metadata"]["checkov"]["skip"]
    assert [k["id"] for k in skips] == ["CKV_AWS_118"] and all(k["comment"] for k in skips)
    assert "MonitoringInterval" not in writer["Properties"]


@pytest.mark.parametrize("engine", ENGINES)
def test_the_settings_say_what_the_review_asked(engine):
    rows = {s.setting: s for s in required_settings(engine, "8.0" if engine == "aurora_mysql" else "16",
                                                    zero_dates=(1, 0))}  # fmt: skip
    assert "cannot be turned on in place" in rows["Encryption at rest"].reason
    if engine == "aurora_mysql":
        assert (
            rows["TLS"].value == "available; require_secure_transport may be ON (the 8.4 default)"
        )
        for name in ("local_infile", "sql_mode"):
            assert (
                "every instance of the cluster must use this DB parameter group"
                in rows[name].reason
            )
    text = " ".join(f"{s.value} {s.reason}" for s in rows.values())
    assert "ADR-033" not in text and "Decision 10" not in text
    assert "later steps (change capture)" in text.lower()
    assert "ADR-033" not in render_ddl(make_step1_inputs(engine))


# ---------------------------------------------------------------------------
# AllowedPattern is matched against the whole value (#1861)
# ---------------------------------------------------------------------------

_ACCOUNT = "123456789012"
_REPRESENTATIVE = {
    "SecretsManagerEndpointOverride": "vpce-0a1b2c3d-abcd1234.secretsmanager.us-east-1.vpce.amazonaws.com",  # pragma: allowlist secret
    "SourceSecretArn": f"arn:aws:secretsmanager:us-east-1:{_ACCOUNT}:secret:dms/source-AbCdEf",
    "TargetSecretArn": f"arn:aws:secretsmanager:us-east-1:{_ACCOUNT}:secret:dms/target-AbCdEf",
    "SourceSecretKmsKeyArn": f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    "TargetSecretKmsKeyArn": f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    "ReplicationKmsKeyArn": f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    "KmsKeyArn": f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    "SourceCertificateArn": f"arn:aws:dms:us-east-1:{_ACCOUNT}:cert:ABCDEFGHIJKLMNOP",
    "TargetCertificateArn": f"arn:aws:dms:us-east-1:{_ACCOUNT}:cert:ABCDEFGHIJKLMNOP",
    "SourceDatabaseName": "shop",
    "TargetDatabaseName": "shop",
    "DatabaseName": "shop",
    "TaskIdentifier": "dm-job1-step-1",
    "MasterUsername": "dbadmin",
}
_REAL_ENGINE_VERSION = {"aurora_mysql": "8.0.mysql_aurora.3.14.0", "aurora_postgresql": "16.4"}
_OTHER_ENGINE_VERSION = {"aurora_mysql": ["8.4.mysql_aurora.4.0.0", "5.7.mysql_aurora.2.12.0"], "aurora_postgresql": ["15.6", "17.1"]}  # fmt: skip


def _aurora_engine_version_pattern(engine: str) -> str:
    parameters = render_aurora_template(make_step1_inputs(engine))["Parameters"]
    return str(parameters["EngineVersion"]["AllowedPattern"])


@pytest.mark.parametrize("engine", ENGINES)
def test_aurora_engine_version_pattern_accepts_a_real_version(engine):
    pattern = _aurora_engine_version_pattern(engine)
    assert re.fullmatch(pattern, _REAL_ENGINE_VERSION[engine]), pattern


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", ENGINES)
def test_aurora_engine_version_pattern_refuses_another_line(engine):
    pattern = _aurora_engine_version_pattern(engine)
    for other in _OTHER_ENGINE_VERSION[engine]:
        assert not re.fullmatch(pattern, other), (pattern, other)


@pytest.mark.parametrize("engine", ENGINES)
def test_aurora_engine_version_fallback_pattern_accepts_a_version(engine):
    inputs = make_step1_inputs(engine)
    replication = inputs.plan.replication.model_copy(update={"target_engine_line": None})
    inputs = dataclasses.replace(
        inputs, plan=inputs.plan.model_copy(update={"replication": replication})
    )
    pattern = render_aurora_template(inputs)["Parameters"]["EngineVersion"]["AllowedPattern"]
    assert re.fullmatch(pattern, _REAL_ENGINE_VERSION[engine]), pattern
    assert not re.fullmatch(pattern, "latest")


def _every_pattern(engine: str):
    inputs = make_step1_inputs(engine)
    for label, template in (
        ("dms", render_dms_template(inputs)),
        ("aurora", render_aurora_template(inputs)),
    ):
        for name, param in template["Parameters"].items():
            if "AllowedPattern" in param:
                yield label, name, param["AllowedPattern"]


@pytest.mark.passes_on_main  # guards every pattern; the new EngineVersion has its own test above
@pytest.mark.parametrize("engine", ENGINES)
def test_every_rendered_allowed_pattern_full_matches_a_valid_value(engine):
    """CloudFormation matches the whole value; ``re.match`` would accept a prefix."""
    unmatched = []
    for label, name, pattern in _every_pattern(engine):
        if name == "EngineVersion" and label == "dms":
            value = "3.6.1"
        elif name == "EngineVersion":
            value = _REAL_ENGINE_VERSION[engine]
        elif name == "InstanceClass":
            value = "dms.r5.large" if label == "dms" else "db.r6g.large"
        else:
            value = _REPRESENTATIVE.get(name)
        assert value is not None, f"{label}.{name} needs a representative value in this test"
        if not re.fullmatch(pattern, value):
            unmatched.append((label, name, pattern, value))
    assert not unmatched
