# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A timestamp column is identified by its collected data type, as a DBA would (#935).

The maintainer decision on #935: a column is a timestamp when the schema says its
type is one (``timestamp``, ``datetime``, ``date`` and each dialect's equivalents),
whatever its name. The name list (``regex_facts.TIMESTAMP_COLUMN_NAMES``) is read
only when the collection carries no column types, and for a column whose table the
facts layer cannot tell (an unqualified name two joined tables both hold, a table
the collection does not list, a statement sqlglot can't read); the reading says
which columns fell back and why, so the analyses' traces can say so.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.agents.referee.triage import triage
from src.tools.analysis.dynamodb_analysis_tools import (
    analyze_dynamodb_use_cases,
    build_decision_trace,
)
from src.tools.analysis.opensearch_analysis_tools import analyze_opensearch_use_cases
from src.tools.database.offline_parser import detect_source_engine, parse_offline_collection
from src.tools.sql.facade import sql_facts
from src.tools.sql.query_facts import (
    ColumnTypes,
    is_timestamp_type,
    query_facts,
    read_timestamp_column,
)
from tests.unit.tools.sql.table_refs_snapshot import SAMPLES, load_sample

_REPO = Path(__file__).resolve().parents[4]


def _collection(tables: dict[str, dict[str, str | None]], engine: str = "mysql") -> dict:
    """A collector output with ``{table: {column: data_type}}`` (``None``: no type)."""
    return {
        "metadata": {"source_database": {"engine": engine}},
        "database_schema": {
            "tables": [
                {
                    "table_id": f"app.{name}",
                    "table_name": name,
                    "row_count": 1000,
                    "primary_key": ["id"],
                    "columns": [
                        {"column_name": column, "data_type": data_type}
                        for column, data_type in columns.items()
                    ],
                }
                for name, columns in tables.items()
            ]
        },
        "queries": {"query_patterns": []},
    }


def _types(tables: dict[str, dict[str, str | None]], engine: str = "mysql") -> ColumnTypes:
    types = ColumnTypes.from_collector(_collection(tables, engine))
    assert types is not None
    return types


def _reading(sql: str, types: ColumnTypes | None, engine: str = "mysql"):
    return query_facts(sql, engine).timestamp_column(types)


# ---------------------------------------------------------------------------
# Which collected types are a timestamp, per dialect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine,data_type,expected",
    [
        ("mysql", "datetime", True),
        ("mysql", "timestamp", True),
        ("mysql", "date", True),
        ("mysql", "datetime(6)", True),
        ("mysql", "time", False),
        ("mysql", "year", False),
        ("mysql", "varchar", False),
        ("mariadb", "datetime", True),
        ("postgresql", "timestamp", True),
        ("postgresql", "timestamptz", True),
        ("postgresql", "timestamp with time zone", True),
        ("postgresql", "timestamp without time zone", True),
        ("postgresql", "timestamp(3) with time zone", True),
        ("postgresql", "date", True),
        ("postgresql", "time without time zone", False),
        ("postgresql", "timetz", False),
        ("postgresql", "interval", False),
        ("postgresql", "int8", False),
        ("sqlserver", "datetime", True),
        ("sqlserver", "datetime2", True),
        ("sqlserver", "datetime2(7)", True),
        ("sqlserver", "smalldatetime", True),
        ("sqlserver", "datetimeoffset", True),
        ("sqlserver", "date", True),
        ("sqlserver", "timestamp", False),  # SQL Server's timestamp is rowversion
        ("sqlserver", "rowversion", False),
        ("sqlserver", "time", False),
        ("oracle", "DATE", True),  # an Oracle DATE holds a time of day too
        ("oracle", "TIMESTAMP(6)", True),
        ("oracle", "TIMESTAMP(6) WITH TIME ZONE", True),
        ("oracle", "TIMESTAMP WITH LOCAL TIME ZONE", True),
        ("oracle", "INTERVAL DAY(2) TO SECOND(6)", False),
        ("oracle", "VARCHAR2", False),
        ("", "datetime", True),
    ],
)
def test_each_dialects_timestamp_types(engine, data_type, expected):
    assert is_timestamp_type({"data_type": data_type}, engine) is expected


@pytest.mark.parametrize(
    "column,engine,expected",
    [
        ({"data_type": "custom_ts", "normalized_data_type": "timestamp"}, "postgresql", True),
        ({"data_type": "datetime", "normalized_data_type": "string"}, "mysql", False),
        ({"data_type": "x", "normalized_data_type": "date"}, "mysql", True),
        ({"data_type": "x", "normalized_data_type": "datetime"}, "mysql", True),
        # An offline SQL Server collection is normalized with MySQL's map, which reads
        # ``timestamp`` as a point in time: on SQL Server it is a row version.
        ({"data_type": "timestamp", "normalized_data_type": "timestamp"}, "sqlserver", False),
        ({"data_type": "", "normalized_data_type": None}, "mysql", False),
    ],
)
def test_the_normalized_type_decides_when_the_collector_set_one(column, engine, expected):
    assert is_timestamp_type(column, engine) is expected


def test_a_collection_with_no_column_types_has_none():
    untyped = _collection({"posts": {"id": None, "created_at": None}})
    assert ColumnTypes.from_collector(untyped) is None
    assert ColumnTypes.from_collector({"database_schema": {"tables": []}}) is None
    assert ColumnTypes.from_collector({}) is None


# ---------------------------------------------------------------------------
# A column's type decides, whatever its name
# ---------------------------------------------------------------------------


class TestTheTypeDecides:
    def test_a_timestamp_typed_column_outside_the_name_list_is_one(self):
        types = _types({"posts": {"id": "bigint", "posted": "datetime"}})
        reading = _reading("SELECT * FROM posts WHERE posted > ?", types)
        assert reading and reading.found
        assert reading.by_type == ("app.posts.posted",)
        assert reading.fallbacks == ()
        # With no column types, the name list does not hold ``posted``.
        assert not _reading("SELECT * FROM posts WHERE posted > ?", None)

    def test_a_listed_name_with_a_non_timestamp_type_is_not_one(self):
        types = _types({"posts": {"id": "bigint", "created_at": "varchar"}})
        assert not _reading("SELECT * FROM posts WHERE created_at = ?", types)
        assert _reading("SELECT * FROM posts WHERE created_at = ?", None)

    def test_an_alias_resolves_the_column_to_its_table(self):
        types = _types(
            {
                "posts": {"id": "bigint", "user_id": "bigint", "created": "datetime"},
                "users": {"id": "bigint", "created": "int"},
            }
        )
        join = "SELECT p.id FROM posts p JOIN users u ON u.id = p.user_id WHERE {} > ?"
        assert _reading(join.format("p.created"), types).by_type == ("app.posts.created",)
        assert not _reading(join.format("u.created"), types)

    def test_an_unqualified_column_belongs_to_the_one_joined_table_that_holds_it(self):
        types = _types(
            {
                "posts": {"id": "bigint", "user_id": "bigint", "posted": "timestamp"},
                "users": {"id": "bigint", "name": "varchar"},
            }
        )
        sql = "SELECT name FROM posts p JOIN users u ON u.id = p.user_id WHERE posted > ?"
        assert _reading(sql, types).by_type == ("app.posts.posted",)

    def test_a_table_name_qualifier_and_a_schema_qualified_table_resolve(self):
        types = _types({"posts": {"id": "bigint", "posted": "datetime"}})
        assert _reading("SELECT posts.posted FROM app.posts", types).by_type == (
            "app.posts.posted",
        )

    def test_an_insert_column_list_is_the_target_tables(self):
        types = _types({"events": {"id": "bigint", "happened": "datetime"}})
        sql = "INSERT INTO events (id, happened) VALUES (?, ?)"
        assert _reading(sql, types).by_type == ("app.events.happened",)

    def test_an_update_set_column_is_the_target_tables(self):
        types = _types({"posts": {"id": "bigint", "bumped": "timestamp"}})
        sql = "UPDATE posts SET bumped = ? WHERE id = ?"
        assert _reading(sql, types).by_type == ("app.posts.bumped",)

    def test_an_outer_column_in_a_subquery_resolves_to_the_outer_table(self):
        types = _types(
            {
                "posts": {"id": "bigint", "posted": "timestamp"},
                "votes": {"id": "bigint", "post_id": "bigint"},
            }
        )
        sql = "SELECT id FROM posts WHERE id IN (SELECT post_id FROM votes WHERE posted > ?)"
        assert _reading(sql, types).by_type == ("app.posts.posted",)

    def test_a_sql_server_update_through_its_from_alias(self):
        types = _types(
            {
                "Ticket": {"TicketID": "int", "ClosedDate": "datetime2"},
                "TicketMessage": {"MessageID": "int", "TicketID": "int"},
            },
            "sqlserver",
        )
        sql = (
            "UPDATE t SET t.[ClosedDate] = SYSUTCDATETIME() FROM [Ticket] t "
            "JOIN [TicketMessage] m ON m.[TicketID] = t.[TicketID] WHERE m.[MessageID] = @id"
        )
        assert _reading(sql, types, "sqlserver").by_type == ("app.Ticket.ClosedDate",)
        unqualified = sql.replace("t.[ClosedDate]", "[ClosedDate]")
        assert _reading(unqualified, types, "sqlserver").by_type == ("app.Ticket.ClosedDate",)

    def test_a_merge_insert_branch_is_the_merge_targets(self):
        types = _types(
            {
                "StockLevel": {"ProductID": "int", "OnHand": "int", "ModifiedDate": "datetime2"},
                "StockMovement": {"ProductID": "int", "Delta": "int"},
            },
            "sqlserver",
        )
        sql = (
            "MERGE [StockLevel] AS tgt USING (SELECT [ProductID], SUM([Delta]) AS [Delta] "
            "FROM [StockMovement] GROUP BY [ProductID]) AS src ON tgt.[ProductID] = src.[ProductID] "
            "WHEN MATCHED THEN UPDATE SET tgt.[OnHand] = tgt.[OnHand] + src.[Delta] "
            "WHEN NOT MATCHED THEN INSERT ([ProductID], [OnHand], [ModifiedDate]) "
            "VALUES (src.[ProductID], src.[Delta], SYSUTCDATETIME());"
        )
        reading = _reading(sql, types, "sqlserver")
        assert reading.by_type == ("app.StockLevel.ModifiedDate",)
        assert reading.fallbacks == ()

    def test_a_set_target_is_the_updated_tables_even_with_a_from_list(self):
        types = _types(
            {
                "topic_hot_scores": {
                    "topic_id": "int4",
                    "score": "float8",
                    "updated_at": "timestamp",
                },
                "topics": {"id": "int4", "updated_at": "varchar"},
            },
            "postgresql",
        )
        sql = (
            "UPDATE topic_hot_scores ths SET score = $1, updated_at = $2 FROM topics "
            "WHERE topics.id = ths.topic_id"
        )
        reading = _reading(sql, types, "postgresql")
        assert reading.by_type == ("app.topic_hot_scores.updated_at",)
        assert reading.fallbacks == ()

    def test_postgresql_timestamptz(self):
        types = _types({"topics": {"id": "int8", "bumped": "timestamptz"}}, "postgresql")
        sql = 'SELECT "topics"."id" FROM "topics" WHERE "topics"."bumped" > $1'
        assert _reading(sql, types, "postgresql").by_type == ("app.topics.bumped",)

    def test_sql_server_bracketed_names_and_datetime2(self):
        types = _types({"Orders": {"OrderID": "int", "OrderDate": "datetime2"}}, "sqlserver")
        sql = "SELECT o.[OrderID] FROM [app].[Orders] o WHERE o.[OrderDate] >= @d"
        assert _reading(sql, types, "sqlserver").by_type == ("app.Orders.OrderDate",)

    def test_sql_server_timestamp_is_a_row_version(self):
        types = _types({"Orders": {"OrderID": "int", "timestamp": "timestamp"}}, "sqlserver")
        sql = "SELECT [timestamp] FROM [Orders] WHERE [OrderID] = @id"
        assert not _reading(sql, types, "sqlserver")

    def test_oracle_date(self):
        types = _types({"orders": {"id": "NUMBER", "order_dt": "DATE"}}, "oracle")
        sql = "SELECT * FROM orders WHERE order_dt > :1"
        assert _reading(sql, types, "oracle").by_type == ("app.orders.order_dt",)

    def test_a_cast_to_a_date_type_is_not_a_column(self):
        types = _types({"t": {"id": "int", "a": "varchar"}})
        assert not _reading("SELECT CAST(a AS DATE) FROM t", types)


# ---------------------------------------------------------------------------
# Where the table can't be told, the name list decides, and the reading says so
# ---------------------------------------------------------------------------


class TestAmbiguityFallsBackToTheNameList:
    def test_an_unqualified_column_two_joined_tables_hold(self):
        types = _types(
            {
                "posts": {"id": "bigint", "topic_id": "bigint", "created_at": "varchar"},
                "topics": {"id": "bigint", "created_at": "varchar"},
            }
        )
        sql = "SELECT * FROM posts p JOIN topics t ON t.id = p.topic_id WHERE created_at > ?"
        reading = _reading(sql, types)
        assert reading.found  # by name: both are varchar, but the table is ambiguous
        assert reading.by_type == ()
        assert reading.by_name == ("created_at",)
        assert any("created_at" in f and "ambiguous" in f for f in reading.fallbacks)

    def test_an_ambiguous_name_outside_the_list_is_not_one(self):
        types = _types(
            {
                "posts": {"id": "bigint", "topic_id": "bigint", "stamp": "timestamp"},
                "topics": {"id": "bigint", "stamp": "timestamp"},
            }
        )
        sql = "SELECT * FROM posts p JOIN topics t ON t.id = p.topic_id WHERE stamp > ?"
        reading = _reading(sql, types)
        assert not reading.found
        assert any("stamp" in f and "ambiguous" in f for f in reading.fallbacks)

    def test_a_table_the_collection_does_not_list(self):
        types = _types({"posts": {"id": "bigint"}})
        reading = _reading("SELECT * FROM archived WHERE created_at > ?", types)
        assert reading.found and reading.by_name == ("created_at",)
        assert any("created_at" in f and "no collected table" in f for f in reading.fallbacks)

    def test_a_derived_tables_column(self):
        types = _types({"posts": {"id": "bigint", "posted": "datetime"}})
        sql = "SELECT x.posted FROM (SELECT posted FROM posts) x"
        reading = _reading(sql, types)
        # The inner column resolves by type; the outer one names a derived table.
        assert reading.by_type == ("app.posts.posted",)
        assert any(f.startswith("posted") for f in reading.fallbacks)

    def test_a_statement_sqlglot_cannot_read(self):
        types = _types({"posts": {"id": "bigint", "created_at": "varchar"}})
        sql = "SELECT * FROM posts WHERE created_at > ? AND (("
        assert sql_facts(sql, "mysql").parsed_by == "regex"
        reading = _reading(sql, types)
        assert reading.found and reading.by_name == ("created_at",)
        assert any("not parsed" in f for f in reading.fallbacks)


# ---------------------------------------------------------------------------
# A collection with no column types reads the name list
# ---------------------------------------------------------------------------


class TestNoTypesReadsTheNameList:
    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT * FROM t WHERE created_at > ?", True),
            ("SELECT * FROM t WHERE last_seen_at > ?", True),
            ("INSERT INTO t (id, event_date) VALUES (?, ?)", True),
            ("SELECT * FROM t WHERE event_time >= ?", True),
            ("SELECT * FROM t WHERE posted > ?", False),
            ("SELECT * FROM t WHERE mydate_x = ?", False),
            ("UPDATE t SET a = 1 WHERE id = ?", False),  # "update" holds "date"
            ("SELECT CAST(a AS DATE) FROM t", False),  # a type, not a column
            ("SELECT * FROM t WHERE note = 'created_at'", False),  # a literal
        ],
    )
    def test_the_name_list(self, sql, expected):
        reading = _reading(sql, None)
        assert reading.found is expected
        assert reading.by_type == ()
        assert reading.no_types

    def test_an_untyped_collection_reads_names(self):
        untyped = _collection({"t": {"id": None, "posted": None, "created_at": None}})
        types = ColumnTypes.from_collector(untyped)
        assert _reading("SELECT * FROM t WHERE created_at > ?", types).found
        assert not _reading("SELECT * FROM t WHERE posted > ?", types).found


