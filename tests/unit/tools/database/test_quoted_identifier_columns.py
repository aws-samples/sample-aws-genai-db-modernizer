# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The live collectors read a quoted identifier as one name (#1737).

Each collector's filter and sort parsing reads its own dialect's quoted names (MySQL
backticks, PostgreSQL and Oracle double quotes, SQL Server brackets and double quotes)
as one column name, as written: words inside a quoted name are never an operator or a
column, and a name that is not one word keeps its spaces and escaped quotes.

Case follows each collection's own names: PostgreSQL folds an unquoted name to lower
case and keeps a quoted one as written (``"Kind"`` and ``Kind`` are two columns), and
Oracle lower-cases every name, quoted or not, as its catalog columns and
``tables_accessed`` are (``"STATUS"`` and ``status`` are one column).
"""

from __future__ import annotations

import re

import pytest

from src.tools.database import live_sql, mysql_tools, oracle_tools, postgres_tools, sqlserver_tools

_MODULES = {
    "mysql": mysql_tools,
    "postgresql": postgres_tools,
    "sqlserver": sqlserver_tools,
    "oracle": oracle_tools,
}


@pytest.mark.parametrize(
    "engine, sql, expected",
    [
        # SQL words inside a quoted name: no WHERE clause, no filter.
        ("mysql", "SELECT `where`, `a = 1 AND b IN (2)` FROM t", None),
        ("postgresql", 'SELECT "where", "a = 1 AND b IN (2)" FROM t', None),
        ("sqlserver", "SELECT [where], [a = 1 AND b IN (2)] FROM t", None),
        ("sqlserver", 'SELECT "where", "a = 1 AND b IN (2)" FROM t', None),
        ("oracle", 'SELECT "where", "a = 1 AND b IN (2)" FROM t', None),
        # A keyword as a quoted name is the column filtered on.
        ("oracle", 'SELECT "order", "join" FROM t WHERE "limit" = 1', ["limit"]),
        ("oracle", 'UPDATE t SET a = 1 WHERE "status" = :1', ["a", "status"]),
        ("sqlserver", 'SELECT id FROM t WHERE "limit" = 1', ["limit"]),
        # A name that is not one word is one column, as written.
        ("mysql", "SELECT id FROM t WHERE `my col` = ?", ["my col"]),
        ("postgresql", 'SELECT id FROM t WHERE "my col" = $1', ["my col"]),
        ("sqlserver", "SELECT id FROM t WHERE [my col] = @p", ["my col"]),
        ("oracle", 'SELECT id FROM t WHERE "my col" = :1', ["my col"]),
        # An escaped quote inside a name is one quote of the name.
        ("mysql", "SELECT id FROM t WHERE `a``b` = 1", ["a`b"]),
        ("postgresql", 'SELECT id FROM t WHERE "a""b" = 1', ['a"b']),
        ("sqlserver", "SELECT id FROM t WHERE [a]]b] = 1", ["a]b"]),
        # A qualified quoted name is its column, as the bare one is.
        ("mysql", "SELECT id FROM t WHERE `t`.`x` = 1", ["x"]),
        ("postgresql", 'SELECT id FROM t WHERE "t"."x" = 1', ["x"]),
        ("sqlserver", "SELECT id FROM t WHERE [t].[x] = 1", ["x"]),
        ("oracle", 'SELECT id FROM t WHERE "t"."x" = 1', ["x"]),
        # A quoted name right before an operator word.
        ("mysql", "SELECT id FROM t WHERE `a`IN (1)", ["a"]),
        ("oracle", 'SELECT id FROM t WHERE "a"IN (1)', ["a"]),
        # Oracle lower-cases every name, quoted or not: "STATUS" and status are one column.
        ("oracle", 'SELECT id FROM t WHERE "Status" = :1 AND Kind = :2', ["status", "kind"]),
        ("oracle", 'SELECT id FROM t WHERE "STATUS" = :1 AND status = :2', ["status"]),
        # PostgreSQL folds an unquoted name and keeps a quoted one: "Kind" is not Kind.
        ("postgresql", "SELECT id FROM t WHERE Kind = $1 AND kind = $2", ["kind"]),
        ("postgresql", 'SELECT id FROM t WHERE "Kind" = $1 AND Kind = $2', ["Kind", "kind"]),
    ],
)
def test_a_quoted_name_is_one_filter_column(engine, sql, expected):
    assert _MODULES[engine]._extract_filter_columns(sql) == expected


@pytest.mark.parametrize("engine", ["sqlserver", "oracle"])
def test_a_column_ends_at_a_word_boundary(engine):
    """``JOIN`` is no column ``JO`` before an ``IN`` operator."""
    sql = "SELECT id FROM t WHERE a IN (SELECT b FROM u JOIN v ON u.x = v.y)"
    assert sorted(_MODULES[engine]._extract_filter_columns(sql)) == ["a", "x"]


@pytest.mark.parametrize(
    "engine, sql, expected",
    [
        # SQL words inside a quoted name: no ORDER BY clause.
        ("mysql", "SELECT `ORDER BY x`, `LIMIT` FROM t WHERE id = 1", None),
        ("postgresql", 'SELECT "ORDER BY x" FROM t WHERE id = 1', None),
        ("sqlserver", "SELECT [ORDER BY x] FROM t WHERE id = 1", None),
        ("oracle", 'SELECT "ORDER BY x" FROM t WHERE id = 1', None),
        # A keyword as a quoted name is the sort column.
        ("mysql", "SELECT id FROM t ORDER BY `desc`, `limit` ASC", ["desc", "limit"]),
        ("postgresql", 'SELECT id FROM t ORDER BY "desc", "limit" ASC', ["desc", "limit"]),
        ("sqlserver", "SELECT id FROM t ORDER BY [desc], [limit] ASC", ["desc", "limit"]),
        ("oracle", 'SELECT id FROM t ORDER BY "desc", "limit" ASC', ["desc", "limit"]),
        # A name that is not one word, and a qualified name, as the bare ones read.
        ("mysql", "SELECT id FROM t ORDER BY `my col` DESC, `t`.`x`", ["my col", "t.x"]),
        ("postgresql", 'SELECT id FROM t ORDER BY "my col" DESC, "t"."x"', ["my col", "t.x"]),
        ("oracle", 'SELECT id FROM t ORDER BY "My Col" DESC, "t"."x"', ["my col", "t.x"]),
        ("oracle", 'SELECT id FROM t ORDER BY "CREATED_AT" DESC', ["created_at"]),
        ("postgresql", 'SELECT id FROM t ORDER BY "Kind", Kind DESC', ["Kind", "kind"]),
        ("sqlserver", "SELECT id FROM t ORDER BY [my col] DESC", ["my col"]),
        # SQL Server reads a qualified sort column as one name, as the others do.
        ("sqlserver", "SELECT id FROM t ORDER BY [t].[x] DESC, u.y", ["t.x", "u.y"]),
        # A column whose name starts with ASC or DESC is a column.
        ("mysql", "SELECT id FROM t ORDER BY description, ascent DESC", ["description", "ascent"]),
    ],
)
def test_a_quoted_name_is_one_sort_column(engine, sql, expected):
    assert _MODULES[engine]._extract_sort_columns(sql) == expected


def test_column_text_names_each_quoted_identifier_once():
    text = live_sql.column_text('SELECT "a b", x FROM t WHERE "c""d" = 1', "postgresql")
    words = re.findall(r"\w+", text.text)
    assert [text.name(w) for w in words if text.is_quoted(w)] == ["a b", 'c"d']
    assert text.name("x") == "x"


def test_column_text_never_reuses_a_name_in_the_text():
    """A bare word that looks like a placeholder is the text's own name."""
    sql = 'SELECT id FROM t WHERE quoted_name_0 = 1 AND "x" = 2'
    text = live_sql.column_text(sql, "postgresql")
    assert not text.is_quoted("quoted_name_0")
    assert postgres_tools._extract_filter_columns(sql) == ["quoted_name_0", "x"]
