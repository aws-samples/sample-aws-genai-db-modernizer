"""A comment between two statements never changes how they parse (#1340).

#1338 trimmed comments after the last ``;``; this covers the ones between statements:
sqlglot reads such a comment as a statement of its own, so the facade fell back with
``not_a_statement``. The cut uses the same dialect-aware scan, so quoted text is data.
"""

from __future__ import annotations

import random

import pytest

from src.shared.sql_comments import block_comment_end, block_comments_nest
from src.tools.sql.facade import read_sql
from src.tools.sql.normalize import (
    _BRACKET_DIALECTS,
    _scan,
    without_comments_between_statements,
)
from src.tools.sql.regex_tables import regex_table_refs

_ENGINES = ("mysql", "postgresql", "sqlserver", "oracle")


def _cut(text: str, engine: str | None) -> str:
    return without_comments_between_statements(text, engine)


def _squeezed(text: str) -> str:
    """Runs of whitespace as one space: the cut keeps the whitespace around a comment."""
    return " ".join(text.split())


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "gap",
    [" /* x */ ", " -- x\n", "/* x */", " /* a */ /* b */ ", " /*+ hint */ "],
)
def test_a_comment_between_two_statements_reads_like_none(engine: str, gap: str) -> None:
    plain = "SELECT * FROM a; SELECT * FROM b"
    tagged = "SELECT * FROM a;" + gap + "SELECT * FROM b"
    assert read_sql(tagged, engine) == read_sql(plain, engine)
    assert (
        read_sql(tagged, engine).facts.fallback_reason
        == read_sql(plain, engine).facts.fallback_reason
    )
    assert regex_table_refs(tagged, engine) == regex_table_refs(plain, engine)


@pytest.mark.parametrize("engine", _ENGINES)
def test_comments_between_empty_statements(engine: str) -> None:
    plain = "SELECT * FROM a; ;"
    tagged = "SELECT * FROM a; /* x */ ; /* y */"
    assert read_sql(tagged, engine) == read_sql(plain, engine)
    assert _squeezed(_cut("a;; /* x */ ;;", engine)) == "a;; ;;"


@pytest.mark.parametrize(
    "text, engine, expected",
    [
        ("SELECT 1; /* x */ SELECT 2", "oracle", "SELECT 1; SELECT 2"),
        ("a; /* x */ ; /* y */", "oracle", "a; ;"),
        ("a;; /* x */ ;;", "mysql", "a;; ;;"),
        ("a; -- x\nb", "postgresql", "a; b"),
        # a comment inside a statement or before the first one is not touched
        ("SELECT /* x */ 1; SELECT 2", "oracle", "SELECT /* x */ 1; SELECT 2"),
        ("/* x */ SELECT 1; SELECT 2", "oracle", "/* x */ SELECT 1; SELECT 2"),
        ("SELECT 1 /* x */; SELECT 2", "oracle", "SELECT 1 /* x */; SELECT 2"),
        # MySQL '#' and executable comments
        ("a; # x\nb", "mysql", "a; b"),
        ("a; # x\nb", "postgresql", "a; # x\nb"),
        ("a; /*!40101 SET NAMES utf8 */ b", "mysql", "a; /*!40101 SET NAMES utf8 */ b"),
        ("a; -- x /*!1 y */\nb", "mysql", "a; b"),
        ("a; /*M!100 x */ b", "mariadb", "a; /*M!100 x */ b"),
        ("a; /*!40101 x */ b", "postgresql", "a; b"),
        ("a; /* x */ /*!40101 y */ b", "mysql", "a; /*!40101 y */ b"),
        # nested block comments
        ("a; /* x /* y */ z */ b", "postgresql", "a; b"),
        ("a; /* x /* y */ z */ b", "sqlserver", "a; b"),
        ("a; /* x /* y */ z */ b", "oracle", "a; z */ b"),
        # quote forms: ';' and comment markers inside them are data
        ("SELECT 'a; /* x */ b'; /* c */ SELECT 2", "mysql", "SELECT 'a; /* x */ b'; SELECT 2"),
        ('SELECT "a; /* x */"; /* c */ b', "postgresql", 'SELECT "a; /* x */"; b'),
        ("SELECT $$a; /* x */$$; /* c */ b", "postgresql", "SELECT $$a; /* x */$$; b"),
        ("SELECT $t$a; -- x$t$; /* c */ b", "postgresql", "SELECT $t$a; -- x$t$; b"),
        ("SELECT E'a\\'; /* x */'; /* c */ b", "postgresql", "SELECT E'a\\'; /* x */'; b"),
        ("SELECT [a; /* x */] FROM t; /* c */ b", "sqlserver", "SELECT [a; /* x */] FROM t; b"),
        ("SELECT [a]]; /* x */] FROM t; -- c\nb", "sqlserver", "SELECT [a]]; /* x */] FROM t; b"),
        (
            "SELECT q'{a'; /* x */}' FROM d; /* c */ b",
            "oracle",
            "SELECT q'{a'; /* x */}' FROM d; b",
        ),
        ("SELECT `a; /* x */` FROM t; /* c */ b", "mysql", "SELECT `a; /* x */` FROM t; b"),
        # unterminated quote or comment: unchanged
        ("a; /* x */ 'open", "oracle", "a; /* x */ 'open"),
        ("a; /* x */ b; /* open", "oracle", "a; /* x */ b; /* open"),
        # nothing to do
        ("", "oracle", ""),
        ("SELECT 1", "oracle", "SELECT 1"),
        ("/* x */", "oracle", "/* x */"),
        ("a;b", "oracle", "a;b"),
    ],
)
def test_cut(text: str, engine: str, expected: str) -> None:
    assert _squeezed(_cut(text, engine)) == _squeezed(expected)


