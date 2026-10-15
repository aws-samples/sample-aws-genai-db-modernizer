"""Access-pattern facts from the sqlglot facade (#728).

:func:`access_facts` reports, per SELECT (or UPDATE/DELETE) scope, the tables it
reads, the columns it pins or bounds, the join pairs, its order, row limit and
aggregates -- facts only, no schema and no class. These tests pin the facts the
classifier (``src.agents.referee.access_pattern``) relies on.
"""

from __future__ import annotations

import pytest

from src.tools.sql.facade import ColumnFact, access_facts


def _scope(sql, engine="postgresql", index=0):
    facts = access_facts(sql, engine)
    assert facts.parsed_by == "sqlglot", facts
    return facts.scopes[index]


class TestPredicates:
    def test_equalities_ranges_joins_and_other_terms(self):
        scope = _scope(
            "SELECT * FROM orders o JOIN items i ON i.order_id = o.id "
            "WHERE o.id = $1 AND o.total BETWEEN $2 AND $3 AND o.created_at > $4 "
            "AND (o.status = $5 OR o.status = $6) AND o.note IS NULL"
        )
        assert [s.alias for s in scope.sources] == ["o", "i"]
        assert scope.equalities == (ColumnFact("o", "id"),)
        assert set(scope.ranges) == {ColumnFact("o", "total"), ColumnFact("o", "created_at")}
        assert scope.joins == ((ColumnFact("i", "order_id"), ColumnFact("o", "id")),)
        assert scope.other_filters == 2

    @pytest.mark.parametrize(
        "where",
        ["id IN ($1, $2)", "id = ANY($1)", "id IN (SELECT order_id FROM items)", "$1 = id"],
    )
    def test_a_column_pinned_to_values_is_an_equality(self, where):
        assert _scope(f"SELECT * FROM orders WHERE {where}").equalities == (ColumnFact(None, "id"),)

    def test_not_in_is_not_an_equality(self):
        scope = _scope("SELECT * FROM orders WHERE id NOT IN ($1, $2)")
        assert scope.equalities == () and scope.other_filters == 1

    def test_an_outer_column_in_a_correlated_subquery_is_a_value(self):
        facts = access_facts(
            "SELECT * FROM orders o WHERE EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.id)",
            "postgresql",
        )
        inner = next(s for s in facts.scopes if s.sources and s.sources[0].alias == "i")
        assert inner.equalities == (ColumnFact("i", "order_id"),)

    @pytest.mark.parametrize(
        "pattern,engine,kind",
        [
            ("'abc%'", "postgresql", "prefix"),
            ("$1 || '%'", "postgresql", "prefix"),
            ("'%abc'", "postgresql", "leading"),
            ("'%' || $1 || '%'", "postgresql", "leading"),
            ("$1", "postgresql", "parameter"),
            ("?", "mysql", "parameter"),
            ("'_x%'", "postgresql", "other"),
        ],
    )
    def test_how_a_like_pattern_addresses_rows(self, pattern, engine, kind):
        """``wildcard_leading`` is #875's definition: a literal first part starting with ``%``;
        a bare bind parameter is its own fact, never a leading wildcard."""
        scope = access_facts(f"SELECT * FROM t WHERE name LIKE {pattern}", engine).scopes[0]
        assert (scope.ranges == (ColumnFact(None, "name"),)) is (kind == "prefix")
        assert scope.text_search is (kind == "leading")
        assert scope.like_parameter is (kind == "parameter")

    def test_text_search_belongs_to_the_scope_that_holds_it(self):
        facts = access_facts(
            "SELECT * FROM orders WHERE id IN (SELECT order_id FROM notes WHERE body LIKE '%x%')",
            "postgresql",
        )
        outer = next(s for s in facts.scopes if s.sources[0].alias == "orders")
        inner = next(s for s in facts.scopes if s.sources[0].alias == "notes")
        assert not outer.text_search and inner.text_search


class TestRowLimit:
    @pytest.mark.parametrize(
        "sql,engine,limit",
        [
            ("SELECT * FROM t ORDER BY id LIMIT 1", "postgresql", "1"),
            ("SELECT * FROM t ORDER BY id LIMIT 1 OFFSET $1", "postgresql", "1"),
            ("SELECT * FROM t ORDER BY id LIMIT $1", "postgresql", "?"),
            ("SELECT * FROM `t` ORDER BY `id` LIMIT ?, 1", "mysql", "1"),
            ("SELECT * FROM t ORDER BY id FETCH FIRST ROW ONLY", "postgresql", "1"),
            ("SELECT TOP (1) * FROM t ORDER BY id", "sqlserver", "1"),
            ("SELECT TOP (1) PERCENT * FROM t ORDER BY id", "sqlserver", "?"),
            ("SELECT TOP (1) WITH TIES * FROM t ORDER BY id", "sqlserver", "?"),
            ("SELECT * FROM t ORDER BY id", "postgresql", None),
        ],
    )
    def test_the_row_count_as_written(self, sql, engine, limit):
        assert _scope(sql, engine).limit == limit

    def test_skip_locked_and_readpast(self):
        assert _scope("SELECT * FROM t FOR UPDATE SKIP LOCKED").skip_locked
        assert not _scope("SELECT * FROM t FOR UPDATE NOWAIT").skip_locked
        assert _scope("SELECT * FROM t WITH (READPAST) WHERE id = 1", "sqlserver").skip_locked


