"""Generate Aurora PostgreSQL DDL from the normalized collector schema.

Pure, deterministic, and LLM-free. Every column the type map cannot resolve
confidently becomes a residual marker (surfaced to the LLM), never a silent
guess. Foreign keys and secondary indexes are emitted after all CREATE TABLE
statements so table ordering never breaks a reference.

Tables are emitted into a single flat namespace; `AgentTable.schema_name` is
intentionally not used, so source tables that share a name across schemas
would collide (out of scope for Phase 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.contracts.schema_design_input import AgentColumn, AgentTable
from src.tools.schema.aurora_common.constraint_translator import (
    default_clause,
    fk_on_delete_clause,
    identity_clause,
    not_null_clause,
)
from src.tools.schema.aurora_common.type_map import resolve_pg_type


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


def _q(identifier: str) -> str:
    """Double-quote a PostgreSQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _column_ddl(table_name: str, col: AgentColumn, residuals: list[dict]) -> ColumnDDL:
    resolution = resolve_pg_type(col.normalized_data_type, max_length=col.max_length)
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
    identity = identity_clause(col.is_auto_increment)
    # Identity and default are mutually exclusive in PostgreSQL — a column
    # cannot be both GENERATED ... AS IDENTITY and carry a DEFAULT clause.
    default = "" if identity else default_clause(col.default_value)
    fragment = (
        f"{_q(col.column_name)} {resolution.aurora_type}"
        f"{identity}"
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


def _create_table_sql(table: AgentTable, columns: list[ColumnDDL]) -> str:
    lines = [f"  {c.fragment}" for c in columns]
    if table.primary_key:
        pk_cols = ", ".join(_q(c) for c in table.primary_key)
        lines.append(f"  PRIMARY KEY ({pk_cols})")
    body = ",\n".join(lines)
    return f"CREATE TABLE {_q(table.table_name)} (\n{body}\n);"


def _index_sql(table: AgentTable) -> list[str]:
    statements: list[str] = []
    for idx in table.indexes or []:
        if idx.is_primary:
            continue  # covered by PRIMARY KEY
        unique = "UNIQUE " if idx.is_unique else ""
        cols = ", ".join(_q(c) for c in idx.columns)
        statements.append(
            f"CREATE {unique}INDEX {_q(idx.index_name)} ON {_q(table.table_name)} ({cols});"
        )
    return statements


def _fk_sql(table: AgentTable) -> list[str]:
    statements: list[str] = []
    for fk in table.foreign_keys or []:
        local = ", ".join(_q(c) for c in fk.columns)
        ref = ", ".join(_q(c) for c in fk.referenced_columns)
        statements.append(
            f"ALTER TABLE {_q(table.table_name)} ADD CONSTRAINT {_q(fk.constraint_name)} "
            f"FOREIGN KEY ({local}) REFERENCES {_q(fk.referenced_table)} ({ref})"
            f"{fk_on_delete_clause(fk.on_delete)};"
        )
    return statements


def generate_pg_ddl(tables: list[AgentTable]) -> DdlResult:
    """Translate normalized source tables into Aurora PostgreSQL DDL."""
    residuals: list[dict] = []
    table_ddls: list[TableDDL] = []

    for table in tables:
        columns = [_column_ddl(table.table_name, c, residuals) for c in table.columns]
        table_ddls.append(
            TableDDL(
                table_name=table.table_name,
                columns=columns,
                create_sql=_create_table_sql(table, columns),
                index_sql=_index_sql(table),
                fk_sql=_fk_sql(table),
            )
        )

    # Assemble: all CREATE TABLEs, then all indexes, then all FKs.
    parts: list[str] = [t.create_sql for t in table_ddls]
    for t in table_ddls:
        parts.extend(t.index_sql)
    for t in table_ddls:
        parts.extend(t.fk_sql)

    return DdlResult(tables=table_ddls, full_ddl="\n\n".join(parts), residuals=residuals)
