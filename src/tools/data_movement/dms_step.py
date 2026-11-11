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
- ``step-1.ddl.sql``: the tables' DDL with instructions to run it, the extensions its column
  types need (Aurora PostgreSQL) and what the source has that step 1 does not move (on Aurora
  PostgreSQL, a commented ``REPLICA IDENTITY FULL`` recommendation per table);
- ``step-1.sequences.sql`` (Aurora PostgreSQL only): one ``setval`` per identity column, for
  the customer to run, since DMS writes the ids but never advances their sequences;
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

from src.contracts.aurora_design_delta import is_pg_qualified_type
from src.contracts.data_movement_plan import (
    DataMovementPlan,
    DmsReplication,
    LobTable,
    UnboundedVarchar,
)
from src.contracts.lineage_ops import CopyOp, UnsupportedOp
from src.contracts.transformation_contract import NOT_MOVED, Rule, StepContract
from src.shared.catalog.dms_sizing import EngineRules, dms_sizing
from src.shared.contract_versioning import load_contract
from src.shared.cutover_message import CUTOVER_DISCLAIMER
from src.shared.engine_names import display_engine
from src.tools.data_movement.dms_sizing import (
    DMS_MAX_LOB_KB,
    LOB_SIZES_COMMAND,
    LOB_SIZES_SQL,
    SKIPPED,
    fails_on_lob_truncation,
    limited_lob_only,
    measure_lob_sql,
    skipped_lob_tables,
    table_lob_limits,
    task_lob_mode,
    unmeasured_lob_tables,
)
from src.tools.transformation_contract.hashing import contract_sha256
from src.tools.transformation_contract.pg_enums import (
    NOT_COLLECTED,
    REFUSED,
    EnumType,
    create_type_sql,
    enum_columns,
)

STEP_DIR = "data-movement/step-1"
AURORA_TEMPLATE = "aurora-target.yaml"
DDL_FILE = "step-1.ddl.sql"
SEQUENCES_FILE = "step-1.sequences.sql"
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


