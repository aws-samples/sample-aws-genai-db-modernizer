"""Collected queries grouped by access pattern (#1146).

The maintainer rule (recorded on #1104 and #1146): two statements are one access
pattern when they read the same data the same way: the same tables, predicate shape
(literals removed, IN-lists of any length one), ORDER BY, limit class (LIMIT 1, LIMIT n,
none) and operation (item read, COUNT or aggregate, write). The projection never
separates them; the union of the returned columns is kept for the designers.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.tools.sql.access_signature import query_signature, returned_columns, scope_signature
from src.tools.sql.facade import access_facts
from src.tools.sql.query_pattern_groups import (
    group_by_query_pattern,
    listed_query_patterns,
    repeated_query_patterns,
)


def _q(qid: str, text: str, freq: float = 60.0, cps: float | None = None) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_text": text,
        "frequency_per_hour": freq,
        "calls_per_second": cps,
    }


def _ids(texts: list[str], engine: str = "postgresql") -> list[tuple[str, ...]]:
    queries = [_q(f"q{i}", t) for i, t in enumerate(texts)]
    return [g.query_ids for g in group_by_query_pattern(queries, engine)]


ONE = [("q0", "q1")]
TWO = [("q0",), ("q1",)]


class TestOnePattern:
    @pytest.mark.parametrize(
        "texts",
        [
            # only the projection differs
            ["SELECT * FROM users WHERE id = $1", "SELECT id, name FROM users WHERE id = $1"],
            # only the literal differs
            ["SELECT a FROM t WHERE k = 1", "SELECT a FROM t WHERE k = 2"],
            # only the IN-list length differs
            ["SELECT a FROM t WHERE k IN ($1)", "SELECT a FROM t WHERE k IN ($1, $2, $3)"],
            # = and IN pin a column the same way
            ["SELECT a FROM t WHERE k = $1", "SELECT b FROM t WHERE k IN ($1, $2)"],
            # LIMIT 10 and LIMIT 50 are both "many"
            ["SELECT a FROM t ORDER BY c LIMIT 10", "SELECT b FROM t ORDER BY c LIMIT 50"],
            # two writes by one key, whatever they set
            ["UPDATE t SET a = $1 WHERE id = $2", "UPDATE t SET b = $1, c = $2 WHERE id = $3"],
            # two inserts into one table, whatever columns they list
            ["INSERT INTO t (a) VALUES ($1)", "INSERT INTO t (a, b) VALUES ($1, $2)"],
        ],
    )
    def test_same_data_read_the_same_way(self, texts: list[str]) -> None:
        assert _ids(texts) == ONE


class TestTwoPatterns:
    @pytest.mark.parametrize(
        "texts",
        [
            # an item read and a COUNT: the caller wants a number
            ["SELECT a FROM t WHERE k = $1", "SELECT COUNT(*) FROM t WHERE k = $1"],
            # a read and a write
            ["SELECT a FROM t WHERE k = $1", "DELETE FROM t WHERE k = $1"],
            # another key column
            ["SELECT a FROM t WHERE k = $1", "SELECT a FROM t WHERE j = $1"],
            # a range, not a pinned value
            ["SELECT a FROM t WHERE k = $1", "SELECT a FROM t WHERE k > $1"],
            # another sort column
            ["SELECT a FROM t ORDER BY c LIMIT 5", "SELECT a FROM t ORDER BY d LIMIT 5"],
            # LIMIT 1 vs LIMIT n
            ["SELECT a FROM t WHERE k = $1 LIMIT 1", "SELECT a FROM t WHERE k = $1 LIMIT 5"],
            # LIMIT n vs no limit
            ["SELECT a FROM t WHERE k = $1 LIMIT 5", "SELECT a FROM t WHERE k = $1"],
            # another table
            ["SELECT a FROM t WHERE k = $1", "SELECT a FROM u WHERE k = $1"],
            # the same outer read, a subquery on another table
            [
                "SELECT a FROM t WHERE id IN (SELECT tid FROM u WHERE k = $1)",
                "SELECT a FROM t WHERE id IN (SELECT tid FROM v WHERE k = $1)",
            ],
            # inserts into two tables
            ["INSERT INTO t (a) VALUES ($1)", "INSERT INTO u (a) VALUES ($1)"],
            # the same SELECT copied into two tables
            [
                "INSERT INTO t SELECT a FROM s WHERE k = $1",
                "INSERT INTO u SELECT a FROM s WHERE k = $1",
            ],
        ],
    )
    def test_different_access(self, texts: list[str]) -> None:
        assert _ids(texts) == TWO


# The #1146 review's probe: five aggregates over the same rows, each its own pattern.
AGGREGATES = [
    "SELECT status, COUNT(*) FROM orders WHERE shop_id = $1 GROUP BY status",
    "SELECT customer_id, COUNT(*) FROM orders WHERE shop_id = $1 GROUP BY customer_id",
    "SELECT customer_id, COUNT(*) FROM orders WHERE shop_id = $1 GROUP BY customer_id "
    "HAVING COUNT(*) > 5",
    "SELECT COUNT(*) FROM orders WHERE shop_id = $1",
    "SELECT SUM(total) FROM orders WHERE shop_id = $1",
]


class TestAggregates:
    def test_the_review_probe_is_five_patterns(self) -> None:
        assert len(_ids(AGGREGATES)) == 5

    @pytest.mark.parametrize(
        "texts",
        [
            # the same grouping, another HAVING value
            [
                "SELECT c, COUNT(*) FROM t WHERE k = $1 GROUP BY c HAVING COUNT(*) > 5",
                "SELECT c, COUNT(*) FROM t WHERE k = $1 GROUP BY c HAVING COUNT(*) > 10",
            ],
            # a GROUP BY written by position, by alias or by its expression
            [
                "SELECT date_trunc('day', ts) AS d, COUNT(*) FROM t WHERE k = $1 GROUP BY 1",
                "SELECT date_trunc('day', ts) AS d, COUNT(*) FROM t WHERE k = $1 GROUP BY d",
                "SELECT COUNT(*) FROM t WHERE k = $1 GROUP BY date_trunc('day', ts)",
            ],
            # another column summed: the projection
            [
                "SELECT SUM(a) FROM t WHERE k = $1",
                "SELECT SUM(b) FROM t WHERE k = $1",
            ],
        ],
    )
    def test_one_pattern(self, texts: list[str]) -> None:
        assert _ids(texts) == [tuple(f"q{i}" for i in range(len(texts)))]

    @pytest.mark.parametrize(
        "texts",
        [
            # wordpress PAT-9 before the review: another GROUP BY item
            [
                "SELECT SUM(m.v), p.d FROM p JOIN m ON p.id = m.pid WHERE p.k = ? "
                "GROUP BY YEAR(p.d), MONTH(p.d)",
                "SELECT m.n, SUM(m.v), p.d FROM p JOIN m ON p.id = m.pid WHERE p.k = ? "
                "GROUP BY YEAR(p.d), MONTH(p.d), m.n",
            ],
            # another date part
            [
                "SELECT COUNT(*) FROM t WHERE k = $1 GROUP BY date_trunc('day', ts)",
                "SELECT COUNT(*) FROM t WHERE k = $1 GROUP BY date_trunc('month', ts)",
            ],
            # COUNT(col) and COUNT(*)
            ["SELECT COUNT(a) FROM t WHERE k = $1", "SELECT COUNT(*) FROM t WHERE k = $1"],
        ],
    )
    def test_two_patterns(self, texts: list[str]) -> None:
        engine = "mysql" if "?" in texts[0] else "postgresql"
        assert _ids(texts, engine) == TWO

    def test_routing_s_scope_signature_ignores_the_aggregate_shape(self) -> None:
        """ADR-031 Amendment 11: the key anchors read ``scope_signature``, which stays
        as #1104 left it: the grouping is a statement-level part only."""
        first, second = (access_facts(t, "postgresql") for t in AGGREGATES[:2])
        assert scope_signature(first, first.scopes[0]) == scope_signature(second, second.scopes[0])
        assert query_signature(first) != query_signature(second)


