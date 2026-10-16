"""Live and offline collections read the same query text the same way (#795).

Every live collector (MySQL, MariaDB, PostgreSQL, SQL Server, Oracle, and the
two RDS Performance Insights / Database Insights call sites) reads a query's
``tables_accessed``, ``has_joins``, ``join_count``, ``has_aggregations`` and
``has_subqueries`` through the SQL facade (``src/tools/sql/facade.py``), as
the offline parser does. Each case below sends one query text through a live
collector (its SQL client stubbed) and through the offline parser, and checks
that the facts match: the four flags exactly, and the tables once both
spellings are resolved to the collection's table ids the way the pipeline
resolves them (``TableNameResolver``). The cases are the regex misreads the
shadow comparison found (#779, #796, #777) and the SQL Server targets the
live regex missed (#742 item 1).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.agents.referee.table_resolution import TableNameResolver
from src.tools.aws import rds_tools
from src.tools.database import live_sql
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import _transform_queries
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

DB = "db"
FACTS = ("has_joins", "join_count", "has_aggregations", "has_subqueries")
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA"  # pragma: allowlist secret  # nosec B105 -- test fixture ARN, not a credential


def _collector(cls):
    return cls(ssm=MagicMock(), host="h", port=1, database=DB, secret_arn=SECRET)


def _live_mysql(sql: str) -> dict:
    c = _collector(MySQLRemoteCollector)
    c.get_version = lambda: "8.0.36"  # type: ignore[method-assign]
    c._query = lambda _q: [{"digest": "d", "query_text": sql, "execution_count": 1}]  # type: ignore[method-assign]
    return dict(c.collect_query_patterns()[0])


def _live_mariadb(sql: str) -> dict:
    c = _collector(MariaDBRemoteCollector)
    c.get_version = lambda: "10.11.6-MariaDB"  # type: ignore[method-assign]
    c._query = lambda _q: [{"digest": "d", "query_text": sql, "execution_count": 1}]  # type: ignore[method-assign]
    return dict(c.collect_query_patterns()[0])


def _live_postgres(sql: str) -> dict:
    c = _collector(PostgreSQLRemoteCollector)
    c._query = lambda _q: [{"queryid": 1, "query": sql, "calls": 1}]  # type: ignore[method-assign]
    return dict(c.collect_query_patterns()[0])


def _live_sqlserver(sql: str) -> dict:
    c = _collector(SQLServerRemoteCollector)
    c._query = lambda _q: [{"query_id": "q", "query_text": sql, "execution_count": 1}]  # type: ignore[method-assign]
    return dict(c.collect_query_patterns()[0])


def _live_oracle(sql: str) -> dict:
    c = _collector(OracleRemoteCollector)
    c.get_current_schema = lambda: "APP"  # type: ignore[method-assign]
    c._query = lambda _q: [{"query_id": "q", "query_text": sql, "execution_count": 1}]  # type: ignore[method-assign]
    return dict(c.collect_query_patterns()[0])


def _live_rds_pi(engine: str):
    def run(sql: str) -> dict:
        pi = MagicMock()
        now = datetime.now(UTC)
        top = {
            "AlignedStartTime": now,
            "Keys": [{"Dimensions": {"db.sql.id": "S1", "db.sql.statement": sql}, "Total": 1.0}],
        }
        pi.describe_dimension_keys.side_effect = [top, {"Keys": []}]
        pi.get_dimension_key_details.return_value = {"Dimensions": []}
        cred_mgr = MagicMock()
        cred_mgr.client.return_value = pi
        result = rds_tools.get_performance_insights_queries(cred_mgr, "db-X", source_engine=engine)
        assert result["available"], result
        return dict(result["query_patterns"][0])

    return run


def _live_rds_di(engine: str):
    def run(sql: str) -> dict:
        logs = MagicMock()
        logs.start_query.return_value = {"queryId": "Q"}
        logs.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                [
                    {"field": "sql.id", "value": "S1"},
                    {"field": "query_text", "value": sql},
                    {"field": "avg_calls_per_sec", "value": "1"},
                ]
            ],
        }
        cred_mgr = MagicMock()
        cred_mgr.client.return_value = logs
        result = rds_tools.get_database_insights(cred_mgr, "db-X", source_engine=engine)
        assert result["available"], result
        return dict(result["query_patterns"][0])

    return run


LIVE = {
    "mysql": [_live_mysql, _live_rds_pi("mysql"), _live_rds_di("mysql")],
    "mariadb": [_live_mariadb],
    "postgresql": [_live_postgres, _live_rds_pi("postgresql"), _live_rds_di("postgresql")],
    "sqlserver": [_live_sqlserver, _live_rds_pi("sqlserver"), _live_rds_di("sqlserver")],
    "oracle": [_live_oracle, _live_rds_pi("oracle"), _live_rds_di("oracle")],
}

# (engine, sql, the collection's tables, the tables both paths must resolve to)
CASES = [
    # MySQL / MariaDB
    (
        "mysql",
        "SELECT * FROM wp_posts p\nJOIN wp_users u ON u.ID = p.post_author",
        {"wp_posts", "wp_users"},
        ["db.wp_posts", "db.wp_users"],
    ),
    (
        "mysql",
        "SELECT `count` FROM wp_options WHERE option_name = ?",
        {"wp_options"},
        ["db.wp_options"],
    ),
    (
        "mysql",
        "SELECT * FROM wp_posts, wp_postmeta WHERE wp_posts.ID = wp_postmeta.post_id",
        {"wp_posts", "wp_postmeta"},
        ["db.wp_posts", "db.wp_postmeta"],
    ),
    (
        "mysql",
        "/* join: select from wp_users */ SELECT option_value FROM wp_options WHERE option_name = ?",
        {"wp_options", "wp_users"},
        ["db.wp_options"],
    ),
    ("mysql", "SELECT 1 FROM DUAL", {"wp_options"}, []),
    ("mysql", "SELECT * FROM db.wp_posts WHERE ID = ?", {"wp_posts"}, ["db.wp_posts"]),
    (
        "mysql",
        "INSERT INTO wp_options (option_name) VALUES (?) ON DUPLICATE KEY UPDATE option_value = ?",
        {"wp_options"},
        ["db.wp_options"],
    ),
    (
        "mysql",
        "LOAD DATA LOCAL INFILE ? INTO TABLE wp_posts FIELDS TERMINATED BY ?",
        {"wp_posts"},
        ["db.wp_posts"],
    ),
    (
        "mysql",
        "SELECT COUNT(*) FROM wp_comments GROUP BY comment_post_ID",
        {"wp_comments"},
        ["db.wp_comments"],
    ),
    (
        "mariadb",
        "SELECT * FROM wp_posts p JOIN wp_users u ON u.ID = p.post_author WHERE p.ID IN (SELECT post_id FROM wp_postmeta)",
        {"wp_posts", "wp_users", "wp_postmeta"},
        ["db.wp_posts", "db.wp_users", "db.wp_postmeta"],
    ),
    ("mariadb", "SELECT `count` FROM wp_options", {"wp_options"}, ["db.wp_options"]),
    # PostgreSQL
    (
        "postgresql",
        "SELECT * FROM topics WHERE deleted_at IS DISTINCT FROM $1",
        {"topics"},
        ["db.topics"],
    ),
    (
        "postgresql",
        "DELETE FROM posts USING topics WHERE posts.topic_id = topics.id",
        {"posts", "topics"},
        ["db.posts", "db.topics"],
    ),
    ("postgresql", "COPY posts FROM STDIN WITH CSV", {"posts"}, ["db.posts"]),
    (
        "postgresql",
        "SELECT user_selectable FROM badge_groupings",
        {"badge_groupings"},
        ["db.badge_groupings"],
    ),
    (
        "postgresql",
        "SELECT * FROM public.topics t JOIN public.posts p ON p.topic_id = t.id",
        {"topics", "posts"},
        ["db.topics", "db.posts"],
    ),
    ("postgresql", "SELECT count(*) FROM posts WHERE user_id = $1", {"posts"}, ["db.posts"]),
    (
        "postgresql",
        "INSERT INTO posts (id) VALUES ($1) ON CONFLICT (id) DO UPDATE SET raw = $2",
        {"posts"},
        ["db.posts"],
    ),
    # SQL Server (#742 item 1: INTO / UPDATE / MERGE targets)
    (
        "sqlserver",
        "UPDATE [dbo].[Orders] SET [Status] = @p0 WHERE [OrderID] = @p1",
        {"Orders"},
        ["db.Orders"],
    ),
    (
        "sqlserver",
        "INSERT INTO [Audit].[EventLog] ([EventType]) VALUES (@p0)",
        {"EventLog"},
        ["db.EventLog"],
    ),
    (
        "sqlserver",
        "MERGE INTO dbo.Inventory AS t USING dbo.Staging AS s ON t.Id = s.Id WHEN MATCHED THEN UPDATE SET t.Qty = s.Qty;",
        {"Inventory", "Staging"},
        ["db.Inventory", "db.Staging"],
    ),
    (
        "sqlserver",
        "DELETE TOP (100) FROM [dbo].[Sessions] WHERE [Expires] < @now",
        {"Sessions"},
        ["db.Sessions"],
    ),
    (
        "sqlserver",
        "SELECT [EventType] FROM [EventLog] WHERE [EventTime] >= @From AND [EventTime] < @To",
        {"EventLog"},
        ["db.EventLog"],
    ),
    ("sqlserver", "SELECT * FROM sys.tables WHERE schema_id = 1", {"Orders"}, []),
    # SQL Server temp tables (#tmp, ##tmp) and table variables (@t) are
    # query-local, never a collected table, on both paths.
    (
        "sqlserver",
        "SELECT o.* FROM #tmp t JOIN dbo.Orders o ON o.Id = t.Id",
        {"Orders", "tmp"},
        ["db.Orders"],
    ),
    (
        "sqlserver",
        "INSERT INTO ##stage (Id) SELECT Id FROM [dbo].[Orders]",
        {"Orders", "stage"},
        ["db.Orders"],
    ),
    ("sqlserver", "SELECT * INTO #t FROM Orders", {"Orders", "t"}, ["db.Orders"]),
    ("sqlserver", "SELECT * FROM [#tmp] WHERE Id = @p0", {"tmp"}, []),
    ("sqlserver", "SELECT * FROM @t WHERE Id = @p0", {"Orders", "t"}, []),
    ("sqlserver", "UPDATE @t SET Qty = o.Qty FROM Orders o", {"Orders", "t"}, ["db.Orders"]),
    # Oracle
    (
        "oracle",
        "SELECT * FROM orders o JOIN customers c ON o.cid = c.id",
        {"orders", "customers"},
        ["db.orders", "db.customers"],
    ),
    ("oracle", "SELECT sysdate FROM dual", {"orders"}, []),
    (
        "oracle",
        "SELECT * FROM hr.employees e, hr.departments d WHERE e.dept_id = d.id",
        {"employees", "departments"},
        ["db.employees", "db.departments"],
    ),
    ("oracle", "UPDATE orders SET status = :1 WHERE id = :2", {"orders"}, ["db.orders"]),
]


def _offline(engine: str, sql: str, known: set[str]) -> dict:
    rows = [{"query_text": sql, "digest": "d", "execution_count": 1}]
    return _transform_queries(rows, DB, known, source_engine=engine)[0]


def _resolved(tables: list[str] | None, known: set[str]) -> list[str]:
    """The collection's table ids for ``tables``, as the pipeline resolves them;
    no table (the collector's ``[]`` or ``["unknown"]``) is no id."""
    resolver = TableNameResolver.from_known_ids({f"{DB}.{t}" for t in known})
    assert resolver is not None
    names = [t for t in (tables or []) if t != "unknown"]
    return [resolver.resolve(t) or t for t in names]


def _case_id(case) -> str:
    return f"{case[0]}:{case[1][:40]}"


@pytest.mark.parametrize(
    "engine,sql,known,expected,live",
    [(e, s, k, x, live) for e, s, k, x in CASES for live in LIVE[e]],
    ids=[f"{_case_id(c)}:{live.__name__}" for c in CASES for live in LIVE[c[0]]],
)
def test_live_and_offline_read_the_same_facts(engine, sql, known, expected, live):
    live_row = live(sql)
    offline_row = _offline(engine, sql, known)

    assert _resolved(live_row["tables_accessed"], known) == expected
    assert _resolved(offline_row["tables_accessed"], known) == expected
    assert {f: live_row.get(f) for f in FACTS} == {f: offline_row[f] for f in FACTS}


@pytest.mark.parametrize(
    "engine,sql,facts",
    [
        # #796: a JOIN after a newline, a column named count, a comma join,
        # a join word in a comment, a column whose name holds "select".
        ("mysql", "SELECT * FROM a\nJOIN b ON a.id = b.id", (True, 1, False, False)),
        ("mysql", "SELECT `count` FROM a", (False, 0, False, False)),
        ("mysql", "SELECT * FROM a, b WHERE a.id = b.id", (True, 1, False, False)),
        ("mysql", "/* join select */ SELECT x FROM a", (False, 0, False, False)),
        ("postgresql", "SELECT user_selectable FROM a", (False, 0, False, False)),
        ("sqlserver", "SELECT COUNT(*) FROM a GROUP BY b", (False, 0, True, False)),
    ],
)
def test_the_live_facts_come_from_the_syntax_tree(engine, sql, facts):
    row = LIVE[engine][0](sql)
    assert tuple(row[f] for f in FACTS) == facts


def test_a_regex_fallback_is_logged_once_per_collection(caplog):
    c = _collector(PostgreSQLRemoteCollector)
    c._query = lambda _q: [  # type: ignore[method-assign]
        {"queryid": 1, "query": "REFRESH MATERIALIZED VIEW leaderboard_cache", "calls": 1},
        {"queryid": 2, "query": "SELECT * FROM topics", "calls": 1},
    ]
    with caplog.at_level(logging.INFO, logger="src.tools.database.live_sql"):
        patterns = c.collect_query_patterns()

    assert patterns[0]["tables_accessed"] == ["leaderboard_cache"]
    fallback_lines = [r.getMessage() for r in caplog.records if "regex fallback" in r.getMessage()]
    assert fallback_lines == [
        "postgres_tools: table references: 1/2 queries used the regex fallback (command=1)"
    ]


def test_a_facade_error_falls_back_to_the_regex_and_warns(monkeypatch, caplog):
    from src.tools.sql import facade

    def boom(sql, engine):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(facade, "read_sql", boom)
    with caplog.at_level(logging.WARNING, logger="src.tools.database.live_sql"):
        row = _live_mysql("SELECT * FROM wp_posts JOIN wp_users ON 1=1")

    assert row["tables_accessed"] == ["wp_posts", "wp_users"]
    assert row["has_joins"] is True
    assert any("sql facade failed" in r.getMessage() for r in caplog.records)


def test_a_facade_error_logs_its_stack_trace_once_per_collection(monkeypatch, caplog):
    from src.tools.sql import facade

    def boom(sql, engine):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(facade, "read_sql", boom)
    c = _collector(PostgreSQLRemoteCollector)
    c._query = lambda _q: [  # type: ignore[method-assign]
        {"queryid": i, "query": f"SELECT * FROM t{i}", "calls": 1} for i in range(3)
    ]
    with caplog.at_level(logging.INFO, logger="src.tools.database.live_sql"):
        patterns = c.collect_query_patterns()

    assert [p["tables_accessed"] for p in patterns] == [["t0"], ["t1"], ["t2"]]
    traces = [r for r in caplog.records if r.exc_info]
    assert len(traces) == 1
    assert [r.getMessage() for r in caplog.records if "regex fallback" in r.getMessage()] == [
        "postgres_tools: table references: 3/3 queries used the regex fallback (facade_error=3)"
    ]


@pytest.mark.parametrize(
    "engine,sql,names",
    [
        # Each module's own name spelling stays: MySQL and SQL Server keep a
        # qualifier as written, PostgreSQL drops a ``public.`` one, Oracle
        # lower-cases; system catalogs are skipped everywhere.
        (
            "mysql",
            "SELECT * FROM wordpress.wp_posts JOIN wp_users",
            ["wordpress.wp_posts", "wp_users"],
        ),
        ("mysql", "SELECT * FROM information_schema.tables", ["unknown"]),
        (
            "postgresql",
            "SELECT * FROM public.topics JOIN audit.events ON 1=1",
            ["topics", "audit.events"],
        ),
        (
            "sqlserver",
            "SELECT * FROM [dbo].[Orders] o JOIN [Customers] c ON o.cid = c.id",
            ["dbo.Orders", "Customers"],
        ),
        ("oracle", 'SELECT * FROM HR."Employees"', ["hr.employees"]),
    ],
)
def test_each_live_module_keeps_its_name_spelling(engine, sql, names):
    assert LIVE[engine][0](sql)["tables_accessed"] == names


def test_the_shared_reader_is_what_every_module_uses():
    # One reader, one dialect per module: the per-module regex extractors are gone.
    from src.tools.database import mysql_tools, oracle_tools, postgres_tools, sqlserver_tools

    for module in (mysql_tools, postgres_tools, sqlserver_tools, oracle_tools, rds_tools):
        assert not hasattr(module, "_TABLE_RE"), module.__name__
    assert live_sql.extract_tables("SELECT * FROM a JOIN b ON 1=1", "mysql") == ["a", "b"]


@pytest.mark.parametrize(
    "sql,known,expected",
    [
        # #783 / #797: a sequence-state read of a relation the schema doesn't
        # hold has no source table on both paths, so both count it as utility.
        ("SELECT last_value FROM public.badges_id_seq", {"badges"}, ["unknown"]),
        ('SELECT "last_value" FROM "public"."badges_id_seq"', {"badges"}, ["unknown"]),
        # A real table named like a sequence stays a table.
        ("SELECT last_value FROM invoice_seq", {"invoice_seq"}, ["db.invoice_seq"]),
        ("SELECT * FROM badges", {"badges"}, ["db.badges"]),
    ],
)
def test_a_live_postgres_sequence_read_is_unknown_like_offline(sql, known, expected):
    from src.agents.collector import postgres_collector
    from src.agents.referee.utility_statements import is_utility_statement

    queries = postgres_collector._build_queries([_live_postgres(sql)])
    postgres_collector._drop_unlisted_sequence_reads(queries, known)
    live_tables = queries.query_patterns[0].tables_accessed
    offline_row = _offline("postgresql", sql, known)

    assert _resolved(live_tables, known) == _resolved(offline_row["tables_accessed"], known)
    assert _resolved(live_tables, known) == [t for t in expected if t != "unknown"]
    assert is_utility_statement(sql, live_tables) == is_utility_statement(
        sql, offline_row["tables_accessed"]
    )
