# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An expression index survives the whole way from a raw row to the draft (#295).

The per-step tests in ``test_postgres_expression_indexes.py`` stop at
``collect_indexes`` / ``parse_offline_collection``, which is exactly where the
first round of this fix stopped: the ``Index(...)`` builders in between never
forwarded the new fields, so the flag was dropped again before the contract,
and an expression-only index failed collection outright. These tests start
from raw rows and end at the generated DDL, on both the live and the offline
path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from src.agents.collector import postgres_collector
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract, Table
from src.contracts.schema_design_input import AgentTable
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.schema.aurora_common.ddl_generator import DdlResult, generate_pg_ddl

# A real pg_get_indexdef runs past excerpt's 80-character cut, which is why the
# expressions are collected on their own.
MIXED_DEF = (
    "CREATE UNIQUE INDEX u_tenant_name ON public.users "
    "USING btree (tenant_id, lower((display_name)::text))"
)
MIXED_EXPRS = "lower((display_name)::text)"


def _live_rows() -> list[dict[str, Any]]:
    """What the live query returns for ``(tenant_id, lower(display_name))``.

    Two rows: the plain key joins a ``pg_attribute`` row, the expression key
    has ``attnum`` 0 and joins nothing, so its column name is NULL.
    """
    base = {
        "index_name": "u_tenant_name",
        "is_unique": True,
        "is_primary": False,
        "index_type": "btree",
        "predicate": None,
        "has_expressions": True,
        "index_expressions": MIXED_EXPRS,
        "index_def": MIXED_DEF,
    }
    return [{**base, "column_name": "tenant_id"}, {**base, "column_name": None}]


def _offline_rows() -> list[dict[str, Any]]:
    """The same index as the offline collection script dumps it."""
    base = {
        "table_name": "users",
        "index_name": "u_tenant_name",
        "seq_in_index": None,
        "non_unique": 0,
        "index_type": "btree",
        "has_expressions": True,
        "index_expressions": MIXED_EXPRS,
        "index_def": MIXED_DEF,
    }
    return [{**base, "column_name": "tenant_id"}, {**base, "column_name": None}]


def _raw_table(indexes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "table_name": "users",
        "schema_name": "public",
        "row_count": 10,
        "columns": [
            {"column_name": "tenant_id", "data_type": "integer", "is_nullable": "NO"},
            {"column_name": "display_name", "data_type": "text", "is_nullable": "YES"},
        ],
        "indexes": indexes,
        "primary_key": ["tenant_id"],
    }


def _collect_live(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = rows
    collector = PostgreSQLRemoteCollector(
        ssm=ssm,
        host="pg-test.example.com",
        port=5432,
        database="testdb",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:t-AAAAAA",  # pragma: allowlist secret  # nosec B106 — test fixture ARN, not a credential
        region="us-east-1",
    )
    return collector.collect_indexes("users")


def _live_table(index_rows: list[dict[str, Any]]) -> Table:
    [table] = postgres_collector._build_tables({"tables": [_raw_table(_collect_live(index_rows))]})
    return table


def _offline_table(index_rows: list[dict[str, Any]]) -> Table:
    parsed = parse_offline_collection(
        {
            "metadata": {"database_name": "db", "engine": "postgresql"},
            "tables": [{"table_name": "users", "row_count": 10}],
            "columns": [
                {
                    "table_name": "users",
                    "column_name": name,
                    "column_type": data_type,
                    "data_type": data_type,
                    "is_nullable": "YES",
                }
                for name, data_type in (("tenant_id", "integer"), ("display_name", "text"))
            ],
            "indexes": index_rows,
            "primary_keys": [],
        }
    )
    contract = CollectorOutputContract.model_validate(
        build_offline_collector_output(
            parsed, job_id="job-295", database_name="db", s3_key="offline.json"
        )
    )
    [table] = contract.database_schema.tables
    return table


# ---------------------------------------------------------------------------
# Raw row -> Index contract, on both paths
# ---------------------------------------------------------------------------


def test_the_live_builder_puts_the_expression_fields_on_the_contract():
    [idx] = _live_table(_live_rows()).indexes

    assert idx.columns == ["tenant_id"]
    assert idx.is_unique is True
    assert idx.has_expressions is True
    assert idx.index_expressions == MIXED_EXPRS
    assert idx.index_def == MIXED_DEF


def test_the_offline_builder_puts_the_expression_fields_on_the_contract():
    [idx] = _offline_table(_offline_rows()).indexes

    assert idx.columns == ["tenant_id"]
    assert idx.has_expressions is True
    assert idx.index_expressions == MIXED_EXPRS


def test_an_expression_only_index_does_not_fail_collection_on_either_path():
    live_only = [r for r in _live_rows() if r["column_name"] is None]
    offline_only = [r for r in _offline_rows() if r["column_name"] is None]

    for table in (_live_table(live_only), _offline_table(offline_only)):
        [idx] = table.indexes
        assert idx.columns == []
        assert idx.has_expressions is True


def test_a_plain_index_carries_no_index_text_onto_the_contract():
    plain = [
        {
            "index_name": "idx_users_tenant",
            "column_name": "tenant_id",
            "is_unique": False,
            "is_primary": False,
            "index_type": "btree",
            "predicate": None,
            # pg_get_indexdef is never null, so the collector always has text
            # here; only an expression index has any use for it, and every
            # other one would just pad each designer's prompt.
            "has_expressions": False,
            "index_expressions": None,
            "index_def": "CREATE INDEX idx_users_tenant ON public.users USING btree (tenant_id)",
        }
    ]

    [idx] = _live_table(plain).indexes

    assert idx.has_expressions is False
    assert idx.index_def is None
    assert idx.index_expressions is None


# ---------------------------------------------------------------------------
# ... and on to the draft
# ---------------------------------------------------------------------------


def _draft(table: Table) -> DdlResult:
    """The Aurora PostgreSQL draft for one collected table."""
    agent_table = AgentTable.model_validate(
        {
            "table_id": table.table_id,
            "table_name": table.table_name,
            "row_count": table.row_count,
            "primary_key": table.primary_key,
            "columns": [
                {
                    "column_name": c.column_name,
                    "data_type": c.data_type,
                    "nullable": c.nullable,
                }
                for c in table.columns
            ],
            "indexes": [i.model_dump() for i in table.indexes or []],
        }
    )
    return generate_pg_ddl([agent_table], source_engine="postgresql")


def test_the_draft_notes_a_collected_expression_index_instead_of_narrowing_it():
    result = _draft(_live_table(_live_rows()))

    assert "u_tenant_name" not in result.full_ddl
    [note] = [n for n in result.index_notes if n["index"] == "u_tenant_name"]
    assert "expression keys" in note["reason"]
    # The expressions themselves, not a CREATE statement cut off before them.
    assert "lower((display_name)::text)" in note["reason"]
