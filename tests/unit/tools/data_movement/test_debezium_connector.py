"""Debezium source connectors on MSK Connect (#1414, ADR-033): config and stacks.

No AWS call: the templates are checked with cfn-lint's Python API, and the
rendered examples next to them are the snapshots.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from cfnlint.api import ManualArgs, lint
from pydantic import ValidationError

from scripts import render_debezium_connector as script
from src.tools.data_movement import debezium_connector as dbz

REPO_ROOT = Path(__file__).resolve().parents[4]
STACK_DIR = REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement"
BUCKET_STACK = STACK_DIR / "debezium-plugin-bucket.yaml"
MYSQL_INPUT = STACK_DIR / "examples" / "wordpress-mysql.input.json"
PG_INPUT = STACK_DIR / "examples" / "wordpress-postgresql.input.json"


def _raw(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _spec(raw: dict[str, Any]) -> dbz.DebeziumConnectorInput:
    return dbz.DebeziumConnectorInput.model_validate(raw)


@pytest.fixture
def mysql_raw() -> dict[str, Any]:
    return _raw(MYSQL_INPUT)


@pytest.fixture
def pg_raw() -> dict[str, Any]:
    return _raw(PG_INPUT)


def _config(raw: dict[str, Any]) -> dict[str, Any]:
    return dbz.render_connector_configuration(_spec(raw))


# ---------------------------------------------------------------------------
# Connector configuration
# ---------------------------------------------------------------------------


COMMON_SETTINGS = {
    "tasks.max": "1",
    "tombstones.on.delete": "true",
    "heartbeat.interval.ms": "10000",
    "topic.prefix": "wordpress",
    "signal.enabled.channels": "source",
    "topic.creation.default.cleanup.policy": "compact",
}


def test_mysql_config_has_the_required_settings(mysql_raw):
    config = _config(mysql_raw)
    assert {k: config[k] for k in COMMON_SETTINGS} == COMMON_SETTINGS
    assert config["connector.class"] == "io.debezium.connector.mysql.MySqlConnector"
    assert config["snapshot.mode"] == "initial"
    assert config["signal.data.collection"] == "wordpress.debezium_signal"
    assert config["database.include.list"] == "wordpress"
    assert config["database.server.id"] == {"Ref": "DatabaseServerId"}
    assert config["message.key.columns"] == (
        "wordpress.wp_options:option_id;wordpress.wp_posts:ID;wordpress.wp_postmeta:meta_id;"
        "wordpress.wp_users:ID;wordpress.wp_usermeta:umeta_id;"
        "wordpress.wp_term_relationships:object_id,term_taxonomy_id"
    )
    # Schema history in Kafka, over IAM, under the topic prefix.
    assert config["schema.history.internal.kafka.topic"] == "wordpress.schema-history.wave-2"
    assert config["schema.history.internal.consumer.group.id"] == ("wordpress-wave-2-schemahistory")
    assert config["schema.history.internal.name"] == "wordpress-wave-2-schemahistory"
    assert config["schema.history.internal.kafka.bootstrap.servers"] == {"Ref": "BootstrapServers"}
    for client in ("producer", "consumer"):
        assert config[f"schema.history.internal.{client}.security.protocol"] == "SASL_SSL"
        assert config[f"schema.history.internal.{client}.sasl.mechanism"] == "AWS_MSK_IAM"
        assert "IAMLoginModule" in config[f"schema.history.internal.{client}.sasl.jaas.config"]


def test_postgresql_config_has_the_required_settings(pg_raw):
    config = _config(pg_raw)
    assert {k: config[k] for k in COMMON_SETTINGS} == COMMON_SETTINGS
    assert config["connector.class"] == "io.debezium.connector.postgresql.PostgresConnector"
    assert config["snapshot.mode"] == "no_data"
    assert config["plugin.name"] == "pgoutput"
    assert config["database.dbname"] == "wordpress"
    assert config["slot.name"] == "debezium_wordpress_wave_2"
    assert config["publication.name"] == "debezium_wordpress_wave_2_pub"
    assert config["publication.autocreate.mode"] == "filtered"
    assert config["signal.data.collection"] == "public.debezium_signal"
    assert config["message.key.columns"] == (
        "public.wp_options:option_id;public.wp_posts:id;public.wp_postmeta:meta_id;"
        "public.wp_term_relationships:object_id,term_taxonomy_id"
    )
    # PostgreSQL reads table schemas from the replication stream: no history topic.
    assert not [k for k in config if k.startswith("schema.history")]
    assert "database.server.id" not in config


ARN = "arn:aws:secretsmanager:eu-west-1:111122223333:secret:db/repl+user-AbC123"


def _encode(node: Any, arn: str) -> str:
    """Evaluate the Fn::Join/Fn::Split/Ref tree CloudFormation would evaluate."""
    if "Ref" in node:
        assert node == {"Ref": "DatabaseSecretArn"}
        return arn
    if "Fn::Join" in node:
        sep, inner = node["Fn::Join"]
        assert set(inner) == {"Fn::Split"}
        split_on, source = inner["Fn::Split"]
        return str(sep).join(_encode(source, arn).split(split_on))
    raise AssertionError(node)


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
def test_credentials_come_from_the_config_provider(fixture, request):
    config = _config(request.getfixturevalue(fixture))
    for key, field in (("database.user", "username"), ("database.password", "password")):
        # ``${!`` is Fn::Sub's literal ``${``: the worker resolves the secret.
        template, variables = config[key]["Fn::Sub"]
        assert template == "${!secretsmanager:${SecretArn}:" + field + "}"
        assert _encode(variables["SecretArn"], ARN) == (
            "arn%3Aaws%3Asecretsmanager%3Aeu-west-1%3A111122223333%3Asecret%3Adb/repl%2Buser-AbC123"
        )
    assert config["database.hostname"] == {"Ref": "DatabaseHost"}
    assert config["database.port"] == {"Ref": "DatabasePort"}


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
def test_every_table_topic_is_created_compacted_with_its_partitions(fixture, request):
    raw = request.getfixturevalue(fixture)
    config = _config(raw)
    groups = config["topic.creation.groups"].split(",")
    assert len(groups) == len(raw["hub"]["tables"])
    for group, hub in zip(groups, raw["hub"]["tables"], strict=True):
        assert re.fullmatch(config[f"topic.creation.{group}.include"], hub["topic"])
        assert config[f"topic.creation.{group}.cleanup.policy"] == "compact"
        assert config[f"topic.creation.{group}.partitions"] == str(hub["partitions"])
    assert config["topic.creation.default.replication.factor"] == {"Ref": "TopicReplicationFactor"}


def test_include_list_matches_only_the_plan_tables(mysql_raw):
    patterns = _config(mysql_raw)["table.include.list"].split(",")
    assert len(patterns) == 6
    assert any(re.fullmatch(p, "wordpress.wp_posts") for p in patterns)
    # Dots are escaped: a lookalike name does not match.
    assert not any(re.fullmatch(p, "wordpressXwp_posts") for p in patterns)
    assert not any(re.fullmatch(p, "wordpress.wp_comments") for p in patterns)


def test_default_topic_names_need_no_rename(mysql_raw):
    assert not [k for k in _config(mysql_raw) if k.startswith("transforms")]


def test_a_plan_topic_name_is_reached_by_a_rename(pg_raw):
    config = _config(pg_raw)
    assert config["transforms"] == "route1"
    assert config["transforms.route1.type"] == "org.apache.kafka.connect.transforms.RegexRouter"
    assert re.fullmatch(config["transforms.route1.regex"], "wordpress.public.wp_options")
    assert config["transforms.route1.replacement"] == "wordpress.options"


def test_table_without_a_primary_key_is_rejected(mysql_raw):
    mysql_raw["source"]["tables"][0]["primary_key_columns"] = []
    with pytest.raises(ValidationError, match="wordpress.wp_options has no primary key"):
        _spec(mysql_raw)


def test_postgresql_table_without_a_primary_key_is_rejected(pg_raw):
    pg_raw["source"]["tables"][1]["primary_key_columns"] = []
    with pytest.raises(ValidationError, match="public.wp_posts has no primary key"):
        _spec(pg_raw)


def test_key_columns_stand_in_for_a_missing_primary_key(mysql_raw):
    mysql_raw["source"]["tables"][0]["primary_key_columns"] = []
    mysql_raw["hub"]["tables"][0]["key_columns"] = ["option_name"]
    keys = _config(mysql_raw)["message.key.columns"].split(";")
    assert keys[0] == "wordpress.wp_options:option_name"


def test_key_columns_override_the_primary_key(pg_raw):
    pg_raw["hub"]["tables"][1]["key_columns"] = ["guid"]
    pg_raw["source"]["tables"][1]["replica_identity"] = "index"
    keys = _config(pg_raw)["message.key.columns"].split(";")
    assert keys[1] == "public.wp_posts:guid"


def _mutate(raw: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    raw = copy.deepcopy(raw)
    target = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return raw


@pytest.mark.parametrize(
    "path, value, message",
    [
        (("hub", "tables", 1, "topic"), "other.wordpress.wp_posts", "must start with 'wordpress.'"),
        (("hub", "tables", 1, "topic"), "wordpress.wordpress.wp_options", "used more than once"),
        (("hub", "tables", 1, "topic"), "wordpress.schema-history.wave-2", "used more than once"),
        (("hub", "tables", 1, "table"), "wp_comments", "is not a source table"),
        (("hub", "tables", 1, "table"), "wp_options", "more than one topic"),
        (("hub", "tables", 1, "key_columns"), ["ID", "ID"], "repeats a key column"),
        (("hub", "tables", 1, "schema_name"), "public", "MySQL tables have no schema_name"),
        (("source", "tables", 1, "name"), "wp_posts;drop", "table name must be"),
        (("source", "tables", 1, "primary_key_columns"), ["ID:1"], "primary key column"),
        (("source", "database"), "word press", "database name must be"),
        (("cdc", "topic_prefix"), "word.press", "topic_prefix must be"),
        (
            ("cdc", "connector_class"),
            "io.debezium.connector.postgresql.PostgresConnector",
            "MySqlConnector",
        ),
        (("cdc", "snapshot_mode"), "always", "snapshot_mode"),
        (("cdc", "heartbeat_interval_ms"), 0, "heartbeat_interval_ms"),
        (("hub", "tables", 1, "cleanup_policy"), "delete", "cleanup_policy"),
        (("hub", "tables", 1, "partitions"), 0, "partitions"),
        (("job_id",), "../x", "job_id must contain"),
    ],
)
def test_inconsistent_inputs_are_rejected(mysql_raw, path, value, message):
    with pytest.raises(ValidationError, match=re.escape(message)):
        _spec(_mutate(mysql_raw, path, value))


def test_a_source_table_without_a_topic_is_rejected(mysql_raw):
    mysql_raw["hub"]["tables"].pop()
    with pytest.raises(
        ValidationError, match="without a hub topic: wordpress.wp_term_relationships"
    ):
        _spec(mysql_raw)


def test_unknown_fields_are_rejected(mysql_raw):
    mysql_raw["cdc"]["tombstones"] = False
    with pytest.raises(ValidationError, match="Extra inputs"):
        _spec(mysql_raw)


def test_worker_properties_register_the_secrets_manager_provider():
    props = dbz.render_worker_properties().splitlines()
    assert "config.providers=secretsmanager" in props
    assert (
        "config.providers.secretsmanager.class="
        "com.amazonaws.kafka.config.providers.SecretsManagerConfigProvider"
    ) in props
    assert "config.providers.secretsmanager.param.region=${AWS::Region}" in props


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _template(raw: dict[str, Any]) -> dict[str, Any]:
    return dbz.render_connector_template(_spec(raw))


def _cfn_lint(text: str) -> list[str]:
    matches = lint(text, config=ManualArgs(regions=["us-east-1"], ignore_checks=["W3037"]))
    return [f"{m.rule.id} {m.message}" for m in matches]


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
def test_rendered_templates_pass_cfn_lint(fixture, request):
    raw = request.getfixturevalue(fixture)
    assert _cfn_lint(dbz.dump_template(_template(raw))) == []


def test_bucket_stack_passes_cfn_lint():
    assert _cfn_lint(BUCKET_STACK.read_text()) == []


@pytest.mark.parametrize("input_path, output_path", script.example_pairs())
def test_examples_are_the_rendered_snapshot(input_path, output_path):
    """The checked-in templates are what the generator renders now.

    Regenerate with ``uv run python scripts/render_debezium_connector.py --examples``.
    """
    assert output_path.exists(), output_path
    assert script.render_file(input_path) == output_path.read_text()


def test_examples_cover_both_engines():
    names = {p.name for p, _ in script.example_pairs()}
    assert names == {"wordpress-mysql.input.json", "wordpress-postgresql.input.json"}


def test_examples_check_mode_passes(capsys):
    assert script.main(["--examples", "--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}


def test_render_script_reports_a_bad_input(tmp_path, mysql_raw, capsys):
    mysql_raw["source"]["tables"][0]["primary_key_columns"] = []
    bad = tmp_path / "in.json"
    bad.write_text(json.dumps(mysql_raw))
    assert script.main(["--input", str(bad)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "no primary key" in out["error"]


# Parameters that name an account, a region, an endpoint, the network or a
# credential: the operator supplies every one, the template defaults none.
DEPLOYMENT_PARAMETERS = {
    "MskClusterArn",
    "BootstrapServers",
    "SubnetIds",
    "SecurityGroupIds",
    "DatabaseHost",
    "DatabaseSecretArn",
    "PluginBucketStackName",
    "PluginFileKey",
    "PluginObjectVersion",
}


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
def test_no_account_region_or_endpoint_defaults(fixture, request):
    template = _template(request.getfixturevalue(fixture))
    params = template["Parameters"]
    assert DEPLOYMENT_PARAMETERS <= set(params)
    assert not [p for p in DEPLOYMENT_PARAMETERS if "Default" in params[p]]
    assert params["SecretKmsKeyArn"]["Default"] == ""
    text = dbz.dump_template(template)
    assert not re.search(r"\b\d{12}\b", text), "an account id is in the template"
    assert not re.search(r"\b[a-z]{2}(-gov)?-[a-z]+-\d\b", text), "a region is in the template"
    assert "amazonaws.com:" not in text


def test_bucket_stack_has_no_account_or_region():
    text = BUCKET_STACK.read_text()
    assert not re.search(r"\b\d{12}\b", text)
    assert not re.search(r"\b[a-z]{2}(-gov)?-[a-z]+-\d\b", text)
    assert "Default" not in yaml.safe_load(_strip_tags(text))["Parameters"]["JobId"]


def _strip_tags(text: str) -> str:
    # Short-form intrinsics (``!Ref``) are not plain YAML; drop the tag for reading.
    return re.sub(r"!(Ref|Sub|GetAtt)\b", "", text)


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
def test_every_resource_is_tagged_with_the_job_id(fixture, request):
    for name, resource in _template(request.getfixturevalue(fixture))["Resources"].items():
        assert {"Key": "modernizer:job-id", "Value": "wordpress-example"} in resource["Properties"][
            "Tags"
        ], name


def test_bucket_stack_buckets_are_tagged_with_the_job_id():
    resources = yaml.safe_load(_strip_tags(BUCKET_STACK.read_text()))["Resources"]
    buckets = [r for r in resources.values() if r["Type"] == "AWS::S3::Bucket"]
    assert len(buckets) == 2
    for bucket in buckets:
        assert {"Key": "modernizer:job-id", "Value": "JobId"} in bucket["Properties"]["Tags"]
        assert bucket["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}


def _statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    role = template["Resources"]["ExecutionRole"]["Properties"]
    (policy,) = role["Policies"]
    out = []
    for statement in policy["PolicyDocument"]["Statement"]:
        if "Fn::If" in statement:
            statement = statement["Fn::If"][1]
        out.append(statement)
    return out


def _resource_strings(statement: dict[str, Any]) -> list[str]:
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    out = []
    for r in resources:
        if isinstance(r, dict) and "Fn::Sub" in r:
            sub = r["Fn::Sub"]
            out.append(sub[0] if isinstance(sub, list) else sub)
        else:
            out.append(json.dumps(r))
    return out


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
def test_execution_role_is_least_privilege(fixture, request):
    raw = request.getfixturevalue(fixture)
    template = _template(raw)
    statements = _statements(template)
    for statement in statements:
        assert statement["Effect"] == "Allow"
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        assert not [a for a in actions if a.endswith(":*") or a == "*"], statement["Sid"]
        assert "*" not in _resource_strings(statement), statement["Sid"]
    # Topic writes stay under the topic prefix and this wave's heartbeat topic.
    (topics,) = [s for s in statements if s["Sid"] == "WriteChangeTopics"]
    grants = [r.rsplit("/", 1)[1] for r in _resource_strings(topics)]
    assert grants == ["wordpress.*", "__debezium-heartbeat.wave-2.wordpress"]
    # The connector reads one topic, MySQL's schema history, and nothing on PostgreSQL.
    read = _granted(template, "kafka-cluster:ReadData", "topic")
    expected = ["wordpress.schema-history.wave-2"] if raw["source"]["dialect"] == "mysql" else []
    assert [g for g in read if not g.startswith("__amazon_msk_connect_")] == expected
    for hub in raw["hub"]["tables"]:
        assert not _covered(hub["topic"], read), hub["topic"]
    trust = template["Resources"]["ExecutionRole"]["Properties"]["AssumeRolePolicyDocument"]
    (assume,) = trust["Statement"]
    assert assume["Principal"] == {"Service": "kafkaconnect.amazonaws.com"}
    assert set(assume["Condition"]) == {"StringEquals", "ArnLike"}


def test_connector_uses_iam_tls_and_cloudwatch_logs(mysql_raw):
    props = _template(mysql_raw)["Resources"]["Connector"]["Properties"]
    assert props["KafkaClusterClientAuthentication"] == {"AuthenticationType": "IAM"}
    assert props["KafkaClusterEncryptionInTransit"] == {"EncryptionType": "TLS"}
    assert props["LogDelivery"]["WorkerLogDelivery"]["CloudWatchLogs"] == {
        "Enabled": True,
        "LogGroup": {"Ref": "WorkerLogGroup"},
    }
    assert props["Capacity"]["ProvisionedCapacity"]["WorkerCount"] == 1
    plugin = _template(mysql_raw)["Resources"]["Plugin"]["Properties"]["Location"]["S3Location"]
    assert plugin == {
        "BucketArn": {"Fn::ImportValue": {"Fn::Sub": "${PluginBucketStackName}-PluginBucketArn"}},
        "FileKey": {"Ref": "PluginFileKey"},
        "ObjectVersion": {"Ref": "PluginObjectVersion"},
    }


# ---------------------------------------------------------------------------
# The role grants what the configuration uses
# ---------------------------------------------------------------------------


def _granted(template: dict[str, Any], action: str, kind: str) -> list[str]:
    """Names (topic or group, after the cluster uuid) the role grants ``action`` on."""
    names = []
    for statement in _statements(template):
        actions = statement["Action"]
        if action not in (actions if isinstance(actions, list) else [actions]):
            continue
        for resource in _resource_strings(statement):
            match = re.search(rf":{kind}/\$\{{ClusterName\}}/\$\{{ClusterUuid\}}/(.+)$", resource)
            if match:
                names.append(match.group(1))
    return names


def _covered(name: str, grants: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in grants)


def _kafka_use(config: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """Topics the configuration writes, topics it reads and groups it joins."""
    written = set()
    for key, value in config.items():
        if key.startswith("transforms.") and key.endswith(".replacement"):
            written.add(value)
    renamed = {
        config[k].replace("\\", "")
        for k in config
        if k.startswith("transforms.") and k.endswith(".regex")
    }
    prefix = config["topic.prefix"]
    for pattern in config["table.include.list"].split(","):
        default = f"{prefix}.{pattern.replace(chr(92), '')}"
        if default not in renamed:
            written.add(default)
    written.add(f"{config['topic.heartbeat.prefix']}.{prefix}")
    read: set[str] = set()
    groups: set[str] = set()
    if "schema.history.internal.kafka.topic" in config:
        history = config["schema.history.internal.kafka.topic"]
        written.add(history)
        read.add(history)
        groups.add(config["schema.history.internal.consumer.group.id"])
        groups.add(config["schema.history.internal.name"])
    return written, read, groups


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
@pytest.mark.parametrize("wave", [2, 3])
def test_every_topic_and_group_the_config_names_is_granted(fixture, wave, request):
    raw = request.getfixturevalue(fixture)
    raw["wave"] = wave
    template = _template(raw)
    config = template["Resources"]["Connector"]["Properties"]["ConnectorConfiguration"]
    written, read, groups = _kafka_use(config)
    assert {h["topic"] for h in raw["hub"]["tables"]} <= written
    for action in (
        "kafka-cluster:CreateTopic",
        "kafka-cluster:WriteData",
        "kafka-cluster:DescribeTopic",
    ):
        grants = _granted(template, action, "topic")
        assert not [t for t in written if not _covered(t, grants)], action
    reads = _granted(template, "kafka-cluster:ReadData", "topic")
    assert not [t for t in read if not _covered(t, reads)]
    for action in ("kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"):
        grants = _granted(template, action, "group")
        assert not [g for g in groups if not _covered(g, grants)], action
        # Exactly the history group (and MSK Connect's own), no wildcard on ours.
        assert [g for g in grants if not g.startswith("__amazon_msk_connect_")] == sorted(groups)[
            :1
        ]


def test_debezium_default_history_group_is_not_what_the_role_grants(mysql_raw):
    """Debezium 2.7's default group, ``<topic.prefix>-schemahistory``, is overridden."""
    template = _template(mysql_raw)
    grants = _granted(template, "kafka-cluster:DescribeGroup", "group")
    assert not _covered("wordpress-schemahistory", grants)
    config = template["Resources"]["Connector"]["Properties"]["ConnectorConfiguration"]
    assert config["schema.history.internal.consumer.group.id"] != "wordpress-schemahistory"


