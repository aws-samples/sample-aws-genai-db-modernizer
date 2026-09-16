"""Tests for the shared deterministic draft builder (ADR-028).

build_pg_draft is the single source of truth used by both the automated
Bedrock agent and the external/interactive seam, so the script-first draft
is identical in both paths.
"""

from __future__ import annotations

from src.contracts.schema_design_input import (
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentTable,
    ForeignKeyAction,
    NormalizedDataType,
)
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


def _orders_table_with_pk_index_and_fk() -> AgentTable:
    return AgentTable(
        table_id="t2",
        table_name="orders",
        row_count=10,
        primary_key=["id"],
        columns=[
            AgentColumn(
                column_name="id",
                normalized_data_type=NormalizedDataType.integer,
                nullable=False,
                is_auto_increment=True,
            ),
            AgentColumn(
                column_name="user_id",
                normalized_data_type=NormalizedDataType.integer,
                nullable=False,
            ),
        ],
        indexes=[AgentIndex(index_name="ix_orders_user_id", columns=["user_id"], is_unique=True)],
        foreign_keys=[
            AgentForeignKey(
                constraint_name="fk_orders_user",
                columns=["user_id"],
                referenced_table="users",
                referenced_columns=["id"],
                on_delete=ForeignKeyAction.CASCADE,
            )
        ],
    )


def test_draft_carries_primary_key_indexes_and_foreign_keys():
    table = _orders_table_with_pk_index_and_fk()
    draft, _ = build_pg_draft([table], "postgresql")

    table_draft = draft["tables"][0]
    assert table_draft["primary_key"] == ["id"]
    assert table_draft["indexes"] == [
        'CREATE UNIQUE INDEX "ix_orders_user_id" ON "orders" ("user_id");'
    ]
    assert table_draft["foreign_keys"] == [
        'ALTER TABLE "orders" ADD CONSTRAINT "fk_orders_user" '  # nosemgrep: string-concat-in-list -- intentional multi-line string
        'FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE;'
    ]
