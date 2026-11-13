# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One ``query_type`` rule, comment-aware and dialect-aware (#1178).

A statement that opens with a comment (``-- endpoint: CartController.Add\\nSELECT``)
used to be typed ``OTHER`` by all six copies of the rule, although the SQL facade
read it as a ``select``.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import detect_source_engine
from src.tools.sql.facade import access_facts
from src.tools.sql.query_type import statement_query_type

_REPO = Path(__file__).resolve().parents[4]
_ENGINES = ("mysql", "mariadb", "postgresql", "sqlserver", "oracle", "db2")
_LOOKUP = "SELECT p.UnitPrice FROM dbo.Products p WHERE p.ProductId = 1"


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "prefix",
    [
        "-- endpoint: CartController.Add\n",
        "/* endpoint: CartController.Add */ ",
        "/* a */\n/* b */\n",
        "-- one\n-- two\n   -- three\n",
        "  \n-- tag\n/* block */\n-- tag 2\n",
        "/* multi\n   line */\n",
    ],
)
def test_a_leading_comment_does_not_change_the_type(engine: str, prefix: str) -> None:
    assert statement_query_type(_LOOKUP, engine) == "SELECT"
    assert statement_query_type(prefix + _LOOKUP, engine) == "SELECT"


@pytest.mark.parametrize(
    ("verb", "sql"),
    [
        ("SELECT", "select 1"),
        ("INSERT", "INSERT INTO t (a) VALUES (1)"),
        ("UPDATE", "update t set a = 1 where id = 2"),
        ("DELETE", "DELETE FROM t WHERE id = 2"),
        ("MERGE", "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN DELETE"),
    ],
)
@pytest.mark.parametrize("engine", _ENGINES)
def test_each_verb_with_and_without_a_comment(engine: str, verb: str, sql: str) -> None:
    assert statement_query_type(sql, engine) == verb
    assert statement_query_type("-- tag\n" + sql, engine) == verb
    assert statement_query_type("/* tag */" + sql, engine) == verb


@pytest.mark.parametrize("engine", ["postgresql", "sqlserver"])
def test_nested_block_comments_where_the_dialect_nests_them(engine: str) -> None:
    sql = "/* outer /* inner */ still a comment */ DELETE FROM t WHERE id = 1"
    assert statement_query_type(sql, engine) == "DELETE"


@pytest.mark.parametrize("engine", ["mysql", "mariadb", "oracle"])
def test_a_block_comment_ends_at_its_first_close_where_it_does_not_nest(engine: str) -> None:
    # The comment ends at the first */, so the statement opens with "still".
    sql = "/* outer /* inner */ still a comment */ DELETE FROM t WHERE id = 1"
    assert statement_query_type(sql, engine) == "OTHER"
    assert statement_query_type("/* a /* b */ DELETE FROM t", engine) == "DELETE"


@pytest.mark.parametrize("engine", ["mysql", "mariadb"])
def test_a_hash_comment_in_the_mysql_family(engine: str) -> None:
    assert statement_query_type("# tag\nSELECT 1", engine) == "SELECT"
    assert statement_query_type("# one\n# two\nUPDATE t SET a = 1", engine) == "UPDATE"


@pytest.mark.parametrize("engine", ["postgresql", "sqlserver", "oracle"])
def test_a_hash_is_not_a_comment_outside_the_mysql_family(engine: str) -> None:
    assert statement_query_type("# tag\nSELECT 1", engine) == "OTHER"


def test_a_mysql_executable_comment_is_code_not_a_comment() -> None:
    assert statement_query_type("/*!40101 SET NAMES utf8 */", "mysql") == "OTHER"
    # MySQL runs the body, but its verb is not the statement's first word: OTHER, as
    # the facade reads it too (not stripped past, not unwrapped).
    assert statement_query_type("/*!50000 SELECT */ 1", "mysql") == "OTHER"
    assert statement_query_type("/*+ hint */ SELECT 1", "mysql") == "SELECT"


def test_replace_is_a_statement_only_in_the_mysql_family() -> None:
    sql = "REPLACE INTO t (a) VALUES (1)"
    assert statement_query_type(sql, "mysql") == "REPLACE"
    assert statement_query_type("-- tag\n" + sql, "mariadb") == "REPLACE"
    assert statement_query_type(sql, "") == "REPLACE"  # as the RDS tools typed it
    for engine in ("postgresql", "sqlserver", "oracle"):
        assert statement_query_type(sql, engine) == "OTHER"