# ---------------------------------------------------------------------------
# Two waves of one job never share a name
# ---------------------------------------------------------------------------


def _wave_names(raw: dict[str, Any], wave: int) -> dict[str, str]:
    raw = copy.deepcopy(raw)
    raw["wave"] = wave
    config = _config(raw)
    names = dbz.connector_names(_spec(raw))
    keys = [
        "slot.name",
        "publication.name",
        "topic.heartbeat.prefix",
        "schema.history.internal.kafka.topic",
        "schema.history.internal.consumer.group.id",
    ]
    out = {k: config[k] for k in keys if k in config}
    out["heartbeat topic"] = f"{config['topic.heartbeat.prefix']}.{config['topic.prefix']}"
    out["connector"] = names.connector
    return out


@pytest.mark.parametrize("fixture", ["mysql_raw", "pg_raw"])
@pytest.mark.parametrize("prefix", ["wordpress", "dm-" + "a" * 128])
def test_two_waves_never_share_a_name(fixture, prefix, request):
    raw = request.getfixturevalue(fixture)
    raw["cdc"]["topic_prefix"] = prefix
    for hub in raw["hub"]["tables"]:
        hub["topic"] = prefix + hub["topic"][len("wordpress") :]
    first, second = _wave_names(raw, 2), _wave_names(raw, 3)
    assert set(first) == set(second)
    assert not set(first.values()) & set(second.values())
    for key in ("slot.name", "publication.name"):
        if key in first:
            # PostgreSQL identifiers: at most 63 bytes, lower case, digits, '_'.
            assert re.fullmatch(r"[a-z0-9_]{1,63}", first[key]), first[key]
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,99}", first["connector"])


