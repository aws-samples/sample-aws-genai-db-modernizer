# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SQL Server temp tables and table variables, read in the tsql dialect (#1876).

sqlglot reads ``#t`` and ``##t`` as tables marked temporary and ``@t`` as a table named
by a parameter. ``facade.temp_object_name`` is the one classification: none of them is
a table reference, and ``SqlFacts.temp_objects`` names them, the regex fallback too.
"""

from __future__ import annotations

import pytest

from src.tools.sql.facade import read_sql, sql_facts, table_references
from src.tools.sql.query_facts import query_facts
from src.tools.sql.regex_facts import regex_routine_source, regex_temp_objects

CASES = [
    ("SELECT TotalRecords = COUNT(*) FROM #PageIndexForUsers", ("#pageindexforusers",), ()),
    ("INSERT INTO @tmp (seckey) VALUES (? + Cast(@UserId as nvarchar(200)))", ("@tmp",), ()),
    (
        "SELECT u.UserId FROM dbo.Users u JOIN #PageIndexForUsers p ON p.UserId = u.UserId",
        ("#pageindexforusers",),
        ("Users",),
    ),
    ("SELECT s.UserId FROM ##ActiveSessions s WHERE s.UserId = @UserId", ("##activesessions",), ()),
    (
        "select p.plan_id from @plansByGroup as p where plan_id = @lastKnownGoodPlanId",
        ("@plansbygroup",),
        (),
    ),
    ("SELECT PlanId FROM dbo.Plans WHERE plan_type = @planType", (), ("Plans",)),
    ("SELECT * FROM tempdb..#Staging", ("#staging",), ()),
    ("UPDATE @t SET a = ? WHERE b = ?", ("@t",), ()),
    ("DELETE FROM #x WHERE a = ?", ("#x",), ()),
    ("SELECT UserId INTO #Ids FROM dbo.Users", ("#ids",), ("Users",)),
    ("DROP TABLE #t", ("#t",), ()),
    ("TRUNCATE TABLE #t", ("#t",), ()),
    ("CREATE TABLE #t (a INT)", ("#t",), ()),
    (
        "MERGE dbo.Tgt AS t USING @src AS s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.a = s.a;",
        ("@src",),
        ("Tgt",),
    ),
    (
        "UPDATE dbo.Users SET a = ? OUTPUT inserted.UserId INTO @ids WHERE b = ?",
        ("@ids",),
        ("Users",),
    ),
]


@pytest.mark.parametrize(("sql", "temp", "tables"), CASES)
def test_the_tsql_reading_names_temp_objects_and_no_table_for_them(sql, temp, tables) -> None:
    facts = sql_facts(sql, "sqlserver")
    assert facts.parsed_by == "sqlglot"
    assert facts.temp_objects == temp
    assert tuple(r.name for r in table_references(sql, "sqlserver").tables) == tables
    assert query_facts(sql, "sqlserver").temp_objects == temp


def test_the_regex_fallback_names_them_too() -> None:
    sql = "IF NOT EXISTS (SELECT 1 FROM @words WHERE [string] = @s)"
    reading = read_sql(sql, "sqlserver")
    assert reading.facts.parsed_by == "regex"
    assert reading.facts.temp_objects == ("@words",)
    assert reading.tables.tables == ()


@pytest.mark.parametrize(
    ("sql", "temp"),
    [
        ("IF OBJECT_ID('tempdb..#t') IS NOT NULL DROP TABLE #t", ("#t",)),
        ("IF OBJECT_ID(?) IS NOT NULL DROP TABLE IF EXISTS #t", ("#t",)),
        ("IF ? = ? TRUNCATE TABLE ##g", ("##g",)),
        ("IF ? = ? CREATE TABLE #t (a INT)", ("#t",)),
        (
            "IF ? = ? MERGE dbo.Tgt AS t USING @src AS s ON t.id = s.id WHEN MATCHED THEN DELETE;",
            ("@src",),
        ),
        ("IF ? = ? INSERT INTO #t (a) VALUES (?)", ("#t",)),
        ("IF ? = ? UPDATE tempdb.dbo.#t SET a = ?", ("#t",)),
        ("IF ? = ? DELETE @t WHERE a = ?", ("@t",)),  # T-SQL's FROM is optional
        ("FETCH NEXT FROM cur INTO @a, @b", ()),  # cursor variables, not tables
        ("IF ? = ? SELECT * FROM dbo.Users WHERE Email = '#tag'", ()),  # a literal
        ("IF ? = ? SELECT @n = COUNT(*) FROM dbo.Users", ()),  # a scalar variable
    ],
)
def test_regex_temp_objects(sql, temp) -> None:
    assert regex_temp_objects(sql, "tsql") == temp


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE dbo.Users SET a = ? OUTPUT inserted.UserId INTO @ids WHERE b = ?",
        "UPDATE dbo.Users SET a = ? OUTPUT inserted.UserId INTO #gone WHERE b = ?",
        "INSERT INTO dbo.Users (a) OUTPUT inserted.UserId INTO @ids VALUES (?)",
        "MERGE dbo.Tgt AS t USING @src AS s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.a = s.a;",
        "DROP TABLE #t",
        "SELECT t.a FROM #t t CROSS APPLY dbo.fn_Split(t.s, ?) f",
        "INSERT INTO @t SELECT * FROM dbo.fn_GetUsers(@x)",
        "SELECT * FROM #t CROSS APPLY OPENJSON(@j)",
    ],
)
def test_the_two_readers_agree(sql) -> None:
    """sqlglot and its regex fallback name the same temp objects and the same function
    source, ``OUTPUT ... INTO`` and ``MERGE ... USING`` included (review of #1879)."""
    parsed = sql_facts(sql, "sqlserver")
    assert parsed.parsed_by == "sqlglot"
    assert parsed.temp_objects == regex_temp_objects(sql, "tsql")
    assert parsed.routine_source == regex_routine_source(sql, "tsql")


@pytest.mark.parametrize(
    ("sql", "routine"),
    [
        ("INSERT INTO @t SELECT * FROM dbo.fn_GetUsers(@x)", True),
        ("INSERT INTO @t SELECT * FROM fn_GetUsers(@x)", True),
        ("INSERT INTO #t EXEC dbo.GetUsers @p", True),  # an EXEC is read by the regex fallback
        ("INSERT INTO #t EXECUTE dbo.GetUsers @p", True),
        ("SELECT t.a FROM #t t CROSS APPLY dbo.fn_Split(t.s, ?) f", True),
        ("SELECT t.a FROM #t t OUTER APPLY dbo.fn_Split(t.s, ?) f", True),
        ("INSERT INTO @t SELECT value FROM STRING_SPLIT(@s, ?)", False),
        ("SELECT * FROM #t CROSS APPLY OPENJSON(@j)", False),
        ("INSERT INTO @t SELECT value FROM GENERATE_SERIES(1, 3)", False),
        ("SELECT COUNT(*) FROM #t WHERE a = dbo.fn_Scalar(?)", False),  # a scalar call
        ("IF ? = ? SELECT * FROM #t (NOLOCK)", False),  # an old-style table hint
        ("EXEC sp_executesql @sql", True),
        ("IF ? = ? SELECT ? AS note FROM #t WHERE n = 'exec'", False),  # a literal
    ],
)
def test_routine_source(sql, routine) -> None:
    assert sql_facts(sql, "sqlserver").routine_source is routine
    assert query_facts(sql, "sqlserver").routine_source is routine


@pytest.mark.passes_on_main  # no other dialect ever read a temp object
def test_other_dialects_read_none() -> None:
    # MySQL's ``SELECT ... INTO @v`` writes a session variable.
    assert sql_facts("SELECT COUNT(*) INTO @n FROM posts", "mysql").temp_objects == ()
    assert regex_temp_objects("SELECT 1 INTO @n", "mysql") == ()
    assert query_facts("SELECT COUNT(*) INTO @n FROM posts", "mysql").temp_objects == ()
    assert not query_facts("CALL refresh(@x)", "mysql").routine_source
    assert not regex_routine_source("SELECT * FROM generate_series(1, 3)", "postgres")
