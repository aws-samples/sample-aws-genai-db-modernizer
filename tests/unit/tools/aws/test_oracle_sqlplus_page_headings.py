# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""sqlplus prints the column headings again at the top of every page (#1895).

The live Oracle session cannot set a page size above 50000, so a result with more rows
repeats the heading and dashes lines. The parser drops those repeats. The output below is
synthetic: no Oracle connection and no customer data.
"""

from __future__ import annotations

import pytest

from src.tools.aws.ssm_executor import _build_sql_command, _parse_tabular_output

PAGE_HEAD = "QUERY_ID   EXECUTION_COUNT\n---------- ---------------\n"


def test_a_repeated_page_heading_is_not_a_row() -> None:
    out = f"\n{PAGE_HEAD}a                        5\n\n{PAGE_HEAD}b                        7\n"
    assert _parse_tabular_output(out, "oracle") == [
        {"query_id": "a", "execution_count": 5},
        {"query_id": "b", "execution_count": 7},
    ]


def test_three_pages_with_form_feeds_and_blank_lines() -> None:
    out = (
        f"\n{PAGE_HEAD}a                        1\n"
        f"\f\n{PAGE_HEAD}b                        2\n\n"
        f"\f\n{PAGE_HEAD}c                        3\n"
    )
    rows = _parse_tabular_output(out, "oracle")
    assert [r["query_id"] for r in rows] == ["a", "b", "c"]
    assert [r["execution_count"] for r in rows] == [1, 2, 3]


@pytest.mark.passes_on_main  # guards a row and a single page the parser already keeps
def test_a_row_whose_value_names_a_column_is_kept() -> None:
    out = f"\n{PAGE_HEAD}QUERY_ID                 5\n"
    assert _parse_tabular_output(out, "oracle") == [{"query_id": "QUERY_ID", "execution_count": 5}]


@pytest.mark.passes_on_main  # guards a row and a single page the parser already keeps
def test_a_single_page_parses_as_before() -> None:
    out = f"\n{PAGE_HEAD}a                        5\nb                        7\n"
    assert [r["query_id"] for r in _parse_tabular_output(out, "oracle")] == ["a", "b"]


def test_the_session_sets_the_largest_page_and_a_form_feed_free_page_break() -> None:
    cmd = _build_sql_command("oracle", "h", 1521, "svc", "arn", "SELECT 1 FROM dual", "us-east-1")
    assert "SET PAGESIZE 50000" in cmd
    assert "SET NEWPAGE NONE" in cmd