def test_distinct_is_read_like_the_projection() -> None:
    """#1146 review decision: DISTINCT stays with the plain read, as the columns do."""
    texts = [
        "SELECT DISTINCT status FROM orders WHERE shop_id = $1",
        "SELECT * FROM orders WHERE shop_id = $1",
    ]
    assert _ids(texts) == ONE


def test_a_cte_s_tables_are_the_pattern_s_tables() -> None:
    """Discourse PAT-550: a WITH ... SELECT reads its CTE's table."""
    text = (
        "WITH ips AS (SELECT ip_address FROM screened_ip_addresses WHERE action = $1) "
        "SELECT COUNT(*) FROM ips GROUP BY ip_address"
    )
    (group,) = group_by_query_pattern([_q("q", text)], "postgresql")
    assert group.tables == ("screened_ip_addresses",)


class TestWriteKinds:
    """#1146 review, decision changed after the Bedrock smoke: an INSERT, an UPDATE, a
    DELETE and an upsert are four item operations, so four patterns even on one key (a
    designer records one operation per access pattern)."""

    @pytest.mark.parametrize(
        "texts, engine",
        [
            # wordpress PAT-5 before the change
            (
                [
                    "UPDATE `wp_options` SET `option_value` = ? WHERE `option_name` = ?",
                    "DELETE FROM `wp_options` WHERE `option_name` = ?",
                ],
                "mysql",
            ),
            (["INSERT INTO t (a) VALUES (?)", "REPLACE INTO t (a) VALUES (?)"], "mysql"),
            (
                [
                    "INSERT INTO t (a) VALUES (?)",
                    "INSERT INTO t (a) VALUES (?) ON DUPLICATE KEY UPDATE a = ?",
                ],
                "mysql",
            ),
            (
                [
                    "INSERT INTO t (a) VALUES ($1)",
                    "INSERT INTO t (a) VALUES ($1) ON CONFLICT (a) DO UPDATE SET b = 1",
                ],
                "postgresql",
            ),
            (["UPDATE t SET a = $1 WHERE id = $2", "INSERT INTO t (a) VALUES ($1)"], "postgresql"),
        ],
    )
    def test_two_patterns(self, texts: list[str], engine: str) -> None:
        assert _ids(texts, engine) == TWO

    def test_the_group_is_named_by_its_write_kind(self) -> None:
        texts = [
            "UPDATE t SET a = $1 WHERE id = $2",
            "DELETE FROM t WHERE id = $1",
            "INSERT INTO t (a) VALUES ($1) ON CONFLICT (a) DO NOTHING",
            "INSERT INTO t (a) VALUES ($1)",
        ]
        groups = group_by_query_pattern([_q(f"q{i}", t) for i, t in enumerate(texts)], "postgresql")
        assert [g.operation for g in groups] == ["update", "delete", "upsert", "insert"]

    def test_an_upsert_and_replace_are_one_kind(self) -> None:
        texts = [
            "REPLACE INTO t (a) VALUES (?)",
            "INSERT INTO t (a, b) VALUES (?, ?) ON DUPLICATE KEY UPDATE b = ?",
        ]
        assert _ids(texts, "mysql") == ONE

    def test_routing_s_scope_signature_still_calls_both_a_write(self) -> None:
        """ADR-031 Amendment 11: the key anchors see the same write either way."""
        update = access_facts("UPDATE t SET a = $1 WHERE k = $2", "postgresql")
        delete = access_facts("DELETE FROM t WHERE k = $1", "postgresql")
        assert scope_signature(update, update.scopes[0]) == scope_signature(
            delete, delete.scopes[0]
        )
        assert query_signature(update, ("t",)) != query_signature(delete, ("t",))


