# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which foreign key column pairs Aurora MySQL refuses (#2239).

Each pair was run on MySQL 8.0.46 (utf8mb4): ``ALTER TABLE c ADD CONSTRAINT ... FOREIGN
KEY (k) REFERENCES p (k)`` with the parent type on ``p.k`` (its primary key) and the child
type on ``c.k`` (indexed). DECIMAL pairs of another precision or scale were accepted, and
are still treated as refused, since the MySQL reference says fixed-precision types need
the same size.
"""

from __future__ import annotations

import pytest

from src.tools.schema.aurora_common.mysql_foreign_keys import refused_pair

_REFUSED = [
    ("BIGINT", "INT"),
    ("INT UNSIGNED", "INT"),
    ("SMALLINT", "INT"),
    ("INT", "BIGINT"),
    ("VARBINARY(36)", "VARCHAR(36)"),
    ("VARCHAR(16)", "VARBINARY(16)"),
    ("NVARCHAR(50)", "VARCHAR(50)"),
    ("DATETIME", "DATE"),
    ("DOUBLE", "FLOAT"),
    ("SMALLINT", "YEAR"),
    ("TEXT", "VARCHAR(50)"),
    ("LONGBLOB", "VARBINARY(16)"),
    # Documented, not refused by 8.0.46: aligned anyway.
    ("DECIMAL(12,0)", "DECIMAL(10,0)"),
    ("DECIMAL(10,0)", "DECIMAL(10,2)"),
]
_ACCEPTED = [
    ("VARCHAR(255)", "VARCHAR(50)"),
    ("VARCHAR(10)", "VARCHAR(255)"),
    ("CHAR(10)", "VARCHAR(50)"),
    ("VARCHAR(50)", "CHAR(10)"),
    ("BINARY(16)", "VARBINARY(16)"),
    ("VARBINARY(8)", "VARBINARY(16)"),
    ("DATETIME(6)", "DATETIME"),
    ("DATETIME(6)", "DATETIME(3)"),
    ("TIMESTAMP", "DATETIME"),
    ("TIME(3)", "TIME"),
    ("BIT(16)", "BIT(8)"),
    ("ENUM('a','b','c')", "ENUM('a','b')"),
    ("DOUBLE", "DOUBLE"),
    ("DECIMAL(10,0)", "DECIMAL(10,0)"),
    ("INT", "INTEGER"),
]


@pytest.mark.parametrize(("child", "parent"), _REFUSED)
def test_a_pair_the_server_refuses_has_a_reason(child, parent):
    assert refused_pair(child, parent)


@pytest.mark.parametrize(("child", "parent"), _ACCEPTED)
def test_a_pair_the_server_accepts_is_left_alone(child, parent):
    assert refused_pair(child, parent) is None


def test_each_reason_says_what_mysql_requires():
    assert "same size and sign" in refused_pair("BIGINT", "INT")
    assert "same precision and scale" in refused_pair("DECIMAL(12,0)", "DECIMAL(10,0)")
    assert "TEXT or BLOB" in refused_pair("TEXT", "VARCHAR(50)")
    assert "lengths may differ" in refused_pair("VARBINARY(36)", "VARCHAR(36)")
