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
        "(SELECT 1) UNION (SELECT 2)",
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


def _sample_queries(sample: str) -> tuple[str, list[str]]:
    with zipfile.ZipFile(_REPO / "docs" / "examples" / sample / f"{sample}.zip") as z:
        name = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data = json.loads(extract_json_object(z.read(name).decode("utf-8")))
    engine, _ = detect_source_engine(data.get("metadata"))
    return engine, [str(q.get("query_text") or "") for q in data["queries"]]


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
