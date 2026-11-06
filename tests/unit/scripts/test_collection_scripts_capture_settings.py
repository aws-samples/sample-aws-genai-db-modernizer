# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The offline collection scripts capture every statement by default (#384).

Each script keeps every statement that ran at least ``min_executions`` times (10 by
default). A cap, ``query_limit``, applies only when the user sets one, through the
client's own mechanism: psql ``-v``, a MySQL session variable (``--init-command``), a
``DECLARE`` line in the SQL Server script (sqlcmd cannot default an unset ``-v``
variable: ``:setvar`` overrides ``-v``), and SQL*Plus positional arguments defaulted
with the ``COLUMN ... NEW_VALUE`` idiom. With no cap the statement query has no
``LIMIT``, ``TOP`` or ``FETCH FIRST`` clause at all: the cap is a filter on a
``ROW_NUMBER()`` rank (MySQL, SQL Server, Oracle) or a clause psql adds only when
``query_limit`` is set. Each script writes both settings into
``metadata.query_capture``.

No database runs here: the checks read the script text, and parse each statement
query with sqlglot in its dialect, with the settings filled in both ways.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlglot

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / f"collect-{name}.sql").read_text()


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i)]


def _pg_queries() -> str:
    return _between(_script("postgresql"), "queries_data AS (", "-- ===================== 11.")


def _mysql_queries() -> str:
    return _between(_script("mysql"), "SELECT JSON_ARRAYAGG(q_obj) INTO @queries", ") AS q;")


def _sqlserver_queries() -> str:
    return _between(_script("sqlserver"), "SET @queries = (", "    FOR JSON PATH\n);")


def _oracle_queries() -> str:
    text = _script("oracle")
    section = _between(text, "DBMS_OUTPUT.PUT_LINE('\"queries\": [');", "-- 2. Metadata")
    return _between(section, "FOR rec IN (", ") LOOP")


SECTIONS = {
    "postgresql": _pg_queries,
    "mysql": _mysql_queries,
    "sqlserver": _sqlserver_queries,
    "oracle": _oracle_queries,
}
# ``TOP 1`` is the SQL Server plan-attribute lookup (one attribute row), not a cap.
FIXED_CAPS = re.compile(
    r"LIMIT\s+\d+|TOP\s*\(?\s*(?!1\b)\d+|FETCH\s+FIRST\s+\d+|ROWNUM\s*<=\s*\d+", re.I
)
FIXED_FLOOR = re.compile(
    r"(COUNT_STAR|calls|execution_count|executions|EXECUTIONS\))\s*>=\s*\d", re.I
)


@pytest.mark.parametrize("engine", list(SECTIONS))
def test_the_statement_query_has_no_fixed_cap_or_floor(engine):
    section = SECTIONS[engine]()
    assert not FIXED_CAPS.findall(section), FIXED_CAPS.findall(section)
    assert not FIXED_FLOOR.findall(section), FIXED_FLOOR.findall(section)


def test_postgresql_takes_both_settings_as_psql_variables():
    text = _script("postgresql")
    # \if and :{?var} need a psql 10+ client (an older one would run every \set).
    assert "psql 10 or later" in text
    assert "\\if :{?query_limit}" in text and "\\if :{?min_executions}" in text
    assert "\\set min_executions 10" in text
    # With no cap the clause is empty; with one it is LIMIT on the value, cast so a
    # value that is not a number fails instead of reaching the SQL.
    assert "\\set query_limit_clause ''" in text
    assert "\\set query_limit_clause 'LIMIT CAST(' :'query_limit' ' AS bigint)'" in text
    section = _pg_queries()
    assert ":query_limit_clause" in section
    assert "calls >= CAST(:'min_executions' AS bigint)" in section
    assert "-v query_limit=" in text and "-v min_executions=" in text


