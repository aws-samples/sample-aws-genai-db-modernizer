"""The facade's query-shape facts (#796): joins, aggregation, subqueries, leading wildcards.

Each fact is read from the sqlglot tree, so comments, string literals and
column names are never read as SQL; when the facade falls back, each fact is
the regex scan the collectors ran before #796 (``src.tools.sql.regex_facts``).
"""

from __future__ import annotations

import pytest

from src.tools.sql import facade
from src.tools.sql.facade import read_sql, regex_reading, sql_facts, table_references
from src.tools.sql.regex_facts import (
    regex_has_aggregation,
    regex_has_joins,
    regex_has_subquery,
    regex_join_count,
    regex_wildcard_leading,
)
from tests.unit.tools.sql._timing import assert_linear


class TestJoinCount:
    @pytest.mark.parametrize(
        "sql,engine,count",
        [
            ("SELECT * FROM a JOIN b ON a.id = b.id", "mysql", 1),
            (
                "SELECT * FROM a\nJOIN b ON a.id = b.id\n\tLEFT JOIN c ON c.id = b.id",
                "postgresql",
                2,
            ),
            # A comma-separated FROM list joins its tables.
            ("SELECT * FROM a, b, c WHERE a.id = b.id AND b.id = c.id", "postgresql", 2),
            # DELETE ... USING a, b: the list's further items, as for FROM.
            ("DELETE FROM a USING b, c WHERE a.id = b.id AND b.id = c.id", "postgresql", 1),
            # A join to a subquery or a lateral subquery reads another table.
            ("SELECT * FROM a JOIN (SELECT id FROM b) x ON x.id = a.id", "mysql", 1),
            (
                "SELECT * FROM a CROSS JOIN LATERAL (SELECT * FROM b WHERE b.a = a.id) x",
                "postgresql",
                1,
            ),
            # Joins inside subqueries and CTE bodies count too.
            (
                "WITH x AS (SELECT a.id FROM a JOIN b ON a.id = b.id) SELECT * FROM x",
                "postgresql",
                1,
            ),
            ("SELECT * FROM a", "mysql", 0),
        ],
    )
    def test_joins_to_another_row_source_are_counted(self, sql, engine, count):
        facts = sql_facts(sql, engine)
        assert facts.parsed_by == "sqlglot"
        assert facts.join_count == count
        assert facts.has_joins is (count > 0)

    @pytest.mark.parametrize(
        "sql,engine",
        [
            (
                "SELECT s.id, j.p FROM sessions s CROSS APPLY OPENJSON(s.cart) "
                "WITH (p int '$.p') j",
                "sqlserver",
            ),
            ("SELECT * FROM a CROSS JOIN unnest(a.tags) t", "postgresql"),
            ("SELECT * FROM a CROSS JOIN generate_series(1, 10) g", "postgresql"),
        ],
    )
    def test_a_table_function_of_the_row_is_not_a_join(self, sql, engine):
        assert sql_facts(sql, engine).join_count == 0

    def test_join_in_a_comment_or_literal_is_not_a_join(self):
        sql = "SELECT id FROM users -- users allowed to join chat\nWHERE note = 'a join b'"
        facts = sql_facts(sql, "postgresql")
        assert (facts.join_count, facts.has_joins) == (0, False)
        assert regex_join_count(sql) == 2  # what the regex read


class TestAggregation:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT COUNT(*) FROM t", "mysql"),
            ("SELECT a, MAX(b) FROM t GROUP BY a", "postgresql"),
            ("SELECT a FROM t GROUP BY a", "postgresql"),  # GROUP BY counts, as before
            ("SELECT GROUP_CONCAT(name) FROM t", "mysql"),
            ("SELECT string_agg(name, ',') FROM t", "postgresql"),
            ("SELECT array_agg(id) FROM t", "postgresql"),
            ("SELECT COUNT(*) OVER () FROM t", "postgresql"),  # a windowed aggregate
        ],
    )
    def test_aggregates_and_group_by_are_aggregation(self, sql, engine):
        assert sql_facts(sql, engine).has_aggregation is True

    @pytest.mark.parametrize(
        "sql",
        [
            # A column named after an aggregate function.
            "UPDATE web_crawler_requests SET count = count + $1 WHERE id = $2",
            "INSERT INTO requests (date, count) VALUES ($1, $2) "
            "ON CONFLICT (date) DO UPDATE SET count = requests.count + excluded.count",
            "SELECT max_posts, min FROM settings",
            # Ranking and offset window functions combine no values.
            "SELECT id, percent_rank() OVER (ORDER BY score) FROM posts",
            "SELECT id, row_number() OVER (ORDER BY count DESC) FROM web_crawler_requests",
            "SELECT id, lag(id) OVER (ORDER BY id) FROM posts",
            "SELECT id FROM t -- count(*) in a comment",
        ],
    )
    def test_column_names_comments_and_ranking_functions_are_not(self, sql):
        assert sql_facts(sql, "postgresql").has_aggregation is False


class TestSubquery:
    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT * FROM t WHERE id IN (SELECT id FROM u)", True),
            ("SELECT * FROM (SELECT id FROM u) x", True),
            ("WITH x AS (SELECT id FROM u) SELECT * FROM x", True),
            ("SELECT id FROM t UNION SELECT id FROM u", True),
            ("INSERT INTO t (id) SELECT id FROM u", False),
            ("SELECT id FROM themes WHERE user_selectable OR id = $1", False),
            ("SELECT 'select' FROM t", False),
        ],
    )
    def test_more_than_one_select_is_a_subquery(self, sql, expected):
        assert sql_facts(sql, "postgresql").has_subquery is expected


