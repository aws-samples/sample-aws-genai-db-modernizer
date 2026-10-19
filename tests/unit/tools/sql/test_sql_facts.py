"""The facade's query-shape facts (#796): joins, aggregation, subqueries, leading wildcards.

Each fact is read from the sqlglot tree, so comments, string literals and
column names are never read as SQL; when the facade falls back, each fact is
the regex scan the collectors ran before #796 (``src.tools.sql.regex_facts``).
"""

from __future__ import annotations

import pytest

from src.tools.sql import facade, regex_facts
from src.tools.sql.facade import access_facts, read_sql, regex_reading, sql_facts, table_references
from src.tools.sql.regex_facts import (
    regex_has_aggregation,
    regex_has_joins,
    regex_has_subquery,
    regex_join_count,
    regex_wildcard_leading,
)
from tests.unit.tools.sql._timing import assert_linear

# The regex fallback of each fact #893 added.
_ADDED_FACT_FALLBACKS = {
    "group_by": regex_facts.regex_group_by,
    "aggregate_function_count": regex_facts.regex_aggregate_function_count,
    "like_parameter": regex_facts.regex_like_parameter,
    "fulltext_search": regex_facts.regex_fulltext_search,
    "pattern_match": regex_facts.regex_pattern_match,
    "similarity_match": regex_facts.regex_similarity_match,
    "window_function": regex_facts.regex_window_function,
    "ranking_window_function": regex_facts.regex_ranking_window_function,
    "offset_window_function": regex_facts.regex_offset_window_function,
    "windowed_aggregate": regex_facts.regex_windowed_aggregate,
    "recursive_cte": regex_facts.regex_recursive_cte,
    "order_by": regex_facts.regex_order_by,
    "row_limit": regex_facts.regex_row_limit,
    "top_n": regex_facts.regex_top_n,
    "timestamp_column": regex_facts.regex_timestamp_column,
    "json_access": regex_facts.regex_json_access,
    "multi_table_write": regex_facts.regex_multi_table_write,
}


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


