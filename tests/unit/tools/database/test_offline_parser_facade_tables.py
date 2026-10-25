"""The offline parser reads table references through the sqlglot facade (#553).

``parse_offline_collection`` used to take ``tables_accessed`` from the regex
scan. It now asks :func:`src.tools.sql.facade.table_references` (dialect from
the collection's source engine), and the regex is only the facade's own
fallback. These tests pin the regex misreads the shadow comparison found on
the samples (#779), the fallback, and that every entry point that goes through
the parser (local scripts, the S3 offline collector, the AWS Transform
orchestrator) gets the same answer.
"""

from __future__ import annotations

import logging

import pytest

import src.tools.sql.facade as facade
from src.tools.database.offline_parser import parse_offline_collection

TABLES = ("orders", "users", "posts", "topics", "bookmarks", "wp_posts")


def _collection(queries: list[str], engine: str = "postgresql") -> dict:
    return {
        "metadata": {"database_name": "shop", "engine": engine},
        "tables": [{"table_name": n} for n in TABLES],
        "queries": [
            {"query_text": q, "digest": f"q{i}", "execution_count": 1}
            for i, q in enumerate(queries)
        ],
    }


def _tables(queries: list[str], engine: str = "postgresql") -> list[list[str]]:
    parsed = parse_offline_collection(_collection(queries, engine))
    return [q["tables_accessed"] for q in parsed["queries"]]


@pytest.mark.parametrize(
    "sql,expected",
    [
        # IS DISTINCT FROM is a comparison, not a FROM clause (22 discourse queries).
        (
            "SELECT * FROM posts p WHERE p.topic_id IS DISTINCT FROM topics.id",
            ["shop.posts"],
        ),
        # A word in a comment is not a table.
        (
            "SELECT id FROM users -- users that are allowed to join chat",
            ["shop.users"],
        ),
        # DELETE ... USING names real tables.
        (
            "DELETE FROM bookmarks b USING topics t, posts p WHERE b.topic_id = t.id",
            ["shop.bookmarks", "shop.topics", "shop.posts"],
        ),
        # Every table of a comma join, not only the first.
        (
            "SELECT * FROM posts, topics WHERE posts.topic_id = topics.id",
            ["shop.posts", "shop.topics"],
        ),
        # EXTRACT(x FROM ...) is a function argument.
        ("SELECT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) FROM orders", ["shop.orders"]),
    ],
)
def test_postgresql_regex_misreads_are_gone(sql, expected):
    assert _tables([sql]) == [expected]


def test_dual_is_not_a_table():
    sql = "INSERT INTO orders (id) SELECT ? FROM DUAL WHERE (SELECT ? FROM DUAL) IS NULL"
    assert _tables([sql], "mysql") == [["shop.orders"]]


def test_copy_from_stdin_resolves_to_the_table():
    # #777: the regex read COPY ... FROM STDIN as a table named STDIN.
    assert _tables(["COPY wp_posts FROM STDIN"]) == [["shop.wp_posts"]]
    assert _tables(["COPY public.wp_posts (id, title) FROM STDIN WITH CSV"]) == [["shop.wp_posts"]]


def _parsed(queries: list[str], engine: str) -> list[dict]:
    queries_out: list[dict] = parse_offline_collection(_collection(queries, engine))["queries"]
    return queries_out


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("COPY wp_posts FROM STDIN", "postgresql"),
        ("COPY public.wp_posts (id, title) FROM STDIN WITH CSV", "postgresql"),
        ("LOAD DATA LOCAL INFILE 'x.csv' INTO TABLE wp_posts", "mysql"),
        ("LOAD DATA INFILE ? REPLACE INTO TABLE shop.wp_posts FIELDS TERMINATED BY ?", "mysql"),
    ],
)
def test_a_bulk_load_is_a_write_to_its_table_on_both_dialects(sql, engine):
    # #777: LOAD DATA resolved to a table named TABLE, and both forms were
    # typed OTHER. They load rows into the table, so they are writes to it.
    (query,) = _parsed([sql], engine)
    assert query["tables_accessed"] == ["shop.wp_posts"]
    assert query["query_type"] == "INSERT"


def test_a_copy_export_is_not_a_write():
    (query,) = _parsed(["COPY wp_posts TO STDOUT"], "postgresql")
    assert query["tables_accessed"] == ["shop.wp_posts"]
    assert query["query_type"] == "OTHER"


def test_a_statement_sqlglot_does_not_model_falls_back_to_the_regex():
    # REFRESH MATERIALIZED VIEW is a generic Command node in sqlglot.
    assert _tables(["REFRESH MATERIALIZED VIEW CONCURRENTLY orders"]) == [["shop.orders"]]


def test_no_table_is_unknown():
    assert _tables(["SELECT 1"]) == [["unknown"]]


