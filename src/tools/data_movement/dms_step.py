# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's delivery: the DMS stack, the suggested Aurora template and the DDL file (#1798).

ADR-033 Decisions 7 and 10. Step 1 moves the whole source database into the Aurora engine
the assessment chose, by AWS DMS: full load, then ongoing replication, into a cluster the
customer owns. From the step 1 plan (``data-movement/step-1/plan.json``), its transformation
contract (``transformation-contract/v<N>/step-1.json``, pinned by its SHA-256) and the DDL
of every table the contract moves, this module renders:

- ``aurora-target.yaml``: a suggested cluster template, headed by the customer-responsibility
  disclaimer and the settings the cluster must have, each with its reason;
- ``step-1.ddl.sql``: the tables' DDL with instructions to run it (on Aurora PostgreSQL, a
  commented ``REPLICA IDENTITY FULL`` recommendation per table);
- ``dms-step-1.yaml``: our stack: a replication subnet group, a provisioned replication
  instance, source and target endpoints on Secrets Manager ARNs, one full-load-and-CDC task.

The DDL is the Aurora DDL generator's, over the whole collector, with every column declared
as the contract's ``target_type`` (which carries the saved design's type where there is one).
It is never the saved design's ``generated_ddl`` alone: a saved design covers only the
tables routed to Aurora, and step 1 moves every wave 1 table.

The contract compiles into table mappings with one explicit selection rule per rule (no
wildcard: no source schema changes during the migration window in this release) and no
transformation rule: the customer's DDL created the tables (``TargetTablePrepMode:
DO_NOTHING``), so Aurora converts each value into the declared column type on insert, and a
conversion it cannot do is an ``unsupported`` field in the contract. A blocked rule is not
selected; ``Step1Inputs.skipped`` says which and why. Nothing here calls AWS, and nothing we
deploy runs inside the customer's cluster.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.data_movement_plan import DataMovementPlan, DmsReplication, UnboundedVarchar
from src.contracts.lineage_ops import CopyOp, UnsupportedOp
from src.contracts.transformation_contract import Rule, StepContract
from src.shared.contract_versioning import load_contract
from src.shared.engine_names import display_engine
from src.tools.transformation_contract.hashing import contract_sha256

STEP_DIR = "data-movement/step-1"
AURORA_TEMPLATE = "aurora-target.yaml"
DDL_FILE = "step-1.ddl.sql"
DMS_TEMPLATE = "dms-step-1.yaml"
AURORA_ENGINES: dict[str, str] = {
    "aurora_mysql": "aurora-mysql",
    "aurora_postgresql": "aurora-postgresql",
}
REQUIRED_CLUSTER_PARAMETERS: dict[str, dict[str, str]] = {
    "aurora_mysql": {"binlog_format": "ROW", "binlog_row_image": "FULL"},
    "aurora_postgresql": {"rds.logical_replication": "1"},
}
STACK_OUTPUTS: tuple[str, ...] = (
    "ReplicationInstanceArn",
    "ReplicationTaskArn",
    "SourceEndpointArn",
    "TargetEndpointArn",
    "SecretsAccessRoleArn",
)
CONTRACT_TAG = "modernizer:contract-sha256"
# The task's LobChunkSize (KB): the chunk full LOB mode reads a LOB in. 64 is the AWS DMS
# recommended value; above it tasks can fail.
FULL_LOB_CHUNK_KB = 64
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,63}")
_NO_RULES = ("no_relational_anchor", "staged_move_pending")
_QUOTE = {"aurora_mysql": "`", "aurora_postgresql": '"'}
_PLAN_SCRIPT = "scripts/run_data_movement_plan.py"
_CONTRACT_SCRIPT = "scripts/run_transformation_contract.py"


class DmsStepError(ValueError):
    """Step 1's inputs cannot produce its delivery."""


class DmsStepSkipped(DmsStepError):
    """Step 1 has no rules (no relational anchor, or a staged move pending): nothing to render."""


@dataclass(frozen=True)
class SkippedRule:
    """A contract rule step 1 does not select: blocked by an error finding."""

    table_id: str
    reason: str


@dataclass(frozen=True)
class UnsupportedField:
    """A field of a moved table no operation can express: DMS copies the source value and
    Aurora converts it on insert, or refuses it; the customer decides what it should be."""

    table_id: str
    column: str
    description: str


@dataclass(frozen=True)
class Step1Inputs:
    plan: DataMovementPlan
    contract: StepContract
    # The contract file's SHA-256: the plan's pin, and the stack's CONTRACT_TAG.
    contract_sha256: str
    ddl: str
    # Blocked rules, left out of the table mappings and the DDL, by table id.
    skipped: tuple[SkippedRule, ...] = ()
    # The moved tables' unsupported fields, by table id and column.
    unsupported: tuple[UnsupportedField, ...] = ()
    # Moved columns whose collector default is a zero date (``table_id.column``, sorted).
    zero_date_defaults: tuple[str, ...] = ()

    @property
    def unbounded_varchar(self) -> UnboundedVarchar:
        """The PostgreSQL source endpoint's ``UnboundedVarcharMaxSize`` input."""
        return _replication(self).unbounded_varchar


def _replication(inputs: Step1Inputs) -> DmsReplication:
    """The plan's step 1 replication section, or why it is missing."""
    if inputs.plan.replication is None:
        raise DmsStepError(
            "the plan has no replication section: run scripts/run_data_movement_plan.py first"
        )
    return inputs.plan.replication


@dataclass(frozen=True)
class ContractTable:
    table_id: str
    schema: str
    name: str
    keyed: bool


class DmsStepBundle(BaseModel):
    """The three inputs in one file: the committed examples' input."""

    model_config = ConfigDict(extra="forbid")

    plan: DataMovementPlan
    contract: StepContract
    ddl: str = Field(..., min_length=1)
    # Source columns (``table_id.column``) whose collector default is a zero date.
    zero_date_defaults: list[str] = Field(default_factory=list)


def _selected(contract: StepContract) -> list[Rule]:
    return sorted(
        (r for r in contract.rules if r.status != "blocked"), key=lambda r: r.source_table
    )


def skipped_rules(contract: StepContract) -> tuple[SkippedRule, ...]:
    """The blocked rules step 1 leaves out, sorted by table id, each with its error findings."""
    return tuple(
        SkippedRule(
            table_id=r.source_table,
            reason="; ".join(f.message for f in r.findings if f.severity == "error") or "blocked",
        )
        for r in sorted(contract.rules, key=lambda r: r.source_table)
        if r.status == "blocked"
    )


def unsupported_fields(contract: StepContract) -> tuple[UnsupportedField, ...]:
    """The unsupported fields of the rules step 1 moves (a blocked rule's are not listed:
    its table does not move), sorted by table id and column."""
    return tuple(
        sorted(
            (
                UnsupportedField(r.source_table, f.source_column, f.op.description)
                for r in _selected(contract)
                for f in r.fields
                if isinstance(f.op, UnsupportedOp)
            ),
            key=lambda u: (u.table_id, u.column),
        )
    )


def contract_tables(contract: StepContract) -> list[ContractTable]:
    """The tables step 1 moves: the contract's unblocked rules, sorted by table id. Views
    are never rules, so never here."""
    out = []
    for rule in _selected(contract):
        schema, _, name = rule.source_table.rpartition(".")
        keyed = not any(f.code == "no_key" for f in rule.findings)
        out.append(ContractTable(table_id=rule.source_table, schema=schema, name=name, keyed=keyed))
    return out


def _check_ddl(ddl: str, contract: StepContract) -> None:
    """Every moved table has its CREATE TABLE: DMS writes into tables the DDL created."""
    q = re.escape(_QUOTE[contract.target.engine])
    name = rf"{q}([^{q}]+){q}"
    # An optional IF NOT EXISTS and an optional quoted schema prefix; the table is the last name.
    created = set(re.findall(rf"CREATE TABLE (?:IF NOT EXISTS )?(?:{q}[^{q}]+{q}\.)?{name}", ddl))
    missing = [r.source_table for r in _selected(contract) if r.target_table not in created]
    if missing:
        raise DmsStepError(f"the DDL creates no table for {', '.join(missing)}")


