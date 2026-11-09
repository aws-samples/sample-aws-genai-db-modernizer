# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's DMS preflight: read-only checks before the stack deploys (#1799).

Eight checks, in a fixed order (``CHECK_ORDER``), stopping at the first failure (the rows
after it are ``not_run``). A ``confirm`` row (checks 3 and 6) becomes ``pass`` when its id is in the
``confirmed`` set ``run_preflight`` takes (checks 3, 6 and 8 can produce one). The checks:

1. ``aurora_cluster``: the named cluster exists and runs the step's Aurora engine at the
   design's version line (``replication.target_engine_line``) or later.
2. ``aurora_parameters``: its cluster parameter group has the values
   ``REQUIRED_CLUSTER_PARAMETERS`` names; the writer's DB parameter group has
   ``REQUIRED_INSTANCE_PARAMETERS`` (``local_infile = 1``) and, when the step copies
   zero-date-capable columns, a ``sql_mode`` without the zero-date modes; neither group
   is ``pending-reboot`` on the writer.
3. ``secrets``: both secrets exist and are not scheduled for deletion; a customer managed
   key is one the stack is told about; no resource policy denies reading them. Metadata
   only: a secret's value is never read.
4. ``instance_class_offered``: the replication instance class is orderable in the region.
5. ``dms_service_roles``: ``dms-vpc-role`` and ``dms-cloudwatch-logs-role`` exist, trust DMS and have their managed policies attached.
6. ``source_cdc``: the source's CDC settings, read through RDS where the API can (on PostgreSQL also
   the slot and WAL sender limits and ``max_slot_wal_keep_size``, #2060); the rest is a checklist.
7. ``dms_certificates``: the MySQL-family endpoints' certificates exist and have not expired.
8. ``secrets_manager_reachable``: the replication subnets reach Secrets Manager (VPC endpoint or NAT).

