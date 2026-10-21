"""A comment after a statement's closing ``;`` never changes how it parses (#1335).

Every sample query is read twice, plain and with ``/*a='b'*/`` appended after its
``;``: the same tables, facts, access facts, regex shadow and stored readings, in
every source dialect. ``query_text`` and the tags keep the comment.
"""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest

from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.facade import read_sql
from src.tools.sql.query_type import statement_query_type
from src.tools.sql.regex_tables import regex_table_refs

_REPO = Path(__file__).resolve().parents[4]
_SAMPLES = {"trailgear": "sqlserver", "wordpress": "mysql", "discourse": "postgresql"}
_ENGINES = ("mysql", "postgresql", "sqlserver", "oracle")
_COMMENT = " /*a='b'*/"


def _queries(name: str) -> list[dict]:
    with zipfile.ZipFile(_REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    return list(data["queries"])


def _terminated(text: str) -> str:
    text = text.rstrip()
    return text if text.endswith(";") else text + ";"


@pytest.mark.parametrize("name", sorted(_SAMPLES))
def test_every_sample_query_reads_the_same_with_a_comment_after_its_semicolon(name: str) -> None:
    differing = []
    for q in _queries(name):
        plain = _terminated(q["query_text"])
        tagged = plain + _COMMENT
        for engine in _ENGINES:
            if (
                read_sql(plain, engine) != read_sql(tagged, engine)
                or regex_table_refs(plain, engine) != regex_table_refs(tagged, engine)
                or statement_query_type(plain, engine) != statement_query_type(tagged, engine)
            ):
                differing.append((engine, plain[:80]))
    assert not differing


@pytest.mark.parametrize("name", sorted(_SAMPLES))
def test_the_collector_stores_the_same_reading_and_keeps_text_and_tags(name: str) -> None:
    raw = {"queries": _queries(name)}
    plain_raw = copy.deepcopy(raw)
    tagged_raw = copy.deepcopy(raw)
    for p, t in zip(plain_raw["queries"], tagged_raw["queries"], strict=True):
        p["query_text"] = _terminated(p["query_text"])
        t["query_text"] = p["query_text"] + _COMMENT
    source = _SAMPLES[name]
    plain = parse_offline_collection(
        {**plain_raw, "metadata": {"source_database": {"engine": source}}}
    )["queries"]
    tagged = parse_offline_collection(
        {**tagged_raw, "metadata": {"source_database": {"engine": source}}}
    )["queries"]
    assert len(plain) == len(tagged) > 50
    ignored = {"query_text", "query_tags", "parsed_facts"}
    for p, t in zip(plain, tagged, strict=True):
        assert {k: v for k, v in p.items() if k not in ignored} == {
            k: v for k, v in t.items() if k not in ignored
        }, p["query_text"][:80]
        assert t["query_text"] == p["query_text"] + _COMMENT  # query_text is unchanged
        assert t["query_tags"] == [*p["query_tags"], "a='b'"]  # tags still read it
        drop = lambda f: {k: v for k, v in f.items() if k != "text_sha256"}  # noqa: E731
        assert drop(t["parsed_facts"]) == drop(p["parsed_facts"]), p["query_text"][:80]


class TestWithoutCommentsAfterTerminator:
    @pytest.mark.parametrize(
        "text, kwargs, expected",
        [
            ("MERGE t USING s ON 1=1; /*a='b'*/", {}, "MERGE t USING s ON 1=1;"),
            ("SELECT 1; -- note", {}, "SELECT 1;"),
            ("SELECT 1; -- a\n/* b */\n", {}, "SELECT 1;"),
            ("SELECT 1;/* a */ /* b */", {}, "SELECT 1;"),
            # several statements: cut after the last ';'
            ("SELECT 1; SELECT 2; /* c */", {}, "SELECT 1; SELECT 2;"),
            # a comment between statements is not after the final one
            ("SELECT 1; /* c */ SELECT 2;", {}, "SELECT 1; /* c */ SELECT 2;"),
            ("SELECT 1; /* c */ SELECT 2", {}, "SELECT 1; /* c */ SELECT 2"),
            # ';' in a string literal or a quoted name ends nothing
            ("SELECT ';' /* c */", {}, "SELECT ';' /* c */"),
            ("SELECT 'a;b'; /* c */", {}, "SELECT 'a;b';"),
            ("SELECT 'it''s; ok'; /* c */", {}, "SELECT 'it''s; ok';"),
            ('SELECT "a;" /* c */', {}, 'SELECT "a;" /* c */'),
            # ';' in a comment ends nothing
            ("SELECT 1 /* ; */", {}, "SELECT 1 /* ; */"),
            ("SELECT 1 -- a;\n", {}, "SELECT 1 -- a;\n"),
            # a ';' before the comment but code after the comment
            ("SELECT 1; /* c */ ;", {}, "SELECT 1; /* c */ ;"),
            # no ';', no comment, nothing to trim
            ("SELECT 1 /* c */", {}, "SELECT 1 /* c */"),
            ("SELECT 1;", {}, "SELECT 1;"),
            ("SELECT 1;  \n", {}, "SELECT 1;  \n"),
            ("/* only */", {}, "/* only */"),
            ("", {}, ""),
            # an unterminated block comment is code
            ("SELECT 1; /* open", {}, "SELECT 1; /* open"),
            # a block comment ends at its first */, unless it nests
            ("SELECT 1; /* a /* b */ c */", {}, "SELECT 1; /* a /* b */ c */"),
            ("SELECT 1; /* a /* b */ c */", {"nested": True}, "SELECT 1;"),
            ("SELECT 1; /* a /* b */ /* c */ d */", {"nested": True}, "SELECT 1;"),
            # '#' is a comment only for MySQL
            ("SELECT 1; # note\n", {"hash_comments": True}, "SELECT 1;"),
            ("SELECT 1; # note", {"hash_comments": True}, "SELECT 1;"),
            ("SELECT 1; # note\n", {}, "SELECT 1; # note\n"),
            ("SELECT '\\';' ; # n", {"hash_comments": True}, "SELECT '\\';' ;"),
            # a MySQL executable comment is code
            (
                "SELECT 1; /*!40101 SET NAMES utf8 */",
                {"mysql_executable": True},
                "SELECT 1; /*!40101 SET NAMES utf8 */",
            ),
            (
                "SELECT 1; /*!40101 x */ -- c",
                {"mysql_executable": True},
                "SELECT 1; /*!40101 x */ -- c",
            ),
            ("SELECT 1; /*+ hint */", {}, "SELECT 1;"),
            # quote forms that hold a ';' or a comment marker as data (review of #1338)
            ("SELECT $$a; /* b */", {"engine": "postgresql"}, "SELECT $$a; /* b */"),
            ("SELECT $$a$$; /* b */", {"engine": "postgresql"}, "SELECT $$a$$;"),
            (
                "SELECT $t$a; -- b$t$ /* c */",
                {"engine": "postgresql"},
                "SELECT $t$a; -- b$t$ /* c */",
            ),
            ("SELECT [a; -- b] FROM t", {"engine": "sqlserver"}, "SELECT [a; -- b] FROM t"),
            ("SELECT [a;] FROM t; /* c */", {"engine": "sqlserver"}, "SELECT [a;] FROM t;"),
            (
                "SELECT [a]]; -- b] FROM t -- c",
                {"engine": "sqlserver"},
                "SELECT [a]]; -- b] FROM t -- c",
            ),
            (
                "SELECT q'{a'; -- b}' FROM dual",
                {"engine": "oracle"},
                "SELECT q'{a'; -- b}' FROM dual",
            ),
            (
                "SELECT q'{a'; -- b}' FROM dual; -- c",
                {"engine": "oracle"},
                "SELECT q'{a'; -- b}' FROM dual;",
            ),
            ("SELECT E'a\\'; -- b'", {"engine": "postgresql"}, "SELECT E'a\\'; -- b'"),
            ("SELECT E'a\\'; -- b'; -- c", {"engine": "postgresql"}, "SELECT E'a\\'; -- b';"),
        ],
    )
    def test_cases(self, text: str, kwargs: dict, expected: str) -> None:
        from src.tools.sql.normalize import without_comments_after_terminator

        if kwargs.get("engine"):
            engine = kwargs["engine"]
        elif kwargs.get("hash_comments") or kwargs.get("mysql_executable"):
            engine = "mysql"
        elif kwargs.get("nested"):
            engine = "postgresql"
        else:
            engine = "oracle"  # ends a block comment at its first */, has no '#' comment
        assert without_comments_after_terminator(text, engine) == expected

    def test_the_engine_picks_the_comment_syntax(self) -> None:
        from src.tools.sql.normalize import without_comments_after_terminator

        assert without_comments_after_terminator("SELECT 1; # n\n", "mysql") == "SELECT 1;"
        assert without_comments_after_terminator("SELECT 1; # n\n", "mariadb") == "SELECT 1;"
        assert (
            without_comments_after_terminator("SELECT 1; # n\n", "postgresql") == "SELECT 1; # n\n"
        )
        assert (
            without_comments_after_terminator("SELECT 1; /* a /* b */ */", "postgresql")
            == "SELECT 1;"
        )
        assert (
            without_comments_after_terminator("SELECT 1; /* a /* b */ */", "sqlserver")
            == "SELECT 1;"
        )
        assert (
            without_comments_after_terminator("SELECT 1; /* a /* b */ */", "oracle")
            == "SELECT 1; /* a /* b */ */"
        )
        assert without_comments_after_terminator("SELECT 1; /* c */", None) == "SELECT 1;"


def test_a_semicolon_in_a_string_literal_is_not_a_terminator() -> None:
    plain = "SELECT a FROM t WHERE b = 'x;y'"
    for engine in _ENGINES:
        assert read_sql(plain + " /*a='b'*/", engine) == read_sql(plain, engine)
        assert read_sql(plain + "; /*a='b'*/", engine) == read_sql(plain + ";", engine)


def test_several_statements_with_a_trailing_comment_read_like_without() -> None:
    plain = "UPDATE a SET x = 1; UPDATE b SET y = 2;"
    for engine in _ENGINES:
        assert read_sql(plain + _COMMENT, engine) == read_sql(plain, engine)
        assert regex_table_refs(plain + _COMMENT, engine) == regex_table_refs(plain, engine)


def test_mysql_hash_comment_after_the_semicolon() -> None:
    plain = "UPDATE `t` SET `x` = ? WHERE `id` = ?;"
    assert read_sql(plain + " # trace\n", "mysql") == read_sql(plain, "mysql")


def test_nested_block_comment_after_the_semicolon_in_postgresql() -> None:
    plain = "DELETE FROM t WHERE id = $1;"
    tagged = plain + " /* a /* b */ c */"
    assert read_sql(tagged, "postgresql") == read_sql(plain, "postgresql")


@pytest.mark.parametrize("engine", _ENGINES)
def test_the_regex_shadow_ignores_table_words_in_a_comment_after_the_semicolon(engine: str) -> None:
    plain = "UPDATE `t` SET x = 1 WHERE id = 2;" if engine == "mysql" else "UPDATE t SET x = 1;"
    tagged = plain + " /* from audit_log join other_table */"
    assert regex_table_refs(tagged, engine) == regex_table_refs(plain, engine) == [([], "t")]
    assert read_sql(tagged, engine) == read_sql(plain, engine)


@pytest.mark.parametrize("engine", [*_ENGINES, "mariadb", "tsql", None])
def test_the_trim_only_ever_removes_a_comment_tail(engine: str | None) -> None:
    """For random inputs the result is a prefix of the text, and what is cut off is
    only comments and whitespace (checked by blanking comments with the comment rule
    of the shared scanner: nothing but whitespace is left of the cut part)."""
    import random

    from src.shared.sql_comments import block_comments_nest, strip_comments
    from src.tools.sql.normalize import without_comments_after_terminator

    pieces = [
        ";", " ", "\n", "'", '"', "`", "[", "]", "$$", "$t$", "E'", "q'{", "}'", "\\",
        "--", "#", "/*", "*/", "/*!", "x", "SELECT", "(", ")", "a;b",
        ";", "; /* c */", "-- c\n", "/* c */", "; -- c\n",
    ]  # fmt: skip
    rng = random.Random(1335)
    trimmed = 0
    for _ in range(4000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        out = without_comments_after_terminator(text, engine)
        assert text.startswith(out), text
        removed = text[len(out) :]
        if removed:
            trimmed += 1
            assert out.endswith(";"), text
            mysql = engine == "mariadb" or engine == "mysql"
            rest = strip_comments(removed, nested=block_comments_nest(engine), hash_comments=mysql)
            assert not rest.strip(), (text, removed)
            assert "/*!" not in removed or not mysql, text
    assert trimmed > 20