class TestTheFactsAddedFor893:
    """Each is the one definition its readers share (``src.tools.sql.query_facts``)."""

    @pytest.mark.parametrize(
        "sql,engine,fact,expected",
        [
            ("SELECT a FROM t GROUP BY a", "postgresql", "group_by", True),
            ("SELECT a FROM t -- GROUP BY a", "postgresql", "group_by", False),
            ("SELECT COUNT(*), MAX(b) FROM t GROUP BY a", "mysql", "aggregate_function_count", 2),
            ("SELECT COUNT ( * ) FROM `t`", "mysql", "aggregate_function_count", 1),
            ("SELECT count FROM t", "postgresql", "aggregate_function_count", 0),
            ("SELECT * FROM t WHERE a LIKE ?", "mysql", "like_parameter", True),
            ("SELECT * FROM t WHERE a ILIKE $1", "postgresql", "like_parameter", True),
            ("SELECT * FROM t WHERE a ILIKE $1", "", "like_parameter", True),  # generic
            ("SELECT * FROM t WHERE a LIKE ? || '%'", "postgresql", "like_parameter", False),
            ("SELECT * FROM t WHERE a LIKE '%x'", "mysql", "like_parameter", False),
            ("SELECT * FROM t WHERE MATCH (a) AGAINST ('x')", "mysql", "fulltext_search", True),
            (
                "SELECT * FROM t WHERE v @@ plainto_tsquery($1)",
                "postgresql",
                "fulltext_search",
                True,
            ),
            ("SELECT * FROM t WHERE CONTAINS([Body], @q)", "sqlserver", "fulltext_search", True),
            (
                "SELECT * FROM t WHERE a = 'MATCH (b) AGAINST (c)'",
                "mysql",
                "fulltext_search",
                False,
            ),
            ("SELECT * FROM t WHERE a ~ $1", "postgresql", "pattern_match", True),
            ("SELECT * FROM t WHERE a REGEXP ?", "mysql", "pattern_match", True),
            ("SELECT regexp_replace(a, $1, $2) FROM t", "postgresql", "pattern_match", False),
            (
                "SELECT * FROM t WHERE similarity(a, $1) > 0.3",
                "postgresql",
                "similarity_match",
                True,
            ),
            (
                "SELECT * FROM t WHERE word_similarity(a, $1) > 0.3",
                "postgresql",
                "similarity_match",
                True,
            ),
            ("SELECT similarity FROM t", "postgresql", "similarity_match", False),
            ("SELECT SUM(a) OVER (ORDER BY b) FROM t", "postgresql", "window_function", True),
            ("SELECT * FROM t ORDER BY rank", "postgresql", "window_function", False),
            (
                "SELECT DENSE_RANK() OVER (ORDER BY a) FROM t",
                "postgresql",
                "ranking_window_function",
                True,
            ),
            (
                "SELECT LEAD(a, 2) OVER (ORDER BY a) FROM t",
                "postgresql",
                "offset_window_function",
                True,
            ),
            (
                "SELECT NTILE($1) OVER (ORDER BY a) FROM t",
                "postgresql",
                "offset_window_function",
                True,
            ),
            ("SELECT ROW_NUMBER() OVER () FROM t", "postgresql", "offset_window_function", False),
            ("SELECT COUNT(*) OVER () FROM t", "postgresql", "windowed_aggregate", True),
            ("SELECT RANK() OVER (ORDER BY a) FROM t", "postgresql", "windowed_aggregate", False),
            ("WITH RECURSIVE r AS (SELECT 1) SELECT * FROM r", "postgresql", "recursive_cte", True),
            (
                "WITH [T] AS (SELECT Id FROM N UNION ALL SELECT n.Id FROM N n JOIN [T] t ON 1=1) "
                "SELECT * FROM [T]",
                "sqlserver",
                "recursive_cte",
                True,
            ),
            (
                "WITH a AS (SELECT 1 UNION SELECT 2) SELECT * FROM a",
                "postgresql",
                "recursive_cte",
                False,
            ),
            ("SELECT * FROM t ORDER BY a", "postgresql", "order_by", True),
            ("SELECT RANK() OVER (ORDER BY a) FROM t", "postgresql", "order_by", False),
            ("SELECT * FROM t LIMIT 5", "postgresql", "row_limit", True),
            ("SELECT TOP (5) * FROM t", "sqlserver", "row_limit", True),
            ("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", "oracle", "row_limit", True),
            ("SELECT credit_limit FROM t", "postgresql", "row_limit", False),
            ("DELETE FROM t WHERE a < ? LIMIT 1000", "mysql", "row_limit", True),
            # The T-SQL bridge drops a DML TOP before parsing; the row limit is kept.
            ("DELETE TOP (5000) FROM [S] WHERE [ExpiresAt] < @now", "sqlserver", "row_limit", True),
            ("SELECT * FROM t ORDER BY a LIMIT 5", "postgresql", "top_n", True),
            (
                "SELECT a FROM t UNION SELECT a FROM u ORDER BY a LIMIT 5",
                "postgresql",
                "top_n",
                True,
            ),
            ("SELECT TOP (5) * FROM t ORDER BY a", "sqlserver", "top_n", True),
            ("SELECT TOP (5) * FROM t", "sqlserver", "top_n", False),
            ("SELECT * FROM t WHERE created_at > ?", "mysql", "timestamp_column", True),
            (
                "INSERT INTO t (id, created_at) VALUES ($1, $2)",
                "postgresql",
                "timestamp_column",
                True,
            ),
            (
                "UPDATE t SET updated_at = now() WHERE id = $1",
                "postgresql",
                "timestamp_column",
                True,
            ),
            ("SELECT CAST(a AS DATE) FROM t", "postgresql", "timestamp_column", False),
            ("SELECT * FROM t WHERE note = 'created_at'", "postgresql", "timestamp_column", False),
            ("SELECT JSON_EXTRACT(doc, '$.a') FROM t", "mysql", "json_access", True),
            ("SELECT doc->>'a' FROM t", "postgresql", "json_access", True),
            ("SELECT doc #> '{a,b}' FROM t", "postgresql", "json_access", True),
            ("SELECT * FROM t WHERE doc ? 'k'", "postgresql", "json_access", False),
            ("SELECT * FROM t WHERE doc @> '{\"a\": 1}'", "postgresql", "json_access", True),
            ("SELECT * FROM t WHERE tags @> ARRAY[1]", "postgresql", "json_access", False),
            # Review of #1118: a PostgreSQL array literal is not a JSON document, an
            # empty '{}' stays JSON, and SQL Server's ISJSON is a JSON read.
            ("SELECT * FROM t WHERE tags @> '{a,b}'", "postgresql", "json_access", False),
            ("SELECT * FROM t WHERE doc @> '[1, 2]'", "postgresql", "json_access", True),
            ("SELECT * FROM t WHERE doc @> '{}'", "postgresql", "json_access", True),
            ("SELECT * FROM t WHERE ISJSON(doc) = 1", "sqlserver", "json_access", True),
            ("SELECT 'isjson(doc)' FROM t", "sqlserver", "json_access", False),
            ("SELECT jsonb_build_object('a', 1)", "postgresql", "json_access", True),
            ("SELECT doc::jsonb FROM t", "postgresql", "json_access", True),
            ("SELECT * FROM t FOR JSON PATH", "sqlserver", "json_access", True),
            ("SELECT * FROM t FOR XML PATH", "sqlserver", "json_access", False),
            ("SELECT * FROM t WHERE doc IS JSON", "oracle", "json_access", True),
            ("SELECT json_value FROM t", "mysql", "json_access", False),
            ("UPDATE a JOIN b ON b.id = a.id SET a.x = ?", "mysql", "multi_table_write", True),
            ("UPDATE a INNER JOIN b USING (id) SET a.x = 1", "mysql", "multi_table_write", True),
            # #920: the fact was MySQL's syntax only (update_target_join); the UPDATE ...
            # FROM of SQL Server and PostgreSQL is the same multi-table write.
            (
                "UPDATE t SET t.a = 1 FROM [T] t JOIN [U] u ON u.id = t.id",
                "sqlserver",
                "multi_table_write",
                True,
            ),
            (
                "UPDATE posts p SET a = 1 FROM topics t WHERE t.id = p.topic_id",
                "postgresql",
                "multi_table_write",
                True,
            ),
            ("UPDATE a SET x = 1 WHERE id = ?", "mysql", "multi_table_write", False),
        ],
    )
    def test_each_fact(self, sql, engine, fact, expected):
        facts = sql_facts(sql, engine)
        assert facts.parsed_by == "sqlglot"
        assert getattr(facts, fact) == expected

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT a, COUNT(*) FROM t GROUP BY a",
            "SELECT a FROM t GROUP BY a",
            "SELECT COUNT(*) FROM t",
            "SELECT a FROM t",
            "SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t",
        ],
    )
    def test_aggregation_is_group_by_or_an_aggregate_function(self, sql):
        facts = sql_facts(sql, "postgresql")
        assert facts.has_aggregation is (facts.group_by or facts.aggregate_function_count > 0)

    def test_the_access_facts_read_the_same_text_search_definition(self):
        sql = "SELECT * FROM t WHERE a ~ $1 AND b LIKE $2"
        scope = access_facts(sql, "postgresql").scopes[0]
        facts = sql_facts(sql, "postgresql")
        assert (scope.text_search, scope.like_parameter) == (True, True)
        assert (facts.pattern_match, facts.like_parameter) == (True, True)
        windowed = "SELECT SUM(a) OVER () FROM t"
        assert access_facts(windowed, "postgresql").windowed_aggregate
        assert sql_facts(windowed, "postgresql").windowed_aggregate


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
        for name, regex in _ADDED_FACT_FALLBACKS.items():
            assert getattr(facts, name) == regex(sql), name
        # has_aggregation stays exactly group_by or a positive count (#893).
        assert facts.has_aggregation is (facts.group_by or facts.aggregate_function_count > 0)

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