def test_mysql_takes_both_settings_as_session_variables():
    text = _script("mysql")
    # A value that is not a positive whole number falls back to the default (the
    # client cannot stop a script on a condition), never to 0 or a wrapped negative;
    # the metadata records the values the run used.
    whole = "CAST(@min_executions AS CHAR) REGEXP '^[1-9][0-9]*$'"
    assert f"SET @min_executions = IF({whole}, CAST(@min_executions AS UNSIGNED), 10);" in text
    cap = "CAST(@query_limit AS CHAR) REGEXP '^[1-9][0-9]*$'"
    assert f"SET @query_limit = IF({cap}, CAST(@query_limit AS UNSIGNED), NULL);" in text
    # The statements selected, so the ingest can tell a list the server's
    # max_allowed_packet turned into NULL from an empty one.
    assert "INTO @query_rows" in text
    assert "JSON_SET(@metadata, '$.query_capture.statements', @query_rows," in text
    usage = _between(text, "-- Usage:", "\n--\n")
    assert "mysql -N --max-allowed-packet=1G" in usage
    section = _mysql_queries()
    assert "COUNT_STAR >= @min_executions" in section
    assert "ROW_NUMBER() OVER (ORDER BY SUM_TIMER_WAIT DESC, DIGEST)" in section
    assert "@query_limit IS NULL OR ranked.time_rank <= @query_limit" in section
    assert '--init-command="SET @query_limit=' in text


def test_sqlserver_declares_both_settings_with_their_defaults():
    text = _script("sqlserver")
    assert "DECLARE @query_limit INT = NULL;" in text
    assert "DECLARE @min_executions INT = 10;" in text
    # A setting that is not a positive whole number stops the script with a message.
    check = _between(text, "DECLARE @min_executions INT = 10;", "DECLARE @metadata")
    assert "IF @min_executions IS NULL OR @min_executions < 1" in check
    assert "IF @query_limit IS NOT NULL AND @query_limit < 1" in check
    # The third is the sampling interval's (#1908).
    assert check.count("RAISERROR(") == 3 and check.count("RETURN;") == 3
    section = _sqlserver_queries()
    assert "HAVING SUM(v.execution_count) >= @min_executions" in section
    assert "@query_limit IS NULL OR g.time_rank <= @query_limit" in section


def test_oracle_takes_both_settings_as_optional_positional_arguments():
    text = _script("oracle")
    head = text[: text.index("SET DEFINE OFF")]
    assert "COLUMN 1 NEW_VALUE 1 NOPRINT" in head and "COLUMN 2 NEW_VALUE 2 NOPRINT" in head
    assert "VARIABLE query_limit NUMBER" in head and "VARIABLE min_executions NUMBER" in head
    # "none" (the shell drops an empty "" argument) and an omitted cap are both no cap.
    assert "IF '&1' IS NULL OR LOWER('&1') = 'none' THEN" in head
    assert ":query_limit := TO_NUMBER('&1');" in head
    assert ":min_executions := NVL(TO_NUMBER('&2'), 10);" in head
    assert "NULLIF(" not in head
    # A setting that fails (20k) or is not a positive whole number stops the script.
    settings = _between(head, "WHENEVER SQLERROR EXIT FAILURE", "WHENEVER SQLERROR CONTINUE")
    # The third is the sampling interval's (#1908).
    assert settings.count("RAISE_APPLICATION_ERROR(") == 3
    assert "@collect-oracle.sql none 5" in text
    section = _oracle_queries()
    assert "HAVING SUM(v.executions) >= :min_executions" in section
    assert ":query_limit IS NULL OR g.time_rank <= :query_limit" in section
    # The total-elapsed denominator uses the same floor.
    declare = _between(text, "-- Pre-compute total elapsed", '"queries": [')
    assert "HAVING SUM(EXECUTIONS) >= :min_executions" in declare


@pytest.mark.parametrize(
    ("engine", "needle"),
    [
        ("postgresql", "'query_capture', json_build_object("),
        ("mysql", "'query_capture', JSON_OBJECT('query_limit', @query_limit"),
        ("sqlserver", "AS query_capture"),
        ("oracle", "'query_capture' VALUE JSON_OBJECT("),
    ],
)
def test_each_script_records_its_settings_in_the_metadata(engine, needle):
    assert needle in _script(engine)


# -- the statement queries parse, with the settings filled in either way ---------------


def _pg_sql(capped: bool) -> str:
    clause = "LIMIT CAST('500' AS bigint)" if capped else ""
    body = _pg_queries().removeprefix("queries_data AS (")
    body = body[: body.rindex(")")]
    return body.replace(":query_limit_clause", clause).replace(":'min_executions'", "'10'")


