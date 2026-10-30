# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Four more query-text detectors run in linear time and answer what their old regex did (#1009).

Each was one regex whose lazy or greedy gap is retried from every occurrence of
its first keyword, so repeated ``JOIN`` with no ``ON``, repeated ``ORDER BY``
with no row limit, and repeated ``WHERE`` with no operator were quadratic:

- ``_JOIN_ON_CLAUSE_RE`` (the ``computed_join_key`` fact, read by the capability gate);
- ``_TOP_N_RE`` (the ``top_n`` regex fallback);
- ``_WHERE_COL_RE`` of the MySQL collector (``filter_columns``, the slowest of the
  three), and the identical one of the PostgreSQL collector.

Like ``test_linear_detectors.py`` (#902), the tests pin that each answers exactly
what the old regex did (every sample query, every SQL-looking string in the
tests, edge cases and generated token soups) and that its cost is linear in the
query length, as a ratio taken in this process (``assert_cost_ratio``, #938).
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.tools.database.filter_columns import where_columns
from src.tools.database.mysql_tools import _extract_filter_columns as mysql_filter_columns
from src.tools.database.postgres_tools import _extract_filter_columns as postgres_filter_columns
from src.tools.sql import query_facts as query_facts_module
from src.tools.sql.query_facts import QueryFacts
from src.tools.sql.regex_facts import regex_top_n
from tests.unit.tools.sql.test_linear_detectors import (  # noqa: F401 (corpus is a fixture)
    _assert_linear_cost,
    corpus,
)

# The regexes as they were before #1009: the reference each rewrite must agree with.
_OLD_JOIN_ON_CLAUSE_RE = re.compile(
    r"\bjoin\b.*?\bon\b\s*(?P<cond>.*?)(?=\b(?:where|group\s+by|order\s+by|having|"
    r"left|right|inner|full|cross|join|limit)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_OLD_TOP_N_RE = re.compile(
    r"order\s+by\b.*\b(?:limit|fetch\s+(?:first|next))\b|\btop\s*(?:\(|\d|@).*\border\s+by\b",
    re.IGNORECASE | re.DOTALL,
)
_OLD_MYSQL_WHERE_COL_RE = re.compile(
    r"WHERE\s+.*?`?(\w+)`?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I
)
_OLD_POSTGRES_WHERE_COL_RE = re.compile(
    r"WHERE\s+.*?\"?(\w+)\"?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I
)


def _old_join_conditions(text: str) -> list[str]:
    return [m.group("cond") for m in _OLD_JOIN_ON_CLAUSE_RE.finditer(text)]


def _check(text: str) -> None:
    assert list(query_facts_module._join_conditions(text)) == _old_join_conditions(text), text[:120]
    assert regex_top_n(text) is bool(_OLD_TOP_N_RE.search(_windowless(text))), text[:120]
    assert where_columns(text, "`") == _OLD_MYSQL_WHERE_COL_RE.findall(text), text[:120]
    assert where_columns(text, '"') == _OLD_POSTGRES_WHERE_COL_RE.findall(text), text[:120]


def _windowless(text: str) -> str:
    from src.tools.sql.regex_facts import _without_windows

    return _without_windows(text)


def test_each_detector_answers_what_its_old_regex_did_on_the_corpus(corpus):  # noqa: F811
    assert len(corpus) > 2_000
    # The corpus holds each detector's positive and negative cases.
    assert any(_old_join_conditions(t) for t in corpus)
    assert any(_OLD_TOP_N_RE.search(_windowless(t)) for t in corpus)
    assert any(_OLD_MYSQL_WHERE_COL_RE.findall(t) for t in corpus)
    assert any(not _OLD_MYSQL_WHERE_COL_RE.findall(t) for t in corpus)
    for text in corpus:
        _check(text)
        assert mysql_filter_columns(text) == (
            list(
                dict.fromkeys(
                    _OLD_MYSQL_WHERE_COL_RE.findall(text)
                    + re.findall(
                        r"(?:AND|OR)\s+`?(\w+)`?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", text, re.I
                    )
                )
            )
            or None
        )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "JOIN",
        "join b on",
        "join b on ",
        "join b on a.x = b.y",
        "join b on a.x = b.y where z = 1",
        "join b on a.x = b.y\n",
        "join b on a.x = b.y\n\n",
        "join b on where x",
        "a join b join c on lower(c.n) = d.n",
        "a join b on lower(b.n) = a.n left join c on c.id = a.id",
        "a join b on x = y join c on lower(y) = z",
        "joint on x",
        "rejoin b on x = y",
        "join b one x = y",
        "join b on\nx = y group\nby z",
        "join b on x = y order   by z",
        "join b on x=y limit 1",
        "join b on x=y fullouter",
        "join b on x=y inner",
        "join b, join c",
        "ORDER BY x",
        "ORDER BY x LIMIT 1",
        "reorder by x limit 1",
        "order bylimit",
        "order by limit",
        "order\nby\nx fetch\nfirst 1 rows only",
        "order by x fetch next 1",
        "order by x fetchfirst",
        "limit 1 order by x",
        "TOP 5 x order by y",
        "top (5) x order by y",
        "top @n x order by y",
        "top x order by y",
        "top 5 limit 1",
        "select top 5 * from t",
        "stop 5 x order by y",
        "order by x top 5 order by y",
        "order by order by x limit",
        "x order by y over (order by z) limit",
        "WHERE",
        "WHERE ",
        "WHERE x",
        "WHERE x = 1",
        "WHERE `x` = 1",
        'WHERE "x" = 1',
        "WHERE `x`= 1 AND y LIKE 'a'",
        "WHERE\n x = 1",
        "WHERE a\n= 1",
        "WHERE a\n b = 1",
        "WHERE (a + b) > 1",
        "WHERE abIN",
        "WHERE abINx",
        "WHERE xisnull",
        "WHERE abc LIKE",
        "WHERE ab`INTO",
        "WHERE ab` = 1",
        "WHERE ab`  \n = 1",
        'WHERE ab"  \n = 1',
        "WHERE `` x = 1",
        "WHERE 1 = 1",
        "WHERE x WHERE y = 1",
        "WHERE x\nWHERE y = 1",
        "elsewhere = 1",
        "WHERE a b c d e = 1 WHERE f g h i = 2",
        "WHERE a = 1 WHERE b = 2 WHERE c",
        "WHERE a = 1\nWHERE b WHERE c = 1",
        "WHEREx = 1",
        "WHERE  \t x IS NULL",
        "WHERE x BETWEEN 1 AND 2",
    ],
)
def test_edge_cases_answer_what_the_old_regexes_did(text):
    _check(text)