def _checked(inputs: Step1Inputs) -> Step1Inputs:
    contract, plan = inputs.contract, inputs.plan
    if contract.status in _NO_RULES:
        raise DmsStepSkipped(contract.status_reason or contract.status)
    if contract.step != 1 or plan.step != 1 or plan.cdc.tool != "dms" or plan.replication is None:
        raise DmsStepError("these are not step 1's DMS plan and contract")
    if contract.assignment_version != plan.assignment_version:
        raise DmsStepError(f"the contract reads assignment v{contract.assignment_version}, "
                           f"the plan v{plan.assignment_version}: write both again")  # fmt: skip
    if plan.replication.target_engine != contract.target.engine:
        raise DmsStepError(f"the plan moves into {plan.replication.target_engine}, the contract into "
                           f"{contract.target.engine}")  # fmt: skip
    if contract.target.engine not in AURORA_ENGINES:
        raise DmsStepError(f"step 1 moves into Aurora, not {contract.target.engine}")
    if not _selected(contract):
        raise DmsStepError(
            f"every step 1 rule is blocked: fix their findings, then run {_CONTRACT_SCRIPT}"
        )
    by_table = {r.source_table: r for r in contract.rules}
    for table in contract_tables(contract):
        for value in (table.schema, table.name):
            if not _NAME.fullmatch(value):
                raise DmsStepError(f"{table.table_id}: {value!r} is not a plain identifier")
        rule = by_table[table.table_id]
        if rule.target_table != table.name:
            raise DmsStepError(f"{table.table_id}: the rule renames it to {rule.target_table}; step 1 "
                               "mirrors table names (DMS cannot rename without a transformation rule)")  # fmt: skip
    if inputs.ddl:
        _check_ddl(inputs.ddl, contract)
    return dataclasses.replace(
        inputs, skipped=skipped_rules(contract), unsupported=unsupported_fields(contract)
    )


def bundle_inputs(bundle: DmsStepBundle) -> Step1Inputs:
    sha = contract_sha256(bundle.contract)
    ref = bundle.plan.contract
    if ref is not None and ref.sha256 is not None and ref.sha256 != sha:
        raise DmsStepError("the contract changed since the plan was written: write the plan again")
    return _checked(
        Step1Inputs(plan=bundle.plan, contract=bundle.contract, contract_sha256=sha, ddl=bundle.ddl,
                    zero_date_defaults=tuple(sorted(set(bundle.zero_date_defaults))))
    )  # fmt: skip


def _load_job(store: Any, db: str, job_id: str, plan: DataMovementPlan) -> Any:
    from src.tools.data_movement.plan import load_inputs

    try:
        job = load_inputs(store, db, job_id)
    except FileNotFoundError as e:
        raise DmsStepError(
            f"the DDL needs the finished job (report, assignment and collector): {e}"
        ) from e
    if job.resolved_version() != plan.assignment_version:
        raise DmsStepError(f"the job reads assignment v{job.resolved_version()}, the plan "
                           f"v{plan.assignment_version}: write the contract and the plan again")  # fmt: skip
    return job


def _zero_date_defaults(job: Any, contract: StepContract) -> tuple[str, ...]:
    """The moved source columns whose collector default is a zero date, matched with the
    ``mysql-zero-dates`` gap rule's own ``default_values`` globs."""
    import fnmatch

    from src.shared.catalog.gaps import gap_rules
    from src.tools.transformation_contract.step1 import _load_collector

    globs = [
        g.lower() for r in gap_rules() if r.id == "mysql-zero-dates" for g in r.match.default_values
    ]
    moved = {r.source_table for r in _selected(contract)}
    collector = _load_collector(job).model_dump(mode="json")
    out = set()
    for table in (collector.get("database_schema") or {}).get("tables") or []:
        if table.get("table_id") not in moved:
            continue
        for column in table.get("columns") or []:
            default = column.get("default_value")
            if default is None or isinstance(default, bool):
                continue
            if any(fnmatch.fnmatchcase(str(default).strip().lower(), g) for g in globs):
                out.add(f"{table['table_id']}.{column['column_name']}")
    return tuple(sorted(out))


def _step1_ddl(job: Any, plan: DataMovementPlan, contract: StepContract) -> str:
    """The DDL of every table step 1 moves: the Aurora DDL generator over the whole
    collector (``step1.design_base``), each column declared as the contract's
    ``target_type``, kept to the moved tables (and foreign keys between them)."""
    from src.tools.schema.aurora_common.ddl_generator import TypeOverride, assemble_full_ddl
    from src.tools.transformation_contract.step1 import _load_collector, design_base

    engine = contract.target.engine
    rules = {r.target_table: r for r in _selected(contract)}
    overrides = {
        (name, f.target_column): TypeOverride(f.target_type, "The step 1 contract's target type.")
        for name, rule in rules.items()
        for f in rule.fields
        if not isinstance(f.op, UnsupportedOp)
    }
    base = design_base(job, engine, _load_collector(job))
    tables = [
        t.model_copy(update={"foreign_keys": [
            fk for fk in t.foreign_keys or [] if fk.referenced_table.rsplit(".", 1)[-1] in rules] or None})
        for t in base.tables
        if t.table_name in rules
    ]  # fmt: skip
    result = dataclasses.replace(base, tables=tables).generate(overrides)
    generated = {t.table_name: t for t in result.tables}
    for name, rule in sorted(rules.items()):
        table = generated.get(name)
        if table is None:
            raise DmsStepError(
                f"{rule.source_table}: the {display_engine(engine)} DDL generator has no such table"
            )
        declared = {c.name: c.aurora_type for c in table.columns}
        for f in rule.fields:
            if (
                not isinstance(f.op, UnsupportedOp)
                and declared.get(f.target_column) != f.target_type
            ):
                raise DmsStepError(f"{rule.source_table}.{f.target_column}: the DDL declares "
                                   f"{declared.get(f.target_column)}, the contract {f.target_type}")  # fmt: skip
    ordered = [generated[name] for name in sorted(generated)]
    return assemble_full_ddl(ordered) + "\n"


def load_step1(store: Any, db: str, job_id: str, *, with_ddl: bool = True) -> Step1Inputs:
    """Step 1's plan, its contract (checked against the plan's pin before anything else is
    read from it) and, with ``with_ddl``, the DDL of every table it moves."""
    plan_key = f"{db}/{job_id}/{STEP_DIR}/plan.json"
    if not store.exists(plan_key):
        raise DmsStepError(f"no step 1 plan at {plan_key}: run {_PLAN_SCRIPT} first")
    plan = load_contract(DataMovementPlan, store.read_json(plan_key), artifact="step 1 plan")
    if plan.contract is None:
        raise DmsStepError(f"the step 1 plan names no transformation contract: run {_CONTRACT_SCRIPT}, "
                           f"then {_PLAN_SCRIPT}")  # fmt: skip
    if plan.contract.sha256 is None:
        raise DmsStepError(f"the step 1 plan pins no contract hash: run {_PLAN_SCRIPT} again")
    contract_key = f"{db}/{job_id}/{plan.contract.artifact}"
    if not store.exists(contract_key):
        raise DmsStepError(f"no transformation contract at {contract_key}: run {_CONTRACT_SCRIPT}, "
                           f"then {_PLAN_SCRIPT}")  # fmt: skip
    raw = store.read_bytes(contract_key)
    sha = hashlib.sha256(raw).hexdigest()
    if sha != plan.contract.sha256:
        raise DmsStepError(f"the contract changed since the plan was written (its SHA-256 is {sha}, the plan "
                           f"pins {plan.contract.sha256}): run {_PLAN_SCRIPT} again")  # fmt: skip
    contract = load_contract(
        StepContract, json.loads(raw), artifact="step 1 transformation contract"
    )
    inputs = _checked(Step1Inputs(plan=plan, contract=contract, contract_sha256=sha, ddl=""))
    if not with_ddl:
        return inputs
    job = _load_job(store, db, job_id, plan)
    ddl = _step1_ddl(job, plan, contract)
    defaults = _zero_date_defaults(job, contract)
    return _checked(dataclasses.replace(inputs, ddl=ddl, zero_date_defaults=defaults))


