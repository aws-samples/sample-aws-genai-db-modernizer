"""Debezium source connectors on Amazon MSK Connect (#1414).

The data movement decisions recorded on #1412 (to become ADR-033) move data
through one compacted Kafka topic per source table, fed by a Debezium source connector running on Amazon
MSK Connect. This module turns the ``source``, ``cdc`` and ``hub`` sections of
a wave's data movement plan into:

- the connector configuration (:func:`render_connector_configuration`): primary
  key message keys, tombstones on delete, heartbeats, schema history in Kafka
  (MySQL), incremental-snapshot signalling, IAM authentication to the cluster,
  credentials from AWS Secrets Manager through the worker's config provider, and
  topic creation with ``cleanup.policy=compact`` and the plan's partitions;
- the CloudFormation template of the connector stack
  (:func:`render_connector_template`): the custom plugin, the worker
  configuration, a least-privilege execution role, a KMS-encrypted CloudWatch
  Logs group for worker logs, and the connector, all tagged with the job id.

The plugin bucket is the static ``infrastructure/cloudformation/data-movement/
debezium-plugin-bucket.yaml`` stack; the README next to it explains the deploy
order. Nothing here calls AWS.

:class:`DebeziumConnectorInput` is a small typed stand-in for the plan. Its
section and field names follow the ``DataMovementPlan`` first cut in #1413
(``source``: engine, dialect, database, tables with primary-key columns;
``cdc``: tool, snapshot mode, heartbeat; ``hub``: per table topic name, key
columns, ``cleanup.policy=compact``, partitions). Once #1413 merges, the plan
feeds this input and this model goes away (#1422); ``cdc.topic_prefix`` and
``cdc.signal_table`` are the two fields this connector needs that #1413's first
cut does not list yet.

Names that must differ between two waves of one job (the PostgreSQL slot and
publication, the heartbeat and schema history topics, the schema history
consumer group) carry the wave number: one job has one topic prefix, and two
connectors sharing a slot or a history topic break each other
(:func:`connector_names`).

Account, region, endpoint, network and credential values are never part of the
input: they are stack parameters with no defaults, so a rendered template can
be reviewed before anything is deployed and carries nothing account-specific.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.shared.job_id import validate_job_id

Dialect = Literal["mysql", "postgresql"]
SnapshotMode = Literal["initial", "initial_only", "no_data", "when_needed"]

CONNECTOR_CLASSES: dict[str, str] = {
    "mysql": "io.debezium.connector.mysql.MySqlConnector",
    "postgresql": "io.debezium.connector.postgresql.PostgresConnector",
}

#: The Secrets Manager config provider the worker configuration registers
#: (the open-source MSK config providers, bundled into the custom plugin).
CONFIG_PROVIDER_NAME = "secretsmanager"
CONFIG_PROVIDER_CLASS = "com.amazonaws.kafka.config.providers.SecretsManagerConfigProvider"

#: Debezium's default heartbeat topic prefix (``topic.heartbeat.prefix``); a
#: wave suffix is added so each wave's connector has its own heartbeat topic.
HEARTBEAT_TOPIC_PREFIX = "__debezium-heartbeat"
#: Topic suffix of the MySQL schema history topic (under the topic prefix).
SCHEMA_HISTORY_SUFFIX = "schema-history"

DEFAULT_PORTS: dict[str, int] = {"mysql": 3306, "postgresql": 5432}
SSL_MODES: dict[str, list[str]] = {
    # Debezium MySQL ``database.ssl.mode`` / PostgreSQL ``database.sslmode``.
    # Encrypt-only for now: the verify modes need the RDS CA bundle in the
    # plugin and a trust store setting, which #1421 adds.
    "mysql": ["required"],
    "postgresql": ["require"],
}

_IAM_JAAS = "software.amazon.msk.auth.iam.IAMLoginModule required;"
_IAM_CALLBACK = "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
_JSON_CONVERTER = "org.apache.kafka.connect.json.JsonConverter"

# Identifiers reach regex include lists, ``message.key.columns`` (``:`` ``,``
# ``;`` separators) and a CloudFormation template, so only plain identifier
# characters pass; anything else is rejected rather than escaped.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,63}")
# Kafka topic names: letters, digits, ``.``, ``_`` and ``-``, at most 249.
_TOPIC_RE = re.compile(r"[A-Za-z0-9._-]{1,249}")
# The topic prefix has no ``.`` so ``<prefix>.`` scopes the IAM topic grant.
# 131 characters fits ``dm-<job_id>`` for a 128-character job id (#1429).
_PREFIX_RE = re.compile(r"[A-Za-z0-9_-]{1,131}")


class DebeziumInputError(ValueError):
    """The plan sections cannot produce a valid Debezium connector."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _identifier(value: str, what: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{what} must be 1 to 64 letters, digits, '_' or '$', starting with a letter or '_'"
        )
    return value