class TestTheFallbackReadsLikeTheParse:
    """Where a cheap regex can, the fallback gives the parsed answer (#893 review)."""

    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT * FROM t WHERE a LIKE ?", True),
            ("SELECT * FROM t WHERE a LIKE @Term", True),
            ("SELECT * FROM t WHERE a LIKE @Prefix + '%'", False),  # a prefix
            ("SELECT * FROM t WHERE a LIKE $1 || '%'", False),
        ],
    )
    def test_a_concatenated_pattern_is_not_a_parameter(self, sql, expected):
        assert regex_facts.regex_like_parameter(sql) is expected

    @pytest.mark.parametrize(
        "sql", ["SELECT GROUP_CONCAT(a) FROM t", "SELECT string_agg(a, ',') FROM t"]
    )
    def test_string_and_array_aggregates_are_aggregation(self, sql):
        assert regex_facts.regex_aggregate_function_count(sql) == 1
        assert regex_has_aggregation(sql)

    @pytest.mark.parametrize(
        "sql,order_by,top_n",
        [
            ("SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t LIMIT 5", False, False),
            (
                "SELECT RANK() OVER (PARTITION BY b ORDER BY a) r FROM t ORDER BY r LIMIT 5",
                True,
                True,
            ),
            ("SELECT * FROM t ORDER BY a LIMIT 5", True, True),
        ],
    )
    def test_an_order_by_inside_a_window_sorts_no_rows(self, sql, order_by, top_n):
        assert regex_facts.regex_order_by(sql) is order_by
        assert regex_facts.regex_top_n(sql) is top_n