Every AWS call goes through :class:`PreflightClients` and is a ``describe``, ``get`` or
``list`` operation.
"""

from __future__ import annotations

import fnmatch
import functools
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import unquote

from botocore.exceptions import BotoCoreError, ClientError

from src.contracts.dms_preflight import CHECK_ORDER, DmsPreflight, PreflightCheck
from src.shared.catalog.gaps import parse_version_line
from src.shared.engine_names import display_engine
from src.tools.aws.credentials import AWSCredentialManager
from src.tools.data_movement.dms_step import (
    AURORA_ENGINES,
    REQUIRED_CLUSTER_PARAMETERS,
    REQUIRED_INSTANCE_PARAMETERS,
    SOURCE_ENDPOINTS,
    TARGET_ENDPOINTS,
    ZERO_DATE_SQL_MODE,
    DmsStepError,
    Step1Inputs,
    instance_parameters,
    load_step1,
)

DMS_SERVICE_ROLES = {
    "dms-vpc-role": "AmazonDMSVPCManagementRole",
    "dms-cloudwatch-logs-role": "AmazonDMSCloudWatchLogsRole",
}
SourceKind = Literal["rds_instance", "aurora_cluster", "self_managed"]
_VERSION_ENGINE = {"aurora_mysql": "mysql", "aurora_postgresql": "postgresql"}
_PENDING_REBOOT = "pending-reboot"
_IN_SYNC = "in-sync"
# Modes that expand to the zero-date modes: TRADITIONAL is NO_ZERO_IN_DATE and NO_ZERO_DATE plus the strict modes.
_ZERO_DATE_MODES = ("NO_ZERO_DATE", "NO_ZERO_IN_DATE", "TRADITIONAL")
_PERMISSIONS = {
    "DescribeDBClusters": "rds:DescribeDBClusters",
    "DescribeDBInstances": "rds:DescribeDBInstances",
    "DescribeDBClusterParameters": "rds:DescribeDBClusterParameters",
    "DescribeDBParameters": "rds:DescribeDBParameters",
    "DescribeSecret": "secretsmanager:DescribeSecret",  # pragma: allowlist secret
    "GetResourcePolicy": "secretsmanager:GetResourcePolicy",
    "DescribeKey": "kms:DescribeKey",
    "DescribeOrderableReplicationInstances": "dms:DescribeOrderableReplicationInstances",
    "GetRole": "iam:GetRole",
    "ListAttachedRolePolicies": "iam:ListAttachedRolePolicies",
    "DescribeCertificates": "dms:DescribeCertificates",
    "DescribeSubnets": "ec2:DescribeSubnets",
    "DescribeVpcEndpoints": "ec2:DescribeVpcEndpoints",
    "DescribeRouteTables": "ec2:DescribeRouteTables",
}


@dataclass(frozen=True)
class PreflightRequest:
    region: str
    aurora_cluster_id: str
    source_secret_arn: str
    target_secret_arn: str
    source_secret_kms_key_arn: str = ""
    target_secret_kms_key_arn: str = ""
    instance_class: str | None = None
    source_kind: SourceKind = "self_managed"
    source_id: str | None = None
    # DMS certificate ARNs (aws dms import-certificate): required on MySQL-family endpoints.
    source_certificate_arn: str = ""
    target_certificate_arn: str = ""
    # The replication subnets (the stack's SubnetIds) and the stack's SecretsManagerEndpointOverride.
    subnet_ids: tuple[str, ...] = ()
    secrets_manager_endpoint_override: str = ""


@dataclass(frozen=True)
class PreflightClients:
    rds: Any
    dms: Any
    secretsmanager: Any
    iam: Any
    ec2: Any
    kms: Any

    @classmethod
    def from_manager(cls, cred: AWSCredentialManager) -> PreflightClients:
        return cls(rds=cred.client("rds"), dms=cred.client("dms"), secretsmanager=cred.client("secretsmanager"),
                   iam=cred.client("iam"), ec2=cred.client("ec2"), kms=cred.client("kms"))  # fmt: skip


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _row(check_id: str, status: str, summary: str, evidence: Mapping[str, Any] | None = None,
         message: str | None = None, checklist: list[str] | None = None) -> PreflightCheck:  # fmt: skip
    return PreflightCheck.model_validate({"id": check_id, "status": status, "summary": summary,
                                          "evidence": {k: str(v) for k, v in (evidence or {}).items()},
                                          "message": message, "checklist": checklist or []})  # fmt: skip


def _is_lookup_error(code: str) -> bool:
    """Codes that mean the id or value is wrong rather than that the identity lacks access."""
    return "NotFound" in code or code in {"InvalidParameterValue", "InvalidParameterCombination",
                                          "ValidationError", "InvalidParameter", "MalformedPolicyDocument",
                                          "NoSuchEntity"} or code.endswith(".Malformed")  # fmt: skip


def _guarded(
    check_id: str,
) -> Callable[[Callable[..., PreflightCheck]], Callable[..., PreflightCheck]]:
    """A refused or failed AWS call inside a check is that check's failed row, naming the API and the
    permission the identity running the preflight needs."""

    def wrap(fn: Callable[..., PreflightCheck]) -> Callable[..., PreflightCheck]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> PreflightCheck:
            try:
                return fn(*args, **kwargs)
            except ClientError as exc:
                api, code = exc.operation_name, _code(exc) or "unknown"
                if _is_lookup_error(code):
                    action = (f"{api} could not find or accept what the preflight passed it ({code}): check the "
                              "ids and region (the cluster, subnets, secrets, certificates), then run the "
                              "preflight again.")  # fmt: skip
                else:
                    action = (f"The identity running the preflight needs {_PERMISSIONS.get(api, api)} (read "
                              "only); check its permissions and the region, then run the preflight again.")  # fmt: skip
                return _row(
                    check_id,
                    "fail",
                    f"{api} failed ({code})",
                    {"api": api, "error_code": code},
                    action,
                )
            except BotoCoreError as exc:
                return _row(check_id, "fail", f"An AWS call failed before AWS answered ({type(exc).__name__})",
                            {"error": type(exc).__name__},
                            "Check the credentials, the region and the network, then run the preflight again.")  # fmt: skip
            except json.JSONDecodeError as exc:
                return _row(check_id, "fail", "A policy document AWS returned could not be parsed",
                            {"error": str(exc)},
                            "Check the document the check names in AWS, then run the preflight again.")  # fmt: skip

        return run

    return wrap


def _cluster(clients: PreflightClients, cluster_id: str) -> Mapping[str, Any] | None:
    try:
        found: list[Mapping[str, Any]] = (
            clients.rds.describe_db_clusters(DBClusterIdentifier=cluster_id).get("DBClusters") or []
        )
    except ClientError as exc:
        if _code(exc) == "DBClusterNotFoundFault":
            return None
        raise
    return found[0] if found else None


def _read_parameters(clients: PreflightClients, operation: str, key: str, group: str,
                     names: Mapping[str, str] | set[str]) -> dict[str, str | None]:  # fmt: skip
    values: dict[str, str | None] = {}
    for page in clients.rds.get_paginator(operation).paginate(**{key: group}):
        for p in page.get("Parameters") or []:
            if p.get("ParameterName") in names:
                values[p["ParameterName"]] = p.get("ParameterValue")
    return values


def _wrong(values: Mapping[str, str | None], required: Mapping[str, str]) -> list[str]:
    return [n for n, v in required.items() if str(values.get(n) or "").strip().upper() != v.upper()]


def _settings(
    names: list[str], values: Mapping[str, str | None], required: Mapping[str, str]
) -> str:
    return ", ".join(f"{n}={values.get(n) or 'unset'} (needs {required[n]})" for n in names)


def _writer(cluster: Mapping[str, Any]) -> Mapping[str, Any] | None:
    members: list[Mapping[str, Any]] = cluster.get("DBClusterMembers") or []
    return next((m for m in members if m.get("IsClusterWriter")), None)


@_guarded("aurora_cluster")
def check_aurora_cluster(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                         ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    engine = inputs.contract.target.engine
    replication = inputs.plan.replication
    line = replication.target_engine_line if replication is not None else None
    cluster = _cluster(clients, req.aurora_cluster_id)
    if cluster is None:
        return _row("aurora_cluster", "fail", f"No Aurora cluster {req.aurora_cluster_id} in {req.region}",
                    {"cluster": req.aurora_cluster_id, "region": req.region},
                    "Create the cluster (aurora-target.yaml is a suggested template) or pass its identifier "
                    "with --aurora-cluster-id.")  # fmt: skip
    ctx["cluster"] = cluster
    evidence = {"cluster": req.aurora_cluster_id, "engine": cluster.get("Engine"),
                "engine_version": cluster.get("EngineVersion"), "status": cluster.get("Status"),
                "storage_encrypted": str(bool(cluster.get("StorageEncrypted"))).lower(),
                "minimum_line": line or "none"}  # fmt: skip
    if cluster.get("Engine") != AURORA_ENGINES[engine]:
        return _row("aurora_cluster", "fail", f"The cluster runs {cluster.get('Engine')}; step 1 moves into "
                    f"{AURORA_ENGINES[engine]}", evidence, f"Use a {display_engine(engine)} cluster.")  # fmt: skip
    version_engine = _VERSION_ENGINE[engine]
    have = parse_version_line(version_engine, str(cluster.get("EngineVersion") or ""))
    need = parse_version_line(version_engine, line) if line else None
    if need is not None and (have is None or have < need):
        return _row("aurora_cluster", "fail", f"Version {cluster.get('EngineVersion')} is below the design's "
                    f"{line} line", evidence, f"Upgrade the cluster to the {line} line or later (or create one "
                    "on it) before step 1.")  # fmt: skip
    if cluster.get("Status") != "available":
        return _row("aurora_cluster", "fail", f"The cluster is {cluster.get('Status') or 'in an unknown state'}, "
                    "not available", evidence, "Wait until the cluster is available (or start it), then run "
                    "the preflight again.")  # fmt: skip
    writer = _writer(cluster)
    if writer is not None:
        writer_id = str(writer.get("DBInstanceIdentifier"))
        found = (
            clients.rds.describe_db_instances(DBInstanceIdentifier=writer_id).get("DBInstances")
            or []
        )
        ctx["writer_instance"] = found[0] if found else None
        writer_status = str(found[0].get("DBInstanceStatus") or "unknown") if found else "not found"
        evidence["writer"], evidence["writer_status"] = writer_id, writer_status
        if writer_status != "available":
            return _row("aurora_cluster", "fail", f"The writer instance {writer_id} is {writer_status}, not "
                        "available", evidence, "Wait until the writer instance is available, then run the "
                        "preflight again.")  # fmt: skip
    return _row("aurora_cluster", "pass", f"{display_engine(engine)} cluster {req.aurora_cluster_id}, version "
                f"{cluster.get('EngineVersion')}" + (f" (the {line} line or later)" if line else "")
                + ", available", evidence)  # fmt: skip


def _sql_mode_forbids_zero_dates(value: str | None) -> bool:
    """An unset sql_mode is the engine default, which forbids them. A mode this list does not know
    that expands to the zero-date modes would pass; the ones MySQL documents are covered."""
    modes = {m.strip().upper() for m in (value or "").split(",") if m.strip()}
    return not modes or bool(modes & set(_ZERO_DATE_MODES))


def _not_applied(
    group: str, status: str, writer_id: str, evidence: dict[str, Any]
) -> PreflightCheck:
    """A group whose values are not applied on the writer (anything but ``in-sync``)."""
    if status == _PENDING_REBOOT:
        action = (f"Reboot the writer instance {writer_id} so the parameter group's values apply, then run the "
                  "preflight again.")  # fmt: skip
    elif status == "applying":
        action = "RDS is applying the values; wait until the status is in-sync, then run the preflight again."
    elif status == "failed-to-apply":
        action = (f"RDS could not apply the group's values; check {group}'s values and the instance's recent "
                  "events, fix them, then run the preflight again.")  # fmt: skip
    elif status == "pending-database-upgrade":
        action = "Finish the pending database upgrade, then run the preflight again."
    else:
        action = "RDS reported no apply status; wait until it reports in-sync, then run the preflight again."
    return _row("aurora_parameters", "fail", f"{group}'s apply status on the writer {writer_id} is {status}, "
                f"not {_IN_SYNC}", evidence, f"{action} The status is group-wide: RDS reports one status for the "
                "whole parameter group on this instance, not per parameter.")  # fmt: skip


@_guarded("aurora_parameters")
def check_aurora_parameters(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                            ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    cluster = ctx["cluster"]
    engine = inputs.contract.target.engine
    required = REQUIRED_CLUSTER_PARAMETERS[engine]
    group = str(cluster.get("DBClusterParameterGroup") or "")
    writer = _writer(cluster)
    if writer is None:
        return _row("aurora_parameters", "fail", "The cluster has no writer instance", {"parameter_group": group},
                    "Add a writer instance to the cluster; DMS writes through the writer endpoint.")  # fmt: skip
    writer_id = str(writer.get("DBInstanceIdentifier"))
    cluster_status = str(writer.get("DBClusterParameterGroupStatus") or "unknown")
    evidence = {
        "cluster_parameter_group": group,
        "writer": writer_id,
        "cluster_parameter_status": cluster_status,
    }
    values = _read_parameters(
        clients, "describe_db_cluster_parameters", "DBClusterParameterGroupName", group, required
    )
    # An unset binlog_row_image is the engine default, FULL (the same rule as the source's check).
    if "binlog_row_image" in required and not str(values.get("binlog_row_image") or "").strip():
        values = {**values, "binlog_row_image": "FULL"}
    evidence.update({n: values.get(n) or "unset" for n in required})
    wrong = _wrong(values, required)
    if wrong:
        return _row("aurora_parameters", "fail", f"{group} has {_settings(wrong, values, required)}", evidence,
                    f"Set {', '.join(f'{n}={required[n]}' for n in wrong)} in the cluster parameter group {group}, "
                    f"then reboot the writer instance {writer_id}.")  # fmt: skip

    # The writer's DB parameter group: the instance-level values (local_infile; sql_mode for zero dates).
    if "writer_instance" in ctx:
        found = [ctx["writer_instance"]] if ctx["writer_instance"] else []
    else:
        found = (
            clients.rds.describe_db_instances(DBInstanceIdentifier=writer_id).get("DBInstances")
            or []
        )
    groups = (found[0].get("DBParameterGroups") or []) if found else []
    if not groups:
        return _row("aurora_parameters", "fail", f"The writer {writer_id} has no DB parameter group", evidence,
                    f"Attach a DB parameter group to {writer_id}.")  # fmt: skip
    instance_group = str(groups[0].get("DBParameterGroupName") or "")
    instance_status = str(groups[0].get("ParameterApplyStatus") or "unknown")
    evidence.update({"db_parameter_group": instance_group, "db_parameter_status": instance_status})
    needed = instance_parameters(inputs)
    fixed = dict(REQUIRED_INSTANCE_PARAMETERS[engine])
    ivalues = _read_parameters(clients, "describe_db_parameters", "DBParameterGroupName", instance_group,
                               set(needed)) if needed else {}  # fmt: skip
    evidence.update({n: ivalues.get(n) or "unset" for n in needed})
    bad = _wrong(ivalues, fixed)
    if bad:
        return _row("aurora_parameters", "fail", f"{instance_group} has {_settings(bad, ivalues, fixed)}", evidence,
                    f"Set {', '.join(f'{n}={fixed[n]}' for n in bad)} in the DB parameter group {instance_group}, "
                    f"then reboot the writer instance {writer_id}.")  # fmt: skip
    if "sql_mode" in needed and _sql_mode_forbids_zero_dates(ivalues.get("sql_mode")):
        return _row("aurora_parameters", "fail", f"{instance_group} has sql_mode={ivalues.get('sql_mode') or 'unset'}, "
                    "which forbids the zero dates this step copies", evidence,
                    f"Set sql_mode to a value without {', '.join(_ZERO_DATE_MODES)} (aurora-target.yaml "
                    f"uses {ZERO_DATE_SQL_MODE}) in the DB parameter group {instance_group}, then reboot "
                    f"the writer instance {writer_id}.")  # fmt: skip

    for name, status in ((group, cluster_status), (instance_group, instance_status)):
        if status != _IN_SYNC:
            return _not_applied(name, status, writer_id, evidence)
    shown = {**{n: values[n] for n in required}, **{n: ivalues[n] for n in needed}}
    return _row("aurora_parameters", "pass", f"{group} and {instance_group} have "
                f"{', '.join(f'{n}={v}' for n, v in shown.items())}, applied on the writer", evidence,
                checklist=[f"Every instance of the cluster uses the DB parameter group {instance_group}, so the "
                           "settings still apply after a failover (only the writer is read here)"])  # fmt: skip


def _aws_managed(kms: str) -> bool:
    return kms == "alias/aws/secretsmanager" or kms.endswith(":alias/aws/secretsmanager")


def _resolved_key(clients: PreflightClients, kms: str) -> tuple[str, str]:
    """(key ARN, key id) of a secret's KmsKeyId, which may be an alias (resolved with DescribeKey)."""
    if "alias/" not in kms:
        return kms, kms.rsplit("/", 1)[-1]
    meta = clients.kms.describe_key(KeyId=kms).get("KeyMetadata") or {}
    return str(meta.get("Arn") or kms), str(meta.get("KeyId") or "")


