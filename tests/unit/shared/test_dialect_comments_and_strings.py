"""MySQL ``#`` comments and PostgreSQL dollar-quoted strings are blanked, per dialect (#1101)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.transaction_shape import transaction_shape
from src.shared.unsupported_pattern import is_dedup_only_group_by, strip_comments_and_literals
from src.tools.sql.query_facts import query_facts
from tests.unit.tools.sql._timing import assert_cost_ratio

# (engine, sql) whose keyword sits inside a MySQL comment or a PostgreSQL string.
_HIDDEN_UPSERT = [
    ("mysql", "SELECT * FROM t # on duplicate key update"),
    ("mariadb", "SELECT * FROM t # replace into x"),
    ("mysql", "SELECT 1 FROM t # note\nWHERE a = 1 # on duplicate key update"),
    ("postgresql", "SELECT $$on conflict$$ FROM t"),
    ("postgres", "SELECT $q$ on conflict do nothing $q$ FROM t"),
    ("postgresql", "SELECT $a$ x $$ on conflict $$ y $a$ FROM t"),
]
_HIDDEN_LATERAL = [
    ("postgresql", "SELECT $$ lateral (x) $$ FROM t"),
    ("postgresql", "SELECT $f$ join lateral (select 1) x $f$ FROM t"),
]
_REAL_UPSERT = [
    ("mysql", "INSERT INTO t VALUES (1) ON DUPLICATE KEY UPDATE a = 1 # again"),
    ("mysql", "# note\nREPLACE INTO t VALUES (1)"),
    ("postgresql", "INSERT INTO t VALUES (1) ON CONFLICT (id) DO NOTHING"),
    ("postgresql", "INSERT INTO t VALUES ($$x$$) ON CONFLICT (id) DO NOTHING"),
]


@pytest.mark.parametrize(("engine", "sql"), _HIDDEN_UPSERT)
def test_an_upsert_keyword_inside_a_comment_or_string_is_not_an_upsert(engine, sql):
    assert query_facts(sql, engine).upsert is False


@pytest.mark.parametrize(("engine", "sql"), _HIDDEN_LATERAL)
def test_lateral_inside_a_dollar_quoted_string_is_not_a_lateral_join(engine, sql):
    assert query_facts(sql, engine).lateral_join is False


@pytest.mark.parametrize(("engine", "sql"), _REAL_UPSERT)
def test_a_real_upsert_still_reads_as_one(engine, sql):
    assert query_facts(sql, engine).upsert is True


def test_a_real_lateral_join_still_reads_with_a_dollar_string_beside_it():
    sql = "SELECT $$x$$ FROM t, LATERAL (SELECT 1) l"
    assert query_facts(sql, "postgresql").lateral_join is True


def test_hash_is_a_comment_for_mysql_and_mariadb_only():
    sql = "SELECT a #b\nFROM t"
    assert "#" not in strip_comments_and_literals(sql, "mysql")
    assert "#" not in strip_comments_and_literals(sql, "MariaDB")
    for engine in ("postgresql", "sqlserver", "oracle", "db2", "", None):
        assert strip_comments_and_literals(sql, engine) == sql


def test_postgresql_reads_hash_as_an_operator():
    # ``#`` is bitwise XOR / the JSON path operator there, so the text after it is live.
    sql = "INSERT INTO t SELECT a # b FROM s ON CONFLICT (id) DO NOTHING"
    assert query_facts(sql, "postgresql").upsert is True


def test_dollar_quoting_is_for_postgresql_only():
    sql = "SELECT $$on conflict$$ FROM t"
    assert "conflict" not in strip_comments_and_literals(sql, "postgresql")
    for engine in ("mysql", "mariadb", "sqlserver", "oracle", "", None):
        assert strip_comments_and_literals(sql, engine) == sql


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT $$a$$ b", "SELECT   b"),
        ("SELECT $t$ $$ x $t$ b", "SELECT   b"),
        ("SELECT $1, $2 FROM t WHERE a = $1", "SELECT $1, $2 FROM t WHERE a = $1"),  # parameters
        ("SELECT a$b$ FROM t$b$", "SELECT a$b$ FROM t$b$"),  # inside identifiers
        ("SELECT $$ unterminated FROM t", "SELECT $$ unterminated FROM t"),
        ("SELECT $a$ x $b$ y $a$ z", "SELECT   z"),  # only the same tag closes
        ("SELECT $a$ x $b$ y", "SELECT $a$ x $b$ y"),  # a different tag is not a closer
        ("SELECT '$$' , $$'$$ FROM t", "SELECT   ,   FROM t"),  # quotes nest in neither way
        ("SELECT $$$$ FROM t", "SELECT   FROM t"),
        ("SELECT $a$$a$ FROM t", "SELECT   FROM t"),  # an empty body
        ("SELECT $a$ $a$ $a$ $a$", "SELECT    "),  # reopens after each close
    ],
)
def test_postgresql_dollar_quoting(sql, expected):
    assert strip_comments_and_literals(sql, "postgresql") == expected


def test_a_hash_inside_a_string_or_a_block_comment_opens_nothing():
    assert strip_comments_and_literals("SELECT '#' /* # */ FROM t", "mysql") == "SELECT     FROM t"
    assert strip_comments_and_literals("SELECT 1 # '", "mysql") == "SELECT 1  "


def test_an_unterminated_dollar_string_is_plain_text_so_a_later_upsert_still_reads():
    sql = "INSERT INTO t VALUES ($$x) ON CONFLICT (id) DO NOTHING"
    assert query_facts(sql, "postgresql").upsert is True


@pytest.mark.parametrize(
    ("engine", "sql"),
    [
        ("postgresql", "UPDATE t SET a = $$ where id = 1 $$"),
        ("mysql", "UPDATE t SET a = 1 # where id = 1"),
    ],
)
def test_a_key_predicate_inside_a_comment_or_string_does_not_key_the_write(engine, sql):
    query = {"query_type": "UPDATE", "query_text": sql, "tables_accessed": ["t"]}
    shape = transaction_shape(query, SourceScope(engine=engine))
    assert shape is not None and shape.shape == "unbounded"


def test_a_key_predicate_outside_them_still_keys_the_write():
    query = {
        "query_type": "UPDATE",
        "query_text": "UPDATE t SET a = $$x$$ WHERE id = 1",
        "tables_accessed": ["t"],
    }
    shape = transaction_shape(query, SourceScope(engine="postgresql"))
    assert shape is not None and shape.shape == "single item"


def _dollar_openers(n_chars: int) -> str:
    return "SELECT * FROM t WHERE id = 1 " + "$a$ " * (n_chars // 4)


def _distinct_dollar_tags(n_chars: int) -> str:
    return "SELECT 1 " + " ".join(f"$t{i}$" for i in range(n_chars // 8))


def _hash_runs(n_chars: int) -> str:
    return "SELECT 1 " + "# x\n" * (n_chars // 4)


def _paired_dollar_strings(n_chars: int) -> str:
    return "SELECT 1 " + "$q$ x $q$ " * (n_chars // 9)


def _linear(make_text: Callable[[int], str], engine: str, long_chars: int = 50_000) -> None:
    short = 1_000
    long_texts = [make_text(long_chars)]
    short_texts = [make_text(short) for _ in range(long_chars // short)]

    def run(texts: list[str]) -> None:
        for text in texts:
            strip_comments_and_literals(text, engine)

    assert_cost_ratio(lambda: run(long_texts), lambda: run(short_texts), 3.0)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("make_text", "engine"),
    [
        (_dollar_openers, "postgresql"),
        (_distinct_dollar_tags, "postgresql"),
        (_paired_dollar_strings, "postgresql"),
        (_hash_runs, "mysql"),
    ],
    ids=lambda p: getattr(p, "__name__", p),
)
def test_the_dialect_strip_is_linear_in_the_query_length(make_text, engine):
    _linear(make_text, engine)


def test_a_hash_inside_a_backtick_identifier_is_not_a_comment():
    sql = "INSERT INTO `t#1` (a) VALUES (1) ON DUPLICATE KEY UPDATE a=1"
    assert query_facts(sql, "mysql").upsert is True
    assert query_facts(sql, "mariadb").upsert is True
    assert "ON DUPLICATE KEY UPDATE" in strip_comments_and_literals(sql, "mysql")


def test_a_write_to_a_hash_named_table_stays_a_single_item():
    query = {
        "query_type": "UPDATE",
        "query_text": "UPDATE `order#items` SET a = 1 WHERE id = 1",
        "tables_accessed": ["order#items"],
    }
    for engine in ("mysql", "mariadb", "postgresql", None):
        scope = SourceScope(engine=engine) if engine else None
        shape = transaction_shape(query, scope)
        assert shape is not None and shape.shape == "single item", engine


def test_the_transaction_shape_still_drops_a_hash_comment_after_a_quoted_name():
    query = {
        "query_type": "UPDATE",
        "query_text": "UPDATE `t` SET a = 1 # WHERE id = 1",
        "tables_accessed": ["t"],
    }
    shape = transaction_shape(query, SourceScope(engine="mysql"))
    assert shape is not None and shape.shape == "unbounded"


def test_the_dedup_check_reads_the_source_dialect():
    # The GROUP BY sits in a MySQL comment: it is no GROUP BY there, a live one elsewhere.
    sql = "SELECT a FROM t # GROUP BY a"
    assert is_dedup_only_group_by(sql, "mysql") is False
    assert is_dedup_only_group_by(sql) is True
    assert query_facts(sql, "mysql").dedup_only_group_by is False