def table_mappings(inputs: Step1Inputs) -> dict[str, Any]:
    """One explicit selection rule per moved table, and no other rule.

    No ``table-settings`` rule carries ``lob-settings``: DMS rejects the per-table full LOB
    rule (``bulk-max-size`` 0) when the task is created, and a nonzero size is combination
    full LOB mode, which MySQL and PostgreSQL endpoints lack. Full LOB mode is a task setting
    (:func:`task_settings`)."""
    rules: list[dict[str, Any]] = []
    for table in contract_tables(inputs.contract):
        n = str(len(rules) + 1)
        rules.append({"rule-type": "selection", "rule-id": n, "rule-name": n,
                      "object-locator": {"schema-name": table.schema, "table-name": table.name},
                      "rule-action": "include", "filters": []})  # fmt: skip
    return {"rules": rules}


def _any_full_lob_table(inputs: Step1Inputs) -> bool:
    """True when a moved table is in full LOB mode in the plan's ``replication.lob_tables``."""
    full_lob = {t.table for t in _replication(inputs).lob_tables if t.mode == "full"}
    return any(t.table_id in full_lob for t in contract_tables(inputs.contract))


def task_settings(inputs: Step1Inputs) -> dict[str, Any]:
    """The fixed task settings (spec section 5); only the LOB mode and size come from the plan.

    Full LOB mode (never truncates) applies to the whole task when any moved table needs it,
    otherwise limited LOB mode at the plan's size."""
    replication = _replication(inputs)
    full_lob = _any_full_lob_table(inputs)
    return {
        "TargetMetadata": {
            "SupportLobs": True,
            "FullLobMode": full_lob,
            "LobChunkSize": FULL_LOB_CHUNK_KB,
            "LimitedSizeLobMode": not full_lob,
            "LobMaxSize": replication.lob_max_size_kb,
            # Transactional apply keeps each key's changes in commit order for the connector
            # that later reads Aurora (ADR-033 Decision 10).
            "BatchApplyEnabled": False,
        },
        # The customer's DDL created the tables; DMS never drops, truncates or creates one.
        "FullLoadSettings": {"TargetTablePrepMode": "DO_NOTHING"},
        # No source schema changes during the migration window in this release.
        "ChangeProcessingDdlHandlingPolicy": {
            "HandleSourceTableDropped": False,
            "HandleSourceTableTruncated": False,
            "HandleSourceTableAltered": False,
        },
        "ValidationSettings": {
            "EnableValidation": True,
            "ValidationMode": "ROW_LEVEL",
            "ThreadCount": 5,
        },
        "Logging": {"EnableLogging": True},
    }


# --- The DMS stack (dms-step-1.yaml) ---------------------------------------------------------

_SECRET_PATTERN = "^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$"  # nosec B105 -- an ARN pattern, not a password
_KMS_PATTERN = "^$|^arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/[a-zA-Z0-9-]+$"
_CERT_ARN = "arn:aws[a-z-]*:dms:[a-z0-9-]+:[0-9]{12}:cert:[A-Za-z0-9-]+"
# Database names per endpoint settings key: MySQL allows a hyphen in a database name.
_DB_NAME_PATTERN: dict[str, str] = {
    "MySqlSettings": "^[A-Za-z0-9_][A-Za-z0-9_$-]{0,63}$",
    "PostgreSqlSettings": "^[A-Za-z_][A-Za-z0-9_$]{0,62}$",
}
# DMS identifiers: a letter first, letters and digits, single hyphens, no hyphen at the end.
_TASK_ID_PATTERN = "^[a-zA-Z][a-zA-Z0-9]*(?:-[a-zA-Z0-9]+)*$"
# A Secrets Manager interface VPC endpoint DNS name (only needed when it has no private DNS).
_DNS_PATTERN = (
    "^$|^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)
_OVERRIDE = "HasSecretsManagerEndpointOverride"
# Source engine (plan.source.engine) -> (DMS EngineName, endpoint settings key).
SOURCE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "mysql": ("mysql", "MySqlSettings"),
    "mariadb": ("mariadb", "MySqlSettings"),
    "postgresql": ("postgres", "PostgreSqlSettings"),
}
TARGET_ENDPOINTS: dict[str, tuple[str, str]] = {
    "aurora_mysql": ("aurora", "MySqlSettings"),
    "aurora_postgresql": ("aurora-postgresql", "PostgreSqlSettings"),
}
# The SslMode that checks the server certificate against the imported CA, per settings key.
# MySQL, MariaDB and Aurora MySQL endpoints support verify-ca and verify-full but not
# require ("Using SSL with AWS DMS"), so on them TLS is always verified and the CA
# certificate is a required parameter; verify-ca, because the source host may be an
# address the certificate does not name. PostgreSQL endpoints take require, so their
# certificate is optional: verify-full when one is passed, encrypted-only otherwise.
VERIFY_SSL_MODE: dict[str, str] = {
    "MySqlSettings": "verify-ca",
    "PostgreSqlSettings": "verify-full",
}
# DMS loads tables in parallel into tables the customer's DDL created (DO_NOTHING), so the
# target session must not check foreign keys. On a MySQL-compatible target the DMS guide
# names two equivalent ways, the Initstmt=SET FOREIGN_KEY_CHECKS=0 extra connection
# attribute and the MySqlSettings AfterConnectScript; the typed setting is used, so the
# endpoint has no free-form attribute string. On PostgreSQL the guide's AfterConnectScript
# sets session_replication_role, which also skips user triggers (Aurora's master user, a
# member of rds_superuser, may set it).
_TARGET_AFTER_CONNECT: dict[str, str] = {
    "MySqlSettings": "SET FOREIGN_KEY_CHECKS=0",
    "PostgreSqlSettings": "SET session_replication_role = replica",
}
# Account-wide roles DMS itself assumes, by their fixed names: created once per account (the
# console creates them), never by this stack, since a second stack would collide on the name.
PREREQUISITE_ROLES: dict[str, str] = {
    "dms-vpc-role": "DMS manages the replication subnet group and the instance's network "
    "interfaces with it (managed policy AmazonDMSVPCManagementRole)",
    "dms-cloudwatch-logs-role": "DMS writes the task's logs to CloudWatch Logs with it "
    "(managed policy AmazonDMSCloudWatchLogsRole)",
}


def task_identifier(job_id: str) -> str:
    """``dm-<job>-step-1``: letters, digits and single hyphens, as DMS requires."""
    text = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9-]", "-", f"dm-{job_id}-step-1"))
    return text[:255].strip("-")


def _tags(inputs: Step1Inputs) -> list[dict[str, str]]:
    return [
        {"Key": "modernizer:job-id", "Value": inputs.plan.job_id},
        {"Key": "modernizer:step", "Value": "1"},
        {"Key": CONTRACT_TAG, "Value": inputs.contract_sha256},
    ]


def _if(condition: str, value: Any) -> dict[str, Any]:
    return {"Fn::If": [condition, value, {"Ref": "AWS::NoValue"}]}


def _not_empty(parameter: str) -> dict[str, Any]:
    return {"Fn::Not": [{"Fn::Equals": [{"Ref": parameter}, ""]}]}


def _text(pattern: str, description: str, default: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"Type": "String", "Description": description, "AllowedPattern": pattern}
    if default is not None:
        p["Default"] = default
    return p


def _keys(inputs: Step1Inputs) -> tuple[tuple[str, str], tuple[str, str]]:
    return (
        SOURCE_ENDPOINTS[inputs.plan.source.engine],
        TARGET_ENDPOINTS[inputs.contract.target.engine],
    )


def _certificate(who: str, settings_key: str) -> dict[str, Any]:
    if settings_key == "MySqlSettings":
        return _text(f"^{_CERT_ARN}$", f"ARN of the DMS certificate (aws dms import-certificate) of the "
                     f"{who}'s certificate authority (the RDS CA bundle for an RDS or Aurora database). "
                     "Required: MySQL-compatible endpoints connect with verified TLS only (verify-ca)")  # fmt: skip
    return _text(f"^$|^{_CERT_ARN}$", f"ARN of the DMS certificate (aws dms import-certificate) of the {who}'s "
                 "certificate authority; when set, the server certificate and host name are verified "
                 "(verify-full), otherwise the connection is encrypted only (require)", "")  # fmt: skip


