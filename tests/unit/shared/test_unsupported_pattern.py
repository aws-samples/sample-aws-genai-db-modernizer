"""Shared field-reading for schema-design ``unsupported_patterns`` entries.

The four schema-design contracts disagree on field names (see
``src/shared/unsupported_pattern.py`` module docstring, including why this
lives in a new ``src.shared`` package rather than ``src.contracts``). This is
used by both ``src.agents.referee.synthesis_report.build_risk_assessment``
(#210: it read only ``pattern_type``/``recommendation``, so
DocumentDB/ElastiCache entries -- which carry ``reason``/``workaround``
instead -- became risks with no text) and
``src.report.renderers._unsupported_pattern_md`` (#204).
"""

from __future__ import annotations

import pytest

from src.shared.unsupported_pattern import (
    is_dedup_only_group_by,
    unsupported_pattern_ids,
    unsupported_pattern_label,
    unsupported_pattern_mitigation,
    unsupported_pattern_text,
)

DYNAMODB_SHAPE = {
    "query_ids": ["254f282ce4da5837c67df5f5d405e1ad3c405cee2c14420378bd8dfd0d86787"],
    "pattern_type": "aggregation",
    "recommendation": "COUNT(*) on wp_postmeta: run a Query with Select=COUNT.",
}

ELASTICACHE_SHAPE = {
    "source_query_ids": ["59163c184972d1ec4ad95106a5fc20c95d19dd06d071f956bf50e0c51ce12bd"],
    "reason": "LEFT JOIN with multiple LIKE predicates cannot be a key lookup.",
    "workaround": "Maintain a secondary index set.",
}

DOCUMENTDB_SHAPE = {
    "source_query_ids": ["abc123"],
    "reason": "CTE queries with window functions are not supported.",
    "workaround": None,
}

OPENSEARCH_SHAPE = {
    "query_ids": ["abc12345"],
    "source_query": "SELECT * FROM t",
    "reason": "full text search",
    "recommendation": "use match query",
}


class TestUnsupportedPatternIds:
    def test_reads_query_ids_or_source_query_ids(self) -> None:
        assert unsupported_pattern_ids(DYNAMODB_SHAPE) == [
            "254f282ce4da5837c67df5f5d405e1ad3c405cee2c14420378bd8dfd0d86787"
        ]
        assert unsupported_pattern_ids(ELASTICACHE_SHAPE) == [
            "59163c184972d1ec4ad95106a5fc20c95d19dd06d071f956bf50e0c51ce12bd"
        ]

    def test_string_value_is_one_id_not_exploded_into_characters(self) -> None:
        """A string under query_ids is a malformed single id, not a list of ids --
        ``list("abc")`` would silently produce ["a", "b", "c"]."""
        assert unsupported_pattern_ids({"query_ids": "abc123"}) == ["abc123"]

    def test_falsy_ids_are_dropped(self) -> None:
        assert unsupported_pattern_ids({"query_ids": ["real-id", "", None, "other"]}) == [
            "real-id",
            "other",
        ]

    def test_missing_ids_is_empty_list(self) -> None:
        assert unsupported_pattern_ids({}) == []
        assert unsupported_pattern_ids({"query_ids": None}) == []


class TestUnsupportedPatternLabel:
    def test_pattern_type_is_used_when_present(self) -> None:
        assert unsupported_pattern_label(DYNAMODB_SHAPE) == "aggregation"

    def test_falls_back_to_generic_label(self) -> None:
        assert unsupported_pattern_label(ELASTICACHE_SHAPE) == "unsupported pattern"
        assert unsupported_pattern_label({}) == "unsupported pattern"

    def test_underscores_become_spaces(self) -> None:
        assert unsupported_pattern_label({"pattern_type": "full_text_search"}) == "full text search"

    def test_strips_leading_and_trailing_asterisks(self) -> None:
        assert unsupported_pattern_label({"pattern_type": "*aggregation*"}) == "aggregation"