class SourceTable(_Model):
    """One table in the plan's ``source.tables``."""

    name: str
    #: PostgreSQL schema (``public`` when omitted); MySQL tables have none.
    schema_name: str | None = None
    primary_key_columns: tuple[str, ...] = ()
    #: PostgreSQL ``REPLICA IDENTITY`` the operator has set on the table
    #: (``full``, or ``index`` for ``USING INDEX``). Required, before deploy,
    #: for a table keyed on anything but its primary key: once the publication
    #: exists, UPDATE and DELETE on a table with no replica identity fail.
    replica_identity: Literal["full", "index"] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _identifier(value, "table name")

    @field_validator("schema_name")
    @classmethod
    def _schema(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "schema name")

    @field_validator("primary_key_columns")
    @classmethod
    def _pk(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_identifier(c, "primary key column") for c in value)


class SourceSection(_Model):
    """The plan's ``source`` section (host and port are stack parameters)."""

    engine: str
    dialect: Dialect
    database: str
    tables: tuple[SourceTable, ...] = Field(min_length=1)

    @field_validator("database")
    @classmethod
    def _database(cls, value: str) -> str:
        return _identifier(value, "database name")


class CdcSection(_Model):
    """The plan's ``cdc`` section."""

    tool: Literal["debezium"] = "debezium"
    connector_class: str | None = None
    snapshot_mode: SnapshotMode = "initial"
    heartbeat_interval_ms: int = Field(default=10_000, ge=1_000, le=3_600_000)
    #: Debezium ``topic.prefix``: every hub topic starts with ``<topic_prefix>.``.
    topic_prefix: str
    #: Incremental-snapshot signalling table (``signal.data.collection``),
    #: created by the operator in the source database or schema.
    signal_table: str = "debezium_signal"

    @field_validator("topic_prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if not _PREFIX_RE.fullmatch(value):
            raise ValueError("topic_prefix must be 1 to 131 letters, digits, '_' or '-'")
        return value

    @field_validator("signal_table")
    @classmethod
    def _signal(cls, value: str) -> str:
        return _identifier(value, "signal table")


class HubTable(_Model):
    """One table's topic in the plan's ``hub`` section."""

    table: str
    schema_name: str | None = None
    topic: str
    #: Message key columns; empty means the source table's primary key.
    key_columns: tuple[str, ...] = ()
    cleanup_policy: Literal["compact"] = "compact"
    partitions: int = Field(default=1, ge=1, le=1_000)

    @field_validator("table")
    @classmethod
    def _table(cls, value: str) -> str:
        return _identifier(value, "hub table")

    @field_validator("schema_name")
    @classmethod
    def _schema(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "schema name")

    @field_validator("topic")
    @classmethod
    def _topic(cls, value: str) -> str:
        if not _TOPIC_RE.fullmatch(value) or value in {".", ".."}:
            raise ValueError("topic must be 1 to 249 letters, digits, '.', '_' or '-'")
        return value

    @field_validator("key_columns")
    @classmethod
    def _keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_identifier(c, "key column") for c in value)


class HubSection(_Model):
    """The plan's ``hub`` section: one compacted topic per source table."""

    tables: tuple[HubTable, ...] = Field(min_length=1)


class DebeziumConnectorInput(_Model):
    """What one Debezium connector needs from a wave's data movement plan."""

    job_id: str
    wave: int = Field(ge=1)
    source: SourceSection
    cdc: CdcSection
    hub: HubSection

    @field_validator("job_id")
    @classmethod
    def _job_id(cls, value: str) -> str:
        return validate_job_id(value)

    @model_validator(mode="after")
    def _consistent(self) -> DebeziumConnectorInput:
        # Resolving every table checks keys, topics and the one-to-one mapping.
        _resolve_tables(self)
        return self


class ResolvedTable(_Model):
    """A source table joined with its hub entry, keys and topic names settled."""

    namespace: str  # MySQL database, or PostgreSQL schema
    name: str
    message_key: tuple[str, ...]  # the Kafka message key columns
    #: PostgreSQL replica identity the key relies on (None: the primary key).
    replica_identity: str | None
    debezium_topic: str  # the topic name Debezium writes before any rename
    topic: str  # the plan's topic name
    partitions: int

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}"


def _namespace(spec_source: SourceSection, schema_name: str | None) -> str:
    if spec_source.dialect == "mysql":
        return spec_source.database
    return schema_name or "public"