def _dms_parameters(inputs: Step1Inputs) -> dict[str, Any]:
    replication, database = _replication(inputs), inputs.plan.source.database
    s = replication.sizing
    (_, source_key), (_, target_key) = _keys(inputs)
    secret = ("Full ARN of the Secrets Manager secret of the {who} (the DMS user): JSON keys username, password, "  # nosec B105 -- help text
              "host and port. Not the RDS-managed master secret, which has no host or port")  # fmt: skip
    kms = "Customer managed KMS key ARN that encrypts the {who} secret (empty when it uses the AWS managed key)"
    params: dict[str, Any] = {
        "InstanceClass": {"Type": "String", "Default": s.instance_class, "AllowedPattern": r"^dms\.[a-z0-9]+\.[a-z0-9]+$",
                          "Description": f"Replication instance class. Sized from the collection: {s.reason}"[:4000]},
        "AllocatedStorage": {"Type": "Number", "Default": s.allocated_storage_gb, "MinValue": 5, "MaxValue": 6144,
                             "Description": "Replication instance storage in GB (task logs and cached changes)"},
        "MultiAZ": {"Type": "String", "Description": "Run the replication instance in two Availability Zones "
                    "(the step's cost prices true; false halves the instance price, for a demo)",
                    "Default": "true" if replication.multi_az_default else "false",
                    "AllowedValues": ["true", "false"]},
        "SubnetIds": {"Type": "List<AWS::EC2::Subnet::Id>", "Description": "Private subnets in at least two "
                      "Availability Zones that reach the source and the Aurora writer endpoint; DMS reads the "
                      "endpoint secrets from them, so it needs a Secrets Manager interface endpoint with private "
                      "DNS, or a NAT route"},
        "SecurityGroupIds": {"Type": "List<AWS::EC2::SecurityGroup::Id>", "Description": "Security groups of the "
                             "replication instance: egress on the database ports to the source and the cluster "
                             "(whose groups must allow it) and egress on 443 to Secrets Manager"},
        "SecretsManagerEndpointOverride": _text(_DNS_PATTERN, "DNS name of the Secrets Manager interface VPC "
                                                "endpoint, only when that endpoint has no private DNS (empty: "
                                                "the regional Secrets Manager endpoint)", ""),
        "SourceSecretArn": _text(_SECRET_PATTERN, secret.format(who="source database")),
        "TargetSecretArn": _text(_SECRET_PATTERN, secret.format(who="Aurora writer endpoint")),
        "SourceSecretKmsKeyArn": _text(_KMS_PATTERN, kms.format(who="source"), ""),
        "TargetSecretKmsKeyArn": _text(_KMS_PATTERN, kms.format(who="target"), ""),
        "SourceCertificateArn": _certificate("source database", source_key),
        "TargetCertificateArn": _certificate("Aurora cluster", target_key),
        "ReplicationKmsKeyArn": _text(_KMS_PATTERN, "Customer managed KMS key ARN for the replication instance's "
                                      "storage (empty: the DMS default key)", ""),
    }  # fmt: skip
    if source_key == "PostgreSqlSettings":
        params["SourceDatabaseName"] = _text(
            _DB_NAME_PATTERN[source_key], "Source database name", database
        )
    params["TargetDatabaseName"] = _text(
        _DB_NAME_PATTERN[target_key], "Aurora database the DDL created the tables in", database
    )
    params["TaskIdentifier"] = _text(_TASK_ID_PATTERN, "Replication task identifier",
                                     task_identifier(inputs.plan.job_id))  # fmt: skip
    params["TaskIdentifier"]["MaxLength"] = 255
    for name, p in params.items():
        # A default its own pattern refuses would fail the stack at create time: refuse it here.
        if (
            "AllowedPattern" in p
            and "Default" in p
            and not re.fullmatch(p["AllowedPattern"], str(p["Default"]))
        ):
            raise DmsStepError(f"the {name} default {p['Default']!r} does not match {p['AllowedPattern']}")  # fmt: skip
    return params


def _secrets_role(inputs: Step1Inputs) -> dict[str, Any]:
    def key_statements(condition: str, key: str) -> list[dict[str, Any]]:
        # Secrets Manager decrypts the secret with the caller's kms:Decrypt; DescribeKey is in
        # the DMS guide's secret access policy.
        via = {
            "StringEquals": {
                "kms:ViaService": {"Fn::Sub": "secretsmanager.${AWS::Region}.amazonaws.com"}
            }
        }
        return [_if(condition, {"Effect": "Allow", "Action": "kms:Decrypt", "Resource": {"Ref": key},
                                "Condition": via}),
                _if(condition, {"Effect": "Allow", "Action": "kms:DescribeKey", "Resource": {"Ref": key}})]  # fmt: skip

    return {
        "Type": "AWS::IAM::Role",
        "Metadata": {"cfn_nag": {"rules_to_suppress": [{"id": "W76", "reason": (
            "One statement per resource scope (the two secrets, then Decrypt and DescribeKey on each "
            "optional secret key); every action is listed and every resource is scoped, which is what "
            "raises the complexity score")}]}},
        "Properties": {
            "Description": "Lets DMS read the two endpoint secrets of this step, and nothing else",
            "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
                # The regional DMS service principal with no condition, exactly as the DMS guide's
                # secret access role: an undocumented condition could fail the endpoint test.
                "Effect": "Allow", "Principal": {"Service": {"Fn::Sub": "dms.${AWS::Region}.amazonaws.com"}},
                "Action": "sts:AssumeRole"}]},
            "Policies": [{"PolicyName": "read-endpoint-secrets", "PolicyDocument": {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                 "Resource": [{"Ref": "SourceSecretArn"}, {"Ref": "TargetSecretArn"}]},
                *key_statements("HasSourceSecretKey", "SourceSecretKmsKeyArn"),
                *key_statements("HasTargetSecretKey", "TargetSecretKmsKeyArn")]}}],
            "Tags": _tags(inputs),
        },
    }  # fmt: skip


def _endpoint(
    inputs: Step1Inputs,
    role: str,
    engine_name: str,
    settings_key: str,
    extra: dict[str, Any],
    attributes: list[str],
) -> dict[str, Any]:
    mysql = settings_key == "MySqlSettings"
    has_cert = f"Has{role}Certificate"
    props: dict[str, Any] = {
        "EndpointType": role.lower(),
        "EngineName": engine_name,
        "SslMode": VERIFY_SSL_MODE[settings_key] if mysql
        else {"Fn::If": [has_cert, VERIFY_SSL_MODE[settings_key], "require"]},
        "CertificateArn": {"Ref": f"{role}CertificateArn"} if mysql
        else _if(has_cert, {"Ref": f"{role}CertificateArn"}),
        settings_key: {"SecretsManagerSecretId": {"Ref": f"{role}SecretArn"},
                       "SecretsManagerAccessRoleArn": {"Fn::GetAtt": ["SecretsAccessRole", "Arn"]}, **extra},
    }  # fmt: skip
    # A MySQL source takes no DatabaseName; a MySQL target takes it with TargetDbType.
    if role == "Target" or not mysql:
        props["DatabaseName"] = {"Ref": f"{role}DatabaseName"}
    props["ExtraConnectionAttributes"] = _attributes(attributes)
    props["Tags"] = _tags(inputs)
    return {"Type": "AWS::DMS::Endpoint", "Properties": props}


def _attributes(static: list[str]) -> Any:
    """The extra connection attributes, ``;``-joined, then the DMS guide's
    ``secretsManagerEndpointOverride=<DNS>`` when the override parameter is set."""
    override = "secretsManagerEndpointOverride=${SecretsManagerEndpointOverride}"
    unset = ";".join(static) if static else {"Ref": "AWS::NoValue"}
    return {"Fn::If": [_OVERRIDE, {"Fn::Sub": ";".join([*static, override])}, unset]}


