# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engine-neutral facts behind the Aurora dialect credits and the text-search class
(catalog PR C, #920).

Each fact is read in the source engine's dialect, so a capability both Aurora engines
have is found whatever dialect the source writes it in:

- ``multi_table_write``: an UPDATE or DELETE whose rows come from a join with another
  table (MySQL's ``UPDATE a JOIN b ... SET`` and ``DELETE a FROM a JOIN b``, the
  ``UPDATE ... FROM`` of PostgreSQL and SQL Server, PostgreSQL's ``DELETE ... USING``);
  a subquery in SET or WHERE is not one.
- ``string_aggregate_call``: a string aggregate, whatever the dialect calls it
  (``GROUP_CONCAT``, ``STRING_AGG``, ``LISTAGG``).
- ``stored_routine_keyword``: a stored routine call or definition, including SQL
  Server's ``EXEC``/``EXECUTE`` of a procedure (only for a SQL Server source:
  PostgreSQL's and MySQL's ``EXECUTE`` runs a prepared statement).
- ``AccessFacts.window_function``: any function over a window (``SqlFacts.window_function``).
- ``ScopeFacts.text_search_kinds``: which text searches a scope's own predicates hold.
"""

from __future__ import annotations

import pytest

from src.tools.sql.facade import access_facts, sql_facts
from src.tools.sql.query_facts import query_facts
from src.tools.sql.regex_facts import regex_multi_table_write


class TestMultiTableWrite:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("UPDATE a JOIN b ON b.id = a.id SET a.x = ?", "mysql"),
            ("UPDATE a INNER JOIN b USING (id) SET a.x = 1", "mysql"),
            ("DELETE a FROM a JOIN b ON a.id = b.id WHERE b.x = ?", "mysql"),
            ("UPDATE posts p SET a = 1 FROM topics t WHERE t.id = p.topic_id", "postgresql"),
            ("DELETE FROM a USING b WHERE a.id = b.id", "postgresql"),
            ("UPDATE t SET t.a = 1 FROM [T] t JOIN [U] u ON u.id = t.id", "sqlserver"),
            ("DELETE t FROM [T] t JOIN [U] u ON u.id = t.id", "sqlserver"),
            # A self-join: the FROM's own alias makes it a second instance of the table.
            (
                "UPDATE posts SET score = p2.score FROM posts p2 WHERE p2.id = posts.parent_id",
                "postgresql",
            ),
            # The target named again with another table in FROM is still a join.
            (
                "UPDATE p SET p.[Price] = s.[Price] FROM [Product] p, [Staging] s WHERE s.[ID] = p.[ID]",
                "sqlserver",
            ),
        ],
    )
    def test_a_write_whose_rows_come_from_a_join_is_one(self, sql, engine):
        facts = sql_facts(sql, engine)
        assert facts.parsed_by == "sqlglot"
        assert facts.multi_table_write
        assert query_facts(sql, engine).multi_table_write
        assert regex_multi_table_write(sql), "the regex fallback reads the same shape"

    @pytest.mark.parametrize(
        "sql,engine",
        [
            (
                "UPDATE t SET x = (SELECT MAX(y) FROM u WHERE u.id = t.id) WHERE id = 1",
                "postgresql",
            ),
            ("DELETE FROM t WHERE id IN (SELECT id FROM u)", "postgresql"),
            ("UPDATE a SET x = 1 WHERE id = ?", "mysql"),
            ("DELETE FROM a WHERE id = ?", "mysql"),
            ("SELECT * FROM a JOIN b ON a.id = b.id", "mysql"),
            ("INSERT INTO a SELECT * FROM b JOIN c ON b.id = c.id", "postgresql"),
            # #983 review: T-SQL names the target again in FROM, often by an alias; a
            # FROM that names only the target table reads no other table.
            ("UPDATE p SET p.[Price] = @p FROM [dbo].[Product] p WHERE p.[ID] = @id", "sqlserver"),
            ("UPDATE [Product] SET [Price] = 1 FROM [Product] WHERE [ID] = @id", "sqlserver"),
            ("UPDATE p SET Price = 1 FROM dbo.Product AS p WHERE p.ID = @id", "sqlserver"),
        ],
    )
    def test_a_subquery_a_single_table_write_or_a_read_is_not(self, sql, engine):
        assert not sql_facts(sql, engine).multi_table_write
        assert not query_facts(sql, engine).multi_table_write
        assert not regex_multi_table_write(sql)


class TestStringAggregate:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT GROUP_CONCAT(name) FROM t GROUP BY k", "mysql"),
            ("SELECT string_agg(name, ',') FROM t GROUP BY k", "postgresql"),
            ("SELECT STRING_AGG([Name], ',') FROM [T] GROUP BY [K]", "sqlserver"),
            ("SELECT LISTAGG(name, ',') WITHIN GROUP (ORDER BY name) FROM t", "oracle"),
        ],
    )
    def test_every_dialects_string_aggregate(self, sql, engine):
        assert query_facts(sql, engine).string_aggregate_call

    @pytest.mark.parametrize(
        "sql", ["SELECT array_agg(name) FROM t", "SELECT COUNT(*) FROM t", "SELECT 'string_agg'"]
    )
    def test_other_aggregates_and_literals_are_not(self, sql):
        assert not query_facts(sql, "postgresql").string_aggregate_call


class TestStoredRoutine:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("CALL refresh_stats()", "mysql"),
            ("CALL refresh_stats()", "postgresql"),
            ("EXEC [dbo].[usp_PlaceOrder] @CustomerID = @CustomerID", "sqlserver"),
            ("EXECUTE dbo.usp_CancelOrder @SalesOrderID = 1", "sqlserver"),
            ("/* job */ exec usp_Recalc", "sqlserver"),
        ],
    )
    def test_a_routine_call(self, sql, engine):
        assert query_facts(sql, engine).stored_routine_keyword

    @pytest.mark.parametrize(
        "sql,engine",
        [
            # PostgreSQL's and MySQL's EXECUTE runs a prepared statement, not a routine.
            ("EXECUTE fetch_user(1)", "postgresql"),
            ("EXECUTE stmt USING @a", "mysql"),
            ("SELECT * FROM t WHERE note = 'exec'", "sqlserver"),
            ("SELECT [ExecutedAt] FROM [Job]", "sqlserver"),
        ],
    )
    def test_a_prepared_statement_or_a_word_is_not(self, sql, engine):
        assert not query_facts(sql, engine).stored_routine_keyword


class TestWindowFunctionAccessFact:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, ROW_NUMBER() OVER (PARTITION BY k ORDER BY id) FROM t",
            "SELECT k, SUM(v) OVER (PARTITION BY k) FROM t",
        ],
    )
    def test_any_window_function(self, sql):
        assert access_facts(sql, "postgresql").window_function

    def test_a_plain_aggregate_is_not_one(self):
        assert not access_facts("SELECT k, SUM(v) FROM t GROUP BY k", "postgresql").window_function


class TestTextSearchKinds:
    @pytest.mark.parametrize(
        "sql,engine,kinds",
        [
            ("SELECT id FROM t WHERE name LIKE '%x%'", "postgresql", {"leading_wildcard"}),
            ("SELECT id FROM t WHERE name ~ 'x+'", "postgresql", {"pattern"}),
            ("SELECT id FROM t WHERE name REGEXP 'x+'", "mysql", {"pattern"}),
            ("SELECT id FROM t WHERE similarity(name, $1) > 0.3", "postgresql", {"similarity"}),
            (
                "SELECT id FROM t WHERE to_tsvector(body) @@ to_tsquery($1)",
                "postgresql",
                {"fulltext"},
            ),
            ("SELECT id FROM t WHERE MATCH(body) AGAINST ('boots')", "mysql", {"fulltext"}),
            ("SELECT id FROM t WHERE name LIKE $1", "postgresql", {"parameter"}),
            ("SELECT id FROM t WHERE name LIKE 'abc%'", "postgresql", set()),
            ("SELECT id FROM t WHERE id = $1", "postgresql", set()),
        ],
    )
    def test_each_scope_names_its_text_searches(self, sql, engine, kinds):
        scope = access_facts(sql, engine).scopes[0]
        assert set(scope.text_search_kinds) == kinds
        assert scope.text_search == bool(kinds - {"parameter"})
        assert scope.like_parameter == ("parameter" in kinds)
