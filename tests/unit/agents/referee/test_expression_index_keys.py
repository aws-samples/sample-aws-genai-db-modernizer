# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An expression index is no key evidence for routing (#2381).

Its plain ``columns`` are only part of its keys, in an order the collection does not
record, and its expression keys are named by no query's columns: a unique index on
``(tenant_id, lower(name))`` does not make ``tenant_id`` unique, and an index on
``(lower(email), created_at)`` is no index on ``created_at``. Routing reads such a table
as it reads the same table without the index.
"""

from __future__ import annotations

import pytest

from src.agents.referee.access_pattern import KEY_LOOKUP, SchemaKeys, classify_query


def _collector(index: dict | None) -> dict:
    columns = ["id", "tenant_id", "name", "email", "created_at"]
    return {
        "database_schema": {
            "tables": [
                {
                    "table_id": "app.accounts",
                    "table_name": "accounts",
                    "schema_name": "app",
                    "primary_key": ["id"],
                    "row_count": 100_000,
                    "columns": [
                        {"column_name": c, "data_type": "integer", "cardinality": 90_000}
                        for c in columns
                    ],
                    "indexes": [index] if index else [],
                }
            ]
        }
    }


def _class(sql: str, index: dict | None) -> str:
    keys = SchemaKeys.from_collector(_collector(index))
    query = {"query_id": "q", "query_text": sql, "query_type": "SELECT"}
    return classify_query(query, keys, "postgresql").name


MIXED_UNIQUE = {
    "index_name": "accounts_tenant_lower_name",
    "columns": ["tenant_id"],
    "is_unique": True,
    "index_type": "btree",
    "has_expressions": True,
    "index_expressions": "lower((name)::text)",
}
MIXED_LEADING_EXPRESSION = {
    "index_name": "accounts_lower_email_created",
    "columns": ["created_at"],
    "is_unique": False,
    "index_type": "btree",
    "has_expressions": True,
    "index_expressions": "lower((email)::text)",
}


def test_a_unique_mixed_expression_index_does_not_make_its_column_a_unique_key():
    sql = "SELECT * FROM accounts WHERE tenant_id = $1"

    assert _class(sql, MIXED_UNIQUE) != KEY_LOOKUP
    assert _class(sql, MIXED_UNIQUE) == _class(sql, None)


@pytest.mark.parametrize("index", [MIXED_UNIQUE, MIXED_LEADING_EXPRESSION])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM accounts WHERE tenant_id = $1",
        "SELECT * FROM accounts WHERE created_at = $1",
        "SELECT * FROM accounts WHERE lower(email) = $1",
    ],
)
def test_a_mixed_expression_index_reads_as_no_index(index, sql):
    assert _class(sql, index) == _class(sql, None)


@pytest.mark.passes_on_main  # the same index with no expression must stay key evidence
def test_the_same_index_without_expressions_is_still_a_unique_key():
    plain = {**MIXED_UNIQUE, "has_expressions": False, "index_expressions": None}

    assert _class("SELECT * FROM accounts WHERE tenant_id = $1", plain) == KEY_LOOKUP