def _resolve_tables(spec: DebeziumConnectorInput) -> list[ResolvedTable]:
    source, cdc = spec.source, spec.cdc
    if source.dialect == "mysql":
        named = [(t.name, t.schema_name) for t in source.tables]
        named += [(h.table, h.schema_name) for h in spec.hub.tables]
        for name, schema_name in named:
            if schema_name is not None:
                raise DebeziumInputError(
                    f"table {name}: MySQL tables have no schema_name "
                    "(the database is the namespace)"
                )
    if cdc.connector_class not in (None, CONNECTOR_CLASSES[source.dialect]):
        raise DebeziumInputError(
            f"cdc.connector_class must be {CONNECTOR_CLASSES[source.dialect]} "
            f"for a {source.dialect} source"
        )

    by_name: dict[str, SourceTable] = {}
    for t in source.tables:
        key = f"{_namespace(source, t.schema_name)}.{t.name}"
        if key in by_name:
            raise DebeziumInputError(f"source table {key} is listed twice")
        by_name[key] = t

    resolved: list[ResolvedTable] = []
    seen_tables: set[str] = set()
    seen_topics: set[str] = set()
    names = connector_names(spec)
    reserved = {names.schema_history_topic, names.heartbeat_topic}
    for hub in spec.hub.tables:
        namespace = _namespace(source, hub.schema_name)
        key = f"{namespace}.{hub.table}"
        table = by_name.get(key)
        if table is None:
            raise DebeziumInputError(f"hub table {key} is not a source table")
        if key in seen_tables:
            raise DebeziumInputError(f"hub table {key} has more than one topic")
        seen_tables.add(key)
        if not hub.topic.startswith(f"{cdc.topic_prefix}."):
            raise DebeziumInputError(
                f"topic {hub.topic} for {key} must start with '{cdc.topic_prefix}.' "
                "(the execution role may write only under the topic prefix)"
            )
        if hub.topic in seen_topics or hub.topic in reserved:
            raise DebeziumInputError(f"topic {hub.topic} is used more than once")
        seen_topics.add(hub.topic)
        keys = hub.key_columns or table.primary_key_columns
        if not keys:
            raise DebeziumInputError(
                f"table {key} has no primary key: give hub key_columns (a unique, non-null "
                "column set) or it cannot have a compacted topic"
            )
        if len(set(keys)) != len(keys):
            raise DebeziumInputError(f"table {key} repeats a key column")
        if source.dialect == "mysql" and table.replica_identity is not None:
            raise DebeziumInputError(f"table {key}: replica_identity is PostgreSQL only")
        off_primary_key = tuple(keys) != table.primary_key_columns
        if source.dialect == "postgresql" and off_primary_key and not table.replica_identity:
            raise DebeziumInputError(
                f"table {key} is keyed on {', '.join(keys)}, not its primary key: set REPLICA "
                "IDENTITY FULL or USING INDEX on it before deploy and give replica_identity "
                "(without it, UPDATE and DELETE on the table fail once the publication exists)"
            )
        resolved.append(
            ResolvedTable(
                namespace=namespace,
                name=table.name,
                message_key=tuple(keys),
                replica_identity=table.replica_identity if off_primary_key else None,
                debezium_topic=f"{cdc.topic_prefix}.{namespace}.{table.name}",
                topic=hub.topic,
                partitions=hub.partitions,
            )
        )
    missing = sorted(set(by_name) - seen_tables)
    if missing:
        raise DebeziumInputError(
            f"source tables without a hub topic: {', '.join(missing)} (one topic per table)"
        )
    return resolved


def resolved_tables(spec: DebeziumConnectorInput) -> list[ResolvedTable]:
    """The plan's tables with their key columns and topic names settled."""
    return _resolve_tables(spec)


class ConnectorNames(_Model):
    """The per-wave names one connector uses besides its table topics."""

    connector: str  # default of the ConnectorName parameter
    heartbeat_prefix: str  # ``topic.heartbeat.prefix``
    heartbeat_topic: str  # ``<heartbeat_prefix>.<topic.prefix>``
    schema_history_topic: str  # MySQL only
    schema_history_group: str  # MySQL only: the history reader's consumer group
    slot: str  # PostgreSQL only
    publication: str  # PostgreSQL only


def _bounded(head: str, tail: str, limit: int, full: str) -> str:
    """``head + tail``, with ``head`` shortened and a hash of ``full`` added past ``limit``.

    The wave suffix (``tail``) always survives, so two waves never collide.
    """
    if len(head) + len(tail) <= limit:
        return head + tail
    digest = hashlib.sha256(full.encode()).hexdigest()[:8]
    return head[: limit - len(tail) - 9] + "_" + digest + tail


def connector_names(spec: DebeziumConnectorInput) -> ConnectorNames:
    prefix, wave = spec.cdc.topic_prefix, spec.wave
    heartbeat_prefix = f"{HEARTBEAT_TOPIC_PREFIX}.wave-{wave}"
    pg = re.sub(r"[^a-z0-9_]", "_", prefix.lower())
    # PostgreSQL names: lower-case letters, digits and '_', at most 63 bytes.
    slot = _bounded(f"debezium_{pg}", f"_wave_{wave}", 63, prefix)
    connector = _bounded(
        f"debezium-{prefix.replace('_', '-').lower()}", f"-wave-{wave}", 100, prefix
    ).replace("_", "-")
    return ConnectorNames(
        connector=connector,
        heartbeat_prefix=heartbeat_prefix,
        heartbeat_topic=f"{heartbeat_prefix}.{prefix}",
        schema_history_topic=f"{prefix}.{SCHEMA_HISTORY_SUFFIX}.wave-{wave}",
        schema_history_group=f"{prefix}-wave-{wave}-schemahistory",
        slot=slot,
        publication=_bounded(f"debezium_{pg}", f"_wave_{wave}_pub", 63, prefix),
    )