@pytest.mark.parametrize(
    ("sql", "engine", "expected"),
    [
        ("COPY wp_posts FROM STDIN WITH CSV", "postgresql", "INSERT"),
        ("-- load\nCOPY wp_posts FROM STDIN", "postgresql", "INSERT"),
        ("COPY wp_posts TO STDOUT", "postgresql", "OTHER"),
        ("LOAD DATA LOCAL INFILE ? INTO TABLE wp_posts", "mysql", "INSERT"),
        ("# load\nLOAD DATA INFILE 'f' INTO TABLE t", "mysql", "INSERT"),
    ],
)
def test_a_bulk_load_is_an_insert(sql: str, engine: str, expected: str) -> None:
    assert statement_query_type(sql, engine) == expected


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "-- only a comment",
        "/* only a comment */",
        "/* unterminated SELECT 1",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SET NAMES utf8",
        "BEGIN NULL; END;",
        "EXEC sp_helpdb",
        "SELECTED_ROWS",
    ],
)
def test_what_does_not_open_with_a_verb_is_other(engine: str, sql: str) -> None:
    assert statement_query_type(sql, engine) == "OTHER"


def test_none_text_is_other() -> None:
    assert statement_query_type(None, "mysql") == "OTHER"  # type: ignore[arg-type]


def test_an_oracle_hint_right_after_the_verb() -> None:
    # The old Oracle copy read the first whitespace-separated token ("SELECT/*+").
    assert statement_query_type("SELECT/*+ INDEX(t) */ * FROM t", "oracle") == "SELECT"


_SAMPLES = ("wordpress", "discourse", "trailgear")


def _sample_collection(sample: str) -> tuple[str, list[dict]]:
    with zipfile.ZipFile(_REPO / "docs" / "examples" / sample / f"{sample}.zip") as z:
        name = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data = json.loads(extract_json_object(z.read(name).decode("utf-8")))
    engine, _ = detect_source_engine(data.get("metadata"))
    return engine, list(data["queries"])


def _sample_queries(sample: str) -> tuple[str, list[str]]:
    engine, queries = _sample_collection(sample)
    return engine, [str(q.get("query_text") or "") for q in queries]


@pytest.mark.parametrize("sample", _SAMPLES)
def test_the_type_agrees_with_the_facades_statement_kind(sample: str) -> None:
    """Every sample query that opens with a verb and that the facade parses gets the
    facade's own statement kind (``REPLACE`` is the facade's ``insert``)."""
    engine, texts = _sample_queries(sample)
    checked = 0
    for text in texts:
        typed = statement_query_type(text, engine)
        facts = access_facts(text, engine)
        if typed == "OTHER" or facts.parsed_by != "sqlglot":
            continue
        expected = "INSERT" if typed == "REPLACE" else typed
        assert facts.kind.upper() == expected, text[:120]
        checked += 1
    assert checked > 50


@pytest.mark.parametrize("prefix", ["", "-- tag\n", "# tag\n", "/* tag */ "])
def test_the_facade_reads_a_tagged_replace_as_the_insert_it_is(prefix: str) -> None:
    """The facade read a MySQL REPLACE as an INSERT only at the very start of the text,
    so a tagged one fell back to a generic command (#1178)."""
    facts = access_facts(prefix + "REPLACE INTO t (a) VALUES (1)", "mysql")
    assert (facts.kind, facts.parsed_by) == ("insert", "sqlglot")