class TestSources:
    def test_a_cte_reference_points_at_the_cte_body(self):
        facts = access_facts(
            "WITH o AS (SELECT * FROM orders WHERE id = $1) SELECT * FROM o JOIN items i "
            "ON i.order_id = o.id",
            "postgresql",
        )
        outer = facts.scopes[0]
        cte = outer.sources[0]
        assert cte.table is None and cte.derived is not None
        assert facts.scopes[cte.derived].sources[0].table.name == "orders"

    def test_the_optional_side_of_a_left_join(self):
        scope = _scope("SELECT * FROM a LEFT JOIN b ON b.a_id = a.id")
        assert [(s.alias, s.optional) for s in scope.sources] == [("a", False), ("b", True)]

    def test_dual_is_not_a_source_and_a_row_function_is_marked(self):
        assert _scope("SELECT 1 FROM DUAL", "mysql").sources == ()
        scope = _scope("SELECT * FROM t, unnest($1::int[]) AS u(v)")
        assert [(s.alias, s.function) for s in scope.sources] == [("t", False), ("u", True)]

    def test_mysql_multi_table_delete_reads_both_tables(self):
        facts = access_facts(
            "DELETE p FROM wp_posts p JOIN wp_postmeta m ON m.post_id = p.ID WHERE m.k = ?", "mysql"
        )
        assert facts.kind == "delete"
        assert [s.alias for s in facts.scopes[0].sources] == ["p", "m"]


class TestStatement:
    def test_kinds_and_flags(self):
        assert access_facts("INSERT INTO t (a) VALUES ($1)", "postgresql").inserts_values
        assert access_facts("UPDATE t SET a = 1 WHERE id = 2", "postgresql").kind == "update"
        assert access_facts("SET NAMES utf8", "mysql").kind == "other"
        assert access_facts("SELECT SUM(a) OVER () FROM t", "postgresql").windowed_aggregate
        assert not access_facts(
            "SELECT row_number() OVER () FROM t", "postgresql"
        ).windowed_aggregate
        assert _scope("SELECT a, COUNT(*) FROM t GROUP BY a").group_by
        assert _scope("SELECT COUNT(*) FROM t").aggregate
        assert not _scope("SELECT COUNT(*) OVER () FROM t").aggregate

    def test_unparseable_text_falls_back_without_guessing(self):
        facts = access_facts("SELECT * FROM t WHERE ((( id =", "postgresql")
        assert facts.parsed_by == "regex" and facts.scopes == ()
        assert facts.fallback_reason == "parse_error"

    def test_a_show_is_a_generic_command(self):
        facts = access_facts("SHOW search_path", "postgresql")
        assert facts.kind == "other" and facts.fallback_reason == "command"


class TestOneDefinitionPerFact:
    """ADR-031 Decision 8: the access facts and ``SqlFacts`` (#875) read a fact the same way."""

    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM t WHERE a LIKE '%x'", "postgresql"),
            ("SELECT * FROM t WHERE a LIKE '%' || $1 || '%'", "postgresql"),
            ("SELECT * FROM t WHERE a LIKE $1", "postgresql"),
            ("SELECT * FROM `t` WHERE `a` LIKE ?", "mysql"),
            ("SELECT * FROM t WHERE a LIKE 'x%'", "postgresql"),
            ("SELECT * FROM t WHERE a LIKE CONCAT('%', @p)", "sqlserver"),
        ],
    )
    def test_a_leading_wildcard(self, sql, engine):
        from src.tools.sql.facade import sql_facts

        leading = any(s.text_search for s in access_facts(sql, engine).scopes)
        assert leading is sql_facts(sql, engine).wildcard_leading

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT SUM(a) OVER () FROM t",
            "SELECT COUNT(*) OVER (PARTITION BY b) FROM t",
            "SELECT row_number() OVER (ORDER BY a) FROM t",
            "SELECT lag(a) OVER (ORDER BY a) FROM t",
        ],
    )
    def test_a_window_function_is_aggregation_only_when_it_combines_values(self, sql):
        from src.tools.sql.facade import sql_facts

        facts = access_facts(sql, "postgresql")
        assert facts.windowed_aggregate is sql_facts(sql, "postgresql").has_aggregation