@pytest.mark.parametrize("name", SAMPLES)
def test_the_samples_carry_column_types_and_the_hint_never_changes_a_reading(name):
    """Every bundled sample records each column's type, so its timestamp columns are
    read by type; skipping the parse on a text that holds no timestamp name and no
    timestamp-typed column's name never changes a reading."""
    data = load_sample(name)
    engine, _port = detect_source_engine(data.get("metadata"))
    parsed = parse_offline_collection(data)
    assert all(c.get("data_type") for t in parsed["tables"] for c in t["columns"])
    types = ColumnTypes.from_collector(
        {
            "metadata": {"source_database": {"engine": engine}},
            "database_schema": {"tables": parsed["tables"]},
        }
    )
    assert types is not None and types.timestamp_names
    for query in parsed["queries"]:
        text = query["query_text"]
        expected = read_timestamp_column(sql_facts(text, engine).columns, types)
        assert query_facts(text, engine).timestamp_column(types) == expected, text[:80]


def test_the_reading_is_kept_per_column_types():
    types = _types({"posts": {"id": "bigint", "posted": "datetime"}})
    facts = query_facts("SELECT * FROM posts WHERE posted > ? -- kept")
    assert facts.timestamp_column(types) is facts.timestamp_column(types)
    assert facts.timestamp_column(types).found and not facts.timestamp_column(None).found