class TestUnsupportedPatternText:
    def test_dynamodb_shape_uses_recommendation(self) -> None:
        text = unsupported_pattern_text(DYNAMODB_SHAPE)
        assert "COUNT(*) on wp_postmeta" in text

    def test_elasticache_shape_combines_reason_and_workaround(self) -> None:
        text = unsupported_pattern_text(ELASTICACHE_SHAPE)
        assert "LEFT JOIN" in text
        assert "Maintain a secondary index set" in text

    def test_opensearch_shape_combines_reason_and_recommendation_not_source_query(self) -> None:
        """``source_query`` is the original SQL, not an explanation -- it is
        intentionally never included in the assembled text."""
        text = unsupported_pattern_text(OPENSEARCH_SHAPE)
        assert "full text search" in text
        assert "use match query" in text
        assert "SELECT * FROM t" not in text

    def test_duplicate_sentence_across_fields_kept_once(self) -> None:
        same = "identical text"
        text = unsupported_pattern_text({"reason": same, "recommendation": same})
        assert text.count(same) == 1

    def test_empty_when_no_fields_present(self) -> None:
        assert unsupported_pattern_text({}) == ""


class TestUnsupportedPatternMitigation:
    def test_prefers_recommendation(self) -> None:
        assert unsupported_pattern_mitigation(OPENSEARCH_SHAPE) == "use match query"

    def test_falls_back_to_workaround(self) -> None:
        assert (
            unsupported_pattern_mitigation(ELASTICACHE_SHAPE) == "Maintain a secondary index set."
        )

    def test_none_when_neither_present(self) -> None:
        assert unsupported_pattern_mitigation(DOCUMENTDB_SHAPE) is None
        assert unsupported_pattern_mitigation({}) is None


# Real query_text from the wordpress e2e evidence that surfaced #336 (job
# 3f327329, schema-dynamodb's unsupported_patterns, pattern_type
# "aggregation"): wp_posts queries whose only GROUP BY is a defensive
# de-duplication guard with no aggregate function and no HAVING clause.
_DEDUP_ONLY_GROUP_BY_SQL = (
    "SELECT `wp_posts` . * FROM `wp_posts` WHERE ? = ? AND `wp_posts` . `post_name` "
    "IN (...) AND ( ? = ? ) AND ( ( `wp_posts` . `post_type` = ? AND "
    "( `wp_posts` . `post_status` = ? ) ) ) GROUP BY `wp_posts` . `ID` "
    "ORDER BY `wp_posts` . `post_date` DESC"
)

# Real query_text for the same job's genuine aggregation (query id
# a355b7403b1d39d90f3ff59ece2a00e0225bbbc7c3a9d3d755d3f0e8d001e077): a 3-table
# join with a real SUM, which must stay classified as unsupported.
_REAL_SUM_AGGREGATION_SQL = (
    "SELECT `order_items` . `order_item_name` AS `order_item_name` , "
    "SUM ( `order_item_meta_discount_amount` . `meta_value` ) AS `discount_amount` , "
    "`posts` . `post_date` AS `post_date` FROM `wp_posts` AS `posts` "
    "INNER JOIN `wp_woocommerce_order_items` AS `order_items` "
    "ON `posts` . `ID` = `order_items` . `order_id` "
    "GROUP BY `order_items` . `order_item_id`"
)


