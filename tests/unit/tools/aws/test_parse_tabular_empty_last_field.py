# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The tabular parser keeps a final row whose last field is empty.

``_parse_tabular_output`` used to ``strip()`` the whole output, which also removed the tabs
that end the last line when its last field is empty. That row then had one field fewer than
the header and was dropped as malformed. The mysql client prints no row-count footer, so its
final row is the last line: a live ``information_schema.columns`` read lost the last column of
a table whenever its ``extra`` (empty for most columns) came last. PostgreSQL's and SQL
Server's footers follow the last row, so they never ended in the tabs. Oracle's sqlplus output
takes the same path: the whole-output strip also removed the leading spaces of its first line,
the header, which a right-aligned numeric first column has, shifting the header out of line
with its dashes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.tools.aws.ssm_executor import _parse_tabular_output
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector

HEADER = "a\tb\tc"


def test_a_final_row_with_an_empty_last_field_is_kept():
    rows = _parse_tabular_output(f"{HEADER}\n1\t2\t\n", "mysql")
    assert rows == [{"a": 1, "b": 2, "c": None}]


def test_a_final_row_with_several_empty_last_fields_is_kept():
    assert _parse_tabular_output(f"{HEADER}\n1\t\t\n", "mysql") == [{"a": 1, "b": None, "c": None}]


def test_a_final_row_without_a_newline_after_it_is_kept():
    assert _parse_tabular_output(f"{HEADER}\n1\t2\t", "mysql") == [{"a": 1, "b": 2, "c": None}]


def test_a_final_row_ending_in_a_windows_line_break_is_kept():
    assert _parse_tabular_output(f"{HEADER}\r\n1\t2\t\r\n", "mysql") == [
        {"a": 1, "b": 2, "c": None}
    ]


def test_every_row_keeps_its_empty_last_field_in_the_middle_and_at_the_end():
    raw = f"{HEADER}\n1\t2\t\n3\t4\tx\n5\t6\t\n"
    assert [r["c"] for r in _parse_tabular_output(raw, "mysql")] == [None, "x", None]
    assert [r["a"] for r in _parse_tabular_output(raw, "mysql")] == [1, 3, 5]


@pytest.mark.passes_on_main  # a middle row was always kept; this guards it
def test_a_middle_row_with_an_empty_last_field_is_kept():
    rows = _parse_tabular_output(f"{HEADER}\n1\t2\t\n3\t4\t5\n", "mysql")
    assert [r["c"] for r in rows] == [None, 5]


@pytest.mark.passes_on_main  # a footer follows the last row, so it never ended in tabs
def test_postgresql_and_sqlserver_rows_with_empty_last_fields_are_kept_before_the_footer():
    pg = f"{HEADER}\n1\t2\t\n(1 row)\n"
    assert _parse_tabular_output(pg, "postgresql") == [{"a": 1, "b": 2, "c": None}]
    mssql = f"{HEADER}\n-\t-\t-\n1\t2\t\n\n(1 rows affected)\n"
    assert _parse_tabular_output(mssql, "sqlserver") == [{"a": 1, "b": 2, "c": None}]


@pytest.mark.passes_on_main  # padding and blank lines around the output are still ignored
def test_blank_lines_and_padding_around_the_output_are_still_ignored():
    raw = f"\n\n{HEADER}  \n1 \t2\t3  \n\n"
    assert _parse_tabular_output(raw, "mysql") == [{"a": 1, "b": 2, "c": 3}]


@pytest.mark.passes_on_main  # a row with a different field count is still dropped
def test_a_row_with_the_wrong_number_of_fields_is_still_dropped():
    assert _parse_tabular_output(f"{HEADER}\n1\t2\n3\t4\t5\n", "mysql") == [
        {"a": 3, "b": 4, "c": 5}
    ]


@pytest.mark.parametrize("cls", [MySQLRemoteCollector, MariaDBRemoteCollector])
def test_a_live_collection_keeps_the_last_column_of_a_table(cls):
    """information_schema.columns prints ``extra`` last, and it is empty for most columns."""
    raw = (
        "column_name\tdata_type\tcolumn_key\textra\n"
        "id\tint\tPRI\tauto_increment\n"
        "body\tlongtext\t\t\n"
    )
    ssm = MagicMock()
    ssm.run_sql_json.side_effect = lambda **kw: _parse_tabular_output(raw, "mysql")
    collector = cls(ssm, "h", 3306, "app", "arn:secret")
    assert [c["column_name"] for c in collector.collect_columns("posts")] == ["id", "body"]


def test_an_oracle_result_with_a_right_aligned_numeric_first_column_parses():
    """sqlplus right-aligns a number, so its header line starts with spaces. Stripping the whole
    output removed them from the first line only, shifting the header out of line with its
    dashes: the first column read as a cut name and its values were wrong."""
    raw = (
        "   ID NAME       QTY\n"
        "----- ---------- ---\n"
        "    1 widget       5\n"
        "   22 gadget      12\n"
    )
    rows = _parse_tabular_output(raw, "oracle")
    assert rows == [
        {"id": 1, "name": "widget", "qty": 5},
        {"id": 22, "name": "gadget", "qty": 12},
    ]


@pytest.mark.passes_on_main  # behaviour main already has: this guards it
def test_an_oracle_row_with_an_empty_last_column_is_kept_after_the_last_line():
    raw = "NAME       NOTE\n---------- ----\nwidget     x\ngadget\n"
    assert [r["note"] for r in _parse_tabular_output(raw, "oracle")] == ["x", None]


@pytest.mark.passes_on_main  # behaviour main already has: this guards it
def test_several_rows_with_empty_last_fields_stay_with_a_multi_row_footer():
    pg = f"{HEADER}\n1\t2\t\n3\t4\t\n5\t6\t7\n(3 rows)\n"
    assert [r["c"] for r in _parse_tabular_output(pg, "postgresql")] == [None, None, 7]
    mssql = f"{HEADER}\n-\t-\t-\n1\t2\t\n3\t4\t\n5\t6\t7\n\n(3 rows affected)\n"
    assert [r["c"] for r in _parse_tabular_output(mssql, "sqlserver")] == [None, None, 7]