# ---------------------------------------------------------------------------
# Every reader reads the type-based fact
# ---------------------------------------------------------------------------

_READERS = (
    "src/agents/referee/triage.py",
    "src/tools/analysis/dynamodb_analysis_tools.py",
    "src/tools/analysis/opensearch_analysis_tools.py",
    "src/tools/analysis/redis_analysis_tools.py",
)


def _timestamp_reads(tree: ast.AST) -> tuple[list[ast.Call], list[ast.Attribute]]:
    calls, attributes = [], []
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "timestamp_column":
                calls.append(node)
                called.add(id(node.func))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "timestamp_column"
            and id(node) not in called
        ):
            attributes.append(node)
    return calls, attributes


def test_every_reader_passes_the_collections_column_types():
    """No reader reads the timestamp-column fact without the collection's types:
    each call passes them (``facts.timestamp_column(types)``)."""
    found = 0
    for path in sorted((_REPO / "src").rglob("*.py")):
        if "node_modules" in path.parts or path.name == "query_facts.py":
            continue
        calls, attributes = _timestamp_reads(ast.parse(path.read_text(encoding="utf-8")))
        rel = path.relative_to(_REPO).as_posix()
        assert not attributes, f"{rel} reads timestamp_column without calling it"
        assert all(
            call.args or call.keywords for call in calls
        ), f"{rel} reads timestamp_column with no column types"
        if calls:
            assert rel in _READERS, f"a new reader of timestamp_column: {rel}"
            found += 1
    assert found == len(_READERS)