def _named(arn: str, key_id: str, given: str) -> bool:
    return (
        bool(given)
        and "alias/" not in given
        and (given == arn or given.rsplit("/", 1)[-1] == key_id)
    )


def _principal_is_everyone(st: Mapping[str, Any]) -> bool:
    principal = st.get("Principal")
    return principal == "*" or (isinstance(principal, Mapping) and principal.get("AWS") == "*")


def _covers_get_secret_value(st: Mapping[str, Any]) -> bool:
    """Whether a statement's Action (or NotAction) reaches secretsmanager:GetSecretValue."""
    target = "secretsmanager:getsecretvalue"

    def listed(key: str) -> bool:
        raw = st.get(key)
        patterns = [raw] if isinstance(raw, str) else list(raw or [])
        return any(fnmatch.fnmatch(target, str(p).lower()) for p in patterns)

    return not listed("NotAction") if "NotAction" in st else listed("Action")


def _deny_statements(policy: str | None) -> tuple[list[str], list[str]]:
    """(hard, undecided): the Sids of Deny statements that reach GetSecretValue. Hard ones deny everyone
    unconditionally. Undecided ones carry a Condition or name a principal, so whether they bind the stack's
    role cannot be decided from metadata."""
    if not policy:
        return [], []
    statements = json.loads(policy).get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    hard: list[str] = []
    undecided: list[str] = []
    for i, st in enumerate(statements):
        if st.get("Effect") != "Deny" or not _covers_get_secret_value(st):
            continue
        sid = str(st.get("Sid") or f"statement {i + 1}")
        if "Condition" not in st and _principal_is_everyone(st):
            hard.append(sid)
        else:
            undecided.append(sid)
    return hard, undecided