def _source_endpoint(inputs: Step1Inputs, engine_name: str, settings_key: str) -> dict[str, Any]:
    if settings_key != "PostgreSqlSettings":
        return _endpoint(inputs, "Source", engine_name, settings_key, {}, [])
    # The task ignores source DDL (ChangeProcessingDdlHandlingPolicy), so DMS creates no DDL
    # capture table or trigger in the customer's source. The heartbeat does create a heartbeat
    # table in the source (HeartbeatSchema, public by default; the README says so); it keeps an
    # idle replication slot advancing, so the slot does not hold WAL.
    extra = {"CaptureDdls": False, "HeartbeatEnable": True}
    limit = inputs.unbounded_varchar.max_bytes
    # CloudFormation's PostgreSqlSettings has no UnboundedVarcharMaxSize; the DMS guide
    # documents it as the source extra connection attribute unboundedVarcharMaxSize.
    attributes = [f"unboundedVarcharMaxSize={limit}"] if limit is not None else []
    return _endpoint(inputs, "Source", engine_name, settings_key, extra, attributes)


def _target_endpoint(inputs: Step1Inputs, engine_name: str, settings_key: str) -> dict[str, Any]:
    extra: dict[str, Any] = {"AfterConnectScript": _TARGET_AFTER_CONNECT[settings_key]}
    if settings_key == "MySqlSettings":
        # Into TargetDatabaseName, whatever the source schema is called.
        extra["TargetDbType"] = "specific-database"
    return _endpoint(inputs, "Target", engine_name, settings_key, extra, [])


def render_dms_template(inputs: Step1Inputs) -> dict[str, Any]:
    """Step 1's stack: a replication subnet group, a provisioned replication instance, source
    and target endpoints on Secrets Manager ARNs, one full-load-and-CDC task with the compiled
    table mappings and task settings. Parameters only: no default names an account, a
    region, an endpoint, the network or a credential."""
    _replication(inputs)
    plan, engine = inputs.plan, inputs.contract.target.engine
    (source_name, source_key), (target_name, target_key) = _keys(inputs)
    roles = " and ".join(PREREQUISITE_ROLES)
    description = (
        f"AWS DMS full load and ongoing replication for step 1 of job {plan.job_id}: the "
        f"{plan.source.engine} database {plan.source.database} into {display_engine(engine)}. "
        "GENERATED by scripts/render_dms_step.py from the step's plan and transformation contract; "
        "regenerate it rather than editing it. Needs the account-wide DMS roles "
        f"{roles} (this stack does not create them). Deploy it only after your Aurora cluster has every "
        "setting aurora-target.yaml lists and every table of step-1.ddl.sql exists: the task creates no table."
    )
    resources: dict[str, Any] = {
        "SecretsAccessRole": _secrets_role(inputs),
        "SubnetGroup": {"Type": "AWS::DMS::ReplicationSubnetGroup", "Properties": {
            "ReplicationSubnetGroupDescription": f"Step 1 replication subnets for job {plan.job_id}",
            "SubnetIds": {"Ref": "SubnetIds"}, "Tags": _tags(inputs)}},
        "ReplicationInstance": {"Type": "AWS::DMS::ReplicationInstance", "Properties": {
            "ReplicationInstanceClass": {"Ref": "InstanceClass"},
            "AllocatedStorage": {"Ref": "AllocatedStorage"},
            "MultiAZ": {"Fn::If": ["IsMultiAZ", True, False]},
            "PubliclyAccessible": False,
            # No EngineVersion: DMS creates the instance on its latest engine version.
            "AutoMinorVersionUpgrade": True,
            "ReplicationSubnetGroupIdentifier": {"Ref": "SubnetGroup"},
            "VpcSecurityGroupIds": {"Ref": "SecurityGroupIds"},
            "KmsKeyId": _if("HasReplicationKey", {"Ref": "ReplicationKmsKeyArn"}),
            "Tags": _tags(inputs)}},
        "SourceEndpoint": _source_endpoint(inputs, source_name, source_key),
        "TargetEndpoint": _target_endpoint(inputs, target_name, target_key),
        "ReplicationTask": {"Type": "AWS::DMS::ReplicationTask", "Properties": {
            "ReplicationTaskIdentifier": {"Ref": "TaskIdentifier"},
            "MigrationType": "full-load-and-cdc",
            "ReplicationInstanceArn": {"Ref": "ReplicationInstance"},
            "SourceEndpointArn": {"Ref": "SourceEndpoint"},
            "TargetEndpointArn": {"Ref": "TargetEndpoint"},
            "TableMappings": json.dumps(table_mappings(inputs), indent=2),
            "ReplicationTaskSettings": json.dumps(task_settings(inputs), indent=2),
            "Tags": _tags(inputs)}},
    }  # fmt: skip
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": description[:1024],
        "Metadata": {"PrerequisiteRoles": dict(PREREQUISITE_ROLES)},
        "Parameters": _dms_parameters(inputs),
        "Conditions": {
            "IsMultiAZ": {"Fn::Equals": [{"Ref": "MultiAZ"}, "true"]},
            **({"HasSourceCertificate": _not_empty("SourceCertificateArn")}
               if source_key != "MySqlSettings" else {}),
            **({"HasTargetCertificate": _not_empty("TargetCertificateArn")}
               if target_key != "MySqlSettings" else {}),
            "HasSourceSecretKey": _not_empty("SourceSecretKmsKeyArn"),
            "HasTargetSecretKey": _not_empty("TargetSecretKmsKeyArn"),
            "HasReplicationKey": _not_empty("ReplicationKmsKeyArn"),
            _OVERRIDE: _not_empty("SecretsManagerEndpointOverride"),
        },  # fmt: skip
        "Resources": resources,
        "Outputs": {
            "ReplicationInstanceArn": {"Value": {"Ref": "ReplicationInstance"}},
            "ReplicationTaskArn": {"Value": {"Ref": "ReplicationTask"}},
            "SourceEndpointArn": {"Value": {"Ref": "SourceEndpoint"}},
            "TargetEndpointArn": {"Value": {"Ref": "TargetEndpoint"}},
            "SecretsAccessRoleArn": {"Value": {"Fn::GetAtt": ["SecretsAccessRole", "Arn"]}},
        },
    }


# --- The suggested Aurora template (aurora-target.yaml) and the DDL file (step-1.ddl.sql) ---

_GENERATED = (
    "GENERATED by scripts/render_dms_step.py from step 1's plan and transformation contract; "
    "regenerate it rather than editing it."
)
CUSTOMER_RESPONSIBILITY = (
    "Creating, configuring, securing and running this Aurora cluster, and running the DDL in step-1.ddl.sql, "
    "are your responsibility. This is a suggested starting point generated from your assessment, not a "
    "production-ready configuration: review every setting against your own requirements, policies and "
    "backups before you deploy it. The data movement stack (dms-step-1.yaml) needs the settings listed "
    "here; nothing we deploy changes the cluster's configuration or runs inside it, and the DMS task only "
    "connects as the DMS user you create to write the rows."
)
# Instance-level parameters (an Aurora MySQL DB parameter group: the cluster parameter group
# refuses them; "Aurora MySQL configuration parameters", instance-level list).
REQUIRED_INSTANCE_PARAMETERS: dict[str, dict[str, str]] = {
    "aurora_mysql": {"local_infile": "1"},
    "aurora_postgresql": {},
}
# The MySQL 8.0 (and 8.4) default sql_mode, from the MySQL reference manual (Server SQL Modes).
MYSQL_80_DEFAULT_SQL_MODE = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
)
_ZERO_DATE_MODES = ("NO_ZERO_IN_DATE", "NO_ZERO_DATE")
ZERO_DATE_SQL_MODE = ",".join(
    m for m in MYSQL_80_DEFAULT_SQL_MODE.split(",") if m not in _ZERO_DATE_MODES
)
_ENGINE_FAMILY = {"aurora_mysql": "aurora-mysql", "aurora_postgresql": "aurora-postgresql"}
_LOG_EXPORTS = {
    "aurora_mysql": ["audit", "error", "general", "slowquery"],
    "aurora_postgresql": ["postgresql"],
}
# An RDS database name: a letter, then letters, digits and underscores.
_AURORA_PG_DB_NAME = "^[A-Za-z][A-Za-z0-9_]{0,62}$"
_PARAMETER_REASONS = {
    "binlog_format": "Later steps (change capture) read Aurora's changes from its binary log; "
    "row-based logging records every changed row. Set here at creation; changing it from OFF on a "
    "running cluster takes effect only after a reboot.",
    "binlog_row_image": "Each logged change carries the whole row, so later steps rebuild it without "
    "reading Aurora.",
    "rds.logical_replication": "Later steps (change capture) read Aurora's changes through logical "
    "replication. A static parameter: set here at creation; changing it later takes effect only "
    "after a reboot.",
    "local_infile": "AWS DMS writes the full load into a MySQL-compatible target with LOAD DATA LOCAL "
    "INFILE; the DMS guide's MySQL-target prerequisites require local_infile = 1 (Using a "
    "MySQL-compatible database as a target for AWS DMS). An instance-level parameter, so it is in the "
    "writer's DB parameter group: every instance of the cluster must use this DB parameter group, since a "
    "failover makes another instance the writer.",
}  # fmt: skip


