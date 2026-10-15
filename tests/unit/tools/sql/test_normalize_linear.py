"""The normalizer runs in linear time (#557 review round 2).

The trailing-word check used ``re.search(r"([A-Za-z_]+)\\s*\\Z", text)``,
which is quadratic on a long run of letters that doesn't end the text: a
100 KB literal took tens of seconds and 1 MB hung. It is now a backward scan
from the end. These tests pin linear scaling (4 MB vs 1 MB, with a hang
ceiling; see _timing.py) and that the scan answers
exactly what the old regex did, on every sample query and on edge cases.
"""

from __future__ import annotations

import random
import re

import pytest

from src.tools.sql.normalize import _trailing_word, normalize_digest_text
from tests.unit.tools.sql._timing import assert_linear
from tests.unit.tools.sql.test_normalize_fixtures import _load_query_texts

_OLD_TRAILING_WORD_RE = re.compile(r"([A-Za-z_]+)\s*\Z")


def _old(text: str) -> tuple[str, int] | None:
    m = _OLD_TRAILING_WORD_RE.search(text)
    return (m.group(1), m.start()) if m else None


def _literal(n: int) -> str:
    return "INSERT INTO t (a) VALUES ('" + "a" * n + "')"


def _letters_before_the_end(n: int) -> str:
    return "SELECT " + "b" * n + " + 1"


@pytest.mark.slow
@pytest.mark.parametrize("dialect", ["mysql", "postgresql", "sqlserver", "oracle"])
def test_a_long_literal_normalizes_in_linear_time(dialect):
    assert normalize_digest_text(_literal(1_000_000), dialect).truncated is False
    assert_linear(_literal, lambda text: normalize_digest_text(text, dialect))


@pytest.mark.slow
def test_a_long_run_of_letters_before_the_end_normalizes_in_linear_time():
    assert_linear(_letters_before_the_end, lambda text: normalize_digest_text(text, "postgresql"))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "SELECT a FROM t",
        "SELECT a FROM t   \n\t",
        "SELECT a FROM t WHERE",
        "x FOR UPDATE",
        "UPDATE",
        "a_b_c",
        "abc1",
        "abc def",
        "t.col",
        "é",
        "aé",
        "SELECT 'aaaa' )",
        "x ",
    ],
)
def test_trailing_word_matches_the_old_regex(text):
    assert _trailing_word(text) == _old(text)


def test_trailing_word_matches_the_old_regex_on_random_text():
    rng = random.Random(557)
    alphabet = "ab_Z9 \t\n.,()'é"
    for _ in range(5000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        assert _trailing_word(text) == _old(text), repr(text)


@pytest.mark.parametrize("sample,dialect", [("wordpress", "mysql"), ("discourse", "postgresql")])
def test_sample_queries_get_the_same_answer(sample, dialect):
    for query in _load_query_texts(sample):
        stripped = query.rstrip()
        assert _trailing_word(stripped) == _old(stripped)
        result = normalize_digest_text(query, dialect)
        assert isinstance(result.truncated, bool)


@pytest.mark.slow
@pytest.mark.parametrize("dialect", ["postgresql", "sqlserver", "mysql"])
def test_a_long_run_of_nested_comment_openers_normalizes_in_linear_time(dialect):
    """#825: nesting adds one depth counter to the scan, still one forward pass."""
    assert_linear(
        lambda n: "SELECT 1 " + "/*" * (n // 2),
        lambda text: normalize_digest_text(text, dialect),
    )
