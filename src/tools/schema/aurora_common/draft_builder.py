"""Single source of truth for building the deterministic Aurora PostgreSQL draft.

Used by both the automated agent (aurora_postgresql_schema_agent) and the
external/interactive seam (scripts/run_schema_design.py) so the script-first
draft is identical in both paths (ADR-028).
"""

from __future__ import annotations

from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.ddl_generator import generate_pg_ddl
from src.tools.schema.aurora_common.source_family import classify_source_family, migration_strategy


def build_pg_draft(tables: list[AgentTable], source_engine: str) -> tuple[dict, str]:
    """Return (draft dict, migration_strategy) from normalized source tables."""
    strategy = migration_strategy(source_engine, "aurora_postgresql")
    ddl = generate_pg_ddl(tables)
    draft = {
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
    }
    return draft, strategy
