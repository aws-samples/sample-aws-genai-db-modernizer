"""Generate Aurora PostgreSQL and MySQL DDL from the normalized collector schema.

Pure, deterministic, and LLM-free. Every column the type map cannot resolve
confidently becomes a residual marker (surfaced to the LLM), never a silent
guess. Foreign keys and secondary indexes are emitted after all CREATE TABLE
statements so table ordering never breaks a reference.

Tables are emitted into a single flat namespace; `AgentTable.schema_name` is
intentionally not used, so source tables that share a name across schemas
would collide (out of scope for Phase 1).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from src.contracts.schema_design_input import AgentColumn, AgentTable
from src.tools.schema.aurora_common.constraint_translator import (
    DefaultResolution,
    fk_on_delete_clause,
    identity_clause,
    mysql_auto_increment_clause,
    mysql_escape_literal,
    not_null_clause,
    pg_escape_literal,
    resolve_default,
)
from src.tools.schema.aurora_common.type_map import (
    TypeResolution,
    resolve_mysql_type,
    resolve_pg_type,
)


@dataclass(frozen=True)
class Dialect:
    """Per-engine DDL parametrization: identifier quoting, auto-increment clause, type resolver."""

    name: str
    quote_char: str
    auto_increment: Callable[[bool | None], str]
    resolve_type: Callable[..., TypeResolution]
    escape_literal: Callable[[str], str]
    # Source expressions whose target equivalent differs per engine. The
    # portable ones live in constraint_translator.
    default_translations: Mapping[str, str] = field(default_factory=dict)

    def q(self, identifier: str) -> str:
        c = self.quote_char
        return c + identifier.replace(c, c + c) + c

    def resolve_default(self, default_value: str | int | float | bool | None) -> DefaultResolution:
        """Resolve a DEFAULT clause using this engine's escaping and vocabulary."""
        return resolve_default(
            default_value,
            escape_literal=self.escape_literal,
            translations=dict(self.default_translations),
        )


POSTGRES = Dialect(
    name="aurora_postgresql",
    quote_char='"',
    auto_increment=identity_clause,
    resolve_type=resolve_pg_type,
    escape_literal=pg_escape_literal,
    default_translations={
        # SQL Server / MySQL UUID generators. pgcrypto's gen_random_uuid is
        # core since PostgreSQL 13, so it is safe on every Aurora PG version.
        "NEWID()": "gen_random_uuid()",
        "UUID()": "gen_random_uuid()",
    },
)
MYSQL = Dialect(
    name="aurora_mysql",
    quote_char="`",
    auto_increment=mysql_auto_increment_clause,
    resolve_type=resolve_mysql_type,
    escape_literal=mysql_escape_literal,
    default_translations={
        "NEWID()": "UUID()",
        "GEN_RANDOM_UUID()": "UUID()",
    },
)


@dataclass
class ColumnDDL:
    name: str
    aurora_type: str
    source_type: str | None
    script_derived: bool
    needs_judgment: bool
    judgment_reason: str
    fragment: str  # e.g. '"email" VARCHAR(255) NOT NULL'


@dataclass
class TableDDL:
    table_name: str
    columns: list[ColumnDDL]
    create_sql: str
    index_sql: list[str] = field(default_factory=list)
    fk_sql: list[str] = field(default_factory=list)


@dataclass
class DdlResult:
    tables: list[TableDDL]
    full_ddl: str
    residuals: list[dict]