_TOKENS = [
    "JOIN",
    "join",
    "ON",
    "on",
    "WHERE",
    "where",
    "ORDER BY",
    "order  by",
    "LIMIT",
    "FETCH FIRST",
    "TOP",
    "top (",
    "5",
    "@n",
    "OVER (",
    "(",
    ")",
    "LEFT",
    "group by",
    "having",
    "=",
    "<",
    "!=",
    "LIKE",
    "IN",
    "IS",
    "BETWEEN",
    "x",
    "ab",
    "a.b",
    "`",
    '"',
    " ",
    "\n",
    "  ",
    "_",
]


@settings(max_examples=1500, deadline=None)
@given(st.lists(st.sampled_from(_TOKENS), max_size=40))
def test_generated_text_answers_what_the_old_regexes_did(tokens):
    _check("".join(tokens))


@settings(max_examples=500, deadline=None)
@given(st.text(alphabet='WHERE whereINLKS=<>!`"x\n \tab', max_size=60))
def test_generated_characters_read_the_same_filter_columns(text):
    assert where_columns(text, "`") == _OLD_MYSQL_WHERE_COL_RE.findall(text)
    assert where_columns(text, '"') == _OLD_POSTGRES_WHERE_COL_RE.findall(text)


# ---------------------------------------------------------------------------
# Linear cost: one long query costs about what the same text as short ones does
# ---------------------------------------------------------------------------


def _joins_without_on(n_chars: int) -> str:
    return "SELECT * FROM a " + "JOIN b " * (n_chars // 7)


def _order_bys_without_limit(n_chars: int) -> str:
    return "ORDER BY x " * (n_chars // 11)


def _wheres_without_operator(n_chars: int) -> str:
    return "SELECT 1 " + "WHERE x " * (n_chars // 8)


def _one_long_word_after_where(n_chars: int) -> str:
    # One word the reading tries every prefix of, for an operator inside it.
    return "SELECT 1 WHERE " + "ab" * (n_chars // 2)


@pytest.mark.slow
def test_the_computed_join_key_is_linear_in_the_query_length():
    # The old regex: 0.17 s for one 20,000-character query on a laptop.
    _assert_linear_cost(
        _joins_without_on, lambda t: QueryFacts(t).computed_join_key, 40_000, copies=3
    )


@pytest.mark.slow
def test_the_top_n_fallback_is_linear_in_the_query_length():
    # The old regex: 0.13 s for one 20,000-character query.
    _assert_linear_cost(_order_bys_without_limit, regex_top_n, 40_000, copies=3)


@pytest.mark.slow
def test_the_mysql_filter_columns_are_linear_in_the_query_length():
    # The old regex: 2.8 s for one 20,000-character query.
    _assert_linear_cost(_wheres_without_operator, mysql_filter_columns, 40_000, copies=3)


@pytest.mark.slow
def test_the_postgres_filter_columns_are_linear_in_the_query_length():
    _assert_linear_cost(_wheres_without_operator, postgres_filter_columns, 40_000, copies=3)


@pytest.mark.slow
@pytest.mark.parametrize("read", [mysql_filter_columns, postgres_filter_columns])
def test_the_filter_columns_are_linear_in_one_long_word(read):
    _assert_linear_cost(_one_long_word_after_where, read, 40_000, copies=3)


def test_the_hostile_texts_hold_no_match():
    """The timing texts are the detectors' worst case: none of them matches."""
    assert not QueryFacts(_joins_without_on(2_000)).computed_join_key
    assert not regex_top_n(_order_bys_without_limit(2_000))
    assert mysql_filter_columns(_wheres_without_operator(2_000)) is None
    assert postgres_filter_columns(_wheres_without_operator(2_000)) is None
    assert mysql_filter_columns(_one_long_word_after_where(2_000)) is None