@pytest.mark.parametrize(
    ("engine", "sql", "expected"),
    [
        (
            "postgresql",
            "WITH a AS (SELECT 1) UPDATE t x SET c = 1 FROM a WHERE x.id = a.id",
            "UPDATE",
        ),
        ("postgresql", "WITH a AS (SELECT 1), b AS (SELECT 2) DELETE FROM t USING a, b", "DELETE"),
        (
            "postgresql",
            "-- tag\nWITH a AS (SELECT 1 AS id) INSERT INTO t SELECT id FROM a",
            "INSERT",
        ),
        ("postgresql", "/* tag */ WITH RECURSIVE a AS (SELECT 1) UPDATE t SET c = 1", "UPDATE"),
        ("sqlserver", "WITH c AS (SELECT id FROM t) UPDATE c SET x = 1", "UPDATE"),
        ("sqlserver", "WITH c AS (SELECT id FROM t) DELETE FROM c", "DELETE"),
        (
            "sqlserver",
            "WITH c AS (SELECT id FROM t) MERGE t USING c ON t.id = c.id "
            "WHEN MATCHED THEN UPDATE SET x = 1;",
            "MERGE",
        ),
        (
            "mysql",
            "WITH c AS (SELECT 1 AS id) UPDATE t JOIN c ON t.id = c.id SET t.x = 1",
            "UPDATE",
        ),
        (
            "mysql",
            "# tag\nWITH c AS (SELECT 1 AS id) DELETE FROM t WHERE id IN (SELECT id FROM c)",
            "DELETE",
        ),
        ("sqlserver", ";WITH c AS (SELECT id FROM t) UPDATE c SET x = 1", "UPDATE"),
        ("sqlserver", "-- tag\n;WITH c AS (SELECT id FROM t) DELETE FROM c", "DELETE"),
    ],
)
def test_a_with_led_write_is_typed_by_its_final_statement(
    engine: str, sql: str, expected: str
) -> None:
    assert statement_query_type(sql, engine) == expected


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "WITH a AS (SELECT 1) SELECT * FROM a",
        "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b",
        "WITH a AS (SELECT 1) SELECT * FROM a WHERE s = 'UPDATE t SET x = 1'",
        "WITH a AS (SELECT 'DELETE FROM t') SELECT * FROM a",
        "WITH",
    ],
)
@pytest.mark.passes_on_main
def test_a_with_led_read_stays_other(engine: str, sql: str) -> None:
    assert statement_query_type(sql, engine) == "OTHER"


@pytest.mark.passes_on_main
def test_a_postgresql_data_modifying_cte_ending_in_select_stays_other() -> None:
    """The facade calls it a select; is_write_query re-reads the text for OTHER."""
    sql = "WITH w AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM w"
    assert statement_query_type(sql, "postgresql") == "OTHER"


@pytest.mark.passes_on_main
def test_a_select_led_statement_with_a_cte_inside_stays_a_select() -> None:
    sql = "SELECT * FROM (WITH w AS (SELECT 1 AS a) SELECT a FROM w) q"
    assert statement_query_type(sql, "postgresql") == "SELECT"


# A statement that opens with a parenthesis is a parenthesized query expression, or a
# set operation over them, when the facade reads it as a query (#2159).
_PAREN_READS = (
    "(SELECT 1 FROM post_actions WHERE post_action_type_id = 2 LIMIT 1) UNION "
    "(SELECT 1 FROM reviewable_scores WHERE reviewable_score_type = 3 LIMIT 1)",
    "(SELECT 1) UNION (SELECT 2)",
    "(SELECT a FROM t WHERE id = 1) UNION ALL (SELECT a FROM u WHERE id = 2)",
    "((SELECT a FROM t)) UNION ((SELECT a FROM u))",
    "(((SELECT a FROM t) UNION (SELECT a FROM u)) UNION (SELECT a FROM v))",
    "(SELECT a FROM t) UNION (SELECT a FROM u) ORDER BY a LIMIT 10",
    "(SELECT a FROM t ORDER BY a LIMIT 5) UNION ALL (SELECT a FROM u) LIMIT 7",
    "(SELECT a FROM t WHERE id = 1)",
    "( \n  SELECT a FROM t\n)\nUNION\n(\n  SELECT a FROM u\n)",
    "(SELECT a FROM t) INTERSECT (SELECT a FROM u)",
    "(SELECT a FROM t) EXCEPT (SELECT a FROM u)",
    "-- tag\n(SELECT a FROM t) UNION (SELECT a FROM u)",
    "/* tag */ (SELECT a FROM t) UNION (SELECT a FROM u)",
    "( /* inner */ SELECT a FROM t) UNION (SELECT a FROM u)",
    "(-- inner\nSELECT a FROM t) UNION (SELECT a FROM u)",
    "(select a from t) union (select a from u)",
)


@pytest.mark.parametrize("engine", ["postgresql", "mysql", "mariadb"])
@pytest.mark.parametrize("sql", _PAREN_READS)
def test_a_parenthesized_query_expression_is_a_select(engine: str, sql: str) -> None:
    assert statement_query_type(sql, engine) == "SELECT"
    # The same as the unparenthesized statement, and as the facade reads it.
    assert access_facts(sql, engine).kind == "select"


@pytest.mark.parametrize(
    ("engine", "sql"),
    [
        (
            "postgresql",
            "(SELECT $1 FROM post_actions WHERE post_action_type_id = $2 LIMIT $3) UNION "
            "(SELECT $4 FROM reviewable_scores WHERE reviewable_score_type =",
        ),
        ("mysql", "(SELECT ? FROM post_actions WHERE id = ? LIMIT ?) UNION (SELECT ? FROM r WHERE"),
    ],
)
def test_a_cut_short_parenthesized_union_is_still_a_select(engine: str, sql: str) -> None:
    """A digest the collector cut short is still a read, as its unparenthesized form is."""
    assert statement_query_type(sql, engine) == "SELECT"


