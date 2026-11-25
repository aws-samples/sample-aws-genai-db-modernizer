# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A SQL Server (max) length is kept in the column's type (#2219), and an offline file
reads a missing length as (max) only when collect-sqlserver.sql wrote it (#2239)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.tools.database.sqlserver_max import (
    SQLSERVER_SCRIPT_VERSION,
    max_type,
    sqlserver_max_column,
    written_by_script,
)

_REPO = Path(__file__).resolve().parents[4]


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
    assert sqlserver_max_column(row, by_script=True) == {**row, "column_type": "nvarchar(max)"}
    assert row == {"column_name": "body", "data_type": "nvarchar"}  # not changed in place


def test_an_offline_row_with_a_length_or_a_column_type_is_unchanged():
    sized = {"column_name": "name", "data_type": "nvarchar", "max_length": 50}
    typed = {"column_name": "body", "data_type": "nvarchar", "column_type": "nvarchar(MAX)"}
    assert sqlserver_max_column(sized, by_script=True) is sized
    assert sqlserver_max_column(typed, by_script=True) is typed


def test_a_file_the_script_did_not_write_keeps_a_missing_length_unknown():
    """A hand-built or reshaped file that drops the length of an nvarchar(50) row: the
    length stays unknown (the draft flags it), never a silent (max). A -1 is (max) in
    any file."""
    row = {"column_name": "name", "data_type": "nvarchar"}
    assert sqlserver_max_column(row, by_script=False) is row
    minus_one = {"column_name": "body", "data_type": "nvarchar", "max_length": -1}
    assert sqlserver_max_column(minus_one, by_script=False)["column_type"] == "nvarchar(max)"


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"engine": "sqlserver", "script_version": 2}, True),
        ({"engine": "sqlserver", "script_version": "2"}, True),
        ({"engine": "sqlserver", "script_version": 3}, True),
        # A collection saved before the marker, or one the script did not write.
        ({"engine": "sqlserver"}, False),
        ({"engine": "sqlserver", "script_version": 1}, False),
        ({"engine": "sqlserver", "script_version": None}, False),
        ({"engine": "sqlserver", "script_version": True}, False),
        ({"engine": "sqlserver", "script_version": "two"}, False),
        ({"engine": "sqlserver", "script_version": [2]}, False),
        (None, False),
        ("sqlserver", False),
    ],
)
def test_written_by_script(metadata, expected):
    assert written_by_script(metadata) is expected


def test_only_the_sqlserver_script_and_its_sample_generator_write_the_marker():
    """collect-sqlserver.sql writes the version in its metadata; the synthetic SQL Server
    sample is the script's output, so its generator writes the same; no other collection
    script writes one."""
    script = (_REPO / "scripts" / "collect-sqlserver.sql").read_text()
    assert re.findall(r"\b(\d+) AS script_version\b", script) == [str(SQLSERVER_SCRIPT_VERSION)]
    generator = (_REPO / "scripts" / "generate_sqlserver_sample.py").read_text()
    assert re.findall(r'"script_version": (\d+)', generator) == [str(SQLSERVER_SCRIPT_VERSION)]
    for other in ("collect-mysql.sql", "collect-postgresql.sql", "collect-oracle.sql"):
        assert "script_version" not in (_REPO / "scripts" / other).read_text(), other
