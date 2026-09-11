"""Tests for the shared deterministic draft builder (ADR-028).

build_pg_draft is the single source of truth used by both the automated
Bedrock agent and the external/interactive seam, so the script-first draft
is identical in both paths.
"""

from __future__ import annotations

from src.contracts.schema_design_input import AgentColumn, AgentTable, NormalizedDataType
from src.tools.schema.aurora_common.draft_builder import build_pg_draft


def _users_table() -> AgentTable:
    return AgentTable(
        table_id="t1",
        table_name="users",
        row_count=5,
        primary_key=["id"],
        columns=[
            AgentColumn(
                column_name="id",
                normalized_data_type=NormalizedDataType.integer,
                nullable=False,
                is_auto_increment=True,
            )
        ],
    )


def test_translate_strategy_for_heterogeneous_source():
    table = _users_table()
    draft, strategy = build_pg_draft([table], "oracle")

    assert strategy == "translate"
    assert draft["migration_strategy"] == "translate"
    assert draft["tables"][0]["columns"][0]["aurora_type"] == "BIGINT"


def test_carry_over_strategy_for_homogeneous_source():
    table = _users_table()
    draft, strategy = build_pg_draft([table], "postgresql")

    assert strategy == "carry_over"
    assert draft["migration_strategy"] == "carry_over"
