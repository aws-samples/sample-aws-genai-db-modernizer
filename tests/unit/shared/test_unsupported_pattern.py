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

from src.shared.unsupported_pattern import (
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