def test_the_tail_case_of_1338_still_holds() -> None:
    assert _cut("SELECT 1; SELECT 2; /* c */", "oracle") == "SELECT 1; SELECT 2;"


def _code(text: str, engine: str) -> str:
    """The code characters of ``text`` (whitespace dropped), read with the scan.

    In MySQL and MariaDB a ``/*! */`` or ``/*M! */`` comment is code, not a comment.
    """
    scan = _scan(text, engine, brackets=engine in _BRACKET_DIALECTS)
    mysql = engine in ("mysql", "mariadb")
    nested = block_comments_nest(engine)
    code: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if not scan.comment_mask[i]:
            code.append(text[i])
            i += 1
        elif text.startswith("--", i) or (mysql and text[i] == "#"):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline + 1
        else:
            end = block_comment_end(text, i, nested)
            if mysql and text.startswith(("/*!", "/*M!"), i):
                code.append(text[i:end])
            i = end
    return "".join("".join(code).split())


@pytest.mark.parametrize("engine", [*_ENGINES, "mariadb", "tsql", ""])
def test_the_cut_keeps_every_non_comment_token_in_order(engine: str) -> None:
    pieces = [
        ";", ";", " ", "\n", "'", '"', "`", "[", "]", "$$", "$t$", "E'", "q'{", "}'", "\\",
        "--", "#", "/*", "*/", "/*!", "/*M!", "x", "SELECT", "(", ")", "a;b",
        "; /* c */", "/* c */", "-- c\n", "; -- c\n", "# c\n", "/* a /* b */ c */",
    ]  # fmt: skip
    rng = random.Random(1340)
    changed = 0
    for _ in range(4000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 16)))
        out = _cut(text, engine)
        if out == text:
            continue
        changed += 1
        scan = _scan(text, engine, brackets=engine in _BRACKET_DIALECTS)
        assert not (scan.unterminated_quote or scan.unterminated_comment), text
        # no code is dropped, in order
        assert _code(out, engine) == _code(text, engine), (text, out)
        # and no comment is left standing right after a ';'
        rescan = _scan(out, engine, brackets=engine in _BRACKET_DIALECTS)
        prev = ""
        for k, ch in enumerate(out):
            if rescan.comment_mask[k]:
                if k and rescan.comment_mask[k - 1]:
                    continue  # inside a run already checked at its start
                executable = engine in ("mysql", "mariadb") and out.startswith(("/*!", "/*M!"), k)
                assert prev != ";" or executable, (text, out)
                prev = "x"
            elif not ch.isspace():
                prev = ";" if ch == ";" and not rescan.mask[k] else "x"
        # idempotent
        assert _cut(out, engine) == out, (text, out)
    assert changed > 100