@dataclass(frozen=True)
class RequiredSetting:
    setting: str
    value: str
    reason: str


_DATE_TYPE = re.compile(r"(?i)^(date|datetime|timestamp)\b")


def zero_date_columns(inputs: Step1Inputs) -> list[tuple[str, str]]:
    """The moved date and time columns that can hold zero dates: a ``copy_edge_value``
    finding on a copy field whose target type is DATE, DATETIME or TIMESTAMP (a copy keeps
    the source's type), sorted by table id and column. The shape check's values are
    synthetic, so this says what the column can hold, not what it holds. Only an Aurora
    MySQL target has the sql_mode they need; a blocked rule's table does not move."""
    if inputs.contract.target.engine != "aurora_mysql":
        return []
    out = set()
    for rule in _selected(inputs.contract):
        copies = {f.source_column: f for f in rule.fields if isinstance(f.op, CopyOp)}
        for finding in rule.findings:
            field = copies.get(finding.column or "")
            if finding.code == "copy_edge_value" and field and _DATE_TYPE.match(field.target_type):
                out.add((rule.source_table, field.source_column))
    return sorted(out)


def _zero_date_counts(inputs: Step1Inputs) -> tuple[int, int]:
    """(N, M): the date and time columns that can hold zero dates, and how many of them
    default to one in the collector."""
    columns = zero_date_columns(inputs)
    defaults = set(inputs.zero_date_defaults)
    return len(columns), sum(1 for t, c in columns if f"{t}.{c}" in defaults)


def _zero_date_reason(counts: tuple[int, int]) -> str:
    n, m = counts
    noun = "column" if n == 1 else "columns"
    return (
        f"{n} date and time {noun} can hold zero dates (MySQL accepts them unless its sql_mode forbids "
        f"them); {m} of them default to `0000-00-00`. Aurora MySQL 8.0's default sql_mode is strict "
        "(STRICT_TRANS_TABLES) with NO_ZERO_DATE and NO_ZERO_IN_DATE, which rejects them, and the AWS DMS "
        "guide says zero DATETIME and TIMESTAMP values are recorded as NULL on a target that does not "
        "support them, so the load would change data without failing. This is the MySQL 8.0 default "
        "without those two modes; strict mode stays on. An instance-level parameter: every instance of the "
        "cluster must use this DB parameter group, since a failover makes another instance the writer. If "
        "the source holds no zero dates (check it), you can drop this setting."
    )


def instance_parameters(inputs: Step1Inputs) -> dict[str, str]:
    """The writer's DB parameter group parameters: the required ones, plus sql_mode when
    the source holds zero dates (``zero_date_columns``)."""
    params = dict(REQUIRED_INSTANCE_PARAMETERS[inputs.contract.target.engine])
    if zero_date_columns(inputs):
        params["sql_mode"] = ZERO_DATE_SQL_MODE
    return params


def required_settings(
    engine: str, line: str | None, *, zero_dates: tuple[int, int] = (0, 0)
) -> list[RequiredSetting]:
    """The settings the cluster must have for step 1 and the later steps, each with its
    reason; ``zero_dates`` is (N, M): the moved date and time columns that can hold zero
    dates, and how many of them default to one."""
    family = display_engine(engine)
    version = (
        f"{family}, the {line} line" if line else f"{family}, a version line with standard support"
    )
    rows = [RequiredSetting("Engine and version", version,
                            "The Aurora design and its DDL target this engine line; another line may refuse "
                            "the DDL or change how values convert.")]  # fmt: skip
    rows += [RequiredSetting(name, value, _PARAMETER_REASONS[name])
             for name, value in REQUIRED_CLUSTER_PARAMETERS[engine].items()]  # fmt: skip
    rows += [RequiredSetting(name, value, _PARAMETER_REASONS[name])
             for name, value in REQUIRED_INSTANCE_PARAMETERS[engine].items()]  # fmt: skip
    if engine == "aurora_mysql" and zero_dates[0]:
        rows.append(RequiredSetting("sql_mode", ZERO_DATE_SQL_MODE, _zero_date_reason(zero_dates)))
    if engine == "aurora_mysql":
        rows.append(RequiredSetting("binlog retention hours", "24 or more",
                                    "CALL mysql.rds_set_configuration('binlog retention hours', 24); later steps "
                                    "read the binary log, so it must outlast an outage of their connector."))  # fmt: skip
    tls = ("verify-ca" if engine == "aurora_mysql"
           else "verify-full with the CA certificate, require without it")  # fmt: skip
    tls_value = ("available; require_secure_transport may be ON (the 8.4 default)" if engine == "aurora_mysql"
                 else "available; rds.force_ssl may be on")  # fmt: skip
    rows += [
        RequiredSetting("Encryption at rest", "StorageEncrypted: true",
                        "The cluster holds a full copy of your production data; encryption is set at creation "
                        "and cannot be turned on in place."),
        RequiredSetting("Writer endpoint", "reachable from the DMS replication instance on the database port",
                        "DMS writes through the cluster's writer endpoint from the subnets of dms-step-1.yaml; "
                        "allow its security group in the cluster's."),
        RequiredSetting("DMS user", "the user and grants after the tables in step-1.ddl.sql",
                        "The DMS target endpoint signs in as this user, from the TargetSecretArn secret (JSON "
                        "keys username, password, host and port), not as the administrator."),
        RequiredSetting("TLS", tls_value,
                        f"The DMS target endpoint always connects with TLS ({tls}), so a cluster that "
                        "requires TLS is fine."),
    ]  # fmt: skip
    return rows


def _settings_for(inputs: Step1Inputs) -> list[RequiredSetting]:
    return required_settings(inputs.contract.target.engine, _replication(inputs).target_engine_line,
                             zero_dates=_zero_date_counts(inputs))  # fmt: skip


def with_header(text: str, lines: list[str], prefix: str = "#") -> str:
    """``text`` with ``lines`` as a leading comment block (word-wrapped at 96 columns; words
    and hyphenated names are never split)."""
    import textwrap

    out: list[str] = []
    for line in lines:
        wrapped = textwrap.wrap(line, 96 - len(prefix) - 1, break_long_words=False,
                                break_on_hyphens=False) or [""]  # fmt: skip
        out += [f"{prefix} {w}".rstrip() for w in wrapped]
    return "\n".join(out) + "\n" + text


def _disclaimer_lines(inputs: Step1Inputs) -> list[str]:
    lines = [
        _GENERATED,
        "",
        "YOUR RESPONSIBILITY: " + CUSTOMER_RESPONSIBILITY,
        "",
        "Required settings:",
    ]
    for s in _settings_for(inputs):
        lines.append(f"- {s.setting} = {s.value}: {s.reason}")
    return lines