def _column_ddl(
    table_name: str, col: AgentColumn, residuals: list[dict], dialect: Dialect
) -> ColumnDDL:
    resolution = dialect.resolve_type(
        col.normalized_data_type,
        max_length=col.max_length,
        numeric_precision=col.numeric_precision,
        numeric_scale=col.numeric_scale,
    )
    source_type = col.normalized_data_type.value if col.normalized_data_type else None
    if resolution.needs_judgment:
        residuals.append(
            {
                "table": table_name,
                "column": col.column_name,
                "source_type": source_type,
                "fallback_type": resolution.aurora_type,
                "reason": resolution.reason,
            }
        )
    auto_increment = dialect.auto_increment(
        col.is_auto_increment
    )  # nosemgrep: is-function-without-parentheses -- property, not a method
    if auto_increment:
        # Identity/auto-increment and DEFAULT are mutually exclusive.
        default = ""
    else:
        resolved_default = dialect.resolve_default(col.default_value)
        default = resolved_default.clause
        if resolved_default.needs_judgment:
            residuals.append(
                {
                    "table": table_name,
                    "column": col.column_name,
                    "source_type": source_type,
                    "fallback_type": resolution.aurora_type,
                    "source_default": resolved_default.source_expression,
                    "reason": resolved_default.reason,
                }
            )
    fragment = (
        f"{dialect.q(col.column_name)} {resolution.aurora_type}"
        f"{auto_increment}"
        f"{not_null_clause(col.nullable)}"
        f"{default}"
    )
    return ColumnDDL(
        name=col.column_name,
        aurora_type=resolution.aurora_type,
        source_type=source_type,
        script_derived=not resolution.needs_judgment,
        needs_judgment=resolution.needs_judgment,
        judgment_reason=resolution.reason,
        fragment=fragment,
    )


def _create_table_sql(table: AgentTable, columns: list[ColumnDDL], dialect: Dialect) -> str:
    lines = [f"  {c.fragment}" for c in columns]
    if table.primary_key:
        pk_cols = ", ".join(dialect.q(c) for c in table.primary_key)
        lines.append(f"  PRIMARY KEY ({pk_cols})")
    body = ",\n".join(lines)
    return f"CREATE TABLE {dialect.q(table.table_name)} (\n{body}\n);"


def _index_sql(table: AgentTable, dialect: Dialect) -> list[str]:
    statements: list[str] = []
    for idx in table.indexes or []:
        if idx.is_primary:  # nosemgrep: is-function-without-parentheses -- property, not a method
            continue  # covered by PRIMARY KEY
        unique = (
            "UNIQUE " if idx.is_unique else ""
        )  # nosemgrep: is-function-without-parentheses -- property, not a method
        cols = ", ".join(dialect.q(c) for c in idx.columns)
        statements.append(
            f"CREATE {unique}INDEX {dialect.q(idx.index_name)} "
            f"ON {dialect.q(table.table_name)} ({cols});"
        )
    return statements


def _fk_sql(table: AgentTable, dialect: Dialect) -> list[str]:
    statements: list[str] = []
    for fk in table.foreign_keys or []:
        local = ", ".join(dialect.q(c) for c in fk.columns)
        ref = ", ".join(dialect.q(c) for c in fk.referenced_columns)
        statements.append(
            f"ALTER TABLE {dialect.q(table.table_name)} "
            f"ADD CONSTRAINT {dialect.q(fk.constraint_name)} "
            f"FOREIGN KEY ({local}) REFERENCES {dialect.q(fk.referenced_table)} ({ref})"
            f"{fk_on_delete_clause(fk.on_delete)};"
        )
    return statements


def _generate(tables: list[AgentTable], dialect: Dialect) -> DdlResult:
    residuals: list[dict] = []
    table_ddls: list[TableDDL] = []

    for table in tables:
        columns = [_column_ddl(table.table_name, c, residuals, dialect) for c in table.columns]
        table_ddls.append(
            TableDDL(
                table_name=table.table_name,
                columns=columns,
                create_sql=_create_table_sql(table, columns, dialect),
                index_sql=_index_sql(table, dialect),
                fk_sql=_fk_sql(table, dialect),
            )
        )

    # Assemble: all CREATE TABLEs, then all indexes, then all FKs.
    parts: list[str] = [t.create_sql for t in table_ddls]
    for t in table_ddls:
        parts.extend(t.index_sql)
    for t in table_ddls:
        parts.extend(t.fk_sql)

    return DdlResult(tables=table_ddls, full_ddl="\n\n".join(parts), residuals=residuals)


def generate_pg_ddl(tables: list[AgentTable]) -> DdlResult:
    """Translate normalized source tables into Aurora PostgreSQL DDL."""
    return _generate(tables, POSTGRES)


def generate_mysql_ddl(tables: list[AgentTable]) -> DdlResult:
    """Translate normalized source tables into Aurora MySQL DDL."""
    return _generate(tables, MYSQL)