def test_long_prefixes_that_share_a_start_get_different_slots(pg_raw):
    def slot(prefix: str) -> str:
        raw = copy.deepcopy(pg_raw)
        raw["cdc"]["topic_prefix"] = prefix
        for hub in raw["hub"]["tables"]:
            hub["topic"] = prefix + hub["topic"][len("wordpress") :]
        return str(_config(raw)["slot.name"])

    assert slot("dm-" + "a" * 100 + "x") != slot("dm-" + "a" * 100 + "y")


# ---------------------------------------------------------------------------
# PostgreSQL replica identity
# ---------------------------------------------------------------------------


def _pg_without_pk(pg_raw: dict[str, Any], **table: Any) -> dict[str, Any]:
    pg_raw["source"]["tables"][0]["primary_key_columns"] = []
    pg_raw["source"]["tables"][0].update(table)
    pg_raw["hub"]["tables"][0]["key_columns"] = ["option_name"]
    return pg_raw


def test_postgresql_table_keyed_without_a_primary_key_needs_replica_identity(pg_raw):
    with pytest.raises(ValidationError, match="REPLICA IDENTITY FULL or USING INDEX"):
        _spec(_pg_without_pk(pg_raw))


def test_postgresql_key_other_than_the_primary_key_needs_replica_identity(pg_raw):
    pg_raw["hub"]["tables"][1]["key_columns"] = ["guid"]
    with pytest.raises(ValidationError, match="public.wp_posts is keyed on guid"):
        _spec(pg_raw)


