"""Three query-text detectors run in linear time and answer what their old regex did (#902).

Each was one regex that is super-linear on long hostile text, run on the raw
query text (the facade's 100,000-character cap does not apply to them):

- the correlated-subquery shape, ``\\(\\s*select\\b.*?\\bwhere\\b.*?\\b\\w+\\.\\w+\\s*=\\s*\\w+\\.\\w+``
  with ``re.S``: about 8x per doubling on many uncorrelated subqueries;
- ``MATCH\\s*\\(.+?\\)\\s*AGAINST\\s*\\(`` (the full-text fallback, the search predicate
  and relevance ranking of the search depth): quadratic on repeated ``MATCH (``
  with no ``AGAINST`` on the line;
- the write with a subquery, ``\\b(INSERT|UPDATE|DELETE)\\b.*\\bSELECT\\b`` with
  ``re.DOTALL``: quadratic on repeated ``UPDATE`` with no ``SELECT``.

Each is now a few left-to-right scans. The tests pin that each answers exactly
what the old regex did (on every sample query, every SQL-looking string in the
tests, and generated token soups) and that its cost is linear in the query
length: the same total text as one long query costs about what it costs as many
short ones, a ratio taken in this process (``assert_cost_ratio``, #938).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.shared.sql_spans import code_text
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql import query_facts as query_facts_module
from src.tools.sql.query_facts import QueryFacts
from src.tools.sql.regex_facts import has_match_against, regex_fulltext_search
from tests.unit.tools.sql._timing import assert_cost_ratio
from tests.unit.tools.sql.table_refs_snapshot import SAMPLES, load_sample

_REPO = Path(__file__).resolve().parents[4]

# The regexes as they were before #902: the reference each rewrite must agree with.
_OLD_CORRELATED_RE = re.compile(
    r"\(\s*select\b.*?\bwhere\b.*?\b\w+\.\w+\s*=\s*\w+\.\w+", re.I | re.S
)
_OLD_MATCH_AGAINST_RE = re.compile(r"MATCH\s*\(.+?\)\s*AGAINST\s*\(", re.IGNORECASE)
_OLD_FULLTEXT_RE = re.compile(
    r"""
    MATCH\s*\(.+?\)\s*AGAINST\s*\(
    |
    \b(?:to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery)\s*\(
    |
    \btsvector\b
    |
    \bts_rank(?:_cd)?\s*\(
    |
    @@
    |
    \b(?:contains|containstable|freetext|freetexttable)\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)
_OLD_WRITE_WITH_SUBQUERY_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE)\b.*\bSELECT\b", re.IGNORECASE | re.DOTALL
)
_OLD_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_OLD_SEARCH_PREDICATE_RE = re.compile(
    r"\b(like|ilike)\b|match\s*\(.+?\)\s*against\s*\(|to_tsquery\s*\(|"
    r"plainto_tsquery\s*\(|@@|similarity\s*\(|word_similarity\s*\(",
    re.IGNORECASE,
)
_OLD_RELEVANCE_RANKING_RE = re.compile(
    r"match\s*\(.+?\)\s*against\s*\(|to_tsquery\s*\(|plainto_tsquery\s*\(|@@",
    re.IGNORECASE,
)
_OLD_FUZZY_MATCH_RE = re.compile(
    r"\bsimilarity\s*\(|\bword_similarity\s*\(|<%|%>|"
    r"[\w)]\s*%\s*(?:\$\d+|\?)(?!\s*(?:=|<>|!=|<=|>=|<|>|[+\-*/%]))",
    re.IGNORECASE,
)


def _old_search_depth(query_text: str, group_by: bool) -> bool:
    text = _OLD_STRING_LITERAL_RE.sub("?", query_text)
    if _OLD_RELEVANCE_RANKING_RE.search(text) or _OLD_FUZZY_MATCH_RE.search(text):
        return True
    return group_by and bool(_OLD_SEARCH_PREDICATE_RE.search(text))


# ---------------------------------------------------------------------------
# The same answers as the old regexes
# ---------------------------------------------------------------------------


_SQL_WORD_RE = re.compile(r"(?i)\b(?:select|insert|update|delete|match|against|where)\b")


