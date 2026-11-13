# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for engine exclusion rules and assignment integration."""

from src.agents.referee.engine_exclusions import EXCLUSION_RULES, ExclusionResult, check_exclusions


def check_all_exclusions(query_id: str, query_text: str) -> list[ExclusionResult]:
    """Every engine's exclusion of the query (the production gate checks one engine at a
    time; ``check_all_exclusions`` had no caller and was removed, #2154)."""
    engines = dict.fromkeys(rule.engine for rule in EXCLUSION_RULES)
    return [r for e in engines if (r := check_exclusions(query_id, query_text, e)) is not None]


class TestExclusionRules:
    """Test individual exclusion rule detection."""

    def test_dynamodb_excludes_leading_wildcard(self):
        sql = "SELECT * FROM posts WHERE title LIKE '%search_term%'"
        result = check_exclusions("q1", sql, "dynamodb")
        assert result is not None
        assert result.rule_id == "ddb-no-leading-wildcard"
        assert result.excluded_engine == "dynamodb"

    def test_dynamodb_allows_trailing_wildcard(self):
        sql = "SELECT * FROM posts WHERE title LIKE 'prefix%'"
        result = check_exclusions("q1", sql, "dynamodb")
        # trailing wildcard is fine — DynamoDB begins_with
        assert result is None

    def test_dynamodb_allows_simple_get(self):
        sql = "SELECT * FROM users WHERE id = ?"
        result = check_exclusions("q1", sql, "dynamodb")
        assert result is None

    def test_dynamodb_allows_aggregation(self):
        """Aggregation is NOT a hard exclusion — DynamoDB can pre-compute aggregates."""
        sql = "SELECT category, COUNT(*) FROM posts GROUP BY category"
        result = check_exclusions("q1", sql, "dynamodb")
        assert result is None

    def test_opensearch_excludes_insert_with_subquery(self):
        sql = """INSERT INTO actions (hook, status, scheduled_date)
                 SELECT 'cron', 'pending', NOW()
                 FROM DUAL WHERE NOT EXISTS (
                     SELECT 1 FROM actions WHERE hook = 'cron' AND status = 'pending'
                 )"""
        result = check_exclusions("q1", sql, "opensearch")
        assert result is not None
        assert result.rule_id == "os-no-write-subquery"

    def test_opensearch_excludes_update_with_subquery(self):
        sql = "UPDATE orders SET status = 'shipped' WHERE id IN (SELECT order_id FROM shipments)"
        result = check_exclusions("q1", sql, "opensearch")
        assert result is not None
        assert result.rule_id == "os-no-write-subquery"

    def test_opensearch_allows_simple_search(self):
        sql = "SELECT * FROM posts WHERE title LIKE '%search%'"
        result = check_exclusions("q1", sql, "opensearch")
        assert result is None

    def test_opensearch_allows_aggregation(self):
        sql = "SELECT category, COUNT(*) FROM posts GROUP BY category"
        result = check_exclusions("q1", sql, "opensearch")
        assert result is None


class TestCheckAllExclusions:
    """Test checking against all engines at once."""

    def test_leading_wildcard_excluded_from_dynamodb_not_opensearch(self):
        sql = "SELECT * FROM posts WHERE title LIKE '%search%'"
        results = check_all_exclusions("q1", sql)
        engines = {r.excluded_engine for r in results}
        assert "dynamodb" in engines
        assert "opensearch" not in engines

    def test_write_subquery_excluded_from_opensearch_not_dynamodb(self):
        sql = "INSERT INTO t1 (col) SELECT col FROM t2 WHERE x = 1"
        results = check_all_exclusions("q1", sql)
        engines = {r.excluded_engine for r in results}
        assert "opensearch" in engines
        assert "dynamodb" not in engines

    def test_aggregation_not_excluded_from_any_engine(self):
        """Aggregation is handled via penalties, not hard exclusions."""
        sql = "SELECT category, COUNT(*) FROM posts GROUP BY category"
        results = check_all_exclusions("q1", sql)
        assert len(results) == 0
