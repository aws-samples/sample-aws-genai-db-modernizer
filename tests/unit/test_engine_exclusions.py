"""Tests for engine exclusion rules and assignment integration."""

from src.agents.referee.engine_exclusions import (
    check_all_exclusions,
    check_exclusions,
    validate_customer_overrides,
)


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


class TestCustomerOverrideValidation:
    """Test that customer overrides are caught for hard exclusions only."""

    def test_catches_leading_wildcard_assigned_to_dynamodb(self):
        assignments = [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "customer_override": True,
            }
        ]
        queries = [
            {"query_id": "q1", "query_text": "SELECT * FROM posts WHERE title LIKE '%term%'"}
        ]
        violations = validate_customer_overrides(assignments, queries)
        assert len(violations) == 1
        assert violations[0]["query_id"] == "q1"
        assert violations[0]["original_engine"] == "dynamodb"
        assert violations[0]["rule_id"] == "ddb-no-leading-wildcard"

    def test_allows_aggregation_override_to_dynamodb(self):
        """Aggregation on DynamoDB is allowed — schema agent will design pre-computed pattern."""
        assignments = [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "customer_override": True,
            }
        ]
        queries = [{"query_id": "q1", "query_text": "SELECT COUNT(*) FROM posts GROUP BY status"}]
        violations = validate_customer_overrides(assignments, queries)
        assert len(violations) == 0

    def test_ignores_non_override_assignments(self):
        assignments = [
            {
                "query_id": "q1",
                "assigned_engine": "opensearch",
                "customer_override": False,
            }
        ]
        queries = [
            {"query_id": "q1", "query_text": "INSERT INTO t (c) SELECT c FROM t2 WHERE x = 1"}
        ]
        violations = validate_customer_overrides(assignments, queries)
        assert len(violations) == 0

    def test_valid_override_passes(self):
        assignments = [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "customer_override": True,
            }
        ]
        queries = [{"query_id": "q1", "query_text": "SELECT * FROM users WHERE id = ?"}]
        violations = validate_customer_overrides(assignments, queries)
        assert len(violations) == 0
