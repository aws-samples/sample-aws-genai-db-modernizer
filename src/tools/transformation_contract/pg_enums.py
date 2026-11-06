# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL enum types on a same-engine step 1 (#1834).

On a PostgreSQL to Aurora PostgreSQL step 1, a column whose type is one of the
collection's enum types (``database_schema.enum_types``, ``pg_enum``'s labels in sort
order) keeps it: its target type is ``"<schema>"."<type>"`` and ``step-1.ddl.sql`` creates
the type before the tables. The type's schema and name must be plain identifiers and each
label a printable string within PostgreSQL's 63-byte limit, or the column keeps the
generator's TEXT, with a reason. A collection with no enum list (read before #1834) keeps
TEXT for every column of a type that is not built in, with a reason too. Labels are
customer data: they reach the DDL only through :func:`create_type_sql`, as string
literals with each single quote doubled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.contracts.aurora_design_delta import IDENTIFIER
from src.contracts.collector_output import CollectorOutputContract
from src.tools.schema.aurora_common.constraint_translator import PG_TARGET
from src.tools.schema.aurora_common.source_types import resolve_source_type

# The finding codes a column that stays TEXT gets: no labels collected, or a refused type.
NOT_COLLECTED = "enum_labels_not_collected"
REFUSED = "enum_type_refused"
_MAX_LABEL_BYTES = 63  # PostgreSQL's NAMEDATALEN - 1


@dataclass(frozen=True)
class EnumType:
    schema: str
    name: str
    labels: tuple[str, ...]

    @property
    def sql_type(self) -> str:
        return f'"{self.schema}"."{self.name}"'


@dataclass(frozen=True)
class EnumColumns:
    # (table name, column) -> the enum the column keeps.
    types: dict[tuple[str, str], EnumType] = field(default_factory=dict)
    # (table id, column) -> (finding code, why the column stays TEXT).
    fallbacks: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)


def _refusal(schema: str, name: str, labels: list[str]) -> str | None:
    if not (IDENTIFIER.fullmatch(schema) and IDENTIFIER.fullmatch(name)):
        return "its enum type's schema or name is not a plain identifier"
    for label in labels:
        if not label.isprintable() or len(label.encode("utf-8")) > _MAX_LABEL_BYTES:
            return (
                f"a label of its enum type is not printable or longer than {_MAX_LABEL_BYTES} bytes"
            )
    if len(set(labels)) != len(labels):
        return "its enum type lists a label twice"
    return None


def enum_columns(collector: CollectorOutputContract, engine: str) -> EnumColumns:
    """The columns that keep their enum type, and the ones that stay TEXT and why: only on
    a PostgreSQL source moving into Aurora PostgreSQL, else nothing."""
    source = collector.metadata.source_database.engine.value
    if source != "postgresql" or engine != PG_TARGET:
        return EnumColumns()
    collected = collector.database_schema.enum_types
    by_name: dict[str, list] = {}
    for e in collected or []:
        by_name.setdefault(e.type_name, []).append(e)
    out = EnumColumns()
    for table in collector.database_schema.tables:
        for column in table.columns:
            where = (table.table_id, column.column_name)
            if collected is None:
                resolved = resolve_source_type(
                    column.data_type, source_family="postgresql", target=PG_TARGET,
                    max_length=column.max_length, numeric_precision=column.numeric_precision,
                    numeric_scale=column.numeric_scale, datetime_precision=column.datetime_precision,
                )  # fmt: skip
                if resolved is None:
                    out.fallbacks[where] = (NOT_COLLECTED, (
                        f"{column.column_name}'s type is not built in and the collection lists no enum "
                        "labels (collected before they were read): it stays TEXT. Collect again with "
                        "scripts/collect-postgresql.sql to keep it if it is an enum."))  # fmt: skip
                continue
            candidates = by_name.get(column.data_type) or []
            same = [e for e in candidates if e.schema_name == table.schema_name]
            match = same or candidates
            if not match:
                continue  # a built-in, domain or composite type: as before
            if len(match) > 1:
                out.fallbacks[where] = (REFUSED, f"{column.column_name}: its enum type exists in more "
                                        "than one schema, so which one it is is ambiguous: it stays TEXT.")  # fmt: skip
                continue
            enum = match[0]
            why = _refusal(enum.schema_name, enum.type_name, enum.labels)
            if why:
                out.fallbacks[where] = (REFUSED, f"{column.column_name}: {why}: it stays TEXT.")
                continue
            out.types[(table.table_name, column.column_name)] = EnumType(
                enum.schema_name, enum.type_name, tuple(enum.labels)
            )
    return out


def create_type_sql(enum: EnumType) -> str:
    """``CREATE TYPE "<schema>"."<type>" AS ENUM ('a', ...);``, labels in sort order, each a
    standard string literal (single quotes doubled; the DDL sets standard_conforming_strings
    on before it, so a backslash is a backslash)."""
    labels = ", ".join("'" + label.replace("'", "''") + "'" for label in enum.labels)
    return f"CREATE TYPE {enum.sql_type} AS ENUM ({labels});"