def _query(qid: str, text: str, query_type: str, tables: list[str]) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": query_type,
        "tables_accessed": tables,
        "calls_per_second": 2.0,
        "rows_returned_avg": 5,
        "join_count": 0,
        "has_joins": False,
    }


def _typed_events_collection() -> dict:
    collection = _collection(
        {
            "events": {"id": "bigint", "happened": "datetime", "created_at": "varchar"},
        }
    )
    collection["queries"]["query_patterns"] = [
        _query(
            "q-typed", "INSERT INTO events (id, happened) VALUES (?, ?)", "INSERT", ["app.events"]
        ),
        _query(
            "q-named", "INSERT INTO events (id, created_at) VALUES (?, ?)", "INSERT", ["app.events"]
        ),
        _query(
            "q-unlisted",
            "INSERT INTO archived (id, created_at) VALUES (?, ?)",
            "INSERT",
            ["app.archived"],
        ),
    ]
    return collection


def test_triage_reads_the_type():
    signals = {s.signal: s for s in triage(_typed_events_collection()).signals}
    time_series = signals["time_series"]
    assert set(time_series.query_ids) == {"q-typed", "q-unlisted"}
    assert "1 by column name" in time_series.evidence


def test_dynamodb_reads_the_type_and_traces_the_fallback():
    collection = _typed_events_collection()
    analysis = analyze_dynamodb_use_cases(collection)
    by_type = {p.pattern_type: set(p.query_ids or []) for p in analysis.patterns_detected}
    assert by_type["time-series-event-log"] == {"q-typed", "q-unlisted"}
    trace = build_decision_trace(collection, analysis, [])
    section = trace["timestamp_columns"]
    assert section["read_by"] == "column type"
    assert [q["query_id"] for q in section["name_fallbacks"]] == ["q-unlisted"]


def test_opensearch_reads_the_type():
    collection = _collection(
        {"events": {"id": "bigint", "happened": "datetime", "created_at": "varchar"}}
    )
    collection["queries"]["query_patterns"] = [
        _query(
            "q-typed",
            "SELECT kind, COUNT(*) FROM events WHERE happened >= ? GROUP BY kind",
            "SELECT",
            ["app.events"],
        ),
        _query(
            "q-named",
            "SELECT kind, COUNT(*) FROM events WHERE created_at >= ? GROUP BY kind",
            "SELECT",
            ["app.events"],
        ),
    ]
    opensearch = analyze_opensearch_use_cases(collection)
    ranges = {
        qid
        for p in opensearch.patterns_detected
        if p.pattern_type == "time-range-query"
        for qid in p.query_ids or []
    }
    assert ranges == {"q-typed"}
