"""Linear-time SQL comment stripping (#808).

The regexes these helpers replace backtracked exponentially: a run of ``--``
can be read as one line comment or as many, and a run of ``*//*`` as one block
comment or as many, so a failing match tried every split. The helpers scan the
text once, so time grows linearly with its length.
"""

import time

import pytest

from src.shared.sql_comments import strip_leading_comments, strip_trailing_comments
from tests.unit.tools.sql._timing import assert_linear

# The two shapes code scanning reported (#808), plus the leading-comment
# variants, at 100k characters.
PATHOLOGICAL = {
    "many --": "--" * 50_000 + "\nx",
    "many -- after code": "SELECT 1 " + "--" * 50_000 + "\nx",
    "many *//*": "/*" + "*//*" * 25_000 + "x",
    "many */ /*": "/*" + " */ /*" * 16_000 + "x",
    "many /* unterminated": "/*" * 50_000,
    "many /* with spaces, unterminated": "/* " * 33_000,
    "many # lines": "#\n" * 50_000 + "x",
}


class TestStripTrailingComments:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("SELECT 1", "SELECT 1"),
            ("SELECT 1 -- note", "SELECT 1"),
            ("SELECT 1 /* note */", "SELECT 1"),
            ("SELECT 1 /* a */ -- b", "SELECT 1"),
            ("SELECT 1 -- a\n/* b */\n", "SELECT 1"),
            ("SELECT 1 /* a -- b */", "SELECT 1"),
            # a comment in the middle is kept
            ("SELECT /* a */ 1", "SELECT /* a */ 1"),
            ("SELECT 1 -- a\nFROM t", "SELECT 1 -- a\nFROM t"),
            # no trailing comment: the text is returned unchanged, whitespace and all
            ("SELECT 1  \n", "SELECT 1  \n"),
            # an unterminated block comment is not a comment
            ("SELECT 1 /* open", "SELECT 1 /* open"),
            # a block comment ends at its first */
            ("SELECT 1 /* a */ b */", "SELECT 1 /* a */ b */"),
            ("", ""),
            ("-- only a comment", ""),
        ],
    )
    def test_strips_only_trailing_comments(self, text, expected):
        assert strip_trailing_comments(text) == expected

    @pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
    def test_pathological_input_finishes_fast(self, text):
        start = time.perf_counter()
        strip_trailing_comments(text)
        assert time.perf_counter() - start < 1.0

    def test_scales_linearly(self):
        assert_linear(lambda n: "--" * (n // 2) + "\nx", strip_trailing_comments)
        assert_linear(lambda n: "/*" + "*//*" * (n // 4) + "x", strip_trailing_comments)
        assert_linear(lambda n: "x /* " * (n // 5), strip_trailing_comments)


class TestStripLeadingComments:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("SELECT 1", "SELECT 1"),
            ("/* a */ SELECT 1", " SELECT 1"),
            ("  /* a */ /* b */SELECT 1", "SELECT 1"),
            ("-- a\nSELECT 1", "SELECT 1"),
            ("-- a\n  -- b\n SELECT 1", " SELECT 1"),
            # a line comment needs its newline; an unterminated block is not a comment
            ("-- a", "-- a"),
            ("/* open SELECT 1", "/* open SELECT 1"),
            # # is a comment only when asked for (MySQL)
            ("# a\nSELECT 1", "# a\nSELECT 1"),
            ("  SELECT 1", "  SELECT 1"),
            ("", ""),
        ],
    )
    def test_strips_leading_comments(self, text, expected):
        assert strip_leading_comments(text) == expected

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("# a\nSELECT 1", "SELECT 1"),
            ("# a\n-- b\n/* c */  SELECT 1", "SELECT 1"),
            ("/* a */\n\n  SELECT 1", "SELECT 1"),
            ("  SELECT 1", "  SELECT 1"),
        ],
    )
    def test_cache_overlay_flavour(self, text, expected):
        """``#`` comments, and the whitespace after the last comment, go too."""
        assert (
            strip_leading_comments(text, hash_comments=True, strip_following_space=True) == expected
        )

    @pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
    def test_pathological_input_finishes_fast(self, text):
        start = time.perf_counter()
        strip_leading_comments(text)
        strip_leading_comments(text, hash_comments=True, strip_following_space=True)
        assert time.perf_counter() - start < 1.0

    def test_scales_linearly(self):
        assert_linear(lambda n: "/* */" * (n // 5) + "x", strip_leading_comments)
        assert_linear(
            lambda n: "#\n" * (n // 2) + "x",
            lambda t: strip_leading_comments(t, hash_comments=True, strip_following_space=True),
        )
