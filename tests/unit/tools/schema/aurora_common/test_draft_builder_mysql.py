"""Tests for the shared deterministic draft builder — Aurora MySQL (ADR-028).

build_mysql_draft shares assembly with build_pg_draft so the script-first
draft is identical in shape across both Aurora engines.
"""

from __future__ import annotations

from src.contracts.schema_design_input import (
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentTable,
    ForeignKeyAction,
)
from src.contracts.schema_design_input import NormalizedDataType as N
from src.tools.schema.aurora_common.draft_builder import build_mysql_draft


def _orders():
    return AgentTable(
        table_id="t1",
        table_name="orders",
        row_count=10,
        primary_key=["id"],
        columns=[
            AgentColumn(
                column_name="id",
                normalized_data_type=N.integer,
                nullable=False,
                is_auto_increment=True,
            ),
            AgentColumn(column_name="user_id", normalized_data_type=N.integer, nullable=False),
        ],
        indexes=[AgentIndex(index_name="ix_orders_user", columns=["user_id"], is_unique=False)],
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


def test_mysql_draft_carry_over_for_mysql_source():
    draft, strategy = build_mysql_draft([_orders()], "mysql")
    assert strategy == "carry_over"
    assert draft["migration_strategy"] == "carry_over"
    assert draft["source_family"] == "mysql"


def test_mysql_draft_translate_for_other_source():
    _, strategy = build_mysql_draft([_orders()], "oracle")
    assert strategy == "translate"


def test_mysql_draft_uses_mysql_ddl_and_carries_structure():
    draft, _ = build_mysql_draft([_orders()], "mysql")
    t = draft["tables"][0]
    assert t["table_name"] == "orders"
    assert t["primary_key"] == ["id"]
    assert t["columns"][0]["aurora_type"] == "BIGINT"
    assert any("`ix_orders_user`" in s for s in t["indexes"])  # backticked MySQL index
    assert any("`fk_orders_user`" in s for s in t["foreign_keys"])  # backticked MySQL FK
    assert "CREATE TABLE `orders`" in draft["full_ddl"]  # MySQL backticks in full DDL
