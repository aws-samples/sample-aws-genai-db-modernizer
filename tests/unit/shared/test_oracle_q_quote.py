"""Oracle's alternative quoting, ``q'[...]'``, in the one span grammar (#1451).

The string runs from ``q'<delimiter>`` to the closing delimiter and a quote (``[ ( { <``
close with their mirror, any other delimiter with itself), so an apostrophe inside it
opens nothing. Only the Oracle dialect reads it: elsewhere ``q`` is a name followed by
a string, as it was.
"""

from __future__ import annotations

import pytest

from src.shared.sql_spans import code_text, literals_and_comments_for, spans
from src.shared.unsupported_pattern import strip_comments_and_literals
from src.tools.sql.query_facts import QueryFacts
from src.tools.sql.regex_facts import scan_sql
from tests.unit.tools.sql._timing import assert_cost_ratio

_DELIMITERS = {
    "[": "]",
    "(": ")",
    "{": "}",
    "<": ">",
    "!": "!",
    "#": "#",
    "|": "|",
    "'": "'",
}


def _q(open_: str, body: str, prefix: str = "q") -> str:
    return f"{prefix}'{open_}{body}{_DELIMITERS[open_]}'"


@pytest.mark.parametrize("prefix", ["q", "Q", "nq", "NQ", "nQ"])
@pytest.mark.parametrize("open_", list(_DELIMITERS))
def test_each_delimiter_form_is_one_string(open_, prefix):
    body = "it's a JOIN u, GROUP BY x"
    sql = f"SELECT id FROM t WHERE note = {_q(open_, body, prefix)} AND deleted_at IS NULL"
    assert code_text(sql, "oracle") == "SELECT id FROM t WHERE note = ? AND deleted_at IS NULL"


def test_the_issue_reproduction():
    sql = "SELECT id FROM t WHERE note = q'[it's]' AND deleted_at IS NULL AND name = 'x'"
    assert QueryFacts(sql, "oracle").is_null is True


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        # The closing delimiter must be followed by a quote.
        ("note = q'[a]b' JOIN u]' AND c IS NULL", "note = ? AND c IS NULL"),
        # A closing delimiter then a quote inside the string: the first pair ends it.
        ("note = q'[a]' JOIN u]' AND c", "note = ? JOIN u]' AND c"),
        # An apostrophe, a doubled one and a lone delimiter inside.
        ("note = q'{it''s } {ok}' AND c", "note = ? AND c"),
        # Empty, and a string of one delimiter's closer.
        ("note = q'[]' AND c", "note = ? AND c"),
        # Two in a row.
        ("a = q'(x)' AND b = nq'<y'>' AND c", "a = ? AND b = ? AND c"),
        # An ordinary string after one, and a comment marker inside one.
        ("a = q'[--]' AND b = 'q' AND c", "a = ? AND b = ? AND c"),
    ],
)
def test_where_a_q_quote_ends(sql, code):
    assert code_text(sql, "oracle") == code


@pytest.mark.passes_on_main
def test_the_q_must_stand_alone():
    # ``seq'[x]'`` is a name followed by a string, not an alternative quote.
    assert code_text("SELECT seq'[x]' FROM t", "oracle") == "SELECT seq? FROM t"
    assert code_text("SELECT a_q'[x]' FROM t", "oracle") == "SELECT a_q? FROM t"


@pytest.mark.passes_on_main
def test_an_unterminated_q_quote_is_plain_text_then_a_string():
    # No ``]'`` anywhere: the q is a name, the rest reads as an ordinary string.
    assert code_text("SELECT q'[abc' FROM t", "oracle") == "SELECT q? FROM t"


def test_a_q_quote_hides_its_text_from_every_pass():
    sql = "SELECT id FROM t WHERE note = q'[it's a JOIN u GROUP BY x]' AND id = 1"
    assert "JOIN" not in scan_sql(sql, "oracle")
    assert "JOIN" not in strip_comments_and_literals(sql, "oracle")
    sql = "SELECT id FROM t WHERE note = q'[it's a JOIN u]' AND deleted_at IS NULL"
    assert QueryFacts(sql, "oracle").is_null is True


@pytest.mark.parametrize("engine", ["mysql", "postgresql", "sqlserver", None, "db2"])
@pytest.mark.passes_on_main
def test_other_dialects_read_q_as_a_name_before_a_string(engine):
    # A name then the string '[it' (closed at the apostrophe), then real SQL.
    sql = "SELECT q'[it's]' FROM t"
    assert code_text(sql, engine).startswith("SELECT q?")
    assert [(s, e) for s, e, _ in spans(sql, literals_and_comments_for(engine))][0] == (8, 13)


@pytest.mark.passes_on_main
def test_another_dialect_still_sees_the_text_after_the_apostrophe():
    sql = "SELECT id FROM t WHERE q'[it's a JOIN u]' = 1"
    assert QueryFacts(sql, "postgresql").join_count == 1
    sql = "SELECT id FROM t WHERE note = q'[it's a JOIN u]' AND deleted_at IS NULL"
    assert QueryFacts(sql, "oracle").is_null is True


@pytest.mark.passes_on_main
def test_a_q_quote_is_not_a_text_inside_a_comment():
    sql = "SELECT id FROM t -- q'[\nWHERE note = 'a' AND c IS NULL"
    assert code_text(sql, "oracle") == "SELECT id FROM t  \nWHERE note = ? AND c IS NULL"


# -- Cost is linear -------------------------------------------------------------------

_SHORT = 1_000


def _unclosed_openers(n_chars: int) -> str:
    # Every opener has its own delimiter and none has a closer: the worst case for a
    # scan that searches for a closer at each one.
    delimiters = "[({<!#|$%&*+,-./:;=>?@^_~"
    unit = "".join(f"q'{d}" for d in delimiters) * 4
    return "SELECT 1 " + (unit * (n_chars // len(unit) + 1))[:n_chars]


def _same_delimiter(n_chars: int) -> str:
    return "SELECT 1 " + "q'[" * (n_chars // 3)


def _many_closed(n_chars: int) -> str:
    return "SELECT 1 " + "q'[a]' " * (n_chars // 7)


@pytest.mark.passes_on_main
@pytest.mark.slow
@pytest.mark.parametrize("make", [_unclosed_openers, _same_delimiter, _many_closed])
def test_scanning_q_quotes_is_linear(make):
    long_chars = 100_000

    def run(texts: list[str]) -> None:
        for text in texts:
            code_text(text, "oracle")

    long_texts = [make(long_chars)]
    short_texts = [make(_SHORT) for _ in range(long_chars // _SHORT)]
    assert_cost_ratio(lambda: run(long_texts), lambda: run(short_texts), 3.0)