def default_connector_name(spec: DebeziumConnectorInput) -> str:
    """``debezium-<prefix>-wave-<n>``: the default of the ConnectorName parameter."""
    return connector_names(spec).connector


def _java_regex(literal: str) -> str:
    """An exact-match Java regex for an identifier or topic name."""
    return re.sub(r"([.\-$])", r"\\\1", literal)


def _group_names(tables: list[ResolvedTable]) -> list[str]:
    names: list[str] = []
    for i, t in enumerate(tables, start=1):
        name = re.sub(r"[^A-Za-z0-9_]", "_", f"{t.namespace}_{t.name}")
        names.append(name if name not in names else f"{name}_{i}")
    return names


def _ref(name: str) -> dict[str, str]:
    return {"Ref": name}


def _sub(text: str) -> dict[str, str]:
    return {"Fn::Sub": text}


def _encoded_secret_arn() -> dict[str, Any]:
    # Kafka's placeholder parser splits on ':', so the config provider takes the
    # ARN URL-encoded (':' as %3A; '+' as %2B, which URL decoding reads as a space).
    plus_encoded = {"Fn::Join": ["%2B", {"Fn::Split": ["+", {"Ref": "DatabaseSecretArn"}]}]}
    return {"Fn::Join": ["%3A", {"Fn::Split": [":", plus_encoded]}]}


def _secret(field: str) -> dict[str, Any]:
    # Fn::Sub keeps ``${!...}`` as a literal ``${...}``: the config provider
    # resolves it on the worker, so the credential never enters the template.
    return {
        "Fn::Sub": [
            f"${{!{CONFIG_PROVIDER_NAME}:${{SecretArn}}:{field}}}",
            {"SecretArn": _encoded_secret_arn()},
        ]
    }


ConfigValue = str | dict[str, Any]


def render_connector_configuration(spec: DebeziumConnectorInput) -> dict[str, ConfigValue]:
    """The connector configuration for ``AWS::KafkaConnect::Connector``.

    Values are strings, or a ``Ref``/``Fn::Sub`` to a stack parameter for the
    host, port, credentials, bootstrap servers and server id.
    """
    dialect = spec.source.dialect
    cdc = spec.cdc
    tables = resolved_tables(spec)
    names = connector_names(spec)
    signal_namespace = _namespace(spec.source, None if dialect == "mysql" else "public")
    signal_table = f"{signal_namespace}.{cdc.signal_table}"

    config: dict[str, ConfigValue] = {
        "connector.class": CONNECTOR_CLASSES[dialect],
        "tasks.max": "1",
        "database.hostname": _ref("DatabaseHost"),
        "database.port": _ref("DatabasePort"),
        "database.user": _secret("username"),
        "database.password": _secret("password"),
    }
    if dialect == "mysql":
        config |= {
            "database.server.id": _ref("DatabaseServerId"),
            "database.ssl.mode": _ref("DatabaseSslMode"),
            "database.include.list": _java_regex(spec.source.database),
        }
    else:
        config |= {
            "database.dbname": spec.source.database,
            "database.sslmode": _ref("DatabaseSslMode"),
            "plugin.name": "pgoutput",
            "slot.name": names.slot,
            "publication.name": names.publication,
            "publication.autocreate.mode": "filtered",
        }
    config |= {
        "table.include.list": ",".join(_java_regex(t.qualified_name) for t in tables),
        "topic.prefix": cdc.topic_prefix,
        "snapshot.mode": cdc.snapshot_mode,
        # One message per row key; a delete is followed by a tombstone so
        # compaction drops the key.
        "message.key.columns": ";".join(
            f"{t.qualified_name}:{','.join(t.message_key)}" for t in tables
        ),
        "tombstones.on.delete": "true",
        "heartbeat.interval.ms": str(cdc.heartbeat_interval_ms),
        "topic.heartbeat.prefix": names.heartbeat_prefix,
        # Incremental snapshots are started by inserting a row into the
        # signalling table (Debezium's ``source`` signal channel).
        "signal.enabled.channels": "source",
        "signal.data.collection": signal_table,
        "key.converter": _JSON_CONVERTER,
        "key.converter.schemas.enable": "false",
        "value.converter": _JSON_CONVERTER,
        "value.converter.schemas.enable": "false",
        # Schemaless JSON keeps exact decimals as strings, not base64 bytes.
        "decimal.handling.mode": "string",
    }
    if dialect == "mysql":
        config |= {
            "include.schema.changes": "false",
            "schema.history.internal.kafka.topic": names.schema_history_topic,
            # The history reader's consumer group; Debezium's default would be
            # ``<topic.prefix>-schemahistory``, shared by every wave of the job.
            "schema.history.internal.name": names.schema_history_group,
            "schema.history.internal.consumer.group.id": names.schema_history_group,
            "schema.history.internal.kafka.bootstrap.servers": _ref("BootstrapServers"),
            "schema.history.internal.store.only.captured.tables.ddl": "true",
        }
        for client in ("producer", "consumer"):
            config |= {
                f"schema.history.internal.{client}.security.protocol": "SASL_SSL",
                f"schema.history.internal.{client}.sasl.mechanism": "AWS_MSK_IAM",
                f"schema.history.internal.{client}.sasl.jaas.config": _IAM_JAAS,
                f"schema.history.internal.{client}.sasl.client.callback.handler.class": (
                    _IAM_CALLBACK
                ),
            }

    renames = [t for t in tables if t.topic != t.debezium_topic]
    if renames:
        routes = [f"route{i}" for i in range(1, len(renames) + 1)]
        config["transforms"] = ",".join(routes)
        for name, t in zip(routes, renames, strict=True):
            config |= {
                f"transforms.{name}.type": "org.apache.kafka.connect.transforms.RegexRouter",
                f"transforms.{name}.regex": _java_regex(t.debezium_topic),
                f"transforms.{name}.replacement": t.topic,
            }

    # Kafka Connect creates each topic on first write (KIP-158): the default
    # group covers the heartbeat topic, one group per table sets its partitions.
    config |= {
        "topic.creation.default.replication.factor": _ref("TopicReplicationFactor"),
        "topic.creation.default.partitions": "1",
        "topic.creation.default.cleanup.policy": "compact",
    }
    groups = _group_names(tables)
    config["topic.creation.groups"] = ",".join(groups)
    for group, t in zip(groups, tables, strict=True):
        config |= {
            f"topic.creation.{group}.include": _java_regex(t.topic),
            f"topic.creation.{group}.replication.factor": _ref("TopicReplicationFactor"),
            f"topic.creation.{group}.partitions": str(t.partitions),
            f"topic.creation.{group}.cleanup.policy": "compact",
        }
    return config


