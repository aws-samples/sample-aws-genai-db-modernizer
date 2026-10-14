"""CTE names defined by WITH are query-local, never tables (#682)."""

import pytest

from src.tools.aws import rds_tools
from src.tools.database import mysql_tools, oracle_tools, postgres_tools, sqlserver_tools
from src.tools.database.offline_parser import _transform_queries
from src.tools.database.sql_cte import drop_cte_references

EXTRACTORS = {
    "mysql": mysql_tools._extract_tables,
    "postgres": postgres_tools._extract_tables,
    "rds": rds_tools._extract_tables,
    "oracle": oracle_tools._extract_tables,
    "sqlserver": sqlserver_tools._extract_tables,
}


def _tables(extract, sql: str) -> set[str]:
    return {t.lower() for t in extract(sql)} - {"unknown"}


def _offline(sql: str, known: set[str] | None = None) -> list[str]:
    out = _transform_queries(
        [{"query_text": sql, "digest": "d", "execution_count": 1}], "db", known or set()
    )
    tables: list[str] = out[0]["tables_accessed"]
    return tables


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("WITH a AS (SELECT 1 FROM r) SELECT * FROM a", {"r"}),
        (
            "WITH RECURSIVE t(n) AS (SELECT 1 FROM r UNION ALL SELECT n FROM t) SELECT * FROM t",
            {"r"},
        ),
        ("WITH t(a,b) AS (SELECT 1,2 FROM r) SELECT * FROM t", {"r"}),
        ('WITH "Q" AS (SELECT 1 FROM r) SELECT * FROM "Q"', {"r"}),
        ("WITH `q` AS (SELECT 1 FROM r) SELECT * FROM `q`", {"r"}),
        (
            "WITH a AS (SELECT (1) x FROM r), b AS (SELECT * FROM a) SELECT * FROM b JOIN s",
            {"r", "s"},
        ),
        (
            "WITH a AS MATERIALIZED (SELECT 1 FROM r), b AS NOT MATERIALIZED (SELECT 2) SELECT * FROM a, b",
            {"r"},
        ),
        ("SELECT * FROM orders WITH (NOLOCK)", {"orders"}),
        ("SELECT * FROM (WITH s AS (SELECT 1 FROM r) SELECT * FROM s) x", {"r"}),
        # Tree-style recursion without the RECURSIVE keyword (T-SQL).
        (
            "WITH Tree AS (SELECT 1 FROM r UNION ALL SELECT 2 FROM r JOIN Tree t ON 1=1) SELECT * FROM Tree",
            {"r"},
        ),
        # XMLNAMESPACES is not a CTE, and the CTE after it is.
        ("WITH XMLNAMESPACES ('u' AS ns), c AS (SELECT 1 FROM r) SELECT * FROM c", {"r"}),
        # A ')' inside a literal must not end a CTE body early.
        ("WITH a AS (SELECT ')' FROM r), b AS (SELECT 1 FROM s) SELECT * FROM b", {"r", "s"}),
    ],
)
@pytest.mark.parametrize("name", EXTRACTORS)
def test_cte_names_are_not_tables(name, sql, expected):
    assert _tables(EXTRACTORS[name], sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        # A non-recursive CTE reading the real table of the same name.
        "WITH orders AS (SELECT * FROM orders WHERE active = 1) SELECT * FROM orders",
        # CTE scope: a subquery's WITH must not hide a real table outside it.
        "SELECT * FROM users u WHERE u.id IN (WITH users AS (SELECT 1 AS id) SELECT id FROM users)",
        # WITH text in a string literal or a comment is not a CTE.
        "SELECT 'with orders as (' FROM orders",
        "SELECT 1 FROM orders -- with orders as (select 1)\n",
        "SELECT /* WITH orders AS (SELECT 1) */ * FROM orders",
    ],
)
@pytest.mark.parametrize("name", EXTRACTORS)
def test_real_tables_survive(name, sql):
    assert _tables(EXTRACTORS[name], sql) & {"orders", "users"}


def test_scope_cases_in_detail():
    sql = (
        "SELECT * FROM users u WHERE u.id IN (WITH users AS (SELECT 1 AS id) SELECT id FROM users)"
    )
    out = drop_cte_references(sql)
    assert out.count("FROM users") == 1  # the outer reference stays, the inner one is blanked
    assert out.index("FROM users") < out.index("WITH")


def test_schema_qualified_name_is_not_a_cte():
    sql = "WITH foo AS (SELECT 1 FROM r) SELECT * FROM dbo.foo"
    assert "dbo.foo" in drop_cte_references(sql)
    sql = "WITH foo AS (SELECT 1 FROM r) SELECT * FROM public.foo"
    assert "public.foo" in drop_cte_references(sql)


OFFLINE_SQL = (
    "WITH [Tree] AS (SELECT c.Id FROM trailgear.ProductCategory c "
    "UNION ALL SELECT c.Id FROM trailgear.ProductCategory c JOIN [Tree] t ON c.Parent = t.Id), "
    "other(x) AS (SELECT 1 FROM real_tbl) "
    "SELECT * FROM [Tree] JOIN other o ON 1=1 JOIN customers u ON 1=1"
)


def test_offline_parser_excludes_ctes():
    out = _offline(OFFLINE_SQL, {"customers", "real_tbl"})
    assert out == ["trailgear.ProductCategory", "db.real_tbl", "db.customers"]


def test_offline_parser_keeps_a_real_table_named_like_its_cte():
    assert _offline("WITH orders AS (SELECT * FROM orders) SELECT * FROM orders", {"orders"}) == [
        "db.orders"
    ]
