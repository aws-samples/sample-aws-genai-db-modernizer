# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2325: the queries each relational engine serves on each table, as a fact.

The table of a design's tables shows one figure per table. It is computed once here, from
the query groups, and the UI only formats it: a query on two tables is in both tables' rows
and once in the engine's own total.
"""

from __future__ import annotations

from src.contracts.synthesis_output import QueryGroupFacts
from src.shared.report_facts import query_group_facts


def _group(name: str, engine: str, queries: dict[str, list[str]]) -> dict:
    return {
        "group_name": name,
        "engines": [engine],
        "access_patterns": [
            {"engine": engine, "query_ids": [qid], "design_rps": 1} for qid in queries
        ],
        "source_queries": [
            {"query_id": qid, "tables_accessed": tables} for qid, tables in queries.items()
        ],
    }


def _rows(report: dict) -> dict[tuple[str, str], tuple[int, list[str]]]:
    facts = query_group_facts(report)
    assert facts is not None
    QueryGroupFacts.model_validate(facts)
    return {
        (r["engine"], r["table"]): (r["query_count"], r["query_ids"])
        for r in facts["table_queries"]
    }


def test_a_join_query_is_in_each_tables_row_and_once_in_the_engine_total() -> None:
    report = {
        "query_groups": [
            _group("wp_posts", "aurora_mysql", {"q1": ["wp_posts"], "q2": ["wp_posts"]}),
            _group("wp_posts, wp_users", "aurora_mysql", {"q3": ["wp_posts", "wp_users"]}),
        ]
    }
    assert _rows(report) == {
        ("aurora_mysql", "wp_posts"): (3, ["q1", "q2", "q3"]),
        ("aurora_mysql", "wp_users"): (1, ["q3"]),
    }
    facts = query_group_facts(report)
    assert facts is not None
    assert facts["relational_query_counts"] == {"aurora_mysql": 3}


def test_a_name_reads_the_same_with_a_schema_or_quotes_or_another_case() -> None:
    report = {
        "query_groups": [
            _group("g", "aurora_mysql", {"q1": ["Shop.`WP_Posts`"], "q2": ["wp_posts"]}),
        ]
    }
    assert _rows(report) == {("aurora_mysql", "wp_posts"): (2, ["q1", "q2"])}


def test_only_the_engines_own_queries_count_and_placeholders_are_left_out() -> None:
    mixed = _group("t", "aurora_mysql", {"q1": ["t"]})
    mixed["access_patterns"].append({"engine": "dynamodb", "query_ids": ["q9"], "design_rps": 1})
    mixed["source_queries"].append({"query_id": "q9", "tables_accessed": ["t"]})
    mixed["source_queries"].append({"query_id": "q1", "tables_accessed": ["t", "unknown"]})
    assert _rows({"query_groups": [mixed]}) == {("aurora_mysql", "t"): (1, ["q1"])}


def test_a_report_with_no_relational_queries_has_no_rows() -> None:
    assert _rows({"query_groups": [_group("t", "dynamodb", {"q1": ["t"]})]}) == {}
