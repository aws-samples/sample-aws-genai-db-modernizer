# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The source's character sets and collations in step 1's contract (#2276).

On Aurora MySQL, from a MySQL or MariaDB source, the contract records the source
database's character set and collation (``target.collation``) and each table's and
column's where it differs from its database's (its table's) on the source. The DDL and
the routine scripts read only these fields (``src/tools/data_movement/collations.py``),
so both give a database the same clause. With no database collation recorded, or one the
target does not offer (``target.collation_note`` says why), nothing is carried and every
script keeps the server defaults; a table or column whose own collation is not offered
gets a ``collation_not_carried`` warning and its database's (its table's) instead.
"""

from __future__ import annotations

from src.contracts.collector_output import CollectorOutputContract, Table
from src.contracts.lineage_ops import UnsupportedOp
from src.contracts.transformation_contract import (
    CharacterSet,
    FieldRule,
    Finding,
    Rule,
    TargetDatabase,
    TargetSpec,
)
from src.shared.table_keys import is_system_schema, spans_schemas
from src.tools.data_movement.collations import NOT_CARRIED, carry, database_target, is_text_type

_SOURCES = ("mysql", "mariadb")


def database_collation(
    collector: CollectorOutputContract, engine: str, line: str | None
) -> TargetSpec:
    """The step's target: ``engine`` with the source database's collation, or why not. A
    collection of several databases that lists each one's (``SourceDatabase.schemas``,
    #2531) gives each its own, in ``databases``; a database it does not list gets the
    source database's."""
    source = collector.metadata.source_database
    if engine != "aurora_mysql" or source.engine.value not in _SOURCES:
        return TargetSpec(engine=engine)
    value, note = carry(source.default_collation, source.default_character_set, line,
                        "the database", "the cluster's server default")  # fmt: skip
    databases = _databases(collector, line, value, note)
    carried = value is not None or any(d.collation is not None for d in databases or [])
    return TargetSpec(engine=engine, collation=value, collation_note=note,
                      engine_line=line if carried else None, databases=databases)  # fmt: skip


def _databases(
    collector: CollectorOutputContract,
    line: str | None,
    value: CharacterSet | None,
    note: str | None,
) -> list[TargetDatabase] | None:
    """Each database of a collection of several, with its own collation (#2531); None for a
    collection of one, or one that lists no database's."""
    source = collector.metadata.source_database
    names = {t.schema_name for t in collector.database_schema.tables}
    if not source.schemas or not spans_schemas(names, source.engine.value):
        return None
    listed = {s.schema_name: s for s in source.schemas}
    out = []
    for name in sorted(n for n in names if n and not is_system_schema(n, source.engine.value)):
        own = listed.get(name)
        if own is not None and own.default_collation:
            v, n = carry(own.default_collation, own.default_character_set, line,
                         "the database", "the cluster's server default")  # fmt: skip
        else:
            v, n = value, note
        out.append(TargetDatabase(name=name, collation=v, collation_note=n))
    return out


def _same(value: CharacterSet, name: str) -> bool:
    return value.collation == name.lower()


def _field(
    f: FieldRule, table: Table, rule: Rule, own: CharacterSet, line: str | None
) -> tuple[FieldRule, Finding | None]:
    column = next((c for c in table.columns if c.column_name == f.source_column), None)
    if (
        column is None
        or not column.collation
        or _same(own, column.collation)
        or isinstance(f.op, UnsupportedOp)
        or not is_text_type(f.target_type)
    ):
        return f.model_copy(update={"collation": None}), None
    value, why = carry(column.collation, column.character_set, line, "the column",
                       f"its table's {own.collation}")  # fmt: skip
    finding = None
    if why:
        finding = Finding(code=NOT_CARRIED, severity="warning", message=why,
                          table_id=rule.source_table, column=f.source_column)  # fmt: skip
    return f.model_copy(update={"collation": value}), finding


def rule_collations(
    rule: Rule, target: TargetSpec, tables: dict[str, Table], line: str | None
) -> Rule:
    """``rule`` with its table's and columns' collations where they differ on the source,
    and a warning for each the target does not offer; none when the database's is not
    carried. Earlier ``collation_not_carried`` findings are replaced, so it runs again
    unchanged."""
    findings = [f for f in rule.findings if f.code != NOT_CARRIED]
    table = tables.get(rule.source_table)
    # #2531: the table's own database's, on a step of several databases.
    database = database_target(target, table.schema_name if table else None)[0]
    if database is None or table is None or rule.status == "blocked":
        fields = [f.model_copy(update={"collation": None}) for f in rule.fields]
        return rule.model_copy(update={"collation": None, "fields": fields, "findings": findings})
    own, value = database, None
    if table.collation and not _same(database, table.collation):
        value, why = carry(table.collation, None, line, "the table",
                           f"its database's {database.collation}")  # fmt: skip
        if why:
            findings.append(Finding(code=NOT_CARRIED, severity="warning", message=why,
                                    table_id=rule.source_table))  # fmt: skip
        own = value or database
    fields = []
    for f in rule.fields:
        field, finding = _field(f, table, rule, own, line)
        fields.append(field)
        if finding:
            findings.append(finding)
    return rule.model_copy(update={"collation": value, "fields": fields, "findings": findings})