def _test_strings() -> list[str]:
    """Every string constant in ``tests/`` that names a SQL keyword the detectors read."""
    found = []
    for path in sorted((_REPO / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        found += [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SQL_WORD_RE.search(node.value)
        ]
    return found


@pytest.fixture(scope="module")
def corpus() -> list[str]:
    texts = _test_strings()
    for name in SAMPLES:
        texts += [q["query_text"] for q in parse_offline_collection(load_sample(name))["queries"]]
    return texts


def _check(text: str) -> None:
    facts = QueryFacts(text)
    assert facts.correlated_subquery_shape is bool(_OLD_CORRELATED_RE.search(text)), text[:120]
    assert has_match_against(text) is bool(_OLD_MATCH_AGAINST_RE.search(text)), text[:120]
    assert regex_fulltext_search(text) is bool(_OLD_FULLTEXT_RE.search(text)), text[:120]
    # The fact reads the text with comments and literals blanked since #1415.
    code = code_text(text, None)
    assert facts.write_with_subquery is bool(_OLD_WRITE_WITH_SUBQUERY_RE.search(code)), text[:120]
    for group_by in (False, True):
        assert query_facts_module._has_search_depth(text, group_by) is _old_search_depth(
            text, group_by
        ), text[:120]


def test_each_detector_answers_what_its_old_regex_did_on_the_corpus(corpus):
    assert len(corpus) > 2_000
    # The corpus holds each detector's positive and negative cases.
    assert any(_OLD_CORRELATED_RE.search(t) for t in corpus)
    assert any(_OLD_MATCH_AGAINST_RE.search(t) for t in corpus)
    assert any(_OLD_WRITE_WITH_SUBQUERY_RE.search(t) for t in corpus)
    for text in corpus:
        _check(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "MATCH ()AGAINST (",  # no character inside the parentheses
        "MATCH ())AGAINST (",
        "MATCH (a\n) AGAINST (",  # the match argument crosses a line
        "MATCH (a)\n AGAINST\n (",  # whitespace around AGAINST crosses lines
        "MATCH\n(a) AGAINST (",
        "rematch (a) against (b)",  # no word boundary before MATCH
        "MATCH (a) x\nMATCH (b) AGAINST ('q')",
        "MATCH (a\nMATCH (b) AGAINST (",
        "MATCH (MATCH (a) AGAINST (",
        "(select a from t where x.y = z.w)",
        "( SELECT a FROM t WHERE t.c = ?) WHERE a.b = c.d",
        "(select a from t) where a.b = c.d",
        "where a.b = c.d (select 1)",
        "(selectx where a.b = c.d",
        "(select a where a.b=c.d",
        "(select a wherea.b = c.d",
        "(select a where x a.b =\n c.d",
        "UPDATE t SET a = 1",
        "update t set a = (select 1)",
        "SELECT 1; DELETE FROM t",
        "INSERTSELECT",
        "insert\nselect",
        "x_update select",
    ],
)
def test_edge_cases_answer_what_the_old_regexes_did(text):
    _check(text)


_TOKENS = [
    "MATCH",
    "match",
    "AGAINST",
    "against",
    "(",
    ")",
    " ",
    "\n",
    "x",
    "a.b",
    "c.d",
    "=",
    "SELECT",
    "select",
    "WHERE",
    "where",
    "INSERT",
    "update",
    "DELETE",
    "_",
    ".",
    "'",
    "@@",
    "%",
    "?",
    "like",
]


@settings(max_examples=600, deadline=None)
@given(st.lists(st.sampled_from(_TOKENS), max_size=40))
def test_generated_text_answers_what_the_old_regexes_did(tokens):
    _check("".join(tokens))


# ---------------------------------------------------------------------------
# Linear cost: one long query costs about what the same text as short ones does
# ---------------------------------------------------------------------------

# Limit on long/short CPU time, as for the query facts' budget (#938): the old
# regexes measure 16 to 250 on these workloads, a linear scan about 1.
MAX_LONG_TO_SHORT = 3.0


def _uncorrelated_subqueries(n_chars: int) -> str:
    part = "x IN (SELECT a FROM t WHERE t.c = ?) UNION ALL "
    return "SELECT * FROM t WHERE " + part * (n_chars // len(part))


def _match_without_against(n_chars: int) -> str:
    # The only AGAINST is on the next line, so no MATCH opens a full-text match.
    return "MATCH (" * (n_chars // 7) + "\n) AGAINST ("


def _search_without_against(n_chars: int) -> str:
    return "SELECT a FROM t WHERE a LIKE ? GROUP BY a " + _match_without_against(n_chars)


def _updates_without_select(n_chars: int) -> str:
    return "UPDATE " * (n_chars // 7)


def _assert_linear_cost(
    make_text: Callable[[int], str],
    read: Callable[[str], object],
    long_chars: int,
    copies: int = 1,
) -> None:
    """``copies`` queries of ``long_chars`` cost at most MAX_LONG_TO_SHORT times the
    same total text cut into queries of 1,000 characters."""
    short_chars = 1_000
    long_texts = [make_text(long_chars) + f" -- {i}" for i in range(copies)]
    short_texts = [
        make_text(short_chars) + f" -- {i}" for i in range(copies * long_chars // short_chars)
    ]

    def run(texts: list[str]) -> None:
        for text in texts:
            read(text)

    assert_cost_ratio(lambda: run(long_texts), lambda: run(short_texts), MAX_LONG_TO_SHORT)


@pytest.mark.slow
def test_the_correlated_subquery_shape_is_linear_in_the_query_length():
    # The old regex: about 3.8 s for one 16,000-character query on a laptop.
    _assert_linear_cost(
        _uncorrelated_subqueries, lambda t: QueryFacts(t).correlated_subquery_shape, 16_000
    )


@pytest.mark.slow
def test_the_match_against_fallback_is_linear_in_the_query_length():
    # The old regex: about 0.55 s for one 40,000-character query.
    _assert_linear_cost(_match_without_against, regex_fulltext_search, 40_000, copies=3)


@pytest.mark.slow
def test_the_search_depth_is_linear_in_the_query_length():
    _assert_linear_cost(
        _search_without_against,
        lambda t: query_facts_module._has_search_depth(t, group_by=True),
        40_000,
        copies=3,
    )


@pytest.mark.slow
def test_the_write_with_a_subquery_is_linear_in_the_query_length():
    # The old regex: about 0.67 s for one 40,000-character query.
    _assert_linear_cost(
        _updates_without_select, lambda t: QueryFacts(t).write_with_subquery, 40_000, copies=3
    )


def test_the_hostile_texts_hold_no_match():
    """The timing texts are the detectors' worst case: none of them matches."""
    assert not QueryFacts(_uncorrelated_subqueries(2_000)).correlated_subquery_shape
    assert not regex_fulltext_search(_match_without_against(2_000))
    assert not query_facts_module._has_search_depth(_search_without_against(2_000), False)
    assert not QueryFacts(_updates_without_select(2_000)).write_with_subquery