class TestLeadingWildcard:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM t WHERE name LIKE '%foo'", "mysql"),
            ("SELECT * FROM t WHERE name LIKE '%foo%'", "postgresql"),
            ("SELECT * FROM t WHERE name ILIKE '%foo'", "postgresql"),
            ("SELECT * FROM t WHERE name NOT LIKE '%foo'", "postgresql"),
            ("SELECT * FROM t WHERE name LIKE '%a!%' ESCAPE '!'", "postgresql"),
            ('SELECT * FROM t WHERE name LIKE "%foo"', "mysql"),  # a MySQL string
            ("SELECT * FROM t WHERE name LIKE '%' || $1 || '%'", "postgresql"),
            ("SELECT * FROM t WHERE name LIKE CONCAT('%', ?, '%')", "mysql"),
            ("SELECT * FROM t WHERE name LIKE CONCAT_WS('', '%', ?)", "mysql"),
            ("SELECT TOP (5) id FROM t WHERE [Name] LIKE '%' + @Term + '%'", "sqlserver"),
            ("SELECT * FROM t WHERE name LIKE ('%' || $1)", "postgresql"),
        ],
    )
    def test_a_pattern_starting_with_percent_is_a_leading_wildcard(self, sql, engine):
        assert sql_facts(sql, engine).wildcard_leading is True

    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM t WHERE name LIKE 'foo%'", "postgresql"),
            ("SELECT * FROM t WHERE name LIKE ?", "mysql"),  # the value is unknown
            ("SELECT * FROM t WHERE name LIKE $1", "postgresql"),
            ("SELECT * FROM t WHERE name LIKE CONCAT(?, '%')", "mysql"),
            ("SELECT '%' AS pct FROM t WHERE name = 'LIKE ''%'", "postgresql"),
            ("SELECT * FROM t -- WHERE name LIKE '%foo'", "postgresql"),
        ],
    )
    def test_other_patterns_are_not(self, sql, engine):
        assert sql_facts(sql, engine).wildcard_leading is False


class TestFallback:
    @pytest.mark.parametrize(
        "sql,engine,reason",
        [
            ("REFRESH MATERIALIZED VIEW CONCURRENTLY mv", "postgresql", "command"),
            (
                "SELECT count(*) FROM a JOIN b ON (a.id = b.id WHERE x LIKE '%y'",
                "mysql",
                "parse_error",
            ),
            ("  ", "mysql", "empty"),
        ],
    )
    def test_a_fallback_is_the_regex_answer(self, sql, engine, reason):
        facts = sql_facts(sql, engine)
        assert (facts.parsed_by, facts.fallback_reason) == ("regex", reason)
        assert facts.join_count == regex_join_count(sql)
        assert facts.has_joins == regex_has_joins(sql)
        assert facts.has_aggregation == regex_has_aggregation(sql)
        assert facts.has_subquery == regex_has_subquery(sql)
        assert facts.wildcard_leading == regex_wildcard_leading(sql)

    def test_over_the_size_cap_is_the_regex_answer(self):
        sql = "SELECT count(*) FROM a JOIN b ON a.id = b.id WHERE n = '" + "x" * 100_001 + "'"
        facts = sql_facts(sql, "postgresql")
        assert facts.fallback_reason == "too_long"
        assert (facts.join_count, facts.has_aggregation) == (1, True)

    def test_an_unexpected_parser_exception_falls_back(self, monkeypatch):
        def boom(*_a, **_k):
            raise RecursionError("too deep")

        monkeypatch.setattr(facade.sqlglot, "parse", boom)
        facts = sql_facts("SELECT count(*) FROM a JOIN b ON a.id = b.id", "mysql")
        assert (facts.parsed_by, facts.fallback_reason) == ("regex", "parse_error")
        assert (facts.join_count, facts.has_aggregation) == (1, True)

    def test_regex_reading_skips_sqlglot(self, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("must not parse")

        monkeypatch.setattr(facade.sqlglot, "parse", boom)
        reading = regex_reading("SELECT * FROM a JOIN b ON a.id = b.id", "mysql", "facade_error")
        assert reading.facts.fallback_reason == reading.tables.fallback_reason == "facade_error"
        assert reading.facts.join_count == 1
        assert [t.written for t in reading.tables.tables] == ["a", "b"]


class TestOneParse:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM a, b WHERE a.id = b.id", "postgresql"),
            ("REFRESH MATERIALIZED VIEW mv", "postgresql"),
            ("SELECT TOP (5) id FROM t WHERE [Name] LIKE '%' + @Term", "sqlserver"),
        ],
    )
    def test_read_sql_is_table_references_plus_sql_facts(self, sql, engine):
        reading = read_sql(sql, engine)
        assert reading.tables == table_references(sql, engine)
        assert reading.facts == sql_facts(sql, engine)

    def test_read_sql_parses_once(self, monkeypatch):
        calls = []
        real = facade.sqlglot.parse

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(facade.sqlglot, "parse", counting)
        read_sql("SELECT * FROM a JOIN b ON a.id = b.id", "mysql")
        assert len(calls) == 1


@pytest.mark.slow
def test_facts_are_read_in_linear_time():
    def build(n: int) -> str:
        return "SELECT count(*) FROM a JOIN b ON a.id = b.id WHERE n LIKE '%" + "a" * n + "'"

    assert_linear(build, lambda text: sql_facts(text, "postgresql"))