def render_aurora_template(inputs: Step1Inputs) -> dict[str, Any]:
    """The suggested cluster: a cluster parameter group, a writer DB parameter group (Aurora
    MySQL), a subnet group, the cluster and its writer. Parameters only: no default names an
    account, a region, the network or a credential; the master password is generated and
    kept in Secrets Manager."""
    from src.shared.catalog.gaps import aurora_version_lines

    engine = inputs.contract.target.engine
    line = _replication(inputs).target_engine_line
    family = _ENGINE_FAMILY[engine]
    lines = list(aurora_version_lines()[engine].lines)
    family_param: dict[str, Any] = {"Type": "String", "Description": "Parameter group family: the engine "
                                    "version's line", "AllowedValues": [f"{family}{x}" for x in lines]}  # fmt: skip
    if line:
        family_param["Default"] = f"{family}{line}"
    settings = _settings_for(inputs)
    pg = engine == "aurora_postgresql"
    params: dict[str, Any] = {
        "SubnetIds": {"Type": "List<AWS::EC2::Subnet::Id>", "Description": "Private subnets in at least two "
                      "Availability Zones for the cluster"},
        "SecurityGroupIds": {"Type": "List<AWS::EC2::SecurityGroup::Id>", "Description": "Security groups of "
                             "the cluster; allow the DMS replication instance on the database port"},
        "EngineVersion": {"Type": "String", "Description": f"{display_engine(engine)} engine version on the "
                          f"{line or 'chosen'} line (aws rds describe-db-engine-versions --engine {family})",
                          "AllowedPattern": rf"^{re.escape(line)}\..+$" if line else r"^[0-9].*$"},
        "ParameterGroupFamily": family_param,
        "InstanceClass": {"Type": "String", "Description": "Writer instance class", "Default": "db.r6g.large",
                          "AllowedPattern": r"^db\.[a-z0-9]+\.[a-z0-9]+$"},
        "MasterUsername": {"Type": "String", "Description": "Administrator user (its password is generated "
                           "and kept in Secrets Manager)", "Default": "dbadmin",
                           "AllowedPattern": "^[A-Za-z][A-Za-z0-9_]{0,15}$"},
        "BackupRetentionDays": {"Type": "Number", "Description": "Automated backup retention in days",
                                "Default": 7, "MinValue": 7, "MaxValue": 35},
        "KmsKeyArn": {"Type": "String", "Description": "Customer managed KMS key ARN for storage encryption "
                      "(empty: the AWS managed key)", "Default": "", "AllowedPattern": _KMS_PATTERN},
    }  # fmt: skip
    cluster: dict[str, Any] = {
        "Engine": AURORA_ENGINES[engine], "EngineVersion": {"Ref": "EngineVersion"},
        "DBClusterParameterGroupName": {"Ref": "ClusterParameterGroup"},
        "DBSubnetGroupName": {"Ref": "SubnetGroup"}, "VpcSecurityGroupIds": {"Ref": "SecurityGroupIds"},
        "MasterUsername": {"Ref": "MasterUsername"}, "ManageMasterUserPassword": True,
        "StorageEncrypted": True, "KmsKeyId": _if("HasKmsKey", {"Ref": "KmsKeyArn"}),
        "DeletionProtection": True, "EnableIAMDatabaseAuthentication": True,
        "BackupRetentionPeriod": {"Ref": "BackupRetentionDays"}, "CopyTagsToSnapshot": True,
        "EnableCloudwatchLogsExports": _LOG_EXPORTS[engine], "Tags": _tags(inputs),
    }  # fmt: skip
    if pg:
        # The MySQL DDL creates its database (CREATE DATABASE IF NOT EXISTS), so a MySQL
        # name RDS would refuse (a hyphen) still works; psql connects to this one.
        database = inputs.plan.source.database
        params["DatabaseName"] = {"Type": "String", "AllowedPattern": _AURORA_PG_DB_NAME,
                                  "Description": "Database the cluster creates; the DDL creates the tables in it"}  # fmt: skip
        if re.fullmatch(_AURORA_PG_DB_NAME, database):
            params["DatabaseName"]["Default"] = database
        cluster["DatabaseName"] = {"Ref": "DatabaseName"}
    writer: dict[str, Any] = {
        "Engine": AURORA_ENGINES[engine], "DBClusterIdentifier": {"Ref": "Cluster"},
        "DBInstanceClass": {"Ref": "InstanceClass"}, "PubliclyAccessible": False,
        "AutoMinorVersionUpgrade": True, "Tags": _tags(inputs),
    }  # fmt: skip
    resources: dict[str, Any] = {
        "ClusterParameterGroup": {"Type": "AWS::RDS::DBClusterParameterGroup", "Properties": {
            "Description": "The cluster settings step 1 and the later steps need (see the Metadata)",
            "Family": {"Ref": "ParameterGroupFamily"},
            "Parameters": dict(REQUIRED_CLUSTER_PARAMETERS[engine]), "Tags": _tags(inputs)}},
    }  # fmt: skip
    instance = instance_parameters(inputs)
    if instance:
        resources["InstanceParameterGroup"] = {"Type": "AWS::RDS::DBParameterGroup", "Properties": {
            "Description": "The writer settings step 1 needs (see the Metadata)",
            "Family": {"Ref": "ParameterGroupFamily"}, "Parameters": instance, "Tags": _tags(inputs)}}  # fmt: skip
        writer["DBParameterGroupName"] = {"Ref": "InstanceParameterGroup"}
    resources["SubnetGroup"] = {"Type": "AWS::RDS::DBSubnetGroup", "Properties": {
        "DBSubnetGroupDescription": f"Subnets of the step 1 Aurora cluster (job {inputs.plan.job_id})",
        "SubnetIds": {"Ref": "SubnetIds"}, "Tags": _tags(inputs)}}  # fmt: skip
    resources["Cluster"] = {"Type": "AWS::RDS::DBCluster", "DeletionPolicy": "Snapshot",
                            "UpdateReplacePolicy": "Snapshot", "Properties": cluster}  # fmt: skip
    resources["Writer"] = {"Type": "AWS::RDS::DBInstance", "Metadata": {"checkov": {"skip": [{
        "id": "CKV_AWS_118", "comment": (
            "Enhanced monitoring needs a monitoring IAM role this suggestion does not create; CloudWatch "
            "metrics and the exported logs cover step 1. Turn it on with your own role if you need it.")}]}},
        "Properties": writer}  # fmt: skip
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (f"SUGGESTED {display_engine(engine)} cluster for step 1 of job {inputs.plan.job_id}. "
                        "Creating and running it is your responsibility: read the header and the Metadata "
                        "before you deploy it. Generated by scripts/render_dms_step.py.")[:1024],  # fmt: skip
        "Metadata": {"Modernizer::CustomerResponsibility": {
            "Disclaimer": CUSTOMER_RESPONSIBILITY,
            "RequiredSettings": [{"Setting": s.setting, "Value": s.value, "Reason": s.reason}
                                 for s in settings]}},  # fmt: skip
        "Parameters": params,
        "Conditions": {"HasKmsKey": _not_empty("KmsKeyArn")},
        "Resources": resources,
        "Outputs": {
            "ClusterIdentifier": {"Value": {"Ref": "Cluster"}},
            "WriterEndpoint": {"Value": {"Fn::GetAtt": ["Cluster", "Endpoint.Address"]}},
            "Port": {"Value": {"Fn::GetAtt": ["Cluster", "Endpoint.Port"]}},
            "MasterUserSecretArn": {
                "Value": {"Fn::GetAtt": ["Cluster", "MasterUserSecret.SecretArn"]}
            },
        },
    }


def _dms_user_lines(engine: str, line: str | None, database: str, schema: str) -> list[str]:
    """The DMS user and its grants, commented out: the DMS guide's target prerequisites. On
    the 8.4 line the user names mysql_native_password (the DMS guide's Aurora MySQL 8.4 note:
    a new user otherwise gets caching_sha2_password)."""
    if engine == "aurora_mysql":
        db = database.replace("`", "``")
        auth = "IDENTIFIED WITH mysql_native_password BY" if line == "8.4" else "IDENTIFIED BY"
        return [
            f"CREATE USER 'dms_user'@'%' {auth} '<password>';",
            "GRANT ALTER, CREATE, DROP, INDEX, INSERT, UPDATE, DELETE, SELECT, CREATE TEMPORARY TABLES "
            f"ON `{db}`.* TO 'dms_user'@'%';",
            "GRANT ALL PRIVILEGES ON `awsdms_control`.* TO 'dms_user'@'%';",
        ]
    return [
        "CREATE USER dms_user WITH PASSWORD '<password>';",
        f'GRANT CONNECT, CREATE ON DATABASE "{database}" TO dms_user;',
        f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO dms_user;',
        f'GRANT UPDATE, INSERT, SELECT, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA "{schema}" TO dms_user;',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT UPDATE, INSERT, SELECT, DELETE, TRUNCATE '
        "ON TABLES TO dms_user;",
        "GRANT rds_superuser TO dms_user;",
        "-- Narrower, if your Aurora version allows it (PostgreSQL 15 and later), instead of rds_superuser:",
        "GRANT SET ON PARAMETER session_replication_role TO dms_user;",
    ]


