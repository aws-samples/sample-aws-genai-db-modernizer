# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL expression-index keys survive collection (#295).

``pg_index.indkey`` holds 0 for an expression key, and 0 is never an
``attnum``, so the old inner join on ``pg_attribute`` dropped those rows: an
index on ``lower(email)`` vanished, and a unique index on
``(tenant_id, lower(name))`` came back as ``(tenant_id)``.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from src.contracts.collector_output import Index
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector


@pytest.fixture
def collector() -> tuple[PostgreSQLRemoteCollector, MagicMock]:
    ssm = MagicMock()
    return (
        PostgreSQLRemoteCollector(
            ssm=ssm,
            host="pg-test.example.com",
            port=5432,
            database="testdb",
            secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA",  # pragma: allowlist secret  # nosec B106 — test fixture ARN/secret ref, not a real credential
            region="us-east-1",
        ),
        ssm,
    )


def _row(**over):
    row = {
        "index_name": "idx",
        "column_name": None,
        "is_unique": False,
        "is_primary": False,
        "index_type": "btree",
        "predicate": None,
        "has_expressions": False,
        "index_expressions": None,
        "index_def": None,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# The live collector
# ---------------------------------------------------------------------------


def test_the_index_query_left_joins_pg_attribute(collector):
    coll, ssm = collector
    ssm.run_sql_json.return_value = []

    coll.collect_indexes("users")

    sql = ssm.run_sql_json.call_args.kwargs["sql"]
    assert "LEFT JOIN pg_attribute" in sql
    assert "indexprs IS NOT NULL" in sql
    assert "pg_get_expr(ix.indexprs, ix.indrelid)" in sql
    assert "pg_get_indexdef" in sql


def test_expression_only_index_is_collected_with_no_columns(collector):
    coll, ssm = collector
    ssm.run_sql_json.return_value = [
        _row(
            index_name="idx_users_lower_email",
            has_expressions=True,
            index_def="CREATE INDEX idx_users_lower_email ON users (lower(email))",
        )
    ]

    [idx] = coll.collect_indexes("users")

    assert idx["index_name"] == "idx_users_lower_email"
    assert idx["columns"] == []
    assert idx["has_expressions"] is True
    assert "lower(email)" in idx["index_def"]


def test_mixed_unique_index_is_flagged_not_narrowed(collector):
    coll, ssm = collector
    ssm.run_sql_json.return_value = [
        _row(
            index_name="u_tenant_name",
            column_name="tenant_id",
            is_unique=True,
            has_expressions=True,
            index_def="CREATE UNIQUE INDEX u_tenant_name ON users (tenant_id, lower(name))",
        ),
        _row(
            index_name="u_tenant_name",
            column_name=None,
            is_unique=True,
            has_expressions=True,
        ),
    ]

    [idx] = coll.collect_indexes("users")

    assert idx["columns"] == ["tenant_id"]
    assert idx["is_unique"] is True
    # The flag is what stops a reader treating ("tenant_id",) as the whole key.
    assert idx["has_expressions"] is True


def test_a_plain_index_keeps_its_columns_and_is_not_flagged(collector):
    coll, ssm = collector
    # pg_get_indexdef is never null, so a plain index arrives with its own
    # CREATE text too. The columns already say everything it says, and every
    # index's definition would ride into each schema designer's prompt, so the
    # collector drops it unless there are expression keys.
    plain_def = "CREATE INDEX idx_users_email ON public.users USING btree (email, tenant_id)"
    ssm.run_sql_json.return_value = [
        _row(index_name="idx_users_email", column_name="email", index_def=plain_def),
        _row(index_name="idx_users_email", column_name="tenant_id", index_def=plain_def),
    ]

    [idx] = coll.collect_indexes("users")

    assert idx["columns"] == ["email", "tenant_id"]
    assert idx["has_expressions"] is False
    assert idx["index_def"] is None
    assert idx["index_expressions"] is None


# ---------------------------------------------------------------------------
# The offline (SQL-file) path
# ---------------------------------------------------------------------------


def test_offline_parser_keeps_an_expression_only_index():
    raw = {
        "metadata": {"database_name": "db"},
        "tables": [{"table_name": "users", "row_count": 1}],
        "columns": [
            {
                "table_name": "users",
                "column_name": "email",
                "column_type": "text",
                "data_type": "text",
                "is_nullable": "YES",
            }
        ],
        "indexes": [
            {
                "table_name": "users",
                "index_name": "idx_users_lower_email",
                "column_name": None,
                "seq_in_index": None,
                "non_unique": 1,
                "index_type": "btree",
                "has_expressions": True,
                "index_def": "CREATE INDEX idx_users_lower_email ON users (lower(email))",
            }
        ],
        "primary_keys": [],
    }

    [table] = parse_offline_collection(raw)["tables"]
    [idx] = table["indexes"]

    assert idx["index_name"] == "idx_users_lower_email"
    assert idx["columns"] == []
    assert idx["has_expressions"] is True


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_index_contract_accepts_an_expression_index_with_no_columns():
    idx = Index(
        index_name="idx_users_lower_email", columns=[], is_unique=False, has_expressions=True
    )

    assert idx.columns == []
    assert idx.has_expressions is True


def test_the_index_contract_still_rejects_an_index_with_no_keys_at_all():
    with pytest.raises(ValidationError, match="at least one column"):
        Index(index_name="idx_nothing", columns=[], is_unique=False)
