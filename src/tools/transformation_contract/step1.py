# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's transformation contract: whole-table mirrors into Aurora (#1764, ADR-033 Decision 7).

No model: one rule per wave 1 table, a field per column; copy when the Aurora type
equals the source type, a cast otherwise. Targets come from the saved Aurora design,
or from the Aurora DDL generator when there is none.

Each table keeps its source schema in the target (#2494): when the collector's tables come
from more than one schema (MySQL: database), a target is keyed ``<schema>.<table>`` as the
schema design keys it (#2455), so ``sales.orders`` and ``archive.orders`` are two targets.
A source of one schema keys targets by the bare table name, as before.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from src.contracts.aurora_design_delta import validate_aurora_type
from src.contracts.collector_output import CollectorOutputContract, Column
from src.contracts.lineage_ops import ColumnRef, CopyOp, TransformOp, UnsupportedOp
from src.contracts.schema_design_input import AgentTable, project_schema_design_tables
from src.contracts.transformation_contract import (
    FieldRule,
    Finding,
    Rule,
    RuleStatus,
    StepContract,
    TargetSpec,
    printable,
)
from src.shared.contract_versioning import load_contract
from src.shared.sql_identifiers import UNSAFE_NAME_REASON, has_unprintable
from src.shared.table_keys import is_system_schema, spans_schemas
from src.shared.table_keys import table_key as table_key_of
from src.tools.data_movement.plan import JobInputs, _aurora_wave, _Schema
from src.tools.data_movement.validation_key import table_key
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, source_data_types
from src.tools.schema.aurora_common.source_family import classify_source_family
from src.tools.schema.aurora_common.source_types import resolve_source_type
from src.tools.transformation_contract.pg_enums import EnumColumns, EnumType, enum_columns

#: The finding that blocks a table whose name no SQL file may carry (#2381).
UNSAFE_NAME = "unsafe_name"
UNSAFE_TABLE_REASON = "the table's " + UNSAFE_NAME_REASON.removeprefix("its ")

AURORA_FOR = {"mysql": "aurora_mysql", "mariadb": "aurora_mysql", "postgresql": "aurora_postgresql"}


@dataclass(frozen=True)
class TargetColumn:
    name: str
    aurora_type: str


@dataclass(frozen=True)
class TargetTable:
    name: str
    columns: tuple[TargetColumn, ...]


def keeps_schemas(collector: CollectorOutputContract) -> bool:
    """Whether step 1 keys its targets ``<schema>.<table>`` (#2494): the collector's tables
    come from more than one schema, a system schema not counted (``spans_schemas``), the
    rule the schema design keys its tables by (#2455)."""
    source = collector.metadata.source_database.engine.value
    return spans_schemas((t.schema_name for t in collector.database_schema.tables), source)


def target_key(table_name: str, schema_name: str | None, qualified: bool) -> str:
    """A target's key: ``<schema>.<table>`` with ``qualified`` (``design_table_name``), else
    the bare table name."""
    return f"{schema_name}.{table_name}" if qualified and schema_name else table_name


def target_tables(
    inputs: JobInputs, engine: str, collector: CollectorOutputContract | None = None
) -> dict[str, TargetTable]:
    """The Aurora tables by :func:`target_key`.

    A saved design covers only the tables routed to Aurora, while wave 1 mirrors every
    table of the wave, so the generator's tables for the whole collector come first and
    the saved design's columns replace the generated ones of the same name, per column
    (a design column the generator lacks is kept, a generated column the design lacks too).
    With targets keyed by schema (#2494), a saved table that names its schema is that
    schema's table; one that does not (a design of one schema, or one saved before #2455)
    is the one table of its name, and reaches no table when two schemas have one.
    """
    collector = collector or _load_collector(inputs)
    qualified = keeps_schemas(collector)
    tables = _generated_tables(inputs, engine, collector)
    by_bare: dict[str, list[str]] = {}
    for t in _user_tables(collector):
        by_bare.setdefault(t.table_name, []).append(target_key(t.table_name, t.schema_name, True))
    saved = inputs.schema_designs.get(engine)
    if saved:
        for d in saved[1].get("table_definitions") or []:
            name = d["table_name"]
            if qualified:
                found = (
                    [target_key(name, d["schema_name"], True)]
                    if d.get("schema_name")
                    else by_bare.get(name, [])
                )
                if len(found) != 1:
                    continue
                name = found[0]
            columns = {c.name: c for c in (tables[name].columns if name in tables else ())}
            for c in d.get("columns") or []:
                columns[c["name"]] = TargetColumn(c["name"], c["aurora_type"])
            tables[name] = TargetTable(name, tuple(columns.values()))
    ids = {target_key(t.table_name, t.schema_name, qualified): t.table_id
           for t in _user_tables(collector)}  # fmt: skip
    return _with_enum_types(tables, enum_columns(collector, engine), ids)


#: The warning a rule gets when a saved design names its table with no schema while two
#: source schemas have a table of that name (a design saved before #2455): its reviewed types
#: reach neither table, and the customer sees that at the gate (#2525 review).
SAVED_TYPES_NOT_APPLIED = "saved_design_types_not_applied"


def unapplied_saved_tables(
    inputs: JobInputs, engine: str, collector: CollectorOutputContract
) -> set[str]:
    """The bare names of the saved design's tables with no schema that two or more of the
    step's schemas have (:func:`target_tables` skips them), when targets keep their schema."""
    saved = inputs.schema_designs.get(engine)
    if not saved or not keeps_schemas(collector):
        return set()
    counts = Counter(t.table_name for t in _user_tables(collector))
    return {
        str(d["table_name"])
        for d in saved[1].get("table_definitions") or []
        if not d.get("schema_name") and counts[d["table_name"]] > 1
    }


def saved_types_finding(table_id: str, table_name: str) -> Finding:
    return Finding(
        code=SAVED_TYPES_NOT_APPLIED,
        severity="warning",
        message=(
            f"the saved Aurora design names {table_name} with no schema, and more than one "
            "source schema has a table of that name, so its column types were not applied to "
            "this table: step 1 uses the generated types. Design the schema again to apply them"
        ),
        table_id=table_id,
    )


def _user_tables(collector: CollectorOutputContract) -> list:
    """The collector's tables outside the source engine's system schemas (#2478)."""
    source = collector.metadata.source_database.engine.value
    return [
        t for t in collector.database_schema.tables if not is_system_schema(t.schema_name, source)
    ]


def _with_enum_types(
    tables: dict[str, TargetTable], enums: EnumColumns, ids: dict[str, str]
) -> dict[str, TargetTable]:
    """Each column that keeps its PostgreSQL enum (#1834) declared as ``"<schema>"."<type>"``,
    over the generator's and a saved design's type: neither saw the labels (a design's delta
    cannot name such a type), so both fall back to TEXT for it. ``ids``: each target's table
    id, which the enums are keyed by."""
    if not enums.types:
        return tables
    out = {}
    for name, t in tables.items():
        table_id = ids.get(name, "")
        out[name] = TargetTable(name, tuple(
            TargetColumn(c.name, enums.types[(table_id, c.name)].sql_type)
            if (table_id, c.name) in enums.types else c
            for c in t.columns))  # fmt: skip
    return out


def _load_collector(inputs: JobInputs) -> CollectorOutputContract:
    """The collector as the pipeline reads it: version checked, older MAJORs migrated."""
    return load_contract(CollectorOutputContract, inputs.collector, artifact="collector output")


def design_base(
    inputs: JobInputs,
    engine: str,
    collector: CollectorOutputContract,
    qualified: bool | None = None,
) -> AuroraDesignBase:
    """The Aurora DDL generator's input for every collector table: what the step 1
    targets are generated from, and what step 1's DDL file is rendered from (#1798).

    ``qualified``: whether each table is created in its source schema and keyed
    ``<schema>.<table>`` (#2494); None decides it from the collector (:func:`keeps_schemas`).
    The DDL passes whether the tables it moves span schemas, so a step whose other schemas
    move nothing keeps the one-schema form."""
    source = collector.metadata.source_database
    if qualified is None:
        qualified = keeps_schemas(collector)
    tables = project_schema_design_tables(collector)
    return AuroraDesignBase(
        engine=engine,
        job_id=inputs.job_id,
        source_database=source.database_name or "",
        source_engine=source.engine.value,
        tables=tables,
        source_data_types=source_data_types(dict(inputs.collector), qualified=qualified),
        schema_qualified=qualified,
        design_names=saved_design_index_names(inputs, engine, tables),
    )


def saved_design_index_names(
    inputs: JobInputs, engine: str, tables: list[AgentTable]
) -> dict[tuple[str, str], str] | None:
    """(table id, collected index name) -> the name the saved ``engine`` design stores
    for that index in ``table_definitions[].indexes`` (#2545): the names the customer's
    design DDL uses, whatever release or delta wrote them. None with no design or a skip
    record. Step 1 creates each index under that name when the name is free.

    A saved table that names its schema is that schema's table. One that does not (a design
    of one schema) is the table of its name in the schema the design's other tables
    resolve to; when nothing tells two schemas' tables apart, each of them gets the
    design's names. Each collected index maps to the stored statement of its own name,
    else to the one an ``index_renamed`` note gives it, else to the one stored statement
    with its key columns that nothing else took; an index the design does not create
    maps to nothing."""
    from src.tools.schema.aurora_common.saved_design import stored_index

    saved = inputs.schema_designs.get(engine)
    if not saved or saved[1].get("status") == "skipped":
        return None
    defs = [d for d in saved[1].get("table_definitions") or [] if isinstance(d, dict)]
    matches = [
        [t for t in tables if t.table_name == d.get("table_name")
         and (not d.get("schema_name") or t.schema_name == d.get("schema_name"))]
        for d in defs
    ]  # fmt: skip
    schemas = {m[0].schema_name for m in matches if len(m) == 1}
    renamed = _renamed_by_notes(saved[1].get("app_layer_notes"))
    out: dict[tuple[str, str], str] = {}
    for d, found in zip(defs, matches, strict=True):
        if len(found) > 1 and len([t for t in found if t.schema_name in schemas]) == 1:
            found = [t for t in found if t.schema_name in schemas]
        stored = [s for s in (stored_index(i) for i in d.get("indexes") or []) if s is not None]
        label = (
            f"{d['schema_name']}.{d['table_name']}" if d.get("schema_name") else d.get("table_name")
        )
        for t in found:
            out.update(_stored_names(t, stored, renamed, str(label)))
    return out


def _renamed_by_notes(notes: object) -> dict[tuple[str, str], str]:
    """(design table, collected name) -> new name, from the design's ``index_renamed`` notes
    (``Created as <new>: ...``, #2410)."""
    out: dict[tuple[str, str], str] = {}
    for n in notes if isinstance(notes, list) else []:
        if not isinstance(n, dict) or n.get("feature") != "index_renamed":
            continue
        obj, text = str(n.get("source_object") or ""), str(n.get("recommendation") or "")
        if "." in obj and text.startswith("Created as ") and ":" in text:
            table, name = obj.rsplit(".", 1)
            out[(table, name)] = text[len("Created as ") :].split(":", 1)[0]
    return out


def _stored_names(
    t: AgentTable,
    stored: list[tuple[str, tuple[str, ...]]],
    renamed: dict[tuple[str, str], str],
    label: str,
) -> dict[tuple[str, str], str]:
    """``t``'s collected indexes mapped to the saved table's stored index names."""
    free = dict(stored)
    out: dict[tuple[str, str], str] = {}
    # An expression index is one only step 1 creates (#2547): no design stores it.
    indexes = [i for i in t.indexes or [] if not i.has_expressions]
    for rule in ("own", "note", "columns"):
        for idx in indexes:
            if (t.table_id, idx.index_name) in out:
                continue
            if rule == "own":
                name = idx.index_name if idx.index_name in free else None
            elif rule == "note":
                name = renamed.get((label, idx.index_name))
                name = name if name in free else None
            else:
                same = [n for n, cols in free.items() if list(cols) == list(idx.columns)]
                name = same[0] if len(same) == 1 else None
            if name is not None and not has_unprintable(name):
                out[(t.table_id, idx.index_name)] = name
                free.pop(name)
    return out


def _generated_tables(
    inputs: JobInputs, engine: str, collector: CollectorOutputContract
) -> dict[str, TargetTable]:
    """The Aurora DDL generator's tables for every collector table.

    ``base_from_outputs`` also needs the analysis output, which a job's inputs do not
    carry and the draft does not use; the base is built from the same table projection
    (``project_schema_design_tables``) it uses.
    """
    base = design_base(inputs, engine, collector)
    return {
        t.table_name: TargetTable(
            t.table_name, tuple(TargetColumn(c.name, c.aurora_type) for c in t.columns)
        )
        for t in base.generate().tables
    }


def _same_type(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def _canonical_source_type(column: Column, source_engine: str, engine: str) -> str:
    """The source column's type spelled as the target engine spells it (``int(11)`` is
    ``INT``, ``character varying`` with a length is ``VARCHAR(n)``); the raw ``data_type``
    when the type has no unambiguous spelling there."""
    resolution = resolve_source_type(
        column.data_type,
        source_family=classify_source_family(source_engine),
        target=engine,
        max_length=column.max_length,
        numeric_precision=column.numeric_precision,
        numeric_scale=column.numeric_scale,
        datetime_precision=column.datetime_precision,
    )
    if resolution is not None and not resolution.needs_judgment:
        return resolution.aurora_type
    return column.data_type


def _field(
    table_id: str,
    column: Column,
    target: TargetColumn,
    source_engine: str,
    engine: str,
    enum: EnumType | None = None,
) -> FieldRule:
    """``enum``: the PostgreSQL enum the column keeps (#1834), its source type too."""
    ref = ColumnRef(table_id=table_id, column=column.column_name)
    source_type = enum.sql_type if enum else _canonical_source_type(column, source_engine, engine)
    op: CopyOp | TransformOp | UnsupportedOp
    try:
        validate_aurora_type(target.aurora_type, engine)
    except ValueError:
        op = UnsupportedOp(
            op="unsupported", description=f"no Aurora type for {column.data_type or 'unknown'}"
        )
    else:
        if _same_type(source_type, target.aurora_type):
            op = CopyOp(op="copy", column=ref)
        else:
            op = TransformOp(
                op="transform",
                column=ref,
                function="cast",
                from_type=source_type or "unknown",
                to_type=target.aurora_type,
            )
    return FieldRule(
        source_column=ref.column, target_column=target.name, target_type=target.aurora_type, op=op
    )


def _ordered(columns: list[Column]) -> list[Column]:
    """By ``ordinal_position``; columns without one keep the collector's order, after."""
    indexed = list(enumerate(columns))
    indexed.sort(
        key=lambda ic: (ic[1].ordinal_position is None, ic[1].ordinal_position or 0, ic[0])
    )
    return [c for _, c in indexed]


def _assignment_version(inputs: JobInputs) -> int:
    """The job's one resolved assignment version (``JobInputs.resolved_version``)."""
    version = inputs.resolved_version()
    if version < 1:
        raise ValueError(f"no assignment version for {inputs.database_name}/{inputs.job_id}")
    return version


def build_step1(inputs: JobInputs) -> StepContract:
    """Step 1's rules for the job; findings are added by the validator."""
    waves = sorted(inputs.report.get("migration_waves") or [], key=lambda w: int(w["wave"]))
    wave = _aurora_wave(waves) if waves else None
    source = (inputs.collector.get("metadata") or {}).get("source_database") or {}
    source_engine = str(source.get("engine") or "")
    wave_engines = (wave or {}).get("engines") or [""]
    engine = AURORA_FOR.get(source_engine, wave_engines[0])
    version = _assignment_version(inputs)
    shape = (wave or {}).get("homogeneity")
    wave_no = (wave or {}).get("wave")

    def contract(
        status: Literal[
            "valid", "has_blocked_rules", "no_relational_anchor", "staged_move_pending"
        ],
        reason: str | None = None,
        views: list[str] | None = None,
        rules: list[Rule] | None = None,
        unsupported: int = 0,
    ) -> StepContract:
        return StepContract(
            job_id=inputs.job_id,
            assignment_version=version,
            step=1,
            wave=wave_no,
            wave_shape=shape,
            target=TargetSpec(engine=engine or "unknown"),
            status=status,
            status_reason=reason,
            blocked_rule_count=sum(1 for r in rules or [] if r.status == "blocked"),
            views=views or [],
            rules=rules or [],
            unsupported_count=unsupported,
        )

    if shape == "cross_engine":
        return contract("staged_move_pending", "staged relational move pending (#1702)")
    if wave is None or shape != "homogeneous":
        return contract("no_relational_anchor", "no relational anchor")

    collector = _load_collector(inputs)
    tables = {t.table_id: t for t in collector.database_schema.tables}
    view_ids = {v.view_id for v in collector.database_schema.views or []}
    # The design base leaves out every table of a system schema of the source engine
    # (#2478): such a rule has no target, so it is blocked, and it never makes a user
    # table's name ambiguous. Of the others, it leaves out a table whose name another table
    # of its own schema has once case is folded. Two schemas' tables of one name, or of
    # names that differ only by case, are two targets, each in its own schema (#2494).
    user_tables = [
        t
        for t in collector.database_schema.tables
        if not is_system_schema(t.schema_name, source_engine)
    ]
    qualified = keeps_schemas(collector)
    names = Counter(t.table_name for t in user_tables)
    folded = Counter(table_key_of(t.table_name, t.schema_name) for t in user_tables)
    targets = target_tables(inputs, engine, collector)
    unapplied = unapplied_saved_tables(inputs, engine, collector)
    enums = enum_columns(collector, engine).types
    rules: list[Rule] = []
    views: list[str] = []
    status: RuleStatus
    # The same resolver the plan uses, so a wave entry the plan finds is found here too.
    schema = _Schema.from_collector(inputs.collector)
    wave_ids = {schema.resolve(str(n)) or str(n) for n in wave.get("tables") or []}
    for table_id in sorted(wave_ids):
        if table_id in view_ids:
            views.append(table_id)
            continue
        if table_id not in tables:
            # A wave table the collector lacks is not a view: a rule with no fields says so.
            rules.append(
                Rule(
                    input_schema="collector",
                    source_table=table_id,
                    target_table=table_id.split(".")[-1],
                    status="blocked",
                    findings=[
                        Finding(
                            code="unknown_table",
                            severity="error",
                            message=f"{table_id} is not in the collection",
                            table_id=table_id,
                        )
                    ],
                )
            )
            continue
        t = tables[table_id]
        if has_unprintable(table_id) or has_unprintable(t.table_name):
            # A name no SQL file may carry (#2381): the rule names the table cleaned
            # (``printable``; a lone surrogate is not even a valid string) and is blocked.
            rules.append(
                Rule(
                    input_schema="collector",
                    source_table=printable(table_id),
                    target_table=printable(t.table_name),
                    status="blocked",
                    findings=[
                        Finding(
                            code=UNSAFE_NAME,
                            severity="error",
                            message=UNSAFE_TABLE_REASON,
                            table_id=table_id,
                        )
                    ],
                )
            )
            continue
        why: tuple[str, str] | None = None
        if is_system_schema(t.schema_name, source_engine):
            why = (
                "system_schema",
                f"{table_id} is in the system schema {t.schema_name}, which the database "
                "creates and owns, so it has no target",
            )
        elif not qualified and names[t.table_name] > 1:
            # Bare targets of one schema: two tables of one name (one with no schema) would
            # share a target. Targets keyed by schema never do (#2494).
            why = (
                "ambiguous_target",
                f"{t.table_name} is the name of another source table and step 1 targets both in "
                "one schema, so its target is ambiguous",
            )
        elif folded[table_key_of(t.table_name, t.schema_name)] > 1:
            why = (
                "colliding_table_name",
                f"{t.table_name} matches the name of another table in schema "
                f"{t.schema_name} once case and quote marks are ignored, so the design "
                "leaves both out and the rule has no target",
            )
        if why is not None:
            finding = Finding(code=why[0], severity="error", message=why[1], table_id=table_id)
            fields: list[FieldRule] = []
            findings, status = [finding], "blocked"
        else:
            target = targets.get(target_key(t.table_name, t.schema_name, qualified))
            by_name = {c.name: c for c in (target.columns if target else ())}
            fields = [
                _field(table_id, c, by_name[c.column_name], source_engine, engine,
                       enums.get((table_id, c.column_name)))  # fmt: skip
                for c in _ordered(list(t.columns))
                if c.column_name in by_name
            ]
            findings, status = [], "valid"
            if t.table_name in unapplied:
                findings = [saved_types_finding(table_id, t.table_name)]
        rules.append(
            Rule(
                input_schema="collector",
                source_table=table_id,
                target_table=t.table_name,
                primary_key=list(t.primary_key or []),
                key_columns=list(key.columns) if (key := table_key(t.model_dump())) else [],
                fields=fields,
                status=status,
                findings=findings,
            )
        )
    unsupported = sum(1 for r in rules for f in r.fields if isinstance(f.op, UnsupportedOp))
    blocked = any(r.status == "blocked" for r in rules)
    return contract(
        "has_blocked_rules" if blocked else "valid",
        views=views,
        rules=rules,
        unsupported=unsupported,
    )