def render_worker_properties() -> str:
    """The worker configuration (``${AWS::Region}`` is filled by ``Fn::Sub``)."""
    return "\n".join(
        [
            f"key.converter={_JSON_CONVERTER}",
            f"value.converter={_JSON_CONVERTER}",
            f"config.providers={CONFIG_PROVIDER_NAME}",
            f"config.providers.{CONFIG_PROVIDER_NAME}.class={CONFIG_PROVIDER_CLASS}",
            f"config.providers.{CONFIG_PROVIDER_NAME}.param.region=${{AWS::Region}}",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# CloudFormation
# ---------------------------------------------------------------------------

_ARN_PREFIX = "arn:${AWS::Partition}"
_KAFKA = _ARN_PREFIX + ":kafka:${AWS::Region}:${AWS::AccountId}"
_CLUSTER_VARS = {
    "ClusterName": {"Fn::Select": [1, {"Fn::Split": ["/", {"Ref": "MskClusterArn"}]}]},
    "ClusterUuid": {"Fn::Select": [2, {"Fn::Split": ["/", {"Ref": "MskClusterArn"}]}]},
}


def _cluster_sub(*resources: str) -> list[dict[str, Any]]:
    return [{"Fn::Sub": [r, _CLUSTER_VARS]} for r in resources]


def _tags(spec: DebeziumConnectorInput) -> list[dict[str, str]]:
    return [
        {"Key": "modernizer:job-id", "Value": spec.job_id},
        {"Key": "modernizer:wave", "Value": str(spec.wave)},
    ]


def _parameters(spec: DebeziumConnectorInput) -> dict[str, Any]:
    dialect = spec.source.dialect
    params: dict[str, Any] = {
        "ConnectorName": {
            "Type": "String",
            "Description": "MSK Connect connector name (unique in the account and region)",
            "Default": default_connector_name(spec),
            "AllowedPattern": "^[a-zA-Z0-9][a-zA-Z0-9-]{0,99}$",
        },
        "MskClusterArn": {
            "Type": "String",
            "Description": "ARN of the MSK cluster (IAM access control on) that holds the topics",
            "AllowedPattern": "^arn:aws[a-z-]*:kafka:[a-z0-9-]+:[0-9]{12}:cluster/[^/]+/[^/]+$",
        },
        "BootstrapServers": {
            "Type": "String",
            "Description": "The cluster's IAM bootstrap brokers (host:9098,host:9098,...)",
            "AllowedPattern": "^[A-Za-z0-9.-]+:[0-9]+(,[A-Za-z0-9.-]+:[0-9]+)*$",
        },
        "SubnetIds": {
            "Type": "List<AWS::EC2::Subnet::Id>",
            "Description": "Private subnets the connector runs in (reach the cluster, the "
            "database and Secrets Manager)",
        },
        "SecurityGroupIds": {
            "Type": "List<AWS::EC2::SecurityGroup::Id>",
            "Description": "Security groups for the connector's network interfaces",
        },
        "DatabaseHost": {
            "Type": "String",
            "Description": "Source database writer endpoint host name",
            "AllowedPattern": "^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$",
        },
        "DatabasePort": {
            "Type": "Number",
            "Description": "Source database port",
            "Default": DEFAULT_PORTS[dialect],
            "MinValue": 1,
            "MaxValue": 65535,
        },
        "DatabaseSslMode": {
            "Type": "String",
            "Description": "TLS mode to the database: encrypted, server certificate not "
            "verified yet (#1421, see the README)",
            "Default": SSL_MODES[dialect][0],
            "AllowedValues": SSL_MODES[dialect],
        },
        "DatabaseSecretArn": {
            "Type": "String",
            "Description": "Full ARN of the Secrets Manager secret with the replication "
            "user's username and password keys (the role may read only this secret)",
            "AllowedPattern": "^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:"
            "[A-Za-z0-9/_+=.@-]+$",
        },
        "SecretKmsKeyArn": {
            "Type": "String",
            "Description": "Customer managed KMS key ARN that encrypts the secret (empty when "
            "the secret uses the AWS managed key)",
            "Default": "",
            "AllowedPattern": "^$|^arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/[a-zA-Z0-9-]+$",
        },
    }
    if dialect == "mysql":
        params["DatabaseServerId"] = {
            "Type": "Number",
            "Description": "Binlog client server id, unique among the source's replicas",
            "MinValue": 1,
            "MaxValue": 4294967295,
        }
    params |= {
        "PluginBucketStackName": {
            "Type": "String",
            "Description": "Name of the debezium-plugin-bucket stack in this account and "
            "region (its PluginBucketArn export is imported, so the bucket is that stack's)",
            "AllowedPattern": "^[A-Za-z][A-Za-z0-9-]{0,127}$",
        },
        "PluginFileKey": {
            "Type": "String",
            "Description": "Object key of the plugin ZIP (Debezium connector plus the MSK "
            "config providers)",
            "AllowedPattern": "^[A-Za-z0-9!_.*'()/-]{1,1024}\\.zip$",
        },
        "PluginObjectVersion": {
            "Type": "String",
            "Description": "S3 version id of the uploaded plugin ZIP (pins the exact file)",
            "MinLength": 1,
        },
        "KafkaConnectVersion": {
            "Type": "String",
            "Description": "MSK Connect Kafka Connect version",
            "Default": "3.7.x",
            "AllowedValues": ["3.7.x", "2.7.1"],
        },
        "McuCount": {
            "Type": "Number",
            "Description": "MSK Connect units for the single worker",
            "Default": 1,
            "AllowedValues": [1, 2, 4, 8],
        },
        "TopicReplicationFactor": {
            "Type": "Number",
            "Description": "Replication factor of the topics the connector creates",
            "Default": 3,
            "MinValue": 1,
            "MaxValue": 5,
        },
        "LogRetentionDays": {
            "Type": "Number",
            "Description": "Days to keep worker logs",
            "Default": 30,
            "AllowedValues": [7, 14, 30, 60, 90, 180, 365],
        },
    }
    return params


def _execution_role(spec: DebeziumConnectorInput) -> dict[str, Any]:
    prefix = spec.cdc.topic_prefix
    names = connector_names(spec)
    mysql = spec.source.dialect == "mysql"
    topic = _KAFKA + ":topic/${ClusterName}/${ClusterUuid}/"
    group = _KAFKA + ":group/${ClusterName}/${ClusterUuid}/"
    statements: list[Any] = [
        {
            "Sid": "ConnectToCluster",
            "Effect": "Allow",
            "Action": [
                "kafka-cluster:Connect",
                "kafka-cluster:DescribeCluster",
                "kafka-cluster:DescribeClusterDynamicConfiguration",
                "kafka-cluster:WriteDataIdempotently",
            ],
            "Resource": {"Ref": "MskClusterArn"},
        },
        {
            "Sid": "WriteChangeTopics",
            "Effect": "Allow",
            "Action": [
                "kafka-cluster:CreateTopic",
                "kafka-cluster:DescribeTopic",
                "kafka-cluster:WriteData",
            ],
            # Table topics (and the MySQL schema history topic) sit under the
            # topic prefix; the heartbeat topic is this wave's own name.
            "Resource": _cluster_sub(
                f"{topic}{prefix}.*",
                f"{topic}{names.heartbeat_topic}",
            ),
        },
        {
            "Sid": "ConnectInternalTopics",
            "Effect": "Allow",
            "Action": [
                "kafka-cluster:CreateTopic",
                "kafka-cluster:DescribeTopic",
                "kafka-cluster:WriteData",
                "kafka-cluster:ReadData",
            ],
            "Resource": _cluster_sub(f"{topic}__amazon_msk_connect_*"),
        },
        *(
            [
                {
                    # The only topic the connector reads: MySQL schema history.
                    "Sid": "ReadSchemaHistory",
                    "Effect": "Allow",
                    "Action": [
                        "kafka-cluster:ReadData",
                        "kafka-cluster:DescribeTopicDynamicConfiguration",
                    ],
                    "Resource": _cluster_sub(f"{topic}{names.schema_history_topic}"),
                }
            ]
            if mysql
            else []
        ),
        {
            "Sid": "ConsumerGroups",
            "Effect": "Allow",
            "Action": ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"],
            # MSK Connect's own group and, on MySQL, the schema history reader's
            # group (``schema.history.internal.consumer.group.id``).
            "Resource": _cluster_sub(
                f"{group}__amazon_msk_connect_*",
                *([f"{group}{names.schema_history_group}"] if mysql else []),
            ),
        },
        {
            "Sid": "ReadDatabaseSecret",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            "Resource": {"Ref": "DatabaseSecretArn"},
        },
        {
            "Fn::If": [
                "HasSecretKmsKey",
                {
                    "Sid": "DecryptDatabaseSecret",
                    "Effect": "Allow",
                    "Action": "kms:Decrypt",
                    "Resource": {"Ref": "SecretKmsKeyArn"},
                    "Condition": {
                        "StringEquals": {
                            "kms:ViaService": {
                                "Fn::Sub": "secretsmanager.${AWS::Region}.amazonaws.com"
                            }
                        }
                    },
                },
                {"Ref": "AWS::NoValue"},
            ]
        },
    ]
    return {
        "Type": "AWS::IAM::Role",
        "Metadata": {
            "cfn_nag": {
                "rules_to_suppress": [
                    {
                        "id": "W76",
                        "reason": "One statement per resource scope (cluster, change topics, "
                        "Connect internal topics, groups, secret); every action is listed and "
                        "every resource is scoped, which is what raises the complexity score",
                    }
                ]
            }
        },
        "Properties": {
            "Description": "MSK Connect service execution role for one Debezium connector",
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "kafkaconnect.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                            "ArnLike": {
                                "aws:SourceArn": {
                                    "Fn::Sub": _ARN_PREFIX
                                    + ":kafkaconnect:${AWS::Region}:${AWS::AccountId}"
                                    + ":connector/${ConnectorName}/*"
                                }
                            },
                        },
                    }
                ],
            },
            "Policies": [
                {
                    "PolicyName": "debezium-connector",
                    "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                }
            ],
            "Tags": _tags(spec),
        },
    }