class TestIsDedupOnlyGroupBy:
    def test_group_by_with_no_aggregate_function_and_no_having_is_dedup_only(self) -> None:
        assert is_dedup_only_group_by(_DEDUP_ONLY_GROUP_BY_SQL) is True

    def test_group_by_with_a_real_aggregate_function_is_not_dedup_only(self) -> None:
        assert is_dedup_only_group_by(_REAL_SUM_AGGREGATION_SQL) is False

    def test_no_group_by_at_all_is_not_dedup_only(self) -> None:
        assert (
            is_dedup_only_group_by("SELECT * FROM wp_posts WHERE post_status = 'publish'") is False
        )

    def test_group_by_with_having_is_not_dedup_only(self) -> None:
        sql = "SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(*) > 1"
        assert is_dedup_only_group_by(sql) is False

    def test_count_without_group_by_is_not_dedup_only(self) -> None:
        assert is_dedup_only_group_by("SELECT COUNT(*) FROM wp_posts") is False

    def test_each_real_aggregate_function_keeps_group_by_as_real_aggregation(self) -> None:
        for fn in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
            sql = f"SELECT customer_id, {fn}(amount) FROM orders GROUP BY customer_id"
            assert is_dedup_only_group_by(sql) is False, fn

    def test_case_insensitive(self) -> None:
        sql = "select wp_posts.id from wp_posts group by wp_posts.id order by post_date desc"
        assert is_dedup_only_group_by(sql) is True

    def test_empty_or_missing_sql_is_not_dedup_only(self) -> None:
        assert is_dedup_only_group_by("") is False
        assert is_dedup_only_group_by(None) is False

    def test_aggregate_in_subquery_or_cte_is_not_dedup_only(self) -> None:
        """Conservative: a real aggregate anywhere in the query disqualifies it,
        even one the outer query's own column list never names."""
        subquery = (
            "SELECT t.id FROM (SELECT dept_id, COUNT(*) c FROM employees "
            "GROUP BY dept_id) t GROUP BY t.id"
        )
        assert is_dedup_only_group_by(subquery) is False
        cte = "WITH agg AS (SELECT x, COUNT(*) c FROM t GROUP BY x) " "SELECT * FROM agg GROUP BY x"
        assert is_dedup_only_group_by(cte) is False

    def test_count_distinct_is_not_dedup_only(self) -> None:
        sql = "SELECT customer_id, COUNT(DISTINCT order_id) FROM orders GROUP BY customer_id"
        assert is_dedup_only_group_by(sql) is False

    def test_lowercase_having_is_not_dedup_only(self) -> None:
        sql = "select customer_id from orders group by customer_id having count(*) > 1"
        assert is_dedup_only_group_by(sql) is False

    def test_sum_over_window_is_not_dedup_only(self) -> None:
        sql = "SELECT id, SUM(amount) OVER (PARTITION BY customer_id) FROM orders GROUP BY id"
        assert is_dedup_only_group_by(sql) is False

    def test_parenthesised_boolean_groups_do_not_look_like_function_calls(self) -> None:
        """A real WordPress idiom: GROUP BY alongside parenthesised AND/OR/IN
        groups in the WHERE clause, no aggregate anywhere -- still dedup-only."""
        sql = (
            "SELECT p.* FROM p WHERE (a = 1 OR a = 2) AND (b IN (1, 2, 3)) "
            "AND NOT (c = 4) GROUP BY p.id"
        )
        assert is_dedup_only_group_by(sql) is True

    def test_backtick_quoted_aggregate_function_name_is_not_dedup_only(self) -> None:
        """``_STRIP_RE`` doesn't touch backticks, but a closing backtick right
        before "(" breaks _FUNCTION_CALL_RE's plain, unquoted match -- a
        dedicated quoted-function check must catch this case too."""
        sql = "SELECT id, `GROUP_CONCAT` ( tag ) FROM t GROUP BY id"
        assert is_dedup_only_group_by(sql) is False

    def test_double_quote_quoted_aggregate_function_name_is_not_dedup_only(self) -> None:
        """``_STRIP_RE`` treats a double-quoted span as a string literal and
        erases it outright, which would otherwise hide the function name from
        _FUNCTION_CALL_RE entirely."""
        sql = "SELECT id, \"string_agg\"(tag, ',') FROM t GROUP BY id"
        assert is_dedup_only_group_by(sql) is False