class SourceObjects(BaseModel):
    """What the collection lists that step 1 does not move: DMS moves table rows only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    procedures: int = Field(0, ge=0)
    # Functions, trigger functions included.
    functions: int = Field(0, ge=0)
    triggers: int = Field(0, ge=0)
    views: int = Field(0, ge=0)


@dataclass(frozen=True)
class NotMoved:
    """One kind of source object step 1 does not move; ``count`` is None when the
    collection does not record it."""

    kind: str
    count: int | None
    note: str


@dataclass(frozen=True)
class Step1Inputs:
    plan: DataMovementPlan
    contract: StepContract
    # The contract file's SHA-256: the plan's pin, and the stack's CONTRACT_TAG.
    contract_sha256: str
    ddl: str
    # Blocked rules, left out of the table mappings and the DDL, by table id.
    skipped: tuple[SkippedRule, ...] = ()
    # Rules that move no rows (#1918): in the DDL, left out of the table mappings, by table id.
    not_moved: tuple[SkippedRule, ...] = ()
    # The moved tables' unsupported fields, by table id and column.
    unsupported: tuple[UnsupportedField, ...] = ()
    # Moved columns whose collector default is a zero date (``table_id.column``, sorted).
    zero_date_defaults: tuple[str, ...] = ()
    # The DDL's identity (or AUTO_INCREMENT) columns, (table id, column), sorted.
    identity_columns: tuple[tuple[str, str], ...] = ()
    # The source's procedures, functions, triggers and views; None when not read.
    source_objects: SourceObjects | None = None
    # The extensions the collection lists as installed; None when it did not read them.
    extensions: tuple[str, ...] | None = None
    # The PostgreSQL enum types the moved columns keep (#1834), by schema and name.
    enum_types: tuple[EnumType, ...] = ()

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
    # The DDL's identity columns, as (table id, column) pairs.
    identity_columns: list[tuple[str, str]] = Field(default_factory=list)
    # The collection's procedures, functions, triggers and views (None: not known).
    source_objects: SourceObjects | None = None
    # The collection's installed extensions, by name (None: not read).
    extensions: list[str] | None = None


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


def not_moved_rules(contract: StepContract) -> tuple[SkippedRule, ...]:
    """The unblocked rules whose table step 1 creates but its task does not move (``moves_rows``
    false, #1918: a type DMS does not migrate), sorted by table id, with their not_moved findings.
    """
    return tuple(
        SkippedRule(
            table_id=r.source_table,
            reason="; ".join(f.message for f in r.findings if f.code == NOT_MOVED),
        )
        for r in _selected(contract)
        if not r.moves_rows
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
    """The tables step 1 moves: the contract's unblocked rules, sorted by table id, but not a
    rule that moves no rows (#1918, its table is still in the DDL). Views are never rules, so
    never here."""
    out = []
    for rule in (r for r in _selected(contract) if r.moves_rows):
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


def _check_enum_types(inputs: Step1Inputs) -> None:
    """Every ``"<schema>"."<type>"`` a moved column declares is an enum the DDL creates (#1834)."""
    created = {e.sql_type for e in inputs.enum_types}
    for rule in _selected(inputs.contract):
        for f in rule.fields:
            if is_pg_qualified_type(f.target_type) and f.target_type not in created:
                raise DmsStepError(f"{rule.source_table}.{f.target_column}: no enum labels for "
                                   f"{f.target_type}: write the contract again from the collection")  # fmt: skip


def _step1_enum_types(job: Any, contract: StepContract) -> tuple[EnumType, ...]:
    """The PostgreSQL enum types the moved columns declare (#1834), sorted by schema and name."""
    from src.tools.transformation_contract.step1 import _load_collector

    enums = enum_columns(_load_collector(job), contract.target.engine).types.values()
    declared = {f.target_type for r in _selected(contract) for f in r.fields}
    return tuple(
        sorted({e for e in enums if e.sql_type in declared}, key=lambda e: (e.schema, e.name))
    )


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
    # The database name goes into CREATE DATABASE/USE and the commented grants of
    # step-1.ddl.sql (#1860): only a name the DMS endpoints take, never a quote or a
    # line break that would end its quotes or its comment.
    database = plan.source.database
    for key in _database_name_keys(inputs):
        if not re.fullmatch(_DB_NAME_PATTERN[key], database):
            raise DmsStepError(f"the source database name {database!r} does not match "
                               f"{_DB_NAME_PATTERN[key]} ({key})")  # fmt: skip
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
        _check_enum_types(inputs)
    return dataclasses.replace(
        inputs, skipped=skipped_rules(contract), unsupported=unsupported_fields(contract),
        not_moved=not_moved_rules(contract),
    )  # fmt: skip


def _database_name_keys(inputs: Step1Inputs) -> list[str]:
    """The endpoint settings whose database name pattern the source database must match:
    the target's, and the source's when it is PostgreSQL (``render_dms_template`` sets
    those two parameters to it)."""
    source = SOURCE_ENDPOINTS.get(inputs.plan.source.engine)
    keys = {TARGET_ENDPOINTS[inputs.contract.target.engine][1]}
    if source and source[1] == "PostgreSqlSettings":
        keys.add(source[1])
    return sorted(keys)


def bundle_inputs(bundle: DmsStepBundle) -> Step1Inputs:
    sha = contract_sha256(bundle.contract)
    ref = bundle.plan.contract
    if ref is not None and ref.sha256 is not None and ref.sha256 != sha:
        raise DmsStepError("the contract changed since the plan was written: write the plan again")
    extensions = None if bundle.extensions is None else tuple(sorted(set(bundle.extensions)))
    return _checked(
        Step1Inputs(plan=bundle.plan, contract=bundle.contract, contract_sha256=sha, ddl=bundle.ddl,
                    zero_date_defaults=tuple(sorted(set(bundle.zero_date_defaults))),
                    identity_columns=tuple(sorted(set(bundle.identity_columns))),
                    source_objects=bundle.source_objects, extensions=extensions)
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


def _step1_ddl(
    job: Any, plan: DataMovementPlan, contract: StepContract
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """The DDL of every table step 1 moves: the Aurora DDL generator over the whole
    collector (``step1.design_base``), each column declared as the contract's
    ``target_type``, kept to the moved tables (and foreign keys between them); and the
    columns it declares identity (AUTO_INCREMENT on Aurora MySQL), as (table id, column)."""
    from src.tools.schema.aurora_common.ddl_generator import TypeOverride, assemble_full_ddl
    from src.tools.transformation_contract.step1 import _load_collector, design_base

    engine = contract.target.engine
    rules = {r.target_table: r for r in _selected(contract)}
    overrides = {
        (name, f.target_column): TypeOverride(
            f.target_type, "The step 1 contract's target type.", engine
        )
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
    identity = sorted(
        (rule.source_table, c.name)
        for name, rule in rules.items()
        for c in generated[name].columns
        if c.identity
    )
    return assemble_full_ddl(ordered) + "\n", tuple(identity)


def source_objects(collector: Any) -> SourceObjects:
    """The collection's procedures, functions (trigger functions included), triggers and
    views: what DMS does not move."""
    schema = collector.database_schema
    routines = [
        str(getattr(p.procedure_type, "value", p.procedure_type)) for p in schema.procedures or []
    ]
    return SourceObjects(
        procedures=sum(1 for r in routines if r == "PROCEDURE"),
        functions=sum(1 for r in routines if r != "PROCEDURE"),
        triggers=len(schema.triggers or []),
        views=len(schema.views or []),
    )


def _collection(job: Any) -> tuple[SourceObjects, tuple[str, ...] | None]:
    from src.tools.transformation_contract.step1 import _load_collector

    collector = _load_collector(job)
    installed = collector.metadata.extensions
    names = None if installed is None else tuple(sorted({e.name.lower() for e in installed}))
    return source_objects(collector), names


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
    ddl, identity = _step1_ddl(job, plan, contract)
    defaults = _zero_date_defaults(job, contract)
    objects, extensions = _collection(job)
    enums = _step1_enum_types(job, contract)
    return _checked(dataclasses.replace(inputs, ddl=ddl, zero_date_defaults=defaults,
                                        identity_columns=identity, source_objects=objects,
                                        extensions=extensions, enum_types=enums))  # fmt: skip


def table_mappings(inputs: Step1Inputs) -> dict[str, Any]:
    """One explicit selection rule per moved table, then a limited LOB rule for each table that
    needs a larger LOB limit than the task's (:func:`_lob_settings_rules`; PostgreSQL and MySQL
    sources) and, from a PostgreSQL source, a validation rule per character array column
    (:func:`_array_validation_rules`).

    No ``table-settings`` rule runs a table in full LOB mode: DMS rejects the per-table full LOB
    rule (``bulk-max-size`` 0) when the task is created, and a nonzero size is combination
    full LOB mode, which MySQL and PostgreSQL endpoints lack. Full LOB mode is a task setting
    (:func:`task_settings`). A selection rule's ``filters`` is optional in DMS, so none is
    written."""
    rules: list[dict[str, Any]] = []
    for table in contract_tables(inputs.contract):
        n = str(len(rules) + 1)
        rules.append({"rule-type": "selection", "rule-id": n, "rule-name": n,
                      "object-locator": {"schema-name": table.schema, "table-name": table.name},
                      "rule-action": "include"})  # fmt: skip
    rules += _lob_settings_rules(inputs, len(rules) + 1)
    return {"rules": rules + _array_validation_rules(inputs, len(rules) + 1)}


def compact_mappings(mappings: dict[str, Any]) -> str:
    """The table mappings as compact JSON, one rule per line: a few hundred tables stay a
    fraction of the pretty-printed size (#1915), and the YAML keeps a ``|-`` block."""
    rules = [json.dumps(r, separators=(",", ":")) for r in mappings["rules"]]
    return '{"rules":[\n' + ",\n".join(rules) + "\n]}" if rules else '{"rules":[]}'


def _source_dialect(inputs: Step1Inputs) -> str:
    return inputs.plan.source.dialect or inputs.plan.source.engine


def _lob_mode(inputs: Step1Inputs) -> tuple[bool, int]:
    """(FullLobMode, LobMaxSize KB) over the moved tables in the plan's ``replication.lob_tables``."""
    replication = _replication(inputs)
    moved = {t.table_id for t in contract_tables(inputs.contract)}
    tables = [t for t in replication.lob_tables if t.table in moved]
    return task_lob_mode(_source_dialect(inputs), tables, replication.lob_max_size_kb)


def _own_lob_limits(inputs: Step1Inputs) -> list[LobTable]:
    """The moved keyed tables whose plan LOB size is above the task's LobMaxSize (a PostgreSQL
    source, #1918, or a MySQL one, #2123); a plan written before then sizes the task for every
    table, so none."""
    if not limited_lob_only(_source_dialect(inputs)):
        return []
    keyed = {t.table_id for t in contract_tables(inputs.contract) if t.keyed}
    replication = _replication(inputs)
    tables = [t for t in replication.lob_tables if t.table in keyed]
    return table_lob_limits(tables, _lob_mode(inputs)[1])


def _lob_settings_rules(inputs: Step1Inputs, first_id: int) -> list[dict[str, Any]]:
    """A ``table-settings`` rule with ``lob-settings`` (mode ``limited``, ``bulk-max-size`` in KB)
    per table of :func:`_own_lob_limits`: it overrides the task's LOB settings for that table
    only, so the task's buffers stay at its own LobMaxSize (#1918)."""
    rules: list[dict[str, Any]] = []
    for i, t in enumerate(_own_lob_limits(inputs)):
        schema, _, name = t.table.rpartition(".")
        n = str(first_id + i)
        rules.append({"rule-type": "table-settings", "rule-id": n, "rule-name": n,
                      "object-locator": {"schema-name": schema, "table-name": name},
                      "lob-settings": {"mode": "limited", "bulk-max-size": str(t.max_size_kb)}})  # fmt: skip
    return rules


# A character array column (VARCHAR[], TEXT[], ...). DMS validation compares a PostgreSQL
# character column through convert_to(), which has no array form: in a live run (#1918) the
# validator failed on a VARCHAR[] column with "function convert_to(character varying[], unknown)
# does not exist" and retried about 40 times a minute, reconnecting each time, while the task's
# memory grew by 250 to 500 MB a minute until DMS stopped it out of memory. Validating the MD5 of
# the array's text form on both sides avoids the error (the text form alone was truncated by the
# validator's fetch of a long array in a live run). The MD5 is cast to a bounded VARCHAR(64), the
# shape of DMS's own LOB hashes: a bare md5() returns text, which DMS handles as a LOB, so in a
# live run every non-NULL array value was reported mismatched although both sides' MD5s were
# equal (#2043).
_CHARACTER_ARRAY = re.compile(
    r"^(?:VARCHAR|CHARACTER VARYING|TEXT|CHAR|CHARACTER|BPCHAR|CITEXT|NAME)\s*(?:\(\s*\d+\s*\))?\s*(?:\[\d*\])+$",
    re.IGNORECASE,
)


def _array_validation_rules(inputs: Step1Inputs, first_id: int) -> list[dict[str, Any]]:
    """A ``validation`` rule per character array column of a moved table (a PostgreSQL source,
    #1918) that validates ``CAST(upper(md5(CAST(column AS text))) AS VARCHAR(64))`` on both
    sides (``override-validation-function``): a bounded varchar, never a LOB to DMS (#2043)."""
    if _source_dialect(inputs) != "postgresql":
        return []
    moved = {t.table_id for t in contract_tables(inputs.contract)}
    cast = "CAST(upper(md5(CAST(${column-name} AS text))) AS VARCHAR(64))"
    rules: list[dict[str, Any]] = []
    for rule in sorted(
        (r for r in inputs.contract.rules if r.source_table in moved), key=lambda r: r.source_table
    ):
        schema, _, name = rule.source_table.rpartition(".")
        for f in sorted(rule.fields, key=lambda f: f.target_column):
            if _CHARACTER_ARRAY.match(f.target_type.strip()):
                n = str(first_id + len(rules))
                rules.append({"rule-type": "validation", "rule-id": n, "rule-name": n, "rule-target": "column",
                              "object-locator": {"schema-name": schema, "table-name": name,
                                                 "column-name": f.source_column},
                              "rule-action": "override-validation-function",
                              "source-function": cast, "target-function": cast})  # fmt: skip
    return rules


def task_settings(inputs: Step1Inputs) -> dict[str, Any]:
    """The fixed task settings (spec section 5); only the LOB mode and size come from the plan.

    Full LOB mode (never truncates) applies to the whole task when any moved table needs it,
    otherwise limited LOB mode at the plan's size. A PostgreSQL or MySQL source always runs
    limited LOB mode (#1918, #2123, :func:`task_lob_mode`).

    Why limited LOB mode, never full, from these sources: in full LOB mode DMS "creates a new row
    in the target table and populates the row with all data except the associated LOB value",
    then "updates the row in the target table with the LOB data", so "all LOB columns on the
    target table must be nullable" (AWS DMS User Guide, "Best practices for AWS DMS",
    "Migrating large binary objects (LOBs)"). Step 1's DDL keeps the source's NOT NULL, so a
    NOT NULL LONGTEXT refused that first insert on Aurora MySQL (#2123), and Aurora PostgreSQL
    got an empty value for a NULL LOB (#1918). In limited mode ("LimitedSizeLobMode": true,
    "FullLobMode": false, and a ``lob-settings`` rule with mode ``limited`` for a larger table)
    a full load migrates "all LOBs inline together with other column data types" (the guide's
    "Table and collection settings rules and operations"), one insert per row. That page also
    says that during change capture limited mode "migrates all LOBs using a source table
    lookup, as in standard full LOB mode"; it does not say whether that lookup splits the insert,
    so a live run checks a captured insert into a NOT NULL LOB column (#2123). LobChunkSize
    applies to full LOB mode only and stays at the guide's recommended 64 KB."""
    full_lob, lob_max_kb = _lob_mode(inputs)
    return {
        "TargetMetadata": {
            "SupportLobs": True,
            "FullLobMode": full_lob,
            "LobChunkSize": FULL_LOB_CHUNK_KB,
            "LimitedSizeLobMode": not full_lob,
            "LobMaxSize": lob_max_kb,
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


def engine_rules(replication: DmsReplication, dialect: str | None = None) -> EngineRules:
    """The plan's DMS engine version and reason; a plan written before contract 1.3 has none,
    so the catalog's applies, with the source ``dialect``'s own reasoning (#2123)."""
    catalog = dms_sizing().engine
    if replication.engine_version is None:
        return EngineRules(version=catalog.version, why=catalog.why_for(dialect))
    return EngineRules(
        version=replication.engine_version,
        why=replication.engine_version_why or catalog.why_for(dialect),
    )


def _dms_parameters(inputs: Step1Inputs) -> dict[str, Any]:
    replication, database = _replication(inputs), inputs.plan.source.database
    s, engine = replication.sizing, engine_rules(replication, _source_dialect(inputs))
    (_, source_key), (_, target_key) = _keys(inputs)
    secret = ("Full ARN of the Secrets Manager secret of the {who} (the DMS user): JSON keys username, password, "  # nosec B105 -- help text
              "host and port. Not the RDS-managed master secret, which has no host or port")  # fmt: skip
    kms = "Customer managed KMS key ARN that encrypts the {who} secret (empty when it uses the AWS managed key)"
    params: dict[str, Any] = {
        "InstanceClass": {"Type": "String", "Default": s.instance_class, "AllowedPattern": r"^dms\.[a-z0-9]+\.[a-z0-9]+$",
                          "Description": f"Replication instance class. Sized from the collection: {s.reason}"[:4000]},
        "EngineVersion": {"Type": "String", "Default": engine.version, "AllowedPattern": r"^[0-9]+\.[0-9]+\.[0-9]+$",
                          "Description": f"AWS DMS engine version of the replication instance. {engine.why}"[:4000]},
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
    if fails_on_lob_truncation(_source_dialect(inputs)):
        # Limited LOB mode truncates a LOB above LobMaxSize with a warning; this stops the task
        # instead, so no value is cut short silently (#1918). PostgreSQL only: the MySQL source
        # endpoint has no such setting (and this function returns above for it).
        extra["FailTasksOnLobTruncation"] = True
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
            # Without an EngineVersion DMS uses its default engine (3.5.4), not its latest.
            "EngineVersion": {"Ref": "EngineVersion"},
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
            "TableMappings": compact_mappings(table_mappings(inputs)),
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


# The extension a PostgreSQL column type's base name needs (pgvector's types, PostGIS's);
# a base name that is itself an installed extension's name (citext, hstore) needs that one.
_TYPE_EXTENSIONS: dict[str, str] = {
    "vector": "vector",
    "halfvec": "vector",
    "sparsevec": "vector",
    "geometry": "postgis",
    "geography": "postgis",
    "citext": "citext",
    "hstore": "hstore",
}
_BASE_TYPE = re.compile(r"[a-z_][a-z0-9_]*")
_EXTENSION_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


@dataclass(frozen=True)
class RequiredExtension:
    """An extension a moved column type needs on Aurora PostgreSQL."""

    name: str
    # The column types (base names, sorted) that need it, and how many moved columns have one.
    types: tuple[str, ...]
    columns: int
    # The newest version the target line offers, from the Aurora PostgreSQL extension
    # catalog; None when the catalog does not list it (or the line is unknown).
    offered: str | None


def required_extensions(inputs: Step1Inputs) -> list[RequiredExtension]:
    """The extensions the moved columns' declared types need, sorted by name (Aurora
    PostgreSQL only): each is created before the tables, or the DDL fails on the type."""
    from src.shared.catalog.gaps import aurora_extension_lines

    if inputs.contract.target.engine != "aurora_postgresql":
        return []
    installed = set(inputs.extensions or ())
    found: dict[str, list[str]] = {}
    for rule in _selected(inputs.contract):
        for f in rule.fields:
            base = _BASE_TYPE.match(f.target_type.strip().lower())
            if base is None:
                continue
            name = _TYPE_EXTENSIONS.get(base.group()) or (
                base.group() if base.group() in installed else None
            )
            if name and _EXTENSION_NAME.fullmatch(name):
                found.setdefault(name, []).append(base.group())
    line = _replication(inputs).target_engine_line
    offered = aurora_extension_lines()["aurora_postgresql"].lines.get(line or "")
    return [
        RequiredExtension(name=name, types=tuple(sorted(set(types))), columns=len(types),
                          offered=offered.extensions.get(name) if offered else None)
        for name, types in sorted(found.items())
    ]  # fmt: skip


def _extension_lines(inputs: Step1Inputs) -> list[str]:
    needed = required_extensions(inputs)
    if not needed:
        return []
    line = _replication(inputs).target_engine_line
    where = f"Aurora PostgreSQL {line}" if line else "your Aurora PostgreSQL version"
    out = ["", "Extensions: the moved columns' types need these extensions, so this file creates them "
           "(CREATE EXTENSION IF NOT EXISTS) before the tables. Each must be available on your Aurora "
           "PostgreSQL version, and creating one needs the administrator (rds_superuser):"]  # fmt: skip
    for e in needed:
        noun = "column" if e.columns == 1 else "columns"
        offered = (f"{where} offers {e.offered}" if e.offered else
                   f"not in the extension list for {where}: check that it is available")  # fmt: skip
        extra = (" halfvec and sparsevec need version 0.7.0 or later."
                 if e.name == "vector" and {"halfvec", "sparsevec"} & set(e.types) else "")  # fmt: skip
        out.append(
            f"- {e.name}, for {e.columns} {noun} of type {', '.join(e.types)}: {offered}.{extra}"
        )
    return out


def moved_identity_columns(inputs: Step1Inputs) -> list[tuple[str, str]]:
    """The identity columns of the tables step 1 moves, sorted by table id and column."""
    moved = {t.table_id for t in contract_tables(inputs.contract)}
    return sorted(c for c in inputs.identity_columns if c[0] in moved)


def not_moved(inputs: Step1Inputs) -> list[NotMoved]:
    """What the source has that step 1 does not move, with the collection's counts: DMS
    moves table rows only. The DDL header and ``run_dms_step.py summary`` list these."""
    objects = inputs.source_objects
    out = []
    for kind in ("procedures", "functions", "triggers", "views"):
        count = None if objects is None else int(getattr(objects, kind))
        note = ("not known: the collection was not read" if count is None
                else "recreate them on Aurora yourself" if count else "none in the collection")  # fmt: skip
        section = inputs.contract.routines
        if count and kind != "views" and section is not None and section.carried:
            note = ("after the full load, /move writes routines-deploy.sql for the routines step 1 "
                    "carries (its routines section names the rest); you run it")  # fmt: skip
        out.append(NotMoved(kind, count, note))
    if inputs.plan.source.engine in ("mysql", "mariadb"):
        out.append(NotMoved("events", None, "not in the collection: list them on the source with "
                            "SHOW EVENTS and recreate them on Aurora yourself"))  # fmt: skip
    if inputs.contract.target.engine == "aurora_postgresql":
        n = len(moved_identity_columns(inputs))
        out.append(NotMoved("sequence values", n, f"{SEQUENCES_FILE} sets them"))
    else:
        out.append(NotMoved("sequence values", None, "none to set: an AUTO_INCREMENT counter moves past "
                            "every value inserted into its column, so it follows the loaded ids"))  # fmt: skip
    return out


def _not_moved_lines(inputs: Step1Inputs) -> list[str]:
    lines = ["Not moved by step 1 (DMS moves table rows only; counts from the collection):"]
    for n in not_moved(inputs):
        lines.append(
            f"- {n.kind}: " + (n.note if n.count is None else f"{n.count} ({n.note})") + "."
        )
    return lines


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pg_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def render_sequences(inputs: Step1Inputs) -> str:
    """``step-1.sequences.sql`` (Aurora PostgreSQL): one ``setval`` per identity column of
    a moved table, sorted by table id and column, setting the column's sequence so the
    next generated value is the column's maximum plus one. DMS writes each row's id as the
    source has it and never advances the sequence, so without it the first insert on
    Aurora without an explicit id collides with a loaded row."""
    if inputs.contract.target.engine != "aurora_postgresql":
        raise DmsStepError(f"{SEQUENCES_FILE} is for an Aurora PostgreSQL target")
    plan = inputs.plan
    columns = moved_identity_columns(inputs)
    by_id = {t.table_id: t for t in contract_tables(inputs.contract)}
    run = (f'psql "host=<writer endpoint> dbname={plan.source.database} user=<admin user> '
           f'sslmode=verify-full sslrootcert=<RDS CA bundle>" -f {SEQUENCES_FILE}')  # fmt: skip
    head = [
        CUTOVER_DISCLAIMER,
        "",
        "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
        "SPDX-License-Identifier: MIT-0",
        "",
        f"{SEQUENCES_FILE}: sets the sequence of every identity column step 1 moves into Aurora "
        f"PostgreSQL, for job {plan.job_id} (assignment v{plan.assignment_version}). {_GENERATED}",
        "",
        "YOUR RESPONSIBILITY: " + CUSTOMER_RESPONSIBILITY,
        "",
        "Why: DMS writes each row's id as the source has it but never advances the column's sequence, "
        "so after the load every sequence still starts at 1 and the first insert without an explicit "
        "id collides with a loaded row. The gate compares rows only and cannot see this.",
        "",
        "Each statement sets the sequence so the next value is the column's maximum plus one; "
        "running it again is safe.",
        f"How: {run}",
        "",
        f"Identity columns: {len(columns)}.",
    ]
    out = with_header("", head, "--")
    if columns:
        out += "\n"
    for table_id, column in columns:
        t = by_id[table_id]
        qualified = f"{_pg_ident(t.schema)}.{_pg_ident(t.name)}"
        col = _pg_ident(column)
        # A file the customer runs, never executed here: schema and table are plain identifiers
        # (_checked), and the column is quoted with its quotes doubled, as an identifier and
        # as a literal.
        sequence = f"pg_get_serial_sequence({_pg_literal(qualified)}, {_pg_literal(column)})"
        out += f"SELECT setval({sequence}, COALESCE(MAX({col}), 0) + 1, false) FROM {qualified};\n"  # nosec B608 -- quoted, see above
    return out


# Per source family (#1918, #2123): the LOB types to name, why the task never runs full LOB
# mode, and whether the source endpoint can stop the task on a truncated value.
_LOB_KINDS = {"postgresql": "text, json, jsonb, arrays and the like",
              "mysql": "mediumtext, longtext, mediumblob, longblob, json and spatial types"}  # fmt: skip
_NO_FULL_LOB = {
    "postgresql": "from a PostgreSQL source, full LOB mode writes a NULL LOB as an empty value",
    "mysql": "from a MySQL source, full LOB mode inserts each row with its LOB columns NULL and "
             "fills them in afterwards, which a NOT NULL LOB column on Aurora refuses",
}  # fmt: skip


def _lob_lines(inputs: Step1Inputs) -> list[str]:
    """The LOB size step 1's task runs at and where it comes from (a PostgreSQL source, #1918, or
    a MySQL one, #2123)."""
    dialect = _source_dialect(inputs)
    if not limited_lob_only(dialect):
        return []
    moved = {t.table_id for t in contract_tables(inputs.contract)}
    lob = [t for t in _replication(inputs).lob_tables if t.table in moved]
    if not lob:
        return []
    family = "postgresql" if dialect == "postgresql" else "mysql"
    _, size = _lob_mode(inputs)
    instance = _replication(inputs).sizing.instance_class
    unmeasured = unmeasured_lob_tables(lob, dialect)
    if unmeasured and family in dms_sizing().lob.measure_required:
        # No stack and no size until every table is measured (#2123): never a 64 KB fallback.
        return ["", (f"LOB columns: {len(lob)} moved table{'' if len(lob) == 1 else 's'} have LOB columns "
                     f"({_LOB_KINDS[family]}). The DMS task runs in limited LOB mode, sized from each table's "
                     f"longest value: {_NO_FULL_LOB[family]}. {len(unmeasured)} of them have a LOB column the "
                     "collection did not measure: " + _first_tables(unmeasured) + ". This source endpoint "
                     "truncates a longer value with no task error, so step 1 writes no DMS stack "
                     "(dms-step-1.yaml) and preflight's lob_sizes check stops until they are measured: run the "
                     f"read-only {LOB_SIZES_SQL} on the source (lob-sizes.md says how), then {LOB_SIZES_COMMAND}, "
                     "which sizes each table from its output and writes this file and the stack.")]  # fmt: skip
    head = (f"LOB columns: {len(lob)} moved table{'' if len(lob) == 1 else 's'} have LOB columns "
            f"({_LOB_KINDS[family]}). The DMS task runs in limited LOB mode at {size} KB (LobMaxSize) on "
            f"{instance}: {_NO_FULL_LOB[family]}. ")  # fmt: skip
    if own := _own_lob_limits(inputs):
        n = len(own)
        head += (f"{n} table{' gets' if n == 1 else 's get'} a larger limit of {'its' if n == 1 else 'their'} own "
                 "(a table-settings rule with lob-settings, mode limited, bulk-max-size in KB), sized from "
                 "the measured longest values: " + ", ".join(f"{t.table} at {t.max_size_kb} KB" for t in own)
                 + f". {dms_sizing().lob.table_settings_why} ")  # fmt: skip
    skipped = skipped_lob_tables(lob)
    if unmeasured:
        head += (f"{len(unmeasured)} of them have a LOB column the collection did not measure, so this size is "
                 "not a measured one for them: "
                 + _first_tables(unmeasured) + f". Before the full load, run {measure_lob_sql(dialect)} on the "
                 f"source for each of their LOB columns; if 2 bytes per byte of the longest value is above that "
                 f"size, raise the task's LobMaxSize (at most {DMS_MAX_LOB_KB} KB) and the stack's "
                 "InstanceClass with it, or collect again and write the plan again. ")  # fmt: skip
    elif skipped:
        head += (f"{SKIPPED}: {len(skipped)} of them run at {dms_sizing().lob.skipped_max_kb} KB: "
                 + _first_tables(skipped) + f". {dms_sizing().lob.skipped_why} ")  # fmt: skip
    else:
        head += ("The size comes from the collection's measured longest values (the plan's "
                 "replication.lob_tables gives each table's); measure again before the full load if the data "
                 "grew. ")  # fmt: skip
    head += ("During change capture DMS allocates memory in proportion to the LOB limit times the LOB columns, "
             "so the plan sizes the replication instance for the largest limit. A larger value is truncated: ")  # fmt: skip
    if fails_on_lob_truncation(dialect):
        head += ("the source endpoint sets FailTasksOnLobTruncation, but in a live full load DMS still "
                 "truncated such a value with only a warning in the task log, ")  # fmt: skip
    else:
        head += ("this source endpoint has no FailTasksOnLobTruncation setting (DMS truncates with only a "
                 "warning in the task log), ")  # fmt: skip
    head += ("so DMS validation is what reports those rows (as mismatched). The tables: "
             + _first_tables([t.table for t in lob]) + ".")  # fmt: skip
    return ["", head]


def _not_moved_table_lines(inputs: Step1Inputs) -> list[str]:
    """The tables this file creates that the DMS task does not move (#1918)."""
    if not inputs.not_moved:
        return []
    return ["Tables created but not moved (the DMS task leaves them out; rebuilding their rows is yours):",
            *(f"- {s.table_id}: {s.reason}" for s in inputs.not_moved)]  # fmt: skip


def _first_tables(names: list[str], shown: int = 10) -> str:
    more = len(names) - shown
    return ", ".join(names[:shown]) + (
        f" and {more} more (the plan's replication.lob_tables)" if more > 0 else ""
    )


def render_ddl(inputs: Step1Inputs) -> str:
    """``step-1.ddl.sql``: the license header, the disclaimer and the instructions as
    comments, then the database (Aurora MySQL) or schema (Aurora PostgreSQL), then the DDL;
    on Aurora PostgreSQL, the extensions its column types need before the tables and the
    commented REPLICA IDENTITY FULL recommendation per table."""
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
        "5. This release does not carry schema changes: a change to these tables or to the source's tables "
        "while the task runs is not applied by step 1.",
    ]
    if pg:
        head.append(f"6. {SEQUENCES_FILE} sets the identity columns' sequences, which DMS never advances: "
                    "run it the same way.")  # fmt: skip
    head += [
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
                    "allows it, GRANT SET ON PARAMETER session_replication_role); drop that grant when "
                    "you end the step.")  # fmt: skip
    else:
        head.append("- The DMS target endpoint turns foreign key checks off for its own sessions during "
                    "the load (SET FOREIGN_KEY_CHECKS=0).")  # fmt: skip
    if zero:
        head += ["", f"Zero dates: aurora-target.yaml sets sql_mode = {ZERO_DATE_SQL_MODE} in the writer's "
                 f"DB parameter group. Why: {_zero_date_reason(counts)} The columns: "
                 + ", ".join(f"{t}.{c}" for t, c in zero) + "."]  # fmt: skip
    head += _extension_lines(inputs)
    head += _lob_lines(inputs)
    head.append("")
    head += [*_not_moved_lines(inputs), ""]
    if inputs.skipped:
        head.append("Tables left out (blocked in the contract; DMS does not move them):")
        head += [f"- {s.table_id}: {s.reason}" for s in inputs.skipped]
    else:
        head.append("Tables left out: none.")
    head += _not_moved_table_lines(inputs)
    if inputs.unsupported:
        head.append(
            "Unsupported fields (DMS copies the source value; Aurora converts it or refuses the row):"
        )
        head += [f"- {u.table_id}.{u.column}: {u.description}" for u in inputs.unsupported]
    else:
        head.append("Unsupported fields: none.")
    head += _enum_header_lines(inputs)
    out = with_header("", head, "--")
    if pg:
        out += (
            f'\nCREATE SCHEMA IF NOT EXISTS "{schemas[0]}";\nSET search_path TO "{schemas[0]}";\n'
        )
        for e in required_extensions(inputs):
            out += f"CREATE EXTENSION IF NOT EXISTS {_pg_ident(e.name)};\n"
    else:
        db = database.replace("`", "``")
        out += f"\nCREATE DATABASE IF NOT EXISTS `{db}`;\nUSE `{db}`;\n"
    out += _enum_type_block(inputs)
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


def _enum_header_lines(inputs: Step1Inputs) -> list[str]:
    """What ``step-1.ddl.sql`` says about PostgreSQL enum types (#1834): the ones it creates
    and how DMS loads them, and the moved columns that stay TEXT and why."""
    lines: list[str] = []
    if inputs.enum_types:
        names = ", ".join(e.sql_type for e in inputs.enum_types)
        lines += ["", f"Enum types: the file creates {names} before the tables, each with the source's labels "
                  "in the source's order, and the columns of those types keep them. AWS DMS reads a "
                  "PostgreSQL enum as a string (up to 64 bytes; its documentation lists ENUM among the types "
                  "it does not migrate when it creates the target tables itself). Here the tables exist, so "
                  "it writes each value as text into the enum column, which accepts only its labels. Test "
                  "the ongoing replication on a copy: if an apply fails with \"is of type "
                  "... but expression is of type character varying\", the commented CREATE CAST after the "
                  "type lets PostgreSQL convert it (YOUR DECISION, as above)."]  # fmt: skip
    fallbacks = [(r.source_table, f) for r in _selected(inputs.contract) for f in r.findings
                 if f.code in (NOT_COLLECTED, REFUSED)]  # fmt: skip
    if fallbacks:
        lines += ["", "Columns that stay TEXT although their PostgreSQL type may be an enum:"]
        lines += [f"- {t}.{f.column}: {f.message}" for t, f in fallbacks]
    return lines


def _enum_type_block(inputs: Step1Inputs) -> str:
    """The CREATE TYPE statements, before the tables (#1834); empty when there is none."""
    if not inputs.enum_types:
        return ""
    lines = ["", "SET standard_conforming_strings = on;"]
    for e in inputs.enum_types:
        lines += [create_type_sql(e),
                  f"-- CREATE CAST (character varying AS {e.sql_type}) WITH INOUT AS ASSIGNMENT;"]  # fmt: skip
    return "\n".join(lines) + "\n"


def lob_measure_columns(inputs: Step1Inputs) -> list[Any]:
    """The moved LOB columns ``lob-sizes.sql`` measures (#2123): a MySQL source's tables the
    collection did not measure; none otherwise."""
    from src.tools.data_movement.lob_sizes import measure_columns

    moved = {t.table_id for t in contract_tables(inputs.contract)}
    return measure_columns(_replication(inputs).lob_tables, _source_dialect(inputs), moved)


def render_files(inputs: Step1Inputs) -> dict[str, str]:
    """The files of step 1, by file name: three, ``step-1.sequences.sql`` on Aurora
    PostgreSQL; while a MySQL LOB table is unmeasured (#2123), ``lob-sizes.sql`` with
    ``lob-sizes.md`` instead of the DMS stack, which is never written with a guessed size."""
    from src.tools.data_movement.debezium_connector import dump_template
    from src.tools.data_movement.lob_sizes import (
        INSTRUCTIONS_FILE,
        SCRIPT,
        render_instructions,
        render_script,
    )

    aurora = with_header(dump_template(render_aurora_template(inputs)), _disclaimer_lines(inputs))
    files = {AURORA_TEMPLATE: aurora, DDL_FILE: render_ddl(inputs)}
    columns = lob_measure_columns(inputs)
    if not columns:  # never a stack with a guessed LOB size (#2123)
        files[DMS_TEMPLATE] = with_header(dump_template(render_dms_template(inputs)), [_GENERATED])
    if inputs.contract.target.engine == "aurora_postgresql":
        files[SEQUENCES_FILE] = render_sequences(inputs)
    if columns:
        step_dir = f"{inputs.plan.database_name}/{inputs.plan.job_id}/{STEP_DIR}"
        files[SCRIPT] = render_script(columns, inputs.contract_sha256)
        files[INSTRUCTIONS_FILE] = render_instructions(columns, step_dir)
    return files


def step1_files(target_engine: str, inputs: Step1Inputs | None = None) -> tuple[str, ...]:
    """The names of the files :func:`render_files` writes for a target engine (and, given the
    step's ``inputs``, the LOB size files while a table needs measuring)."""
    from src.tools.data_movement.lob_sizes import INSTRUCTIONS_FILE, SCRIPT

    measuring = inputs is not None and bool(lob_measure_columns(inputs))
    names = [AURORA_TEMPLATE, DDL_FILE] + ([] if measuring else [DMS_TEMPLATE])
    if target_engine == "aurora_postgresql":
        names.append(SEQUENCES_FILE)
    if measuring:
        names += [SCRIPT, INSTRUCTIONS_FILE]
    return tuple(names)


def write_step1_delivery(store: Any, db: str, job_id: str) -> dict[str, Any]:
    """Write step 1's files under the job; ``skipped`` when the step moves no table."""
    try:
        inputs = load_step1(store, db, job_id)
    except DmsStepSkipped as skipped:
        return {"status": "skipped", "reason": str(skipped)}
    from src.tools.data_movement.lob_sizes import INSTRUCTIONS_FILE, SCRIPT

    paths = []
    files = render_files(inputs)
    for name, text in files.items():
        path = f"{db}/{job_id}/{STEP_DIR}/{name}"
        store.write_bytes(path, text.encode("utf-8"))
        paths.append(path)
    # No stale script once nothing is left to measure, and no stale stack while something is (#2123).
    for name in (SCRIPT, INSTRUCTIONS_FILE, DMS_TEMPLATE):
        if name not in files:
            store.delete(f"{db}/{job_id}/{STEP_DIR}/{name}")
    replication = _replication(inputs)
    pg = inputs.contract.target.engine == "aurora_postgresql"
    return {
        "status": "complete",
        "files": paths,
        "lob_sizes_needed": len({c.table for c in lob_measure_columns(inputs)}),
        "instance_class": replication.sizing.instance_class,
        "contract_sha256": inputs.contract_sha256,
        "replica_identity_recommended": len(contract_tables(inputs.contract)) if pg else 0,
        "sequences_to_set": len(moved_identity_columns(inputs)) if pg else 0,
        "extensions": [e.name for e in required_extensions(inputs)],
    }