def _log_resources(spec: DebeziumConnectorInput) -> dict[str, Any]:
    log_group_name = "/aws/msk-connect/${ConnectorName}"
    return {
        "LogKey": {
            "Type": "AWS::KMS::Key",
            "DeletionPolicy": "Delete",
            "UpdateReplacePolicy": "Delete",
            "Properties": {
                "Description": "Encrypts the connector's worker logs",
                "EnableKeyRotation": True,
                "KeyPolicy": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AccountAdministers",
                            "Effect": "Allow",
                            "Principal": {
                                "AWS": {"Fn::Sub": _ARN_PREFIX + ":iam::${AWS::AccountId}:root"}
                            },
                            "Action": "kms:*",
                            "Resource": "*",
                        },
                        {
                            "Sid": "LogsEncryptThisLogGroup",
                            "Effect": "Allow",
                            "Principal": {
                                "Service": {"Fn::Sub": "logs.${AWS::Region}.amazonaws.com"}
                            },
                            "Action": [
                                "kms:Encrypt*",
                                "kms:Decrypt*",
                                "kms:ReEncrypt*",
                                "kms:GenerateDataKey*",
                                "kms:Describe*",
                            ],
                            "Resource": "*",
                            "Condition": {
                                "ArnEquals": {
                                    "kms:EncryptionContext:aws:logs:arn": {
                                        "Fn::Sub": _ARN_PREFIX
                                        + ":logs:${AWS::Region}:${AWS::AccountId}:log-group:"
                                        + log_group_name
                                    }
                                }
                            },
                        },
                    ],
                },
                "Tags": _tags(spec),
            },
        },
        "WorkerLogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "DeletionPolicy": "Delete",
            "UpdateReplacePolicy": "Delete",
            "Properties": {
                "LogGroupName": {"Fn::Sub": log_group_name},
                "RetentionInDays": {"Ref": "LogRetentionDays"},
                "KmsKeyId": {"Fn::GetAtt": ["LogKey", "Arn"]},
                "Tags": _tags(spec),
            },
        },
    }