def render_ddl(inputs: Step1Inputs) -> str:
    """``step-1.ddl.sql``: the license header, the disclaimer and the instructions as
    comments, then the database (Aurora MySQL) or schema (Aurora PostgreSQL), then the DDL;
    on Aurora PostgreSQL, the commented REPLICA IDENTITY FULL recommendation per table."""
    engine = inputs.contract.target.engine
    plan = inputs.plan
    tables = contract_tables(inputs.contract)
    pg = engine == "aurora_postgresql"
    schemas = sorted({t.schema for t in tables})
    if pg and len(schemas) != 1:
        raise DmsStepError(f"step 1 tables are in {len(schemas)} schemas ({', '.join(schemas)}); this release "
                           "moves one PostgreSQL schema")  # fmt: skip
    database = plan.source.database
    where = (f'the database {database}, schema {schemas[0]}' if pg
             else f"the database {database} (the file creates it if it does not exist)")  # fmt: skip
    run = (f'psql "host=<writer endpoint> dbname={database} user=<admin user> sslmode=verify-full '
           'sslrootcert=<RDS CA bundle>" -f step-1.ddl.sql' if pg else
           "mysql --host=<writer endpoint> --user=<admin user> --password --ssl-mode=VERIFY_IDENTITY "
           "--ssl-ca=<RDS CA bundle> < step-1.ddl.sql")  # fmt: skip
    zero = zero_date_columns(inputs)
    counts = _zero_date_counts(inputs)
    head = [
        "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
        "SPDX-License-Identifier: MIT-0",
        "",
        f"step-1.ddl.sql: the tables step 1 moves into {display_engine(engine)}, from the Aurora design of job "
        f"{plan.job_id} (assignment v{plan.assignment_version}). {_GENERATED}",
        "",
        "YOUR RESPONSIBILITY: " + CUSTOMER_RESPONSIBILITY,
        "",
        "How to run it:",
        "1. Create the cluster with every setting aurora-target.yaml lists (that template is a suggestion).",
        f"2. Run this file on the writer endpoint as the administrator, in {where}: {run}. The administrator "
        "password is in the secret of the cluster's MasterUserSecretArn output. The table names are not "
        "qualified: the DMS target endpoint writes into this same database "
        f"(TargetDatabaseName, default {database}).",
        "3. Create the DMS user with the statements at the end of this file, after the tables (uncomment "
        "them, choose the password), and put its user name, password, the writer endpoint and the port in "
        "the secret you pass as TargetSecretArn.",
        "4. Start the DMS task only after this file has run: deploy dms-step-1.yaml once every table below "
        "exists. The task creates no table (TargetTablePrepMode DO_NOTHING) and loads into these exactly "
        "as declared.",
        "5. Do not change these tables or the source's tables until the step is cut over: this release does "
        "not carry schema changes during the migration window.",
        "",
        "About this DDL:",
        "- Keep the declared column names: DMS matches columns by name, not by position, so the column "
        "order does not matter but a renamed column gets no data.",
        "- It sets no character set or collation: the cluster's server defaults apply. Check them against "
        "the source's before you run it.",
        "- The indexes of a saved schema design are not in this file: it creates the keys, the source's "
        "indexes and the foreign keys between moved tables only. Add the design's indexes when you choose.",
    ]
    if pg:
        head.append("- The DMS target endpoint sets session_replication_role = replica to skip foreign keys "
                    "and triggers during the load, which needs rds_superuser (or, if your Aurora version "
                    "allows it, GRANT SET ON PARAMETER session_replication_role); drop that grant after "
                    "the cutover.")  # fmt: skip
    else:
        head.append("- The DMS target endpoint turns foreign key checks off for its own sessions during "
                    "the load (SET FOREIGN_KEY_CHECKS=0).")  # fmt: skip
    if zero:
        head += ["", f"Zero dates: aurora-target.yaml sets sql_mode = {ZERO_DATE_SQL_MODE} in the writer's "
                 f"DB parameter group. Why: {_zero_date_reason(counts)} The columns: "
                 + ", ".join(f"{t}.{c}" for t, c in zero) + "."]  # fmt: skip
    head.append("")
    if inputs.skipped:
        head.append("Tables left out (blocked in the contract; DMS does not move them):")
        head += [f"- {s.table_id}: {s.reason}" for s in inputs.skipped]
    else:
        head.append("Tables left out: none.")
    if inputs.unsupported:
        head.append(
            "Unsupported fields (DMS copies the source value; Aurora converts it or refuses the row):"
        )
        head += [f"- {u.table_id}.{u.column}: {u.description}" for u in inputs.unsupported]
    else:
        head.append("Unsupported fields: none.")
    out = with_header("", head, "--")
    if pg:
        out += (
            f'\nCREATE SCHEMA IF NOT EXISTS "{schemas[0]}";\nSET search_path TO "{schemas[0]}";\n'
        )
    else:
        db = database.replace("`", "``")
        out += f"\nCREATE DATABASE IF NOT EXISTS `{db}`;\nUSE `{db}`;\n"
    out += "\n" + inputs.ddl.strip() + "\n"
    replication = _replication(inputs)
    user = ["", "The DMS user, after the tables and the schema its grants name (YOUR RESPONSIBILITY, as "
            "above; uncomment to run):"]  # fmt: skip
    out += "\n" + with_header("", user, "--")
    out += "".join(f"-- {s}\n" for s in _dms_user_lines(engine, replication.target_engine_line,
                                                        database, schemas[0]))  # fmt: skip
    if pg:
        advice = [
            "",
            "RECOMMENDED, YOUR DECISION (same responsibility as above): REPLICA IDENTITY FULL on every table. "
            "The later steps read Aurora's changes through logical replication; without it, they cannot read "
            "the updates and deletes of that table in full, and on a table with no key PostgreSQL refuses "
            "updates and deletes once a publication includes it. The cost: PostgreSQL writes the whole old "
            "row to the WAL for every update and delete, so WAL volume and its storage grow. Uncomment to "
            "apply it.",
        ]
        out += "\n" + with_header("", advice, "--")
        out += (
            "\n".join(
                f'-- ALTER TABLE "{t.schema}"."{t.name}" REPLICA IDENTITY FULL;' for t in tables
            )
            + "\n"
        )
    return out


def render_files(inputs: Step1Inputs) -> dict[str, str]:
    """The three files of step 1, by file name."""
    from src.tools.data_movement.debezium_connector import dump_template

    aurora = with_header(dump_template(render_aurora_template(inputs)), _disclaimer_lines(inputs))
    dms = with_header(dump_template(render_dms_template(inputs)), [_GENERATED])
    return {AURORA_TEMPLATE: aurora, DDL_FILE: render_ddl(inputs), DMS_TEMPLATE: dms}


def write_step1_delivery(store: Any, db: str, job_id: str) -> dict[str, Any]:
    """Write step 1's three files under the job; ``skipped`` when the step moves no table."""
    try:
        inputs = load_step1(store, db, job_id)
    except DmsStepSkipped as skipped:
        return {"status": "skipped", "reason": str(skipped)}
    paths = []
    for name, text in render_files(inputs).items():
        path = f"{db}/{job_id}/{STEP_DIR}/{name}"
        store.write_bytes(path, text.encode("utf-8"))
        paths.append(path)
    replication = _replication(inputs)
    pg = inputs.contract.target.engine == "aurora_postgresql"
    return {
        "status": "complete",
        "files": paths,
        "instance_class": replication.sizing.instance_class,
        "contract_sha256": inputs.contract_sha256,
        "replica_identity_recommended": len(contract_tables(inputs.contract)) if pg else 0,
    }