def test_postgresql_key_equal_to_the_primary_key_needs_nothing(pg_raw):
    pg_raw["hub"]["tables"][1]["key_columns"] = ["id"]
    assert dbz.replica_identity_prerequisites(_spec(pg_raw)) == []


@pytest.mark.parametrize("mode", ["index", "full"])
def test_replica_identity_is_listed_before_deploy(pg_raw, mode, tmp_path, capsys):
    raw = _pg_without_pk(pg_raw, replica_identity=mode)
    spec = _spec(raw)
    assert dbz.replica_identity_prerequisites(spec) == [("public.wp_options", mode)]
    description = dbz.render_connector_template(spec)["Description"]
    assert f"set REPLICA IDENTITY on: public.wp_options ({mode})." in description
    assert len(description.encode()) <= 1024
    src, out = tmp_path / "in.json", tmp_path / "stack.yaml"
    src.write_text(json.dumps(raw))
    assert script.main(["--input", str(src), "--output", str(out)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["replica_identity_before_deploy"] == [
        {"table": "public.wp_options", "replica_identity": mode}
    ]


def test_many_replica_identity_tables_keep_the_description_in_bounds(pg_raw):
    tables = [f"t{i:03d}_" + "x" * 50 for i in range(40)]
    pg_raw["source"]["tables"] = [
        {"name": t, "schema_name": "public", "replica_identity": "full"} for t in tables
    ]
    pg_raw["hub"]["tables"] = [
        {
            "table": t,
            "schema_name": "public",
            "topic": f"wordpress.public.{t}",
            "key_columns": ["k"],
        }
        for t in tables
    ]
    description = dbz.render_connector_template(_spec(pg_raw))["Description"]
    assert len(description.encode()) <= 1024
    assert "more (README)." in description


def test_replica_identity_is_postgresql_only(mysql_raw):
    mysql_raw["source"]["tables"][0]["replica_identity"] = "full"
    with pytest.raises(ValidationError, match="replica_identity is PostgreSQL only"):
        _spec(mysql_raw)


# ---------------------------------------------------------------------------
# TLS and the plugin bucket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture, mode", [("mysql_raw", "required"), ("pg_raw", "require")])
def test_only_encrypting_ssl_modes_until_verification_lands(fixture, mode, request):
    param = _template(request.getfixturevalue(fixture))["Parameters"]["DatabaseSslMode"]
    assert param["AllowedValues"] == [mode]


def test_bucket_stack_exports_what_the_connector_stack_imports(mysql_raw):
    outputs = yaml.safe_load(_strip_tags(BUCKET_STACK.read_text()))["Outputs"]
    assert outputs["PluginBucketArn"]["Export"] == {"Name": "${AWS::StackName}-PluginBucketArn"}
    assert "PluginBucketArn" not in _template(mysql_raw)["Parameters"]
