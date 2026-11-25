# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How many bytes each Aurora MySQL draft type counts toward a row (#2239).

Every number here was measured on MySQL 8.0.46 (InnoDB, utf8mb4, 16 KB pages), the
engine line Aurora MySQL version 3 runs: the server row as the longest ``VARBINARY(n) NOT
NULL`` that still fits beside the column, the InnoDB record as the most fixed-width bytes
that still fit beside it and a ``BIGINT`` primary key.
"""

from __future__ import annotations

import pytest

from src.tools.schema.aurora_common.mysql_row_size import (
    MYSQL_MAX_RECORD_BYTES,
    MYSQL_MAX_ROW_BYTES,
    column_width,
    fit_row,
    lob_type,
    row_size,
)

# (type, bytes toward the 65,535-byte row, bytes toward the InnoDB record), NOT NULL.
_MEASURED = [
    ("TINYINT", 1, 1),
    ("BOOLEAN", 1, 1),
    ("SMALLINT", 2, 2),
    ("MEDIUMINT", 3, 3),
    ("INT", 4, 4),
    ("INT UNSIGNED", 4, 4),
    ("BIGINT", 8, 8),
    ("FLOAT", 4, 4),
    ("FLOAT(30)", 8, 8),
    ("DOUBLE", 8, 8),
    ("REAL", 8, 8),
    ("DECIMAL", 5, 5),
    ("DECIMAL(10,2)", 5, 5),
    ("DECIMAL(18,0)", 8, 8),
    ("DECIMAL(38,10)", 18, 18),
    ("DECIMAL(65,30)", 30, 30),
    ("DATE", 3, 3),
    ("TIME", 3, 3),
    ("TIME(1)", 4, 4),
    ("TIME(3)", 5, 5),
    ("TIME(6)", 6, 6),
    ("DATETIME", 5, 5),
    ("DATETIME(3)", 7, 7),
    ("DATETIME(6)", 8, 8),
    ("TIMESTAMP", 4, 4),
    ("TIMESTAMP(6)", 7, 7),
    ("YEAR", 1, 1),
    ("BIT", 1, 1),
    ("BIT(9)", 2, 2),
    ("BIT(64)", 8, 8),
    ("ENUM('a','b')", 1, 1),
    ("SET('a','b','c','d','e','f','g','h','i')", 2, 2),
    ("CHAR", 4, 5),
    ("CHAR(10)", 40, 41),
    ("CHAR(63)", 252, 253),
    ("CHAR(64)", 256, 41),
    ("CHAR(255)", 1020, 41),
    ("NCHAR(10)", 30, 31),
    ("BINARY(16)", 16, 16),
    ("BINARY(255)", 255, 255),
    ("VARCHAR(10)", 41, 41),
    ("VARCHAR(63)", 253, 253),
    ("VARCHAR(64)", 258, 41),
    ("VARCHAR(4000)", 16002, 41),
    ("NVARCHAR(10)", 31, 31),
    ("NVARCHAR(85)", 256, 256),
    ("NVARCHAR(86)", 260, 41),
    ("VARBINARY(255)", 256, 256),
    ("VARBINARY(256)", 258, 41),
    ("VARBINARY(8000)", 8002, 41),
    ("TINYTEXT", 9, 41),
    ("TEXT", 10, 41),
    ("MEDIUMTEXT", 11, 41),
    ("LONGTEXT", 12, 41),
    ("TINYBLOB", 9, 41),
    ("BLOB", 10, 41),
    ("MEDIUMBLOB", 11, 41),
    ("LONGBLOB", 12, 41),
    ("JSON", 12, 41),
    ("GEOMETRY", 12, 41),
    ("POINT", 12, 41),
]


@pytest.mark.parametrize(("aurora_type", "row", "record"), _MEASURED)
def test_each_type_counts_what_the_server_counts(aurora_type, row, record):
    width = column_width(aurora_type)
    assert (width.row, width.record) == (row, record)


def test_the_limits_are_the_servers():
    assert MYSQL_MAX_ROW_BYTES == 65535
    assert MYSQL_MAX_RECORD_BYTES == 8125


def test_a_nullable_column_adds_a_bit_to_both_counts():
    """Eight nullable TINYINTs take one NULL byte, nine take two (measured)."""
    eight = row_size([("TINYINT", True)] * 8 + [("BIGINT", False)], has_primary_key=True)
    nine = row_size([("TINYINT", True)] * 9 + [("BIGINT", False)], has_primary_key=True)
    assert (eight.row_bytes, nine.row_bytes) == (8 + 1 + 8, 9 + 2 + 8)
    # The record adds its 5-byte header and InnoDB's 13 bytes of transaction id and roll
    # pointer; a table with no primary key also gets a 6-byte row id.
    assert eight.record_bytes == 5 + 1 + 13 + 8 + 8
    no_pk = row_size([("BIGINT", False)], has_primary_key=False)
    assert no_pk.record_bytes == 5 + 13 + 6 + 8


@pytest.mark.parametrize(
    ("columns", "fits"),
    [
        # The server's own edge cases: 65,535 bytes fit, one more does not.
        ([("VARBINARY(65533)", False)], True),
        ([("VARBINARY(65533)", True)], False),
        ([("VARBINARY(65532)", True)], True),
        ([("VARCHAR(16383)", False), ("TINYINT", False)], True),
        ([("VARCHAR(16383)", False), ("TINYINT", True)], False),
        # Nine VARBINARY(8000), legal in SQL Server: 72,018 bytes.
        ([("VARBINARY(8000)", True)] * 9, False),
        ([("VARBINARY(8000)", True)] * 8, True),
        # Forty VARCHAR(50) fit the record, forty-one do not (measured, BIGINT key).
        ([("BIGINT", False)] + [("VARCHAR(50)", False)] * 40, True),
        ([("BIGINT", False)] + [("VARCHAR(50)", False)] * 41, False),
    ],
)
def test_a_row_fits_exactly_when_the_server_creates_the_table(columns, fits):
    assert row_size(columns, has_primary_key=True).fits is fits


@pytest.mark.parametrize(
    ("aurora_type", "lob"),
    [
        ("VARCHAR(4000)", "TEXT"),
        ("VARCHAR(16383)", "TEXT"),
        ("VARCHAR(16384)", "MEDIUMTEXT"),
        ("VARCHAR(32767)", "MEDIUMTEXT"),
        ("VARBINARY(8000)", "BLOB"),
        ("VARBINARY(65535)", "BLOB"),
        # A model's NVARCHAR override (utf8mb3): TEXT in the table's utf8mb4 holds it.
        ("NVARCHAR(4000)", "TEXT"),
        ("NATIONAL VARCHAR(4000)", "TEXT"),
        ("NVARCHAR(20000)", "MEDIUMTEXT"),
    ],
)
def test_a_moved_column_gets_the_smallest_lob_that_holds_it(aurora_type, lob):
    assert lob_type(aurora_type) == lob


@pytest.mark.parametrize("aurora_type", ["CHAR(10)", "BINARY(16)", "INT", "TEXT", "DATETIME"])
def test_only_varchar_and_varbinary_move(aurora_type):
    """CHAR and BINARY pad their values, so TEXT or BLOB would change what they hold."""
    assert lob_type(aurora_type) is None


def test_fit_row_moves_the_widest_columns_last_first_until_the_row_fits():
    columns = [("BIGINT", False)] + [("VARBINARY(8000)", True)] * 9
    moved, size = fit_row(columns, movable=[False] + [True] * 9, has_primary_key=True)
    assert moved == [9]  # one is enough, and the last of equals goes first
    assert size.fits


def test_fit_row_moves_for_the_record_limit_too():
    """Sixty VARCHAR(50) fit 65,535 bytes but not the InnoDB record: the moves stop as
    soon as the record fits. A moved column still takes 41 bytes of it, so 25 move and
    35 stay (measured: 36 do not fit)."""
    columns = [("BIGINT", False)] + [("VARCHAR(50)", False)] * 60
    moved, size = fit_row(columns, movable=[False] + [True] * 60, has_primary_key=True)
    assert size.fits
    assert moved == list(range(60, 35, -1))


def test_fit_row_never_moves_a_column_it_may_not_and_says_when_it_cannot_fit():
    columns = [("BIGINT", False)] + [("VARBINARY(8000)", True)] * 9
    moved, size = fit_row(columns, movable=[False] * 10, has_primary_key=True)
    assert moved == []
    assert not size.fits


def test_fit_row_moves_nothing_that_saves_nothing():
    """A VARCHAR(10) takes 41 bytes of the record, as a TEXT column does: moving it would
    lose its length and save no byte."""
    columns = [("BIGINT", False)] + [("VARCHAR(10)", False)] * 200
    moved, size = fit_row(columns, movable=[False] + [True] * 200, has_primary_key=True)
    assert moved == []
    assert not size.fits


def test_a_row_under_both_limits_moves_nothing():
    columns = [("BIGINT", False), ("VARCHAR(255)", True), ("VARBINARY(8000)", True)]
    moved, size = fit_row(columns, movable=[False, True, True], has_primary_key=True)
    assert moved == []
    assert size.fits
