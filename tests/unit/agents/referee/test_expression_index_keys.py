# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An expression index is no key evidence, for routing or any other reader (#2381).

Its plain ``columns`` are only part of its keys, in an order the collection does not
record, and its expression keys are named by no query's columns: a unique index on
``(tenant_id, lower(name))`` does not make ``tenant_id`` unique, and an index on
``(lower(email), created_at)`` is no index on ``created_at``. Routing reads such a table
as it reads the same table without the index, and so do the partition-load facts, the
DynamoDB relationship detection and the redesign's composite keys: all four read the one
rule, ``src.shared.unique_index_keys.unique_key_columns`` (ADR-031 Decision 14
Amendment 5).
"""

from __future__ import annotations

import pytest

from src.agents.referee.access_pattern import KEY_LOOKUP, SchemaKeys, classify_query
from src.agents.referee.redesign_findings import (
    ACCEPTED,
    COMPOSITE_TAIL,
    REFUSED,
    RedesignModel,
)
from src.shared.partition_load import key_distinct_values
from src.tools.analysis.dynamodb_analysis_tools import detect_relationships


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


def test_a_leading_expression_does_not_make_a_later_column_an_index_key():
    sql = "SELECT * FROM accounts WHERE created_at = $1"

    assert _class(sql, MIXED_LEADING_EXPRESSION) == _class(sql, None)


@pytest.mark.passes_on_main  # the same index with no expression must stay key evidence
def test_the_same_index_without_expressions_is_still_a_unique_key():
    plain = {**MIXED_UNIQUE, "has_expressions": False, "index_expressions": None}

    assert _class("SELECT * FROM accounts WHERE tenant_id = $1", plain) == KEY_LOOKUP


# ---------------------------------------------------------------------------
# The other readers of a unique index (#2381 review)
# ---------------------------------------------------------------------------


def _table(index: dict | None) -> dict:
    [table] = _collector(index)["database_schema"]["tables"]
    return dict(table)


def test_partition_load_gives_a_mixed_unique_index_no_unique_key_count():
    keyed = {tuple(k["columns"]): k for k in key_distinct_values(_table(MIXED_UNIQUE))}

    # tenant_id has its collected cardinality, not one distinct value per row.
    assert keyed[("tenant_id",)]["distinct_values"] == 90_000
    assert key_distinct_values(_table(MIXED_UNIQUE)) == key_distinct_values(_table(None))


@pytest.mark.passes_on_main  # the plain unique index must stay a unique key
def test_partition_load_still_counts_a_plain_unique_index_as_one_value_per_row():
    plain = {**MIXED_UNIQUE, "has_expressions": False, "index_expressions": None}
    keyed = {tuple(k["columns"]): k for k in key_distinct_values(_table(plain))}

    assert keyed[("tenant_id",)]["distinct_values"] == 100_000


def _relationship(index: dict | None) -> str:
    collector = _collector(index)
    collector["database_schema"]["tables"][0]["foreign_keys"] = [
        {"constraint_name": "fk_tenant", "columns": ["tenant_id"],
         "referenced_table": "tenants", "referenced_columns": ["id"]}
    ]  # fmt: skip
    [relationship] = detect_relationships(collector)
    return str(relationship["type"])


def test_a_foreign_key_from_a_mixed_unique_index_column_is_one_to_many():
    assert _relationship(MIXED_UNIQUE) == "1:M"


@pytest.mark.passes_on_main  # a plain single-column unique index still makes it 1:1
def test_a_foreign_key_from_a_plain_unique_column_is_one_to_one():
    plain = {**MIXED_UNIQUE, "has_expressions": False, "index_expressions": None}
    assert _relationship(plain) == "1:1"


def _redesign(index: dict) -> list[tuple[str, str, tuple[str, ...]]]:
    collector = _collector(index)
    collector["metadata"] = {"source_database": {"engine": "postgresql"}}
    query = {"query_id": "q", "query_text": "SELECT * FROM accounts WHERE name = $1",
             "query_type": "SELECT", "calls_per_second": 5.0,
             "tables_accessed": ["app.accounts"]}  # fmt: skip
    collector["queries"] = {"query_patterns": [query]}
    model = RedesignModel.from_collector(collector)
    return [(c.label, c.outcome, c.reasons) for c in model.candidate_keys(query)]


MIXED_COMPOSITE = {**MIXED_UNIQUE, "columns": ["tenant_id", "name"]}


def test_a_mixed_unique_index_makes_no_composite_tail_for_the_redesign():
    assert _redesign(MIXED_COMPOSITE) == [("accounts.name", ACCEPTED, ())]


@pytest.mark.passes_on_main  # a plain composite unique key still refuses its later column
def test_a_plain_composite_unique_key_still_refuses_its_later_column():
    plain = {**MIXED_COMPOSITE, "has_expressions": False, "index_expressions": None}
    assert _redesign(plain) == [("accounts.name", REFUSED, (COMPOSITE_TAIL,))]