class TestNoSignature:
    @pytest.mark.parametrize(
        "text",
        [
            "SET search_path = app",
            "",
            "SELECT nextval('seq')",  # reads no stored table
            "THIS IS NOT SQL (",  # the facade falls back
        ],
    )
    def test_is_its_own_pattern(self, text: str) -> None:
        assert _ids([text, text]) == TWO
        facts = access_facts(text, "postgresql")
        assert query_signature(facts) is None


def test_a_digest_cut_short_is_its_own_pattern() -> None:
    """#986: a cut digest's facts show less than the query does, so it is no evidence
    that it reads what a complete statement reads."""
    cut = "SELECT a FROM t JOIN u ON ( t.id = u.t_id AND u.k = ? ) WHERE t.x = ? AND t ."
    whole = "SELECT a FROM t JOIN u ON ( t.id = u.t_id AND u.k = ? ) WHERE t.x = ?"
    facts = access_facts(cut, "mysql")
    assert facts.truncated_read and query_signature(facts) is None
    assert _ids([whole, cut], "mysql") == TWO


class TestGroups:
    def test_frequency_and_rate_are_summed_and_ids_kept(self) -> None:
        queries = [
            _q("a", "SELECT * FROM t WHERE k = $1", 30.0, 0.01),
            _q("b", "SELECT x, y FROM t WHERE k IN ($1, $2)", 90.0, None),
            _q("c", "SELECT x FROM u WHERE k = $1", 5.0),
        ]
        first, second = group_by_query_pattern(queries, "postgresql")
        assert first.pattern_id == "PAT-1" and first.query_ids == ("a", "b")
        assert first.frequency_per_hour == 120.0
        assert first.calls_per_second == 0.01
        assert first.operation == "read" and first.tables == ("t",)
        assert first.returned_columns == ("*", "x", "y")
        assert second.pattern_id == "PAT-2" and second.calls_per_second is None

    def test_an_id_seen_twice_counts_once(self) -> None:
        queries = [_q("a", "SELECT a FROM t"), _q("a", "SELECT a FROM t")]
        (group,) = group_by_query_pattern(queries, "postgresql")
        assert group.query_ids == ("a",) and group.frequency_per_hour == 60.0

    def test_no_queries_no_groups(self) -> None:
        assert group_by_query_pattern([], "postgresql") == []
        assert repeated_query_patterns([], "postgresql") == []

    def test_the_source_dialect_reads_the_text(self) -> None:
        texts = [
            "SELECT [Name] FROM [Product] WITH (NOLOCK) WHERE [ProductID] = @id",
            "SELECT [Sku] FROM [Product] WHERE [ProductID] = @id",
        ]
        assert _ids(texts, "sqlserver") == ONE

    def test_the_designer_list_holds_only_patterns_of_several_queries(self) -> None:
        queries = [
            _q("a", "SELECT a FROM t WHERE k = $1"),
            _q("b", "SELECT b FROM t WHERE k = $1"),
            _q("c", "SELECT c FROM u WHERE k = $1"),
        ]
        (listed,) = repeated_query_patterns(queries, "postgresql")
        assert listed == {
            "pattern_id": "PAT-1",
            "query_ids": ["a", "b"],
            "frequency_per_hour": 120.0,
            "calls_per_second": None,
            "operation": "read",
            "tables": ["t"],
            "returned_columns": ["a", "b"],
        }
        assert listed_query_patterns({"queries": {"query_pattern_groups": [listed]}}) == [listed]
        assert listed_query_patterns({"queries": {}}) is None
        assert listed_query_patterns({}) is None


def test_the_projection_is_read_but_not_part_of_the_signature() -> None:
    star = access_facts("SELECT t.*, u.name FROM t JOIN u ON u.id = t.uid", "postgresql")
    named = access_facts("SELECT t.a FROM t JOIN u ON u.id = t.uid", "postgresql")
    assert returned_columns(star) == ("*", "name")
    assert returned_columns(named) == ("a",)
    assert query_signature(star) == query_signature(named)