_DESCRIPTION_LIMIT = 1024  # CloudFormation's template Description limit, in bytes


def replica_identity_prerequisites(spec: DebeziumConnectorInput) -> list[tuple[str, str]]:
    """PostgreSQL tables keyed off their primary key, with the replica identity
    the operator must set on each before the connector is deployed."""
    return [
        (t.qualified_name, t.replica_identity)
        for t in resolved_tables(spec)
        if t.replica_identity is not None
    ]


def _bounded_list(head: str, items: list[str], limit: int) -> str:
    """``head`` and the items joined by ", ", as many as fit in ``limit``."""
    for shown in range(len(items), 0, -1):
        rest = len(items) - shown
        text = head + ", ".join(items[:shown]) + (f" and {rest} more (README)." if rest else ".")
        if len(text) <= limit:
            return text
    return head + f"{len(items)} tables (see the render script's output)."


def render_connector_template(spec: DebeziumConnectorInput) -> dict[str, Any]:
    """The CloudFormation template of one connector stack."""
    dialect = spec.source.dialect
    engine_label = "MySQL" if dialect == "mysql" else "PostgreSQL"
    description = (
        f"Debezium {engine_label} source connector on Amazon MSK Connect for job "
        f"{spec.job_id}, wave {spec.wave} (#1412). Generated by "
        "scripts/render_debezium_connector.py from the wave's data movement plan; "
        "regenerate it rather than editing it. Deploy the debezium-plugin-bucket stack "
        "first and upload the plugin ZIP (see the README next to it)."
    )
    identity = replica_identity_prerequisites(spec)
    if identity:
        description += _bounded_list(
            " Before deploy, set REPLICA IDENTITY on: ",
            [f"{name} ({mode})" for name, mode in identity],
            _DESCRIPTION_LIMIT - len(description),
        )
    resources: dict[str, Any] = {
        "ExecutionRole": _execution_role(spec),
        **_log_resources(spec),
        "Plugin": {
            "Type": "AWS::KafkaConnect::CustomPlugin",
            "Properties": {
                "Name": {"Fn::Sub": "${ConnectorName}-plugin"},
                "Description": f"Debezium {engine_label} connector and the MSK config providers",
                "ContentType": "ZIP",
                "Location": {
                    "S3Location": {
                        "BucketArn": {
                            "Fn::ImportValue": {
                                "Fn::Sub": "${PluginBucketStackName}-PluginBucketArn"
                            }
                        },
                        "FileKey": {"Ref": "PluginFileKey"},
                        "ObjectVersion": {"Ref": "PluginObjectVersion"},
                    }
                },
                "Tags": _tags(spec),
            },
        },
        "WorkerConfiguration": {
            "Type": "AWS::KafkaConnect::WorkerConfiguration",
            "Properties": {
                "Name": {"Fn::Sub": "${ConnectorName}-worker"},
                "Description": "JSON converters and the Secrets Manager config provider",
                "PropertiesFileContent": {"Fn::Base64": {"Fn::Sub": render_worker_properties()}},
                "Tags": _tags(spec),
            },
        },
        "Connector": {
            "Type": "AWS::KafkaConnect::Connector",
            "Properties": {
                "ConnectorName": {"Ref": "ConnectorName"},
                "ConnectorDescription": f"Debezium {engine_label} CDC into compacted per-table "
                f"topics (job {spec.job_id}, wave {spec.wave})",
                "KafkaConnectVersion": {"Ref": "KafkaConnectVersion"},
                "Capacity": {
                    # Debezium MySQL and PostgreSQL connectors run one task.
                    "ProvisionedCapacity": {"McuCount": {"Ref": "McuCount"}, "WorkerCount": 1}
                },
                "KafkaCluster": {
                    "ApacheKafkaCluster": {
                        "BootstrapServers": {"Ref": "BootstrapServers"},
                        "Vpc": {
                            "SecurityGroups": {"Ref": "SecurityGroupIds"},
                            "Subnets": {"Ref": "SubnetIds"},
                        },
                    }
                },
                "KafkaClusterClientAuthentication": {"AuthenticationType": "IAM"},
                "KafkaClusterEncryptionInTransit": {"EncryptionType": "TLS"},
                "Plugins": [
                    {
                        "CustomPlugin": {
                            "CustomPluginArn": {"Fn::GetAtt": ["Plugin", "CustomPluginArn"]},
                            "Revision": {"Fn::GetAtt": ["Plugin", "Revision"]},
                        }
                    }
                ],
                "WorkerConfiguration": {
                    "WorkerConfigurationArn": {
                        "Fn::GetAtt": ["WorkerConfiguration", "WorkerConfigurationArn"]
                    },
                    "Revision": {"Fn::GetAtt": ["WorkerConfiguration", "Revision"]},
                },
                "ServiceExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]},
                "LogDelivery": {
                    "WorkerLogDelivery": {
                        "CloudWatchLogs": {"Enabled": True, "LogGroup": {"Ref": "WorkerLogGroup"}}
                    }
                },
                "ConnectorConfiguration": render_connector_configuration(spec),
                "Tags": _tags(spec),
            },
        },
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": description,
        "Parameters": _parameters(spec),
        "Conditions": {
            "HasSecretKmsKey": {"Fn::Not": [{"Fn::Equals": [{"Ref": "SecretKmsKeyArn"}, ""]}]}
        },
        "Resources": resources,
        "Outputs": {
            "ConnectorArn": {"Value": {"Fn::GetAtt": ["Connector", "ConnectorArn"]}},
            "ExecutionRoleArn": {"Value": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}},
            "WorkerLogGroupName": {"Value": {"Ref": "WorkerLogGroup"}},
            "CustomPluginArn": {"Value": {"Fn::GetAtt": ["Plugin", "CustomPluginArn"]}},
        },
    }


class _TemplateDumper(yaml.SafeDumper):
    """Block-style YAML; multi-line strings as literal blocks; no anchors."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_TemplateDumper.add_representer(str, _str_presenter)


def dump_template(template: Mapping[str, Any]) -> str:
    """The template as YAML, keys in render order."""
    text: str = yaml.dump(
        dict(template),
        Dumper=_TemplateDumper,
        sort_keys=False,
        default_flow_style=False,
        width=100,
        allow_unicode=False,
    )
    return text