class TestTheTestedColumnOfALeadingWildcardLike:
    """#942: ``WildcardLike.use`` names the one column a leading-wildcard LIKE tests and
    the tables it can belong to, read through the query's aliases and scopes."""

    @staticmethod
    def _use(sql, engine="mysql"):
        (like,) = sql_facts(sql, engine).wildcard_likes
        return like.use

    @staticmethod
    def _tables(use):
        return [[t.name for t in level] for level in use.scopes]

    def test_a_qualified_column_names_the_table_its_alias_stands_for(self):
        use = self._use("SELECT 1 FROM posts p JOIN users u ON u.id = p.uid WHERE u.name LIKE '%x'")
        assert (use.name, self._tables(use)) == ("name", [["users"]])

    def test_an_unqualified_column_lists_every_table_of_its_scope(self):
        use = self._use("SELECT 1 FROM posts p JOIN users u ON u.id = p.uid WHERE name LIKE '%x'")
        assert self._tables(use) == [["posts", "users"]]

    def test_a_function_of_one_column_tests_that_column(self):
        (like,) = sql_facts(
            "SELECT 1 FROM posts WHERE lower(title) LIKE '%x'", "mysql"
        ).wildcard_likes
        assert like.column is None  # not a bare column: its type is not the expression's
        assert (like.use.name, self._tables(like.use)) == ("title", [["posts"]])

    def test_an_expression_over_several_columns_names_none(self):
        assert self._use("SELECT 1 FROM posts WHERE concat(a, b) LIKE '%x'") is None

    def test_a_derived_table_or_cte_column_has_no_stored_table(self):
        derived = self._use("SELECT 1 FROM (SELECT title AS t FROM posts) d WHERE d.t LIKE '%x'")
        cte = self._use(
            "WITH c AS (SELECT title FROM posts) SELECT 1 FROM c WHERE c.title LIKE '%x'"
        )
        assert self._tables(derived) == [] and self._tables(cte) == []

    def test_a_correlated_subquery_column_is_looked_up_outward(self):
        use = self._use(
            "SELECT 1 FROM posts p WHERE EXISTS (SELECT 1 FROM tags g WHERE p.title LIKE '%x')"
        )
        assert self._tables(use) == [["posts"]]

    def test_the_regex_fallback_knows_no_tables(self):
        (like,) = sql_facts(
            "SELECT * FROM posts WHERE title LIKE '%x' AND (((", "mysql"
        ).wildcard_likes
        assert like.column == "title" and like.use is None

    def test_it_survives_the_stored_form(self):
        from src.tools.sql.stored_facts import readings_from_stored, stored_reading

        sql = "SELECT 1 FROM posts p WHERE p.title LIKE '%x'"
        stored = stored_reading(sql, "mysql", read_sql(sql, "mysql"))
        restored = readings_from_stored(stored, sql, "mysql")
        assert restored is not None
        assert restored[0].wildcard_likes == sql_facts(sql, "mysql").wildcard_likes
        assert restored[0].wildcard_likes[0].use is not None