# Review table (#336 PR review): every one of these must NEVER be classified as
# dedup-only -- each is either a real aggregate under a name the original
# COUNT/SUM/AVG/MIN/MAX-only denylist missed, a window function, a
# ROLLUP/CUBE/GROUPING SETS super-aggregate, or a GROUP BY that is dead text
# inside a comment or string literal (so the live query has no real GROUP BY
# at all, and must not be treated as a resolvable one).
_NEVER_RESOLVED_CASES = {
    "GROUP_CONCAT": "SELECT id, GROUP_CONCAT(tag) FROM t GROUP BY id",
    "STRING_AGG": "SELECT id, STRING_AGG(tag, ',') FROM t GROUP BY id",
    "ARRAY_AGG": "SELECT id, ARRAY_AGG(tag) FROM t GROUP BY id",
    "JSON_ARRAYAGG": "SELECT id, JSON_ARRAYAGG(tag) FROM t GROUP BY id",
    "JSON_OBJECTAGG": "SELECT id, JSON_OBJECTAGG(k, v) FROM t GROUP BY id",
    "JSON_AGG": "SELECT id, JSON_AGG(tag) FROM t GROUP BY id",
    "JSONB_AGG": "SELECT id, JSONB_AGG(tag) FROM t GROUP BY id",
    "LISTAGG": "SELECT id, LISTAGG(tag, ',') FROM t GROUP BY id",
    "XMLAGG": "SELECT id, XMLAGG(tag) FROM t GROUP BY id",
    "BOOL_OR": "SELECT id, BOOL_OR(flag) FROM t GROUP BY id",
    "BOOL_AND": "SELECT id, BOOL_AND(flag) FROM t GROUP BY id",
    "EVERY": "SELECT id, EVERY(flag) FROM t GROUP BY id",
    "BIT_OR": "SELECT id, BIT_OR(flag) FROM t GROUP BY id",
    "BIT_AND": "SELECT id, BIT_AND(flag) FROM t GROUP BY id",
    "BIT_XOR": "SELECT id, BIT_XOR(flag) FROM t GROUP BY id",
    "STDDEV": "SELECT id, STDDEV(amount) FROM t GROUP BY id",
    "STDDEV_POP": "SELECT id, STDDEV_POP(amount) FROM t GROUP BY id",
    "STDDEV_SAMP": "SELECT id, STDDEV_SAMP(amount) FROM t GROUP BY id",
    "VARIANCE": "SELECT id, VARIANCE(amount) FROM t GROUP BY id",
    "VAR_POP": "SELECT id, VAR_POP(amount) FROM t GROUP BY id",
    "VAR_SAMP": "SELECT id, VAR_SAMP(amount) FROM t GROUP BY id",
    "MODE": "SELECT id, MODE() WITHIN GROUP (ORDER BY amount) FROM t GROUP BY id",
    "PERCENTILE_CONT": (
        "SELECT id, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM t GROUP BY id"
    ),
    "ANY_VALUE": "SELECT id, ANY_VALUE(name) FROM t GROUP BY id",
    "window function (ROW_NUMBER)": (
        "SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t GROUP BY id"
    ),
    "window function (RANK)": "SELECT id, RANK() OVER (ORDER BY id) FROM t GROUP BY id",
    "WITH ROLLUP": "SELECT id, region FROM t GROUP BY id, region WITH ROLLUP",
    "ROLLUP(...)": "SELECT id FROM t GROUP BY ROLLUP(id, region)",
    "CUBE(...)": "SELECT id FROM t GROUP BY CUBE(id, region)",
    "GROUPING SETS": "SELECT id FROM t GROUP BY GROUPING SETS ((id), (region))",
    "GROUP BY only in a -- line comment": "SELECT id FROM t -- GROUP BY id\nWHERE id = 1",
    "GROUP BY only in a /* */ block comment": "SELECT id FROM t /* GROUP BY id */ WHERE id = 1",
    "GROUP BY only in a string literal": "SELECT id FROM t WHERE note = 'please GROUP BY id'",
    "backtick-quoted function name": "SELECT id, `GROUP_CONCAT` ( tag ) FROM t GROUP BY id",
    "double-quote-quoted function name": "SELECT id, \"string_agg\"(tag, ',') FROM t GROUP BY id",
}


@pytest.mark.parametrize("sql", _NEVER_RESOLVED_CASES.values(), ids=_NEVER_RESOLVED_CASES.keys())
def test_never_resolved(sql: str) -> None:
    assert is_dedup_only_group_by(sql) is False
