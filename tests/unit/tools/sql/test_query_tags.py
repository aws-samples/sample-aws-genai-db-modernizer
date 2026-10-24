"""Leading comment tags of a query (#1180): reading, caps, sqlcommenter, isolation.

``src/tools/sql/query_tags.py`` reads the comments a statement opens with into
``QueryPattern.query_tags``. These tests cover what a tag is in each dialect, the
count and length caps, the sqlcommenter parser, and that tags stay out of model
inputs and out of every routing, scoring and rendering step.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.tools.sql.query_pattern_groups import repeated_query_patterns
from src.tools.sql.query_tags import (
    MAX_QUERY_TAG_CHARS,
    MAX_QUERY_TAGS,
    QueryTags,
    parse_sqlcommenter,
    statement_tags,
    without_query_tags,
)
from tests.unit.tools.sql._timing import assert_linear

REPO = Path(__file__).resolve().parents[4]


class TestStatementTags:
    @pytest.mark.parametrize(
        "sql, engine, expected",
        [
            # no comment
            ("SELECT * FROM cart WHERE id = ?", "mysql", []),
            ("", "mysql", []),
            # sqlcommenter, Rails QueryLogs and Hibernate shapes, prepended
            (
                "/*controller='cart',action='add'*/ SELECT * FROM cart",
                "mysql",
                ["controller='cart',action='add'"],
            ),
            (
                "/*application:Shop,controller:carts,action:show*/ SELECT 1 FROM carts",
                "postgresql",
                ["application:Shop,controller:carts,action:show"],
            ),
            (
                "/* insert com.shop.Cart */ insert into cart (id) values (?)",
                "oracle",
                ["insert com.shop.Cart"],
            ),
            # several comments, each one tag, in order, trimmed
            (
                "-- endpoint: CartController.Add\n/* request: 42 */\nSELECT 1 FROM cart",
                "sqlserver",
                ["endpoint: CartController.Add", "request: 42"],
            ),
            # a trailing comment is a tag only when it parses as sqlcommenter pairs (#1313)
            ("SELECT * FROM cart /*controller='cart'*/", "mysql", ["controller='cart'"]),
            ("SELECT * FROM cart /* free text */", "mysql", []),
            ("SELECT * FROM cart -- endpoint: x\n", "postgresql", []),
            ("SELECT /* inline */ * FROM cart", "mysql", []),
            ("SELECT /*a='b'*/ * FROM cart", "mysql", []),
            ("/* lead */ SELECT /* inline */ * FROM cart /* trail */", "mysql", ["lead"]),
            # a statement that is only a line comment has no code, so no leading comment
            ("-- endpoint: x", "mysql", []),
            # empty comments and optimizer hints are not tags
            ("/**/ /*  */ SELECT 1", "mysql", []),
            ("/*+ INDEX(cart idx) */ SELECT 1 FROM cart", "oracle", []),
            ("--+ INDEX(cart idx)\nSELECT 1 FROM cart", "oracle", []),
            # a + after a space is no hint
            ("/* +1 retry */ SELECT 1 FROM cart", "mysql", ["+1 retry"]),
            # an unterminated block is not a comment
            ("/* open SELECT 1", "mysql", []),
        ],
    )
    def test_tags(self, sql: str, engine: str, expected: list[str]) -> None:
        assert statement_tags(sql, engine) == expected

    def test_hash_comments_are_tags_only_in_the_mysql_family(self) -> None:
        sql = "# endpoint: wp-admin/post.php\nSELECT 1 FROM wp_posts"
        assert statement_tags(sql, "mysql") == ["endpoint: wp-admin/post.php"]
        assert statement_tags(sql, "mariadb") == ["endpoint: wp-admin/post.php"]
        assert statement_tags(sql, "postgresql") == []  # # is an operator there

    def test_block_comments_nest_in_postgresql_and_sql_server(self) -> None:
        sql = "/* a /* b */ c */ SELECT 1"
        assert statement_tags(sql, "postgresql") == ["a /* b */ c"]
        assert statement_tags(sql, "sqlserver") == ["a /* b */ c"]
        # MySQL ends the comment at the first */, so " c */ SELECT 1" is code
        assert statement_tags(sql, "mysql") == ["a /* b"]

    def test_a_mysql_executable_comment_is_code_not_a_tag(self) -> None:
        sql = "/* tag */ /*!40101 SET NAMES utf8 */"
        assert statement_tags(sql, "mysql") == ["tag"]
        # Elsewhere /*! is a plain comment, and this statement is all comments.
        assert statement_tags("/* tag */ /*! x */ SELECT 1", "postgresql") == ["tag", "! x"]

    def test_tags_read_in_linear_time(self) -> None:
        assert_linear(
            lambda n: "/* */" * (n // 5) + "SELECT 1", lambda t: statement_tags(t, "postgresql")
        )
        assert_linear(lambda n: "#\n" * (n // 2) + "SELECT 1", lambda t: statement_tags(t, "mysql"))
        assert_linear(
            lambda n: "/*" + "x" * n + "*/ SELECT 1", lambda t: statement_tags(t, "mysql")
        )


class TestTrailingSqlcommenterTags:
    """A trailing comment is a tag only when ``parse_sqlcommenter`` reads it (#1313)."""

    @pytest.mark.parametrize(
        "sql, engine, expected",
        [
            # the shape sqlcommenter appends
            (
                "SELECT * FROM cart /*action='add',controller='cart'*/",
                "postgresql",
                ["action='add',controller='cart'"],
            ),
            # before the closing semicolon, where drivers put it
            ("SELECT 1 FROM cart /*controller='cart'*/;", "postgresql", ["controller='cart'"]),
            # free text and malformed sqlcommenter are not tags
            ("SELECT 1 FROM cart /* hot path */", "postgresql", []),
            ("SELECT 1 FROM cart /*controller='cart*/", "postgresql", []),
            ("SELECT 1 FROM cart /*controller='cart',*/", "postgresql", []),
            ("SELECT 1 FROM cart /*controller='a',controller='b'*/", "postgresql", []),
            ("SELECT 1 FROM cart /**/", "postgresql", []),
            # leading and trailing, leading first
            (
                "/* batch job */ SELECT 1 FROM cart /*controller='cart'*/",
                "mysql",
                ["batch job", "controller='cart'"],
            ),
            # each trailing comment is judged alone
            (
                "SELECT 1 FROM cart /*route='%2Fcart'*/ /* note */ /*db='x'*/",
                "sqlserver",
                ["route='%2Fcart'", "db='x'"],
            ),
            # a statement that is only a tag is a leading tag, once
            ("/*controller='cart'*/", "mysql", ["controller='cart'"]),
            ("/*controller='cart'*/ SELECT 1", "mysql", ["controller='cart'"]),
            # a comment marker inside a string literal or quoted name is data
            ("SELECT '/*controller=''cart''*/' FROM cart", "postgresql", []),
            ("SELECT \"/*controller='a'*/\" FROM cart", "postgresql", []),
            ("SELECT 'x' FROM cart /*controller='cart'*/", "postgresql", ["controller='cart'"]),
            ("SELECT 1 FROM cart WHERE a = 'it''s /*b='c'*/", "postgresql", []),
            # an unterminated block is code, not a comment
            ("SELECT 1 FROM cart /*controller='cart'", "postgresql", []),
            # a line comment is a tag only with its newline, as for leading comments
            ("SELECT 1 FROM cart -- controller='cart'", "postgresql", []),
            ("SELECT 1 FROM cart\n-- controller='cart'\n", "postgresql", ["controller='cart'"]),
            ("SELECT 1 FROM cart # controller='cart'\n", "mysql", ["controller='cart'"]),
            ("SELECT 1 FROM cart # controller='cart'\n", "postgresql", []),
            # nested block comments in PostgreSQL and SQL Server
            ("SELECT 1 /*a='b' /* c */ */", "postgresql", []),
            ("SELECT 1 /* x /*a='b'*/ */", "sqlserver", []),
            ("SELECT 1 /*a='b'*/ /* c */", "sqlserver", ["a='b'"]),
            # an executable comment is code
            ("SELECT 1 /*!40101 a='b' */", "mysql", []),
            ("SELECT 1 /*x='y /* z */'*/", "postgresql", ["x='y /* z */'"]),
            # a hint is not a tag
            ("SELECT 1 /*+ a='b' */", "oracle", []),
        ],
    )
    def test_trailing(self, sql: str, engine: str, expected: list[str]) -> None:
        assert statement_tags(sql, engine) == expected

    @pytest.mark.passes_on_main  # a negative case: the comment is no tag before and after
    def test_mysql_ends_a_block_at_the_first_close(self) -> None:
        # /*a='b' /* c */ ends at the first */: the body is not sqlcommenter
        assert statement_tags("SELECT 1 /*a='b' /* c */", "mysql") == []

    def test_the_same_tag_leading_and_trailing_is_one_tag(self) -> None:
        rows = [
            "/*controller='cart'*/ SELECT 1 /*controller='cart'*/",
            "SELECT 1 /*controller='cart'*/",
            "SELECT 1 /*action='add'*/",
        ]
        acc = QueryTags.of(rows, "mysql")
        assert acc.fields() == {
            "query_tags": ["controller='cart'", "action='add'"],
            "query_tags_dropped": 0,
        }

    def test_trailing_tags_share_the_caps_and_the_dropped_count(self) -> None:
        rows = [f"SELECT 1 /*trace='{i}'*/" for i in range(MAX_QUERY_TAGS + 3)]
        fields = QueryTags.of(rows, "postgresql").fields()
        assert fields["query_tags"] == [f"trace='{i}'" for i in range(MAX_QUERY_TAGS)]
        assert fields["query_tags_dropped"] == 3
        huge = "SELECT 1 /*v='" + "x" * MAX_QUERY_TAG_CHARS + "'*/"
        assert QueryTags.of([huge, "SELECT 1 /*a='b'*/"], "postgresql").fields() == {
            "query_tags": ["a='b'"],
            "query_tags_dropped": 1,
        }

    @pytest.mark.passes_on_main  # a time bound the new scan must also meet
    @pytest.mark.parametrize("engine", ["postgresql", "mysql"])
    def test_trailing_tags_read_in_linear_time(self, engine: str) -> None:
        def read(t: str) -> list[str]:
            return statement_tags(t, engine)

        assert_linear(lambda n: "SELECT 1 " + "/*" * (n // 2), read)
        assert_linear(lambda n: "SELECT 1 " + "/* a */" * (n // 7), read)
        assert_linear(lambda n: "SELECT '" + "'" * n, read)


class TestQueryTags:
    def test_distinct_tags_in_first_seen_order(self) -> None:
        acc = QueryTags()
        acc.add(["b", "a"])
        acc.add(["a", "c", "b"])
        assert acc.fields() == {"query_tags": ["b", "a", "c"], "query_tags_dropped": 0}

    def test_none_is_empty(self) -> None:
        assert QueryTags().fields() == {"query_tags": [], "query_tags_dropped": 0}
        assert QueryTags.of(["SELECT 1", "SELECT 2"], "mysql").fields() == {
            "query_tags": [],
            "query_tags_dropped": 0,
        }

    def test_the_count_cap_keeps_the_first_and_counts_the_rest(self) -> None:
        callers = [f"-- endpoint: C{i}.Run\nSELECT 1" for i in range(MAX_QUERY_TAGS + 8)]
        # every caller twice: a repeated dropped tag counts once
        fields = QueryTags.of(callers + callers, "mysql").fields()
        assert fields["query_tags"] == [f"endpoint: C{i}.Run" for i in range(MAX_QUERY_TAGS)]
        assert fields["query_tags_dropped"] == 8

    def test_a_huge_tag_is_dropped_whole_not_cut(self) -> None:
        huge = "x" * (MAX_QUERY_TAG_CHARS + 1)
        edge = "y" * MAX_QUERY_TAG_CHARS
        fields = QueryTags.of([f"/* {huge} */ /* {edge} */ /* ok */ SELECT 1"], "mysql").fields()
        assert fields == {"query_tags": [edge, "ok"], "query_tags_dropped": 1}

    def test_a_count_dropped_earlier_is_kept(self) -> None:
        acc = QueryTags()
        acc.add(["a"], dropped=3)
        acc.add(["a", "b"], dropped=0)
        assert acc.fields() == {"query_tags": ["a", "b"], "query_tags_dropped": 3}


class TestParseSqlcommenter:
    def test_pairs_are_url_decoded(self) -> None:
        tag = "action='%2Fparam*d',controller='index',framework='spring',traceparent='00-5bd66ef5095369c7b0d1f8f4bd33716a-c532cb4098ac3dd2-01'"
        assert parse_sqlcommenter(tag) == {
            "action": "/param*d",
            "controller": "index",
            "framework": "spring",
            "traceparent": "00-5bd66ef5095369c7b0d1f8f4bd33716a-c532cb4098ac3dd2-01",
        }

    def test_escaped_quote_and_encoded_key(self) -> None:
        assert parse_sqlcommenter(r"name='it\'s',route%20id='a%2Cb'") == {
            "name": "it's",
            "route id": "a,b",
        }

    def test_empty_value(self) -> None:
        assert parse_sqlcommenter("a=''") == {"a": ""}

    @pytest.mark.parametrize(
        "tag",
        [
            "",
            "endpoint: CartController.Add",  # a plain tag
            "application:Shop,controller:carts",  # Rails' legacy format
            "controller=cart",  # value not quoted
            "controller='cart",  # unterminated value
            "='cart'",  # empty key
            "a='1',a='2'",  # key given twice
            "a='1' b='2'",  # no comma
            "a='1',",  # trailing comma
            "a='1'x",  # text after a value
        ],
    )
    def test_malformed_is_none(self, tag: str) -> None:
        assert parse_sqlcommenter(tag) is None


class TestModelInputs:
    def test_without_query_tags_removes_them_and_copies(self) -> None:
        collector: dict[str, Any] = {
            "job_id": "j",
            "queries": {
                "total_queries_analyzed": 1,
                "query_patterns": [
                    {"query_id": "q1", "query_tags": ["t"], "query_tags_dropped": 2},
                    {"query_id": "q2"},
                ],
            },
        }
        out = without_query_tags(collector)
        assert out["queries"]["query_patterns"] == [{"query_id": "q1"}, {"query_id": "q2"}]
        assert out["queries"]["total_queries_analyzed"] == 1
        assert collector["queries"]["query_patterns"][0]["query_tags"] == ["t"]  # not modified

    def test_without_query_tags_leaves_untagged_output_alone(self) -> None:
        collector = {"queries": {"query_patterns": [{"query_id": "q1"}]}}
        assert without_query_tags(collector) is collector
        assert without_query_tags({}) == {}


def test_tags_never_split_a_query_pattern() -> None:
    """Two queries of one shape with different tags still make one query pattern."""
    base = {
        "query_text": "SELECT * FROM cart WHERE id = ?",
        "query_type": "SELECT",
        "tables_accessed": ["shop.cart"],
        "frequency_per_hour": 10.0,
    }
    plain = [{**base, "query_id": "q1"}, {**base, "query_id": "q2"}]
    tagged = [
        {**base, "query_id": "q1", "query_tags": ["controller='a'"], "query_tags_dropped": 0},
        {**base, "query_id": "q2", "query_tags": ["controller='b'"], "query_tags_dropped": 3},
    ]
    assert repeated_query_patterns(tagged, "mysql") == repeated_query_patterns(plain, "mysql")


# Where routing, scoring, triage, analysis, design, the access-pattern signature
# (src/tools/sql) and rendering live. Tags are for the caller: no module here may
# name them, read them with the tag module's helpers or import that module. Only the
# tag module itself and the model-input choke point (which removes them) may; the
# collector writers (src/tools/database, src/agents/collector) are outside these roots.
_NO_TAG_READERS = (
    "src/agents/referee",
    "src/agents/analysis",
    "src/agents/schema_design",
    "src/tools/analysis",
    "src/tools/schema",
    "src/tools/sql",
    "src/report",
    "src/shared",
    "src/storage",
    "src/orchestrator",
    "src/ui/src",
)
_NO_TAG_READER_FILES = ("scripts/run_post_schema_routing.py",)
_ALLOWED = {
    "src/tools/sql/query_tags.py",
    "src/storage/collector_for_models.py",
    "src/shared/sql_comments.py",  # defines leading_comments, the comment rule itself
}
_TAG_RE = re.compile(
    r"\b(?:query_tags|query_tags_dropped|without_query_tags|statement_tags|QueryTags"
    r"|QUERY_TAG_FIELDS|parse_sqlcommenter|leading_comments|trailing_comments)\b"
)


def _tag_readers() -> list[str]:
    readers = []
    paths = [p for root in _NO_TAG_READERS for p in sorted((REPO / root).rglob("*"))]
    paths += [REPO / f for f in _NO_TAG_READER_FILES]
    for path in paths:
        rel = str(path.relative_to(REPO))
        if (
            path.suffix not in (".py", ".js", ".jsx", ".ts", ".tsx")
            or "node_modules" in path.parts
            or rel in _ALLOWED
        ):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _TAG_RE.search(line):
                readers.append(f"{rel}:{n}: {line.strip()}")
    return readers


def test_no_routing_scoring_or_rendering_step_reads_tags() -> None:
    readers = _tag_readers()
    assert (
        not readers
    ), "query tags are the caller's join key, never a routing input:\n" + "\n".join(readers)


@pytest.mark.parametrize(
    "line",
    [
        "from src.tools.sql.query_tags import QueryTags, statement_tags",
        "from src.tools.sql import query_tags",
        "tags = statement_tags(q['query_text'], engine)",
        "if q.get('query_tags'):",
        "for f in QUERY_TAG_FIELDS:",
        "out = without_query_tags(collector)",
        "texts = leading_comments(q['query_text'], hash_comments=True)",
    ],
)
def test_the_scan_catches_imports_and_uses(line: str) -> None:
    assert _TAG_RE.search(line)