@pytest.mark.parametrize("engine", ["sqlserver", "oracle", "db2"])
def test_a_parenthesized_union_is_a_select_in_other_dialects(engine: str) -> None:
    sql = "(SELECT a FROM t WHERE id = 1) UNION ALL (SELECT a FROM u WHERE id = 2)"
    assert statement_query_type(sql, engine) == "SELECT"
    assert statement_query_type("-- tag\n" + sql, engine) == "SELECT"


def test_a_comment_inside_the_parenthesis_follows_the_dialect() -> None:
    sql = "( # tag\nSELECT a FROM t) UNION (SELECT a FROM u)"
    assert statement_query_type(sql, "mysql") == "SELECT"
    # ``#`` is no comment in PostgreSQL, so the parenthesis opens no SELECT there.
    assert statement_query_type(sql, "postgresql") == "OTHER"


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "sql",
    [
        "(",
        "()",
        "(((",
        "(1, 2)",
        "((1))",
        "(SELECTED_ROWS)",
        "(SET NAMES utf8)",
        "(VALUES (1), (2))",
        "(foo bar) UNION (baz)",
        "( -- only a comment",
        # An expression around a subquery is no statement, as the facade reads it.
        "(SELECT 1) = 1",
        "(SELECT a FROM t) AS x",
        "(SELECT a FROM t) IS NULL",
        # Only a query expression is ever wrapped: a wrapped DML verb stays OTHER, so
        # is_write_query re-reads its text and still finds the write.
        "(DELETE FROM t WHERE id = 1)",
        "(UPDATE t SET a = 1)",
    ],
)
def test_a_parenthesis_that_opens_no_select_stays_other(engine: str, sql: str) -> None:
    assert statement_query_type(sql, engine) == "OTHER"


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", ["postgresql", "mysql", "sqlserver"])
@pytest.mark.parametrize(
    "sql",
    [
        "WITH a AS (SELECT 1 AS x) (SELECT x FROM a) UNION (SELECT 2)",
        "(WITH a AS (SELECT 1 AS x) SELECT x FROM a) UNION (SELECT 2)",
    ],
)
def test_a_with_led_read_in_parentheses_stays_other(engine: str, sql: str) -> None:
    """A ``WITH`` read stays ``OTHER`` (#1287) whether or not it is wrapped."""
    assert statement_query_type(sql, engine) == "OTHER"


@pytest.mark.passes_on_main
def test_a_wrapped_data_modifying_cte_stays_other() -> None:
    sql = "(WITH w AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM w)"
    assert statement_query_type(sql, "postgresql") == "OTHER"


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("engine", "sql", "expected"),
    [
        ("postgresql", "INSERT INTO t (SELECT a FROM u)", "INSERT"),
        ("mysql", "INSERT INTO t (a) (SELECT a FROM u UNION SELECT a FROM v)", "INSERT"),
        (
            "postgresql",
            "DELETE FROM t WHERE id IN ((SELECT id FROM u) UNION (SELECT id FROM v))",
            "DELETE",
        ),
        ("mysql", "UPDATE t SET a = (SELECT MAX(a) FROM u) WHERE id = 1", "UPDATE"),
        ("mysql", "REPLACE INTO t (SELECT * FROM u)", "REPLACE"),
        ("postgresql", "SELECT * FROM ((SELECT a FROM t) UNION (SELECT a FROM u)) q", "SELECT"),
    ],
)
def test_a_parenthesized_subquery_inside_a_statement_keeps_its_verb(
    engine: str, sql: str, expected: str
) -> None:
    assert statement_query_type(sql, engine) == expected


def test_the_discourse_parenthesized_unions_are_selects() -> None:
    """The three discourse statements that open with a parenthesis (#2159), the
    existence-probe union ``4582415969089977699`` among them, are reads."""
    engine, queries = _sample_collection("discourse")
    typed = {
        str(q.get("digest")): statement_query_type(str(q.get("query_text") or ""), engine)
        for q in queries
        if str(q.get("query_text") or "").lstrip().startswith("(")
    }
    assert typed == {
        "7727645011310952261": "SELECT",
        "-8610820431710591320": "SELECT",
        "4582415969089977699": "SELECT",
    }