class TestTheTestedColumnOfARegularExpression:
    """#1098: ``SqlFacts.regex_matches`` names the column each regular-expression or
    ``SIMILAR TO`` match tests and its candidate tables, as ``wildcard_likes`` does."""

    @staticmethod
    def _tables(use):
        return [[t.name for t in level] for level in use.scopes]

    @pytest.mark.parametrize(
        ("predicate", "engine"),
        [
            ("u.name REGEXP 'x'", "mysql"),
            ("u.name RLIKE 'x'", "mysql"),
            ("REGEXP_LIKE(u.name, 'x')", "mysql"),
            ("u.name ~ 'x'", "postgresql"),
            ("u.name ~* 'x'", "postgresql"),
            ("u.name !~ 'x'", "postgresql"),
            ("u.name SIMILAR TO 'x%'", "postgresql"),
            ("lower(u.name) ~ 'x'", "postgresql"),
        ],
    )
    def test_each_form_names_its_column_and_table(self, predicate, engine):
        sql = f"SELECT 1 FROM posts p JOIN users u ON u.id = p.uid WHERE {predicate}"
        facts = sql_facts(sql, engine)
        assert facts.pattern_match
        (match,) = facts.regex_matches
        assert (match.use.name, self._tables(match.use)) == ("name", [["users"]])

    def test_an_expression_over_several_columns_names_none(self):
        (match,) = sql_facts("SELECT 1 FROM t WHERE concat(a, b) REGEXP 'x'", "mysql").regex_matches
        assert match.use is None

    def test_a_column_on_the_pattern_side_is_flagged(self):
        (match,) = sql_facts("SELECT 1 FROM t WHERE 'abc' ~ title", "postgresql").regex_matches
        assert match.use is None and match.column_pattern
        (plain,) = sql_facts("SELECT 1 FROM t WHERE title ~ 'abc'", "postgresql").regex_matches
        assert not plain.column_pattern

    @pytest.mark.parametrize(
        ("sql", "engine"),
        [
            ("SELECT 1 FROM t WHERE REGEXP_LIKE(name, 'x', 'i')", "oracle"),
            ("SELECT 1 FROM t WHERE REGEXP_LIKE(name, 'x')", "db2"),
            ("SELECT 1 FROM t WHERE REGEXP_LIKE(name, 'x')", "sqlserver"),
            ("SELECT 1 FROM t WHERE name REGEXP 'x'", "mariadb"),
        ],
    )
    def test_the_other_source_dialects_name_the_column(self, sql, engine):
        (match,) = sql_facts(sql, engine).regex_matches
        assert match.use.name == "name"

    def test_one_entry_per_match_in_text_order(self):
        facts = sql_facts("SELECT 1 FROM t WHERE a REGEXP 'x' AND b REGEXP 'y'", "mysql")
        assert [m.use.name for m in facts.regex_matches] == ["a", "b"]

    def test_none_without_a_regex_and_none_on_the_regex_fallback(self):
        assert sql_facts("SELECT 1 FROM t WHERE a LIKE '%x'", "mysql").regex_matches == ()
        broken = sql_facts("SELECT 1 FROM t WHERE a REGEXP 'x' AND (((", "mysql")
        assert broken.pattern_match and broken.regex_matches == ()

    def test_it_survives_the_stored_form(self):
        from src.tools.sql.stored_facts import readings_from_stored, stored_reading

        sql = "SELECT 1 FROM posts p WHERE p.title ~ 'x'"
        stored = stored_reading(sql, "postgresql", read_sql(sql, "postgresql"))
        restored = readings_from_stored(stored, sql, "postgresql")
        assert restored is not None
        assert restored[0].regex_matches == sql_facts(sql, "postgresql").regex_matches
        assert restored[0].regex_matches[0].use is not None
