# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A SQL Server (max) length is kept in the column's type (#2219)."""

from __future__ import annotations

import pytest

from src.tools.database.sqlserver_max import max_type, sqlserver_max_column


@pytest.mark.parametrize(
    ("data_type", "max_length", "absent_is_max", "expected"),
    [
        ("nvarchar", -1, False, "nvarchar(max)"),
        ("VARCHAR", "-1", False, "varchar(max)"),
        ("varbinary", -1, True, "varbinary(max)"),
        ("nvarchar", None, True, "nvarchar(max)"),  # collect-sqlserver.sql: NULL for -1
        ("nvarchar", None, False, None),  # live: no length is not (max)
        ("nvarchar", 100, True, None),
        ("ntext", -1, False, None),  # takes no (max)
        ("int", None, True, None),
        ("nvarchar", "n/a", False, None),
    ],
)
def test_max_type(data_type, max_length, absent_is_max, expected):
    assert max_type(data_type, max_length, absent_is_max=absent_is_max) == expected


def test_an_offline_row_gets_the_type_in_column_type_and_keeps_data_type():
    row = {"column_name": "body", "data_type": "nvarchar"}
    assert sqlserver_max_column(row) == {**row, "column_type": "nvarchar(max)"}
    assert row == {"column_name": "body", "data_type": "nvarchar"}  # not changed in place


def test_an_offline_row_with_a_length_or_a_column_type_is_unchanged():
    sized = {"column_name": "name", "data_type": "nvarchar", "max_length": 50}
    typed = {"column_name": "body", "data_type": "nvarchar", "column_type": "nvarchar(MAX)"}
    assert sqlserver_max_column(sized) is sized
    assert sqlserver_max_column(typed) is typed
