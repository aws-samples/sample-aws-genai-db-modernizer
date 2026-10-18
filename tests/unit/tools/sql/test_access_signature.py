"""The access-pattern signature (``src.tools.sql.access_signature``, #1104 review).

Equal signatures are one access pattern: the same statement collected twice, copies
whose IN-lists differ in length, and reads that differ only in the columns they return.
A different operation (item read, aggregate, write), table, predicate, sort order or
limit class is another access pattern.
"""

from __future__ import annotations

import pytest

from src.tools.sql.access_signature import (
    AccessSignature,
    limit_class,
    operation,
    scope_operation,
    scope_signature,
    statement_signature,
)
from src.tools.sql.facade import access_facts


def _sig(sql: str, engine: str = "postgresql") -> AccessSignature:
    signature = statement_signature(access_facts(sql, engine))
    assert signature is not None
    return signature


class TestOneAccessPattern:
    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(
                "SELECT id FROM t WHERE status IN ($1)",
                "SELECT id FROM t WHERE status IN ($1, $2, $3)",
                id="in-list length",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id, name, created_at FROM t WHERE status = $1",
                id="projection",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = 'open' AND kind = 3",
                "SELECT id FROM t WHERE kind = $2 AND status = $1",
                id="literals and term order",
            ),
            pytest.param(
                "SELECT x.id FROM t x WHERE x.status = $1",
                "SELECT t.id FROM t WHERE t.status = $1",
                id="alias",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1 LIMIT 10",
                "SELECT id FROM t WHERE status = $1 LIMIT $2",
                id="limit n, written or a placeholder",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id FROM t WHERE status = $1",
                id="the same statement twice",
            ),
        ],
    )
    def test_equal(self, first: str, second: str) -> None:
        assert _sig(first) == _sig(second)

    def test_mysql_digest_placeholders_too(self) -> None:
        assert _sig("SELECT id FROM t WHERE status IN (?)", "mysql") == _sig(
            "SELECT id FROM t WHERE status IN (?, ?)", "mysql"
        )


class TestAnotherAccessPattern:
    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT COUNT(*) FROM t WHERE status = $1",
                id="item read vs count",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "DELETE FROM t WHERE status = $1",
                id="read vs write",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id FROM u WHERE status = $1",
                id="table",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id FROM t WHERE status = $1 AND created_at > $2",
                id="a range",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id FROM t WHERE kind = $1",
                id="pinned column",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id FROM t WHERE status = $1 ORDER BY created_at",
                id="order",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1 ORDER BY a, b",
                "SELECT id FROM t WHERE status = $1 ORDER BY b, a",
                id="order sequence",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1 LIMIT 1",
                "SELECT id FROM t WHERE status = $1 LIMIT 2",
                id="limit 1 vs limit n",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id FROM t WHERE status = $1 LIMIT $2",
                id="no limit vs limit n",
            ),
            pytest.param(
                "SELECT id FROM t WHERE status = $1",
                "SELECT id FROM t WHERE status = $1 AND name ILIKE '%' || $2",
                id="text search",
            ),
        ],
    )
    def test_different(self, first: str, second: str) -> None:
        assert _sig(first) != _sig(second)


class TestParts:
    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT id FROM t WHERE a = $1", "read"),
            ("SELECT count(*) FROM t WHERE a = $1", "aggregate"),
            ("SELECT a, max(b) FROM t GROUP BY a", "aggregate"),
            ("SELECT a FROM t GROUP BY a", "aggregate"),
            ("INSERT INTO t (a) VALUES ($1)", "write"),
            ("UPDATE t SET a = $1 WHERE id = $2", "write"),
            ("DELETE FROM t WHERE a = $1", "write"),
            (
                "SELECT id FROM t WHERE id IN (SELECT t_id FROM u GROUP BY t_id)",
                "read",
            ),
        ],
    )
    def test_operation_is_the_statements(self, sql: str, expected: str) -> None:
        assert operation(access_facts(sql, "postgresql")) == expected

    @pytest.mark.parametrize(
        ("limit", "expected"), [(None, "none"), ("1", "one"), ("10", "many"), ("?", "many")]
    )
    def test_limit_class(self, limit: str | None, expected: str) -> None:
        assert limit_class(limit) == expected

    def test_a_nested_lookup_takes_the_statements_operation(self) -> None:
        """A subquery's lookup is the same lookup inside a read or a count, but the
        statement's operation tells the two access patterns apart."""
        inner = "SELECT category_id FROM g WHERE permission IN ($1)"
        read = access_facts(f"SELECT 1 FROM c WHERE c.id IN ({inner}) LIMIT $2", "postgresql")
        count = access_facts(f"SELECT COUNT(*) FROM c WHERE c.id IN ({inner})", "postgresql")
        (read_inner,) = [s for s in read.scopes if s.sources[0].table.name == "g"]
        (count_inner,) = [s for s in count.scopes if s.sources[0].table.name == "g"]
        first, second = scope_signature(read, read_inner), scope_signature(count, count_inner)
        assert (first.tables, first.equalities) == (second.tables, second.equalities)
        assert (first.operation, second.operation) == ("read", "aggregate")

    def test_no_scope_is_no_signature(self) -> None:
        assert statement_signature(access_facts("SET search_path = app", "postgresql")) is None
        assert statement_signature(access_facts("", "postgresql")) is None


class TestScopeOperation:
    def _inner(self, sql: str, table: str):
        facts = access_facts(sql, "postgresql")
        (scope,) = [
            s
            for s in facts.scopes
            if s.sources and getattr(s.sources[0].table, "name", "") == table
        ]
        return facts, scope

    def test_the_rows_a_delete_changes_are_a_write(self) -> None:
        facts, scope = self._inner("DELETE FROM b WHERE k = $1", "b")
        assert scope_operation(facts, scope) == "write"

    def test_a_subquery_inside_a_write_reads_its_table(self) -> None:
        """``DELETE FROM a ... IN (SELECT id FROM b WHERE k = $1)`` reads ``b``: another
        access pattern of ``b`` than ``DELETE FROM b WHERE k = $1``."""
        nested, inner = self._inner("DELETE FROM a WHERE x IN (SELECT id FROM b WHERE k = $1)", "b")
        own, scope = self._inner("DELETE FROM b WHERE k = $1", "b")
        assert scope_operation(nested, inner) == "read"
        assert scope_signature(nested, inner) != scope_signature(own, scope)

    def test_an_insert_select_is_a_write_as_a_statement(self) -> None:
        facts = access_facts("INSERT INTO a (id) SELECT id FROM b WHERE k = $1", "postgresql")
        assert operation(facts) == "write"
        assert statement_signature(facts).operation == "write"  # type: ignore[union-attr]
        assert scope_operation(facts, facts.scopes[0]) == "read"