@_guarded("secrets")
def check_secrets(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                  ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    evidence: dict[str, str] = {}
    checklist: list[str] = []
    undecided_all: list[str] = []
    for role, arn, key in (("source", req.source_secret_arn, req.source_secret_kms_key_arn),
                           ("target", req.target_secret_arn, req.target_secret_kms_key_arn)):  # fmt: skip
        try:
            desc = clients.secretsmanager.describe_secret(SecretId=arn)
        except ClientError as exc:
            if _code(exc) != "ResourceNotFoundException":
                raise
            return _row("secrets", "fail", f"No {role} secret {arn}", evidence,
                        "Create it with username, password, host and port keys (the DMS endpoint reads them) "
                        f"and pass its full ARN as --{role}-secret-arn.")  # fmt: skip
        kms = str(desc.get("KmsKeyId") or "")
        evidence[f"{role}_secret"] = arn
        evidence[f"{role}_kms_key"] = kms or "aws/secretsmanager"
        if desc.get("DeletedDate"):
            return _row("secrets", "fail", f"The {role} secret is scheduled for deletion", evidence,
                        "Restore it (aws secretsmanager restore-secret) or pass another secret.")  # fmt: skip
        if kms and not _aws_managed(kms):
            key_arn, key_id = _resolved_key(clients, kms)
            if key_arn != kms:
                evidence[f"{role}_kms_key_resolved"] = key_arn
            if not _named(key_arn, key_id, key):
                return _row("secrets", "fail", f"The {role} secret is encrypted with {key_arn}", evidence,
                            f"Pass that key's ARN ({key_arn}) as --{role}-secret-kms-key-arn and as the stack's "
                            f"{role.capitalize()}SecretKmsKeyArn parameter, so the stack's role may decrypt it.")  # fmt: skip
        hard, undecided = _deny_statements(
            clients.secretsmanager.get_resource_policy(SecretId=arn).get("ResourcePolicy"))  # fmt: skip
        evidence[f"{role}_policy_denies"] = ", ".join(hard) or "none"
        if undecided:
            evidence[f"{role}_policy_conditional_denies"] = ", ".join(undecided)
        if hard:
            return _row("secrets", "fail", f"The {role} secret's resource policy denies everyone reading it "
                        f"({', '.join(hard)})", evidence,
                        "Remove or narrow that Deny so the DMS step's role (SecretsAccessRole in dms-step-1.yaml, "
                        f"assumed by dms.{req.region}.amazonaws.com) may read the secret, then run the preflight "
                        "again.")  # fmt: skip
        undecided_all += [f"{sid} ({role} secret)" for sid in undecided]
        checklist += [f"The {role} secret's resource policy has a Deny of secretsmanager:GetSecretValue ({sid}) "
                      "with a condition or a named principal; confirm it does not apply to SecretsAccessRole "
                      "(dms-step-1.yaml)." for sid in undecided]  # fmt: skip
    if checklist:
        return _row("secrets", "confirm", "A resource policy has a Deny the preflight cannot decide from metadata: "
                    + ", ".join(undecided_all), evidence,
                    "Check each item, then run the preflight again with --confirm secrets.", checklist)  # fmt: skip
    return _row("secrets", "pass", "Both secrets exist; nothing denies the stack's role reading these secrets "
                "(the stack is not deployed yet; values not read)", evidence)  # fmt: skip


# --- Checks 4 to 8 ------------------------------------------------------------------------

_FAMILY = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgresql"}
SOURCE_PARAMETERS: dict[str, dict[str, str]] = {
    "mysql": {"binlog_format": "ROW", "binlog_row_image": "FULL"},
    "postgresql": {"rds.logical_replication": "1"},
}
# What the AWS API cannot read on an RDS or Aurora source.
UNREADABLE: dict[str, list[str]] = {
    "mysql": ["Binary log retention covers the full load plus an outage: CALL mysql.rds_set_configuration("
              "'binlog retention hours', 24); (binlog retention hours is not readable through the AWS API; read "
              "it on the source with CALL mysql.rds_show_configuration;)"],
    "postgresql": [],
}  # fmt: skip
_TLS_ITEM = (
    "The source serves TLS with a certificate authority you can import into DMS "
    "(aws dms import-certificate) and accepts connections from the replication instance's subnets"
)
_GRANTS_ITEM = (
    "The DMS user has the grants step-1.ddl.sql names for the source: %s, and SELECT on every "
    "step 1 table"
)
# The PostgreSQL settings that bound what the DMS slot may keep (#2060): read with the CDC settings.
SLOT_PARAMETERS = ("max_replication_slots", "max_wal_senders", "max_slot_wal_keep_size")
_SLOT_LIMIT_ITEM = (
    "max_slot_wal_keep_size (PostgreSQL 13 and later) covers the longest pause of the task you expect: "
    "past it, PostgreSQL invalidates the DMS slot and the task needs a full reload; at -1 (no limit) the "
    "slot keeps WAL until the source's storage fills, so watch pg_replication_slots while the step runs"
)
_REPLICA_IDENTITY_ITEM = "Every table without a primary key has REPLICA IDENTITY FULL, or DMS cannot replicate its updates and deletes"
# The task turns the DMS heartbeat on (HeartbeatEnable), which creates its heartbeat table in the
# heartbeat schema, public by default; from PostgreSQL 15, public grants CREATE to its owner only.
HEARTBEAT_SCHEMA = "public"
_HEARTBEAT_ITEM = (
    f"The source user has CREATE on the heartbeat schema {HEARTBEAT_SCHEMA} (GRANT CREATE ON SCHEMA "
    f"{HEARTBEAT_SCHEMA} TO <source user>): the task's DMS heartbeat creates its table there, and from "
    "PostgreSQL 15 that schema grants CREATE to its owner only, so without it the task fails at start"
)
SELF_MANAGED: dict[str, list[str]] = {
    "mysql": [
        "binlog_format = ROW and binlog_row_image = FULL",
        "Binary log retention (binlog_expire_logs_seconds) covers the full load plus an outage",
        _GRANTS_ITEM % "REPLICATION CLIENT and REPLICATION SLAVE",
        _TLS_ITEM,
    ],
    "postgresql": [
        "wal_level = logical",
        "max_replication_slots and max_wal_senders leave room for the DMS slot",
        _SLOT_LIMIT_ITEM,
        _REPLICA_IDENTITY_ITEM,
        _GRANTS_ITEM % "the REPLICATION attribute",
        _HEARTBEAT_ITEM,
        _TLS_ITEM,
    ],
}  # fmt: skip
# What the AWS API cannot read on an RDS or Aurora source: still a checklist, so the row is ``confirm``.
MANAGED: dict[str, list[str]] = {
    "mysql": [
        *UNREADABLE["mysql"],
        _GRANTS_ITEM % "REPLICATION CLIENT and REPLICATION SLAVE",
        _TLS_ITEM,
    ],
    "postgresql": [
        "The DMS user is a member of the rds_replication role (GRANT rds_replication TO the DMS user), "
        "and has SELECT on every step 1 table",
        _REPLICA_IDENTITY_ITEM,
        _HEARTBEAT_ITEM,
        _TLS_ITEM,
    ],
}
# The engines the plan's source family may run on, per RDS API ``Engine``.
_RDS_ENGINES = {
    "mysql": ("mysql", "mariadb", "aurora-mysql"),
    "postgresql": ("postgres", "aurora-postgresql"),
}
_SESSION_ROLE_ITEM = (
    "On the Aurora PostgreSQL target, the user in the target secret may set "
    "session_replication_role (DMS sets it to replica on its connection)"
)


@_guarded("instance_class_offered")
def check_instance_class(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                         ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    # Regional: the API lists the classes orderable in the region, not per Availability Zone, so a class
    # can still be unavailable in the zone a subnet picks (the stack's create then fails with a capacity error).
    cls = ctx["instance_class"]
    offered: set[str] = set()
    for page in clients.dms.get_paginator("describe_orderable_replication_instances").paginate():
        offered |= {
            str(o.get("ReplicationInstanceClass"))
            for o in page.get("OrderableReplicationInstances") or []
        }
    evidence = {"instance_class": cls, "region": req.region, "offered_classes": len(offered)}
    if cls not in offered:
        return _row("instance_class_offered", "fail", f"{cls} is not offered in {req.region}", evidence,
                    "Pass an offered class with --instance-class and as the stack's InstanceClass parameter: "
                    + ", ".join(sorted(c for c in offered if c.startswith("dms."))[:12]))  # fmt: skip
    return _row("instance_class_offered", "pass", f"{cls} is offered in {req.region}", evidence)


def _trusts_dms(doc: Any, region: str) -> bool:
    """Whether a trust policy lets the DMS service principal (global or regional) assume the role."""
    if isinstance(doc, str):
        doc = json.loads(unquote(doc))
    statements = doc.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    principals = {"dms.amazonaws.com", f"dms.{region}.amazonaws.com"}
    for st in statements:
        if st.get("Effect") != "Allow":
            continue
        actions = (
            [st["Action"]] if isinstance(st.get("Action"), str) else list(st.get("Action") or [])
        )
        raw = (st.get("Principal") or {}) if isinstance(st.get("Principal"), Mapping) else {}
        services = (
            [raw["Service"]]
            if isinstance(raw.get("Service"), str)
            else list(raw.get("Service") or [])
        )
        if principals & set(services) and any(
            a in ("sts:AssumeRole", "sts:*", "*") for a in actions
        ):
            return True
    return False


@_guarded("dms_service_roles")
def check_dms_service_roles(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                            ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    problems: dict[str, str] = {}
    for name, policy in DMS_SERVICE_ROLES.items():
        try:
            role = clients.iam.get_role(RoleName=name).get("Role") or {}
        except ClientError as exc:
            if _code(exc) != "NoSuchEntity":
                raise
            problems[name] = "missing"
            continue
        if not _trusts_dms(role.get("AssumeRolePolicyDocument") or {}, req.region):
            problems[name] = "its trust policy does not let dms.amazonaws.com assume it"
        attached = _paged(
            clients.iam, "list_attached_role_policies", "AttachedPolicies", RoleName=name
        )
        if policy not in {str(p.get("PolicyName")) for p in attached}:
            missing_policy = f"{policy} is not attached"
            problems[name] = (
                f"{problems[name]}; {missing_policy}" if name in problems else missing_policy
            )
    evidence = {name: problems.get(name, "ok") for name in DMS_SERVICE_ROLES}
    if problems:
        return _row("dms_service_roles", "fail", "DMS service roles to fix: "
                    + "; ".join(f"{n} ({why})" for n, why in problems.items()), evidence,
                    "DMS needs dms-vpc-role (trusting dms.amazonaws.com, with AmazonDMSVPCManagementRole) to "
                    "create the replication subnet group and dms-cloudwatch-logs-role (trusting "
                    "dms.amazonaws.com, with AmazonDMSCloudWatchLogsRole) for the task's logs; create or fix "
                    "them once per account (README).")  # fmt: skip
    return _row("dms_service_roles", "pass", "The DMS service roles exist, trust DMS and have their managed "
                "policies", evidence)  # fmt: skip


def _checklist_row(summary: str, evidence: dict[str, Any], items: list[str]) -> PreflightCheck:
    if not items:
        return _row("source_cdc", "pass", summary, evidence)
    heartbeat = (f" On PostgreSQL 15 and later, that includes CREATE on the heartbeat schema "
                 f"{HEARTBEAT_SCHEMA} for the source user." if _HEARTBEAT_ITEM in items else "")  # fmt: skip
    return _row("source_cdc", "confirm", summary + "; confirm the checklist", evidence,
                "Check each item on the source, then run the preflight again with --confirm source_cdc "
                f"(alias --confirm-source-checklist).{heartbeat}", items)  # fmt: skip


def _not_in_sync(group: str, status: str, reboot: str, evidence: dict[str, Any]) -> PreflightCheck:
    action = reboot if status == _PENDING_REBOOT else (
        f"RDS reports {status} for {group}; wait until it is {_IN_SYNC} (or fix the group), then run the "
        "preflight again.")  # fmt: skip
    return _row("source_cdc", "fail", f"The source's {group} apply status is {status}, not {_IN_SYNC}",
                evidence, action)  # fmt: skip


def _engine_mismatch(resource: Mapping[str, Any], family: str, source_id: str, kind: str,
                     evidence: dict[str, Any]) -> PreflightCheck | None:  # fmt: skip
    """The source's RDS ``Engine`` must be in the plan's source family and of the kind: an RDS instance
    runs mysql, mariadb or postgres, an Aurora cluster an Aurora engine."""
    found = str(resource.get("Engine") or "unknown")
    if found in _RDS_ENGINES[family] and (
        found.startswith("aurora-") == (kind == "aurora_cluster")
    ):
        return None
    other = "aurora_cluster" if kind == "rds_instance" else "rds_instance"
    if found in _RDS_ENGINES[family]:
        return _row("source_cdc", "fail", f"{source_id} runs {found}, which is not what --source-kind {kind} "
                    "reads", evidence | {"engine": found}, f"Use --source-kind {other} for {source_id}.")  # fmt: skip
    return _row("source_cdc", "fail", f"{source_id} runs {found}; the plan's source is a {family} database",
                evidence | {"engine": found}, "Pass the identifier of the source database the plan assesses "
                "with --source-id.")  # fmt: skip


@_guarded("source_cdc")
def check_source_cdc(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                     ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    engine = inputs.plan.source.engine
    family = _FAMILY.get(engine)
    if family is None:
        return _row("source_cdc", "fail", f"No CDC check for a {engine} source", {"source_engine": engine},
                    "Step 1 moves MySQL, MariaDB and PostgreSQL sources only.")  # fmt: skip
    required = SOURCE_PARAMETERS[family]
    evidence: dict[str, Any] = {"source_kind": req.source_kind, "source_engine": engine}
    if req.source_kind == "self_managed":
        planned = [f"{x.name} = {x.value}" + (f": {x.note}" if getattr(x, "note", None) else "")
                   for x in inputs.plan.cdc.required_source_settings]  # fmt: skip
        extra = [_SESSION_ROLE_ITEM] if inputs.contract.target.engine == "aurora_postgresql" else []
        items = list(dict.fromkeys([*planned, *SELF_MANAGED[family], *extra]))
        return _checklist_row("Self-managed source: its CDC settings are not readable through the AWS API",
                              evidence, items)  # fmt: skip
    if not req.source_id:
        return _row("source_cdc", "fail", f"No source identifier for a {req.source_kind} source", evidence,
                    "Pass the source's identifier with --source-id, or use --source-kind self_managed.")  # fmt: skip
    source_id = req.source_id
    if req.source_kind == "rds_instance":
        try:
            found = (
                clients.rds.describe_db_instances(DBInstanceIdentifier=source_id).get("DBInstances")
                or []
            )
        except ClientError as exc:
            if _code(exc) != "DBInstanceNotFound":
                raise
            found = []
        if not found:
            return _row("source_cdc", "fail", f"No RDS instance {source_id}", evidence,
                        "Pass the source's identifier with --source-id, or --source-kind self_managed.")  # fmt: skip
        inst = found[0]
        mismatch = _engine_mismatch(inst, family, source_id, req.source_kind, evidence)
        if mismatch:
            return mismatch
        group_row = (inst.get("DBParameterGroups") or [{}])[0]
        group = str(group_row.get("DBParameterGroupName") or "")
        pending = str(group_row.get("ParameterApplyStatus") or "unknown")
        values = _read_parameters(
            clients,
            "describe_db_parameters",
            "DBParameterGroupName",
            group,
            _names(required, family),
        )
        retention = inst.get("BackupRetentionPeriod")
        evidence |= {
            "source": source_id,
            "parameter_group": group,
            "backup_retention_days": retention,
        }
        if family == "mysql" and not retention:
            return _row("source_cdc", "fail", f"Automated backups are off on {source_id}, so binary logging is off",
                        evidence, "Set a backup retention period of at least 1 day on the source.")  # fmt: skip
        reboot = f"Reboot the source instance {source_id} so its parameter group's values apply."
    else:
        cluster = _cluster(clients, source_id)
        if cluster is None:
            return _row("source_cdc", "fail", f"No Aurora cluster {source_id}", evidence,
                        "Pass the source's identifier with --source-id, or --source-kind self_managed.")  # fmt: skip
        mismatch = _engine_mismatch(cluster, family, source_id, req.source_kind, evidence)
        if mismatch:
            return mismatch
        group = str(cluster.get("DBClusterParameterGroup") or "")
        writer = _writer(cluster) or {}
        pending = str(writer.get("DBClusterParameterGroupStatus") or "unknown")
        values = _read_parameters(clients, "describe_db_cluster_parameters", "DBClusterParameterGroupName",
                                  group, _names(required, family))  # fmt: skip
        evidence |= {"source": source_id, "parameter_group": group}
        reboot = f"Reboot the source writer instance {writer.get('DBInstanceIdentifier')} so the values apply."
    # An unset binlog_row_image is the engine default, FULL; an unset binlog_format is not ROW.
    defaulted = family == "mysql" and not str(values.get("binlog_row_image") or "").strip()
    if defaulted:
        values = {**values, "binlog_row_image": "FULL"}
    evidence |= {n: values.get(n) or "unset" for n in required}
    if defaulted:
        evidence["binlog_row_image"] = "FULL (engine default)"
    wrong = _wrong(values, required)
    if wrong:
        return _row("source_cdc", "fail", f"The source's {group} has {_settings(wrong, values, required)}",
                    evidence, f"Set {', '.join(f'{n}={required[n]}' for n in wrong)} in {group}, then reboot "
                    "the source.")  # fmt: skip
    slots: list[str] = []
    if family == "postgresql":
        evidence |= _slot_evidence(values)
        no_room = [n for n in SLOT_PARAMETERS[:2] if _integer(values.get(n)) == 0]
        if no_room:
            return _row("source_cdc", "fail", f"The source's {group} has "
                        + ", ".join(f"{n}={values.get(n)}" for n in no_room), evidence,
                        f"Set {' and '.join(no_room)} to at least 1 (DMS needs one replication slot and one WAL "
                        f"sender) in {group}, then reboot the source.")  # fmt: skip
        slots = [_slot_limit_item(values.get("max_slot_wal_keep_size"))]
    if pending != _IN_SYNC:
        return _not_in_sync(group, pending, reboot, evidence)
    shown = ", ".join(f"{n}={v}" + (" (engine default FULL)" if n == "binlog_row_image" and defaulted else "")
                      for n, v in required.items())  # fmt: skip
    extra = [_SESSION_ROLE_ITEM] if inputs.contract.target.engine == "aurora_postgresql" else []
    return _checklist_row(f"The source has {shown}", evidence, [*MANAGED[family], *slots, *extra])


def _names(required: Mapping[str, str], family: str) -> set[str]:
    return set(required) | (set(SLOT_PARAMETERS) if family == "postgresql" else set())


def _integer(value: str | None) -> int | None:
    """A parameter's value as an integer; None when unset or an RDS formula ({DBInstanceClassMemory...})."""
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _slot_evidence(values: Mapping[str, str | None]) -> dict[str, str]:
    out = {n: str(values.get(n) or "engine default") for n in SLOT_PARAMETERS}
    if not values.get("max_slot_wal_keep_size"):
        out["max_slot_wal_keep_size"] = "engine default (-1, no limit)"
    return out


def _slot_limit_item(value: str | None) -> str:
    """What the source's max_slot_wal_keep_size means for this move (#2060)."""
    limit = _integer(value)
    if limit is None or limit < 0:
        shown = "the engine default, -1" if not value else value
        return (f"max_slot_wal_keep_size (PostgreSQL 13 and later) is {shown} (no limit; on PostgreSQL 12 "
                "and earlier the setting does not exist and a slot keeps WAL the same way): while the task is "
                "stopped or behind, its "
                "replication slot keeps WAL until an RDS source's storage fills (Aurora's volume grows and bills it); /move's status warns on the "
                "source's slot lag and free storage, and ending the step names the slot to drop")  # fmt: skip
    return (f"max_slot_wal_keep_size is {limit} MB: past that much retained WAL, PostgreSQL invalidates the "
            "DMS slot and the task needs a full reload, so it must cover the longest pause of the task you "
            "expect")  # fmt: skip


@_guarded("dms_certificates")
def check_dms_certificates(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                           ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    """MySQL-family endpoints verify the server certificate (verify-ca), so their certificate must be
    there and unexpired; PostgreSQL endpoints default to ``require`` and need one only when passed.
    """
    now: datetime = ctx["now"]
    endpoints = (("source", SOURCE_ENDPOINTS[inputs.plan.source.engine][1], req.source_certificate_arn),
                 ("target", TARGET_ENDPOINTS[inputs.contract.target.engine][1], req.target_certificate_arn))  # fmt: skip
    evidence: dict[str, Any] = {}
    for role, settings_key, arn in endpoints:
        mysql = settings_key == "MySqlSettings"
        flag = f"--{role}-certificate-arn"
        if not arn:
            if mysql:
                return _row("dms_certificates", "fail", f"The {role} endpoint verifies the server certificate "
                            "(verify-ca) and no certificate ARN was given", evidence,
                            f"Import the {role} database's CA bundle (aws dms import-certificate) and pass its "
                            f"ARN as {flag} and as the stack's {role.capitalize()}CertificateArn parameter.")  # fmt: skip
            evidence[f"{role}_certificate"] = "none (ssl mode require)"
            continue
        found: list[Any] = []
        for page in clients.dms.get_paginator("describe_certificates").paginate(
                Filters=[{"Name": "certificate-arn", "Values": [arn]}]):  # fmt: skip
            found += page.get("Certificates") or []
        evidence[f"{role}_certificate"] = arn
        if not found:
            return _row("dms_certificates", "fail", f"No DMS certificate {arn} in {req.region}", evidence,
                        f"Import the CA bundle (aws dms import-certificate) in {req.region} and pass the new "
                        f"ARN as {flag}.")  # fmt: skip
        valid_to = found[0].get("ValidToDate")
        evidence[f"{role}_valid_to"] = valid_to.isoformat() if valid_to else "unknown"
        if valid_to is None or valid_to <= now:
            return _row("dms_certificates", "fail", f"The {role} certificate {arn} expired "
                        f"({evidence[f'{role}_valid_to']})" if valid_to else
                        f"The {role} certificate {arn} has no expiry date", evidence,
                        f"Import a current CA bundle (aws dms import-certificate) and pass its ARN as {flag}.")  # fmt: skip
    return _row(
        "dms_certificates", "pass", "The endpoint certificates exist and have not expired", evidence
    )


def _paged(clients_api: Any, operation: str, key: str, **params: Any) -> list[Any]:
    out: list[Any] = []
    for page in clients_api.get_paginator(operation).paginate(**params):
        out += page.get(key) or []
    return out


def _default_route(tables: list[Any]) -> str:
    """What a subnet's 0.0.0.0/0 route goes to: ``nat`` (a NAT gateway), ``none`` (no such route, or only an
    internet gateway or blackhole) or ``other`` (anything else: a transit or virtual private gateway, a NAT
    instance, a network interface, a peering connection; this check cannot judge it)."""
    kinds = set()
    for table in tables:
        for r in table.get("Routes") or []:
            if (
                r.get("DestinationCidrBlock") != "0.0.0.0/0"
                or r.get("State", "active") == "blackhole"
            ):
                continue
            if r.get("NatGatewayId"):
                kinds.add("nat")
            elif not str(r.get("GatewayId") or "").startswith("igw-"):
                # A transit, virtual private or local gateway, a peering connection, an instance or an
                # interface: where it leads is not decidable here.
                kinds.add("other")
    return "nat" if "nat" in kinds else "other" if "other" in kinds else "none"


_LIMIT = (
    "This check recognises only NAT gateways and interface endpoints; a transit gateway, NAT instance "
    "or network interface default route needs --confirm secrets_manager_reachable once you have "
    "checked it reaches Secrets Manager."
)


@_guarded("secrets_manager_reachable")
def check_secrets_manager_reachable(inputs: Step1Inputs, req: PreflightRequest, clients: PreflightClients,
                                    ctx: dict[str, Any]) -> PreflightCheck:  # fmt: skip
    """DMS reads the endpoint secrets from the replication instance's subnets, so they need a path to
    Secrets Manager: an interface endpoint with private DNS, or a NAT gateway route."""
    if req.secrets_manager_endpoint_override:
        return _row("secrets_manager_reachable", "pass", "The stack is given a Secrets Manager endpoint "
                    f"({req.secrets_manager_endpoint_override}); its reachability is not checked",
                    {"secrets_manager_endpoint_override": req.secrets_manager_endpoint_override})  # fmt: skip
    if not req.subnet_ids:
        return _row("secrets_manager_reachable", "fail", "No replication subnets were given", {},
                    "Pass the subnets the stack will use with --subnet-ids (the stack's SubnetIds).")  # fmt: skip
    subnets = _paged(clients.ec2, "describe_subnets", "Subnets", SubnetIds=list(req.subnet_ids))
    vpcs = sorted({str(s.get("VpcId")) for s in subnets})
    evidence: dict[str, Any] = {"subnets": ", ".join(req.subnet_ids), "vpcs": ", ".join(vpcs)}
    if len(vpcs) != 1 or len(subnets) != len(set(req.subnet_ids)):
        return _row("secrets_manager_reachable", "fail", "The replication subnets are not all found in one VPC",
                    evidence, "Pass subnets of one VPC with --subnet-ids.")  # fmt: skip
    vpc = vpcs[0]
    service = f"com.amazonaws.{req.region}.secretsmanager"
    endpoints = _paged(clients.ec2, "describe_vpc_endpoints", "VpcEndpoints", Filters=[
        {"Name": "vpc-id", "Values": [vpc]}, {"Name": "service-name", "Values": [service]},
        {"Name": "vpc-endpoint-type", "Values": ["Interface"]}])  # fmt: skip
    evidence["secretsmanager_endpoints"] = ", ".join(
        f"{e.get('VpcEndpointId')} ({e.get('State')}, private DNS {bool(e.get('PrivateDnsEnabled'))})"
        for e in endpoints) or "none"  # fmt: skip
    ready = next(
        (e for e in endpoints if e.get("State") == "available" and e.get("PrivateDnsEnabled")), None
    )
    if ready:
        groups = (
            ", ".join(str(g.get("GroupId")) for g in ready.get("Groups") or [])
            or "its security groups"
        )
        return _row("secrets_manager_reachable", "pass", f"VPC {vpc} has the Secrets Manager interface endpoint "
                    f"{ready.get('VpcEndpointId')} with private DNS", evidence,
                    checklist=[f"The endpoint's security group ({groups}) allows TCP 443 from the replication "
                               "instance (the endpoint's security group is not read here)"])  # fmt: skip
    routes: dict[str, str] = {}
    for subnet in req.subnet_ids:
        tables = _paged(clients.ec2, "describe_route_tables", "RouteTables",
                        Filters=[{"Name": "association.subnet-id", "Values": [subnet]}])  # fmt: skip
        if not tables:
            tables = _paged(clients.ec2, "describe_route_tables", "RouteTables", Filters=[
                {"Name": "vpc-id", "Values": [vpc]}, {"Name": "association.main", "Values": ["true"]}])  # fmt: skip
        routes[subnet] = _default_route(tables)
    without = [n for n, kind in routes.items() if kind == "none"]
    other = [n for n, kind in routes.items() if kind == "other"]
    evidence["subnets_without_default_route"] = ", ".join(without) or "none"
    evidence["subnets_with_other_default_route"] = ", ".join(other) or "none"
    if without:
        return _row("secrets_manager_reachable", "fail", f"VPC {vpc} has no Secrets Manager interface endpoint "
                    f"with private DNS, and {', '.join(without)} "
                    f"{'has no 0.0.0.0/0 route' if len(without) == 1 else 'have no 0.0.0.0/0 route'} to a NAT gateway",
                    evidence, f"Either create a {service} interface endpoint in {vpc} with private DNS enabled "
                    "(or pass its DNS name as the stack's SecretsManagerEndpointOverride), or route each "
                    f"replication subnet's 0.0.0.0/0 to a NAT gateway. {_LIMIT}")  # fmt: skip
    if other:
        return _row("secrets_manager_reachable", "confirm", f"{', '.join(other)} route 0.0.0.0/0 to a transit "
                    "gateway, NAT instance or network interface; confirm; whether it reaches Secrets Manager is "
                    "not decidable here", evidence, _LIMIT,
                    [f"{n}'s 0.0.0.0/0 route reaches Secrets Manager (and allows its TCP 443 traffic)"
                     for n in other])  # fmt: skip
    return _row("secrets_manager_reachable", "pass", "Every replication subnet routes 0.0.0.0/0 to a NAT "
                "gateway", evidence)  # fmt: skip


CHECKS: tuple[tuple[str, Callable[..., PreflightCheck]], ...] = (
    ("aurora_cluster", check_aurora_cluster),
    ("aurora_parameters", check_aurora_parameters),
    ("secrets", check_secrets),
    ("instance_class_offered", check_instance_class),
    ("dms_service_roles", check_dms_service_roles),
    ("source_cdc", check_source_cdc),
    ("dms_certificates", check_dms_certificates),
    ("secrets_manager_reachable", check_secrets_manager_reachable),
)


def _confirmed(row: PreflightCheck) -> PreflightCheck:
    return row.model_copy(update={"status": "pass", "summary": row.summary.replace("; confirm the checklist", "").replace("; confirm; ", "; ")
                                  + "; confirmed by the operator",
                                  "evidence": {**row.evidence, "confirmed_by": "the operator"}})  # fmt: skip


def run_preflight(inputs: Step1Inputs, request: PreflightRequest, clients: PreflightClients, *,
                  now: datetime, confirmed: set[str] | frozenset[str] = frozenset()) -> DmsPreflight:  # fmt: skip
    """Run the checks in order, stop at the first failure (the rest are ``not_run``). A ``confirm`` row
    whose id is in ``confirmed`` becomes ``pass`` ("confirmed by the operator")."""
    unknown = sorted(set(confirmed) - set(CHECK_ORDER))
    if unknown:
        raise ValueError(
            f"unknown check ids to confirm: {', '.join(unknown)}; the ids are {', '.join(CHECK_ORDER)}"
        )
    if inputs.plan.replication is None:
        raise DmsStepError("the step 1 plan has no replication section: run the plan script again")
    ctx: dict[str, Any] = {"instance_class": request.instance_class or inputs.plan.replication.sizing.instance_class,
                           "now": now}  # fmt: skip
    rows: list[PreflightCheck] = []
    failed = False
    for check_id, check in CHECKS:
        if failed:
            rows.append(_row(check_id, "not_run", "Not run: an earlier check failed"))
            continue
        row = check(inputs, request, clients, ctx)
        if row.status == "confirm" and check_id in confirmed:
            row = _confirmed(row)
        rows.append(row)
        failed = row.status == "fail"
    status: Literal["pass", "fail", "needs_confirmation"] = (
        "fail"
        if failed
        else ("needs_confirmation" if any(r.status == "confirm" for r in rows) else "pass")
    )
    return DmsPreflight(job_id=inputs.plan.job_id, database_name=inputs.plan.database_name, region=request.region,
                        checked_at=now, contract_sha256=inputs.contract_sha256, instance_class=ctx["instance_class"],
                        status=status, checks=rows)  # fmt: skip


def write_preflight(store: Any, db: str, job_id: str, request: PreflightRequest, clients: PreflightClients, *,
                    now: datetime | None = None, confirmed: set[str] | frozenset[str] = frozenset()
                    ) -> tuple[str, DmsPreflight]:  # fmt: skip
    inputs = load_step1(store, db, job_id, with_ddl=False)
    result = run_preflight(
        inputs, request, clients, now=now or datetime.now(UTC), confirmed=confirmed
    )
    path = f"{db}/{job_id}/data-movement/step-1/preflight.json"
    store.write_json(path, result.model_dump(mode="json"))
    return path, result
