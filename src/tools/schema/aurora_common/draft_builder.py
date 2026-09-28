"""Single source of truth for building the deterministic Aurora draft.

Shared assembly for both Aurora PostgreSQL and Aurora MySQL drafts, used by
both the automated agent (aurora_postgresql_schema_agent /
aurora_mysql_schema_agent) and the external/interactive seam
(scripts/run_schema_design.py) so the script-first draft is identical in
both paths (ADR-028).
"""

from __future__ import annotations

from src.contracts.schema_design_input import AgentQueryPattern, AgentTable
from src.tools.schema.aurora_common.access_patterns import derive_access_patterns
from src.tools.schema.aurora_common.ddl_generator import (
    DdlResult,
    generate_mysql_ddl,
    generate_pg_ddl,
)
from src.tools.schema.aurora_common.source_family import classify_source_family, migration_strategy


def _assemble_draft(
    tables: list[AgentTable],
    ddl: DdlResult,
    strategy: str,
    source_engine: str,
    queries: list[AgentQueryPattern],
) -> dict:
    derived = derive_access_patterns(queries, tables)
    return {
        "migration_strategy": strategy,
        "source_family": classify_source_family(source_engine),
        "tables": [
            {
                "table_name": t.table_name,
                "primary_key": src.primary_key or [],
                "indexes": t.index_sql,
                "foreign_keys": t.fk_sql,
                "columns": [
                    {
                        "name": c.name,
                        "aurora_type": c.aurora_type,
                        "source_type": c.source_type,
                        "script_derived": c.script_derived,
                        "needs_judgment": c.needs_judgment,
                        "judgment_reason": c.judgment_reason,
                    }
                    for c in t.columns
                ],
            }
            for src, t in zip(tables, ddl.tables, strict=True)
        ],
        "full_ddl": ddl.full_ddl,
        "residuals": ddl.residuals,
        # Kept separate from ``residuals``, which is column-typed and paired with
        # the prompt's "the draft's column types are authoritative" instruction.
        # These are pattern-shaped and carry a different question — which index
        # serves the pattern — so merging them would aim a column-type
        # instruction at dicts that have no column.
        "access_patterns": derived.patterns,
        "access_pattern_residuals": derived.residuals,
    }


def build_pg_draft(
    tables: list[AgentTable],
    source_engine: str,
    queries: list[AgentQueryPattern],
) -> tuple[dict, str]:
    """Return (draft dict, migration_strategy) for Aurora PostgreSQL.

    ``queries`` is required rather than defaulted on purpose. ADR-028 requires
    the automated agent and the external seam to produce an identical draft; a
    default would let one caller silently omit access patterns and the drift
    would be invisible. Required makes it a type error instead.
    """
    strategy = migration_strategy(source_engine, "aurora_postgresql")
    ddl = generate_pg_ddl(tables)
    return _assemble_draft(tables, ddl, strategy, source_engine, queries), strategy


def build_mysql_draft(
    tables: list[AgentTable],
    source_engine: str,
    queries: list[AgentQueryPattern],
) -> tuple[dict, str]:
    """Return (draft dict, migration_strategy) for Aurora MySQL.

    ``queries`` is required — see ``build_pg_draft``.
    """
    strategy = migration_strategy(source_engine, "aurora_mysql")
    ddl = generate_mysql_ddl(tables)
    return _assemble_draft(tables, ddl, strategy, source_engine, queries), strategy
