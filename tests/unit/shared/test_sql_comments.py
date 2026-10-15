"""Linear-time SQL comment stripping (#808).

The regexes these helpers replace backtracked exponentially: a run of ``--``
can be read as one line comment or as many, and a run of ``*//*`` as one block
comment or as many, so a failing match tried every split. The helpers scan the
text once, so time grows linearly with its length.
"""

import json
from pathlib import Path

import pytest

from src.shared.sql_comments import (
    block_comments_nest,
    strip_comments,
    strip_leading_comments,
    strip_trailing_comments,
    unwrap_executable_comment,
)
from tests.unit.tools.sql._timing import assert_fast, assert_linear

# CPU seconds one pathological input may use. Exponential backtracking needs hours,
# so this is far from the slow code; it only has to clear a loaded runner.
BUDGET_S = 1.0

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
        def run() -> None:
            strip_trailing_comments(text)

        assert_fast(run, BUDGET_S)

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
        def run() -> None:
            strip_leading_comments(text)
            strip_leading_comments(text, hash_comments=True, strip_following_space=True)

        assert_fast(run, BUDGET_S)

    def test_scales_linearly(self):
        assert_linear(lambda n: "/* */" * (n // 5) + "x", strip_leading_comments)
        assert_linear(
            lambda n: "#\n" * (n // 2) + "x",
            lambda t: strip_leading_comments(t, hash_comments=True, strip_following_space=True),
        )


# Shared with src/ui/src/utils/sqlComments.js's tests (#814): one comment rule.
SHARED_CASES = json.loads((Path(__file__).parent / "sql_comment_cases.json").read_text())


class TestSharedCases:
    @pytest.mark.parametrize(
        "case", SHARED_CASES["leading"], ids=[repr(c["text"]) for c in SHARED_CASES["leading"]]
    )
    def test_leading_case(self, case):
        assert strip_leading_comments(case["text"], **case["options"]) == case["expected"]


class TestNestedBlockComments:
    """#814: PostgreSQL and SQL Server nest block comments; MySQL and Oracle
    end a block comment at its first ``*/``."""

    @pytest.mark.parametrize(
        "dialect, nests",
        [
            ("postgresql", True),
            ("postgres", True),
            ("aurora_postgresql", True),
            ("PostgreSQL", True),
            ("sqlserver", True),
            ("mysql", False),
            ("mariadb", False),
            ("aurora_mysql", False),
            ("oracle", False),
            ("", False),
            (None, False),
        ],
    )
    def test_which_dialects_nest(self, dialect, nests):
        assert block_comments_nest(dialect) is nests

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("SELECT 1 /* a /* b */ c */", "SELECT 1"),
            ("SELECT 1 /* a /* b /* c */ d */ e */ -- f", "SELECT 1"),
            ("SELECT 1 /* a */ b */", "SELECT 1 /* a */ b */"),
            # unterminated (PostgreSQL rejects it): not a comment, and no later
            # /* starts one, but a later -- still does
            ("SELECT 1 /* a /* b */", "SELECT 1 /* a /* b */"),
            ("SELECT 1 /* a /* b */ -- c", "SELECT 1 /* a /* b */"),
            ("SELECT /* a /* b */ c */ 1", "SELECT /* a /* b */ c */ 1"),
        ],
    )
    def test_trailing_nested(self, text, expected):
        assert strip_trailing_comments(text, nested=True) == expected

    def test_trailing_without_nesting_ends_at_first_close(self):
        assert strip_trailing_comments("SELECT 1 /* a /* b */ c */") == (
            "SELECT 1 /* a /* b */ c */"
        )

    @pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
    def test_pathological_input_finishes_fast(self, text):
        def run() -> None:
            strip_leading_comments(text, nested=True)
            strip_trailing_comments(text, nested=True)

        assert_fast(run, BUDGET_S)

    def test_scales_linearly(self):
        # A run of unterminated /* opens one nesting level each; rescanning the
        # rest of the text from every one of them would be quadratic.
        assert_linear(
            lambda n: "x " + "/*" * (n // 2), lambda t: strip_trailing_comments(t, nested=True)
        )
        assert_linear(lambda n: "/*" * (n // 2), lambda t: strip_leading_comments(t, nested=True))
        assert_linear(
            lambda n: "x " + "/* /* */" * (n // 8),
            lambda t: strip_trailing_comments(t, nested=True),
        )
        assert_linear(
            lambda n: "/* */" * (n // 5) + "x", lambda t: strip_leading_comments(t, nested=True)
        )


def test_the_ui_scanner_uses_pythons_whitespace():
    """#814 review: ``sqlComments.js`` lists exactly the characters ``str.isspace()``
    accepts (JS's ``\\s`` differs on U+001C-U+001F, U+0085 and U+FEFF)."""
    import re

    js = (Path(__file__).parents[3] / "src/ui/src/utils/sqlComments.js").read_text()
    block = js[js.index("const PY_SPACE = new Set(") : js.index("].map(")]
    listed = {int(h, 16) for h in re.findall(r"0x([0-9a-f]+)", block)}
    python = {c for c in range(0x110000) if chr(c).isspace()}
    assert listed == python


class TestMySqlExecutableComments:
    """#780: MySQL runs the body of ``/*! ... */`` (and MariaDB of ``/*M! ... */``),
    optionally gated on a server version, so it is code, not a comment."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("/*!40101 SET NAMES utf8 */", "/*!40101 SET NAMES utf8 */"),
            ("/* a */ /*!40101 SET NAMES utf8 */", "/*!40101 SET NAMES utf8 */"),
            ("# a\n/*M!100100 SET x=1 */", "/*M!100100 SET x=1 */"),
            # an optimizer hint or an ordinary comment is still a comment
            ("/*+ hint */ SELECT 1", "SELECT 1"),
            ("/* ! not executable */ SELECT 1", "SELECT 1"),
        ],
    )
    def test_executable_comment_ends_the_leading_run(self, text, expected):
        assert (
            strip_leading_comments(
                text, hash_comments=True, strip_following_space=True, mysql_executable=True
            )
            == expected
        )

    def test_without_the_option_it_is_a_comment(self):
        assert strip_leading_comments("/*!40101 SET NAMES utf8 */ x") == " x"

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("/*!40101 SET NAMES utf8 */", " SET NAMES utf8 "),
            ("/*!40101 SET NAMES utf8 */;", " SET NAMES utf8 ;"),
            ("  /*!SET NAMES utf8 */", "SET NAMES utf8 "),
            ("/*! SET NAMES utf8 */", " SET NAMES utf8 "),
            ("/*!80016 SET a=0 */", " SET a=0 "),
            ("/*M!100100 SET a=0 */", " SET a=0 "),
            # not an executable comment, or never closed: unchanged
            ("/* SET a=0 */", "/* SET a=0 */"),
            ("/*!40101 SET a=0", "/*!40101 SET a=0"),
            ("SELECT /*!40001 SQL_NO_CACHE */ 1", "SELECT /*!40001 SQL_NO_CACHE */ 1"),
            # only when nothing but whitespace or ; follows (#829 review)
            ("/*! note */ SELECT 1", "/*! note */ SELECT 1"),
            ("/*!40101 SET a=0 */ SELECT 1", "/*!40101 SET a=0 */ SELECT 1"),
            ("/*!40101 SET a=0 */ ; \n", " SET a=0  ; \n"),
            ("", ""),
        ],
    )
    def test_unwrap_executable_comment(self, text, expected):
        assert unwrap_executable_comment(text) == expected

    @pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
    def test_pathological_input_finishes_fast(self, text):
        def run() -> None:
            strip_leading_comments(text, hash_comments=True, mysql_executable=True)
            unwrap_executable_comment(text)
            unwrap_executable_comment("/*!" + text)

        assert_fast(run, BUDGET_S)


class TestStripComments:
    """#780: every comment, wherever it is, with string literals kept as data."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("SELECT 1", "SELECT 1"),
            ("UPDATE STATISTICS t -- SET later", "UPDATE STATISTICS t  "),
            ("A /* x */ B -- y\nC", "A   B  \nC"),
            ("A /* a /* b */ c */ B", "A   c */ B"),
            ("A '-- not' B", "A '-- not' B"),
            ("A '/* not */' B", "A '/* not */' B"),
            ("A 'it''s -- not' B -- yes", "A 'it''s -- not' B  "),
            ("A -- don't\nB -- it's", "A  \nB  "),
            ("A /* open", "A /* open"),
            ("A 'open -- x", "A 'open -- x"),
            ("A # b", "A # b"),
            ("", ""),
        ],
    )
    def test_removes_comments_outside_literals(self, text, expected):
        assert strip_comments(text) == expected

    def test_nested(self):
        assert strip_comments("A /* a /* b */ c */ B", nested=True) == "A   B"

    def test_hash_comments(self):
        assert strip_comments("A # b\nC", hash_comments=True) == "A  \nC"

    @pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
    def test_pathological_input_finishes_fast(self, text):
        def run() -> None:
            strip_comments(text)
            strip_comments(text, nested=True, hash_comments=True)

        assert_fast(run, BUDGET_S)

    def test_scales_linearly(self):
        assert_linear(lambda n: "x /* " * (n // 5), strip_comments)
        assert_linear(lambda n: "'" + "x" * n, strip_comments)
        assert_linear(lambda n: "x '/*' " * (n // 7), strip_comments)
        assert_linear(lambda n: "x " + "/*" * (n // 2), lambda t: strip_comments(t, nested=True))