@pytest.mark.parametrize(
    "engine,sql,expected",
    [
        # #483: a catalog table the statement reads is named, schema-qualified.
        (
            "postgresql",
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = $1",
            ["information_schema.schemata"],
        ),
        (
            "mysql",
            "SELECT column_default IS NOT NULL FROM information_schema.columns WHERE x = ?",
            ["information_schema.columns"],
        ),
        ("sqlserver", "SELECT name FROM sys.objects WHERE type = 'U'", ["sys.objects"]),
        (
            "sqlserver",
            "SELECT o.name FROM sys.objects o JOIN orders ON 1 = 1",
            ["sys.objects", "shop.orders"],
        ),
        # Maintenance statements: the table is named, as a collected table's id.
        ("sqlserver", "ALTER INDEX [IX_orders] ON [orders] REORGANIZE", ["shop.orders"]),
        ("sqlserver", "UPDATE STATISTICS [dbo].[orders]", ["shop.orders"]),
        ("mysql", "OPTIMIZE TABLE orders, users", ["shop.orders", "shop.users"]),
        ("mysql", "ALTER TABLE `orders` ADD INDEX i (id)", ["shop.orders"]),
        ("postgresql", "VACUUM ANALYZE public.orders", ["shop.orders"]),
        ("postgresql", "TRUNCATE orders", ["shop.orders"]),
        ("oracle", "TRUNCATE TABLE orders", ["shop.orders"]),
    ],
)
def test_a_statement_that_names_a_table_does_not_report_unknown(engine, sql, expected):
    assert _tables([sql], engine) == [expected]


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "engine,sql",
    [
        ("postgresql", "SET client_min_messages TO 'warning'"),
        ("postgresql", "SHOW search_path"),
        ("postgresql", "BEGIN"),
        ("postgresql", "VACUUM"),
        ("postgresql", "SELECT pg_get_serial_sequence($1, $2)"),
        ("postgresql", "CREATE EXTENSION IF NOT EXISTS hstore"),
        ("mysql", "SELECT FOUND_ROWS()"),
        ("sqlserver", "EXEC [dbo].[usp_PlaceOrder] @CustomerID = @CustomerID"),
        ("sqlserver", "SELECT NEXT VALUE FOR [dbo].[OrderNumberSeq] AS [N]"),
    ],
)
def test_a_statement_with_no_table_stays_unknown(engine, sql):
    assert _tables([sql], engine) == [["unknown"]]


@pytest.mark.passes_on_main
def test_an_unlisted_sequence_read_stays_unknown():
    # #783: a sequence is not a schema table.
    sql = "SELECT last_value FROM public.badges_id_seq"
    assert _tables([sql]) == [["unknown"]]


@pytest.mark.parametrize(
    "engine,expected",
    [
        ("postgresql", "postgresql"),
        ("mysql", "mysql"),
        ("sqlserver", "sqlserver"),
        ("oracle", "oracle"),
    ],
)
def test_the_facade_gets_the_collection_source_engine(monkeypatch, engine, expected):
    seen: list[str] = []
    real = facade.read_sql

    def spy(sql, source_engine):
        seen.append(source_engine)
        return real(sql, source_engine)

    monkeypatch.setattr(facade, "read_sql", spy)
    _tables(["SELECT * FROM orders"], engine)
    assert seen == [expected]


def test_tsql_dml_top_is_read_with_the_tsql_dialect():
    assert _tables(["DELETE TOP (100) FROM orders WHERE id < 5"], "sqlserver") == [["shop.orders"]]


def test_fallbacks_are_logged_once_per_collection(caplog):
    with caplog.at_level(logging.INFO, logger="src.tools.database.offline_parser"):
        _tables(["REFRESH MATERIALIZED VIEW orders", "SELECT * FROM users"])
    lines = [r.getMessage() for r in caplog.records if "regex fallback" in r.getMessage()]
    assert lines == [
        "offline_parser: table references: 1/2 queries used the regex fallback" " ({'command': 1})"
    ]


def test_a_failing_facade_falls_back_to_the_regex(monkeypatch, caplog):
    def boom(sql, engine):
        raise RuntimeError("facade bug")

    monkeypatch.setattr(facade, "read_sql", boom)
    with caplog.at_level(logging.INFO, logger="src.tools.database.offline_parser"):
        tables = _tables(["SELECT * FROM posts p WHERE p.x IS DISTINCT FROM topics.id"])
    # The regex's own answer, phantom included: the run still completes.
    assert tables == [["shop.posts", "topics.id"]]
    messages = [r.getMessage() for r in caplog.records]
    assert any("facade failed" in m for m in messages)
    assert (
        "offline_parser: table references: 1/1 queries used the regex fallback"
        " ({'facade_error': 1})"
    ) in messages
