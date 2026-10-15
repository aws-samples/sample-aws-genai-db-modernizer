"""The offline parser's query-shape facts come from the sqlglot facade (#796).

``has_joins``, ``join_count``, ``has_aggregations`` and ``has_subqueries`` used
to be regexes on the raw text, which read comments, string literals and column
names as SQL. These tests pin the misreads the bundled samples had (discourse:
21 queries) and that a statement sqlglot can't read keeps the regex answer.
"""

from __future__ import annotations

import pytest

import src.tools.sql.facade as facade
from src.tools.database.offline_parser import parse_offline_collection

_FACTS = ("has_joins", "join_count", "has_aggregations", "has_subqueries")


def _facts(sql: str, engine: str = "postgresql") -> dict:
    data = {
        "metadata": {"database_name": "shop", "engine": engine},
        "tables": [{"table_name": n} for n in ("users", "posts", "topics", "themes", "requests")],
        "queries": [{"query_text": sql, "digest": "q0", "execution_count": 1}],
    }
    query = parse_offline_collection(data)["queries"][0]
    return {k: query[k] for k in _FACTS}


@pytest.mark.parametrize(
    "sql,changed",
    [
        # A column named count is not an aggregate (discourse: 8 queries).
        ("UPDATE requests SET count = count + $1 WHERE id = $2", {"has_aggregations": False}),
        # A column named user_selectable is not a second SELECT (4 queries).
        ("SELECT id FROM themes WHERE (user_selectable OR id = $1)", {"has_subqueries": False}),
        # JOIN after a newline or a tab is a join (3 queries).
        ("SELECT * FROM users u\nJOIN posts p ON p.user_id = u.id", {"has_joins": True}),
        # A comma-separated FROM list joins its tables (1 query).
        (
            "SELECT * FROM users, posts, topics WHERE posts.user_id = users.id",
            {"has_joins": True, "join_count": 2},
        ),
        # A "join" in a comment is not a join (3 queries).
        (
            "SELECT u.id FROM users u -- users allowed to join chat\n"
            "JOIN posts p ON p.user_id = u.id",
            {"join_count": 1},
        ),
    ],
)
def test_regex_misreads_are_gone(sql, changed):
    facts = _facts(sql)
    assert {k: facts[k] for k in changed} == changed


def test_a_fallback_keeps_the_regex_facts():
    # REFRESH MATERIALIZED VIEW is a generic Command node in sqlglot.
    assert _facts("REFRESH MATERIALIZED VIEW mv_count -- join select select") == {
        "has_joins": True,
        "join_count": 1,
        "has_aggregations": False,
        "has_subqueries": True,
    }


def test_a_failing_facade_falls_back_to_the_regex_facts(monkeypatch):
    def boom(sql, engine):
        raise RuntimeError("facade bug")

    monkeypatch.setattr(facade, "read_sql", boom)
    assert _facts("UPDATE requests SET count = count + $1 WHERE id = $2") == {
        "has_joins": False,
        "join_count": 0,
        "has_aggregations": True,
        "has_subqueries": False,
    }
