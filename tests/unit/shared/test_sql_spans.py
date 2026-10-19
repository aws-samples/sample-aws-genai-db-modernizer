"""The comment/literal/identifier scans run in linear time and answer what their regexes did (#886).

``unsupported_pattern.strip_comments_and_literals`` and ``query_shape``'s quoted
identifier handling used to be two regexes (``_STRIP_RE``, ``_QUOTED_IDENT_RE``)
that rescan the rest of the text from every unterminated opener: a run of
``'\\`` or ``[`` was quadratic (the shape check behind ``aurora-anti-05`` /
``aurora-anti-06`` runs them on the raw query text). They are now one scan,
``sql_spans``. The tests pin that the scan answers exactly what each old regex
did (every sample query, every SQL-looking string in the tests, edge cases and
generated token soups) and that its cost is linear in the query length, as a
ratio taken in this process (``assert_cost_ratio``, #938).
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee.query_shape import (
    _normalized_sql,
    _unquote_identifiers,
    single_table_access_shape,
)
from src.shared.sql_spans import LITERALS_AND_COMMENTS, QUOTED_IDENTIFIERS, spans, sub_spans
from src.shared.unsupported_pattern import is_dedup_only_group_by, strip_comments_and_literals
from tests.unit.tools.sql._timing import assert_cost_ratio
from tests.unit.tools.sql.test_linear_detectors import corpus  # noqa: F401 (fixture)

# The regexes as they were before #886: the reference each scan must agree with.
_OLD_STRIP_RE = re.compile(
    r"/\*.*?\*/" r"|--[^\n]*" r"|'(?:[^'\\]|\\.|'')*'" r'|"(?:[^"\\]|\\.|"")*"',
    re.DOTALL,
)
_OLD_QUOTED_IDENT_RE = re.compile(r'"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]')


# ``query_shape._normalized_sql`` before it used the shared scanner (#871): the
# reference the one-scan version must agree with.
_OLD_SPAN_OPENER_RE = re.compile(r"/\*|--|['\"`\[]")
# What can end or escape inside each quoted span: ``'`` literals take backslash
# escapes, ``"`` identifiers do not.
_OLD_QUOTED_SPAN_STOPS = {"'": re.compile(r"['\\]"), '"': re.compile('"')}
_OLD_SPAN_CLOSERS = {"`": "`", "[": "]", "/*": "*/"}


def _old_quoted_span_end(text: str, start: int, quote: str) -> int:
    """End (exclusive) of the ``quote``-quoted span opening at ``start``, or -1.

    A doubled quote is an escaped quote, and in a ``'`` literal so is ``\\``
    followed by any character. Same answer as the regex
    ``'(?:[^'\\]|\\.|'')*'`` (``"(?:[^"]|"")*"`` without backslash): when the
    text ends inside the span, the span closes at the first quote of its last
    doubled pair, as the regex does when it backtracks; with no pair it is
    unterminated.
    """
    stops = _OLD_QUOTED_SPAN_STOPS[quote]
    k = start + 1
    last_pair = -1
    while True:
        stop = stops.search(text, k)
        if stop is None:
            return last_pair + 1 if last_pair >= 0 else -1
        pos = stop.start()
        if text[pos] == "\\":
            if pos + 1 >= len(text):
                return last_pair + 1 if last_pair >= 0 else -1
            k = pos + 2
        elif text.startswith(quote * 2, pos):
            last_pair = pos
            k = pos + 2
        else:
            return pos + 1


def _old_normalized_sql(text: str) -> str:
    """``text`` with comments and literals blanked and quoted identifiers made ``ident``.

    One left-to-right pass in linear time (review of #871): at each step the
    span that opens first -- a ``/* */`` or ``--`` comment, a ``'...'``
    literal (``''`` and backslash escapes), or a ``"..."`` / `` `...` `` /
    ``[...]`` identifier -- is replaced whole, so a quote inside one never
    pairs with a quote outside it. An opener with no closer is plain text, and
    once one kind has no closer left, every later opener of that kind is
    skipped at once instead of rescanning the rest of the text.
    """
    out: list[str] = []
    unclosed: set[str] = set()
    last = i = 0
    while (match := _OLD_SPAN_OPENER_RE.search(text, i)) is not None:
        start, opener = match.start(), match.group()
        end = -1
        if opener not in unclosed:
            if opener == "--":
                newline = text.find("\n", start)
                end = len(text) if newline < 0 else newline
            elif opener in _OLD_SPAN_CLOSERS:
                closer = _OLD_SPAN_CLOSERS[opener]
                found = text.find(closer, start + len(opener))
                end = found + len(closer) if found >= 0 else -1
            else:
                end = _old_quoted_span_end(text, start, opener)
            if end < 0:
                unclosed.add(opener)
        if end < 0:
            i = start + 1
            continue
        out.append(text[last:start])
        out.append(" " if opener in ("--", "/*", "'") else " ident ")
        last = i = end
    out.append(text[last:])
    return "".join(out)


def _old_strip(text: str) -> str:
    return _OLD_STRIP_RE.sub(" ", text)


def _old_unquote(text: str) -> str:
    return _OLD_QUOTED_IDENT_RE.sub(lambda m: m.group(0)[1:-1].replace('""', '"'), text)


def _old_ident(text: str) -> str:
    return _OLD_QUOTED_IDENT_RE.sub(" ident ", text)


def _check(text: str) -> None:
    assert strip_comments_and_literals(text) == _old_strip(text), text[:120]
    assert _unquote_identifiers(text) == _old_unquote(text), text[:120]
    assert sub_spans(text, QUOTED_IDENTIFIERS, lambda _s: " ident ") == _old_ident(text), text[:120]
    assert _normalized_sql(text, None) == _old_normalized_sql(text), text[:120]


def test_the_scans_answer_what_the_old_regexes_did_on_the_corpus(corpus):  # noqa: F811
    assert len(corpus) > 2_000
    for text in corpus:
        _check(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "'",
        '"',
        "`",
        "[",
        "/*",
        "--",
        "'abc",
        "'abc'",
        "'it''s'",
        "'it\\'s'",
        "'trailing\\",
        "'a\\'",
        "'a''b",  # unterminated, but a doubled pair: closes at the pair's first quote
        "'a''",
        "''''",
        "'''",
        '"a""b"',
        '"a\\"b"',  # a backslash escapes in a literal, not in an identifier
        '"a""b',
        '"a\\" \'x\' "',
        "`a``b`",
        "[a]]b]",
        "[a",
        "]",
        "x [a 'b] c'",
        "'a [b' ]",
        "/* a ' */ 'b' /* c",
        "/* a */ -- b\n'c'",
        "-- 'a\n'b'",
        "'a -- b' -- c",
        "\"a'b\" 'c\"d'",
        "'a\nb' \"c\nd\"",
        "'\\'\\'\\'",
        "'\\\n'",
        "/*/ */",
        "/**/",
        "'\\'' x ''",
        'SELECT "t"."id" FROM `t` WHERE [id] = \'x\' /* c */ -- d',
        '"a" "b',
        "[a] [b",
        "' [ ' ] [ ]",
    ],
)
def test_edge_cases_answer_what_the_old_regexes_did(text):
    _check(text)


def test_spans_report_every_span_in_order():
    text = "a 'x' /* y */ -- z\nb \"w\""
    found = [(text[s:e], op) for s, e, op in spans(text, LITERALS_AND_COMMENTS)]
    assert found == [("'x'", "'"), ("/* y */", "/*"), ("-- z", "--"), ('"w"', '"')]


def test_a_dedup_proof_still_ignores_a_group_by_inside_a_literal():
    assert not is_dedup_only_group_by("SELECT a FROM t WHERE b = 'GROUP BY'")
    assert is_dedup_only_group_by("SELECT a FROM t /* x ' */ GROUP BY a -- '")


_TOKENS = [
    "'",
    '"',
    "`",
    "[",
    "]",
    "\\",
    "/*",
    "*/",
    "--",
    "\n",
    " ",
    "x",
    "''",
    '""',
    "SELECT",
    "GROUP BY",
]


@settings(max_examples=1500, deadline=None)
@given(st.lists(st.sampled_from(_TOKENS), max_size=30))
def test_generated_text_answers_what_the_old_regexes_did(tokens):
    _check("".join(tokens))


# ---------------------------------------------------------------------------
# Linear cost: one long query costs about what the same text as short ones does
# ---------------------------------------------------------------------------

# Limit on long/short CPU time, as for the other linear-detector tests: the old
# regexes measure 20 to 100 on these texts, a linear scan about 1.
MAX_LONG_TO_SHORT = 3.0
_SHORT_CHARS = 1_000


def _quote_backslash_run(n_chars: int) -> str:
    return "SELECT * FROM t WHERE id = 1 " + "'\\" * (n_chars // 2)


def _open_brackets(n_chars: int) -> str:
    return "SELECT * FROM t WHERE id = 1 " + "[" * n_chars


def _open_comments(n_chars: int) -> str:
    return "SELECT * FROM t WHERE id = 1 " + "/* " * (n_chars // 3)


def _assert_linear_cost(
    make_text: Callable[[int], str], read: Callable[[str], object], long_chars: int
) -> None:
    """One query of ``long_chars`` costs at most MAX_LONG_TO_SHORT times the same
    total text cut into queries of 1,000 characters."""
    long_texts = [make_text(long_chars)]
    short_texts = [make_text(_SHORT_CHARS) for _ in range(long_chars // _SHORT_CHARS)]

    def run(texts: list[str]) -> None:
        for text in texts:
            read(text)

    assert_cost_ratio(lambda: run(long_texts), lambda: run(short_texts), MAX_LONG_TO_SHORT)


def _shape(text: str) -> object:
    query = {"query_type": "SELECT", "query_text": text, "tables_accessed": ["t"]}
    return single_table_access_shape(query, ["id"])


@pytest.mark.slow
@pytest.mark.parametrize(
    "make_text", [_quote_backslash_run, _open_brackets, _open_comments], ids=lambda f: f.__name__
)
def test_the_shape_check_is_linear_in_the_query_length(make_text):
    # The old regexes: 2.9 s for 25,000 characters of ``'\``, 0.18 s for ``[``.
    _assert_linear_cost(make_text, _shape, 50_000)


@pytest.mark.slow
@pytest.mark.parametrize(
    "make_text", [_quote_backslash_run, _open_brackets, _open_comments], ids=lambda f: f.__name__
)
def test_the_strip_is_linear_in_the_query_length(make_text):
    _assert_linear_cost(make_text, strip_comments_and_literals, 50_000)


def test_the_hostile_texts_hold_no_span():
    """The timing texts are the worst case: no opener has a closer."""
    for make_text in (_quote_backslash_run, _open_brackets, _open_comments):
        text = make_text(2_000)
        assert strip_comments_and_literals(text) == text
    assert _unquote_identifiers(_open_brackets(2_000)) == _open_brackets(2_000)