def _mysql_sql(capped: bool) -> str:
    sql = _mysql_queries().replace("INTO @queries ", "") + ") AS q"
    return (
        sql.replace("@query_limit", "500" if capped else "NULL")
        .replace("@min_executions", "10")
        .replace("@db", "'app'")
    )


def _sqlserver_sql(capped: bool) -> str:
    sql = _sqlserver_queries().removeprefix("SET @queries = (")
    return sql.replace("@query_limit", "500" if capped else "NULL").replace("@min_executions", "10")


def _oracle_sql(capped: bool) -> str:
    sql = _oracle_queries().removeprefix("FOR rec IN (")
    sql = sql.replace(":query_limit", "500" if capped else "NULL")
    sql = sql.replace(":min_executions", "10").replace("v_owner", "'APP'")
    return sql.replace("v_total_elapsed", "1").replace("RETURNING CLOB", "")


PARSE = {
    "postgresql": (_pg_sql, "postgres"),
    "mysql": (_mysql_sql, "mysql"),
    "sqlserver": (_sqlserver_sql, "tsql"),
    "oracle": (_oracle_sql, "oracle"),
}


@pytest.mark.parametrize("capped", [False, True], ids=["no cap", "cap"])
@pytest.mark.parametrize("engine", list(PARSE))
def test_the_statement_query_parses_in_its_dialect(engine, capped):
    build, dialect = PARSE[engine]
    sql = build(capped)
    parsed = sqlglot.parse_one(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    assert parsed is not None
    rendered = parsed.sql(dialect=dialect).upper()
    if not capped:
        assert not FIXED_CAPS.search(rendered)
    # The cap filled in is 500, as a whole number (a fixed 5000 is not it).
    has_cap = re.search(r"(?<![\d.])500(?![\d.])", rendered) is not None
    assert has_cap is capped


def _selects(tree: sqlglot.exp.Expression) -> list[sqlglot.exp.Select]:
    return list(tree.find_all(sqlglot.exp.Select))


@pytest.mark.parametrize("engine", ["sqlserver", "oracle"])
def test_the_floor_and_the_rank_are_on_the_grouped_query(engine):
    # The parse alone accepts HAVING before GROUP BY (tsql), so the tree is checked:
    # the floor is the grouped query's HAVING, and the rank that the cap filters is
    # computed on that same query, so it counts only shapes that pass the floor.
    build, dialect = PARSE[engine]
    tree = sqlglot.parse_one(build(True), read=dialect)
    grouped = [s for s in _selects(tree) if s.args.get("having") is not None]
    assert len(grouped) == 1
    query = grouped[0]
    assert query.args.get("group") is not None
    assert ">= 10" in query.args["having"].sql(dialect=dialect)
    ranks = [
        w
        for w in query.find_all(sqlglot.exp.Window)
        if isinstance(w.this, sqlglot.exp.RowNumber) and w.parent_select is query
    ]
    assert len(ranks) == 1
    outer = query.parent_select
    assert outer is not None and "time_rank <= 500" in outer.args["where"].sql(dialect=dialect)


@pytest.mark.parametrize("engine", ["mysql"])
def test_the_mysql_rank_counts_only_statements_that_pass_the_floor(engine):
    build, dialect = PARSE[engine]
    tree = sqlglot.parse_one(build(True), read=dialect)
    ranked = [
        s
        for s in _selects(tree)
        if any(isinstance(w.this, sqlglot.exp.RowNumber) for w in s.find_all(sqlglot.exp.Window))
        and s.args.get("where") is not None
        and "COUNT_STAR >= 10" in s.args["where"].sql(dialect=dialect)
    ]
    assert len(ranked) == 1
    outer = ranked[0].parent_select
    assert outer is not None and "time_rank <= 500" in outer.args["where"].sql(dialect=dialect)


def test_the_postgresql_floor_is_on_the_statement_query():
    tree = sqlglot.parse_one(_pg_sql(True), read="postgres")
    inner = [s for s in _selects(tree) if s.args.get("limit") is not None]
    assert len(inner) == 1
    assert "calls >= CAST('10' AS BIGINT)" in inner[0].args["where"].sql(dialect="postgres")
