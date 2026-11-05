# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Consumers that read a table id find a PostgreSQL table named after its schema (#1903).

An offline PostgreSQL collection names its tables ``public.<table>`` (as the live
collector does), while the source database is still ``discourse``: the summary
post-check still recognises ``topics``, and schema design's grouping still picks the
query's collected table as its primary one.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_grounding import check_summary_grounding
from src.agents.schema_design.group_splitter import get_primary_table

ENGINE_TABLES = {
    "aurora_postgresql": ["public.topic_users", "public.posts"],
    "dynamodb": ["public.topic_users"],
}


def test_the_summary_check_recognises_a_schema_qualified_table_by_its_name():
    findings = check_summary_grounding(
        "ElastiCache serves the topic_users lookups.", ENGINE_TABLES, "discourse"
    )

    assert [f["table"] for f in findings] == ["public.topic_users"]
    assert findings[0]["high_confidence"]


@pytest.mark.passes_on_main  # no false finding, before or after #1903
def test_a_correct_claim_about_a_schema_qualified_table_is_kept():
    assert (
        check_summary_grounding(
            "DynamoDB serves the topic_users lookups.", ENGINE_TABLES, "discourse"
        )
        == []
    )


def test_the_primary_table_is_the_first_collected_one():
    query = {"tables_accessed": ["pg_class", "public.topics", "public.posts"]}

    assert get_primary_table(query, "discourse", frozenset({"public.topics", "public.posts"})) == (
        "public.topics"
    )


def test_the_database_qualified_table_still_comes_first():
    # MySQL ids lead with the database: the rule before #1903, unchanged.
    query = {"tables_accessed": ["v_recent", "wordpress.wp_posts"]}

    assert get_primary_table(query, "wordpress", frozenset({"v_recent", "wordpress.wp_posts"})) == (
        "wordpress.wp_posts"
    )
    assert get_primary_table({"tables_accessed": ["x"]}, "wordpress") == "x"
