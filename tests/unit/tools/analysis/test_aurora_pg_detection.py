"""Unit tests for Aurora PostgreSQL regex-based feature detection.

Tests cover false-positive prevention and false-negative fixes compared to the
previous naive substring-matching approach.
"""


from src.tools.analysis.aurora_pg_analysis_tools import detect_pg_specific_features

# ==========================================================================
# CTE RECURSIVE  (aurora-pg-01)
# ==========================================================================


class TestCteRecursive:
    def test_standard_with_recursive(self):
        sql = "WITH RECURSIVE tree AS (SELECT id FROM categories)"
        assert "aurora-pg-01" in detect_pg_specific_features(sql)

    def test_lowercase_with_recursive(self):
        sql = "with recursive cte as (select 1)"
        assert "aurora-pg-01" in detect_pg_specific_features(sql)

    def test_whitespace_between_with_and_recursive(self):
        sql = "WITH\n  RECURSIVE tree AS (SELECT id FROM nodes)"
        assert "aurora-pg-01" in detect_pg_specific_features(sql)

    def test_with_tab_between_with_and_recursive(self):
        sql = "WITH\tRECURSIVE cte AS (SELECT 1)"
        assert "aurora-pg-01" in detect_pg_specific_features(sql)

    def test_non_recursive_cte_does_not_match(self):
        sql = "WITH cte AS (SELECT * FROM orders) SELECT * FROM cte"
        assert "aurora-pg-01" not in detect_pg_specific_features(sql)

    def test_word_recursive_in_column_name_does_not_match(self):
        # Column named "recursive_id" — no WITH before it
        sql = "SELECT recursive_id FROM hierarchy"
        assert "aurora-pg-01" not in detect_pg_specific_features(sql)


# ==========================================================================
# Window functions  (aurora-pg-02)
# ==========================================================================


class TestWindowFunctions:
    def test_row_number_over(self):
        sql = "SELECT ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) FROM emp"
        assert "aurora-pg-02" in detect_pg_specific_features(sql)

    def test_rank_over(self):
        sql = "SELECT RANK() OVER (ORDER BY score DESC) FROM scores"
        assert "aurora-pg-02" in detect_pg_specific_features(sql)

    def test_lag_lead(self):
        sql = "SELECT LAG(amount) OVER (PARTITION BY user_id ORDER BY ts) FROM events"
        assert "aurora-pg-02" in detect_pg_specific_features(sql)

    def test_over_with_multiple_spaces(self):
        sql = "SELECT SUM(val) OVER   (PARTITION BY grp) FROM t"
        assert "aurora-pg-02" in detect_pg_specific_features(sql)

    def test_partition_by_alone(self):
        sql = "SELECT AVG(x) OVER (PARTITION BY region) FROM sales"
        assert "aurora-pg-02" in detect_pg_specific_features(sql)

    def test_column_named_overdue_does_not_trigger(self):
        sql = "SELECT overdue, overall_score FROM tasks WHERE overdue = TRUE"
        assert "aurora-pg-02" not in detect_pg_specific_features(sql)

    def test_column_named_overall_does_not_trigger(self):
        sql = "SELECT overall FROM metrics"
        assert "aurora-pg-02" not in detect_pg_specific_features(sql)

    def test_over_keyword_without_parens_does_not_trigger(self):
        # "handed over the data" — narrative text, no window function syntax
        sql = "SELECT comment FROM notes WHERE comment LIKE '%handed over%'"
        assert "aurora-pg-02" not in detect_pg_specific_features(sql)


# ==========================================================================
# JSONB operations  (aurora-pg-03)
# ==========================================================================


class TestJsonbOperations:
    def test_json_arrow_arrow_operator(self):
        sql = "SELECT data->>'name' FROM users"
        assert "aurora-pg-03" in detect_pg_specific_features(sql)

    def test_jsonb_agg(self):
        sql = "SELECT jsonb_agg(row_to_json(t)) FROM t"
        assert "aurora-pg-03" in detect_pg_specific_features(sql)

    def test_jsonb_cast(self):
        sql = "SELECT '{\"key\": 1}'::jsonb @> '{\"key\": 1}'::jsonb"
        assert "aurora-pg-03" in detect_pg_specific_features(sql)

    def test_jsonb_containment_with_json_literal(self):
        # @> followed by a JSON literal (single-quoted object)
        sql = "SELECT id FROM docs WHERE payload @> '{\"active\": true}'"
        assert "aurora-pg-03" in detect_pg_specific_features(sql)

    def test_jsonb_build_object(self):
        sql = "SELECT jsonb_build_object('key', val) FROM t"
        assert "aurora-pg-03" in detect_pg_specific_features(sql)

    def test_at_gt_array_does_not_trigger_jsonb(self):
        # @> ARRAY[...] is array containment, NOT jsonb
        sql = "SELECT * FROM t WHERE tags @> ARRAY['foo', 'bar']"
        # Should match array (aurora-pg-04) but NOT jsonb (aurora-pg-03)
        results = detect_pg_specific_features(sql)
        assert "aurora-pg-04" in results
        assert "aurora-pg-03" not in results


# ==========================================================================
# Array operations  (aurora-pg-04)
# ==========================================================================


class TestArrayOperations:
    def test_array_agg(self):
        sql = "SELECT array_agg(id) FROM orders"
        assert "aurora-pg-04" in detect_pg_specific_features(sql)

    def test_unnest(self):
        sql = "SELECT unnest(tags) FROM articles"
        assert "aurora-pg-04" in detect_pg_specific_features(sql)

    def test_any_array_literal(self):
        sql = "SELECT * FROM t WHERE id = ANY(ARRAY[1, 2, 3])"
        assert "aurora-pg-04" in detect_pg_specific_features(sql)

    def test_array_brackets(self):
        sql = "SELECT ARRAY[1, 2, 3] AS nums"
        assert "aurora-pg-04" in detect_pg_specific_features(sql)

    def test_array_containment_with_array_literal(self):
        sql = "SELECT * FROM t WHERE tags @> ARRAY['foo']"
        assert "aurora-pg-04" in detect_pg_specific_features(sql)

    def test_any_with_subquery_does_not_trigger(self):
        # ANY(subquery) is not array syntax
        sql = "SELECT * FROM t WHERE id = ANY(SELECT id FROM other)"
        assert "aurora-pg-04" not in detect_pg_specific_features(sql)


# ==========================================================================
# Full-text search / tsvector  (aurora-pg-05)
# ==========================================================================


class TestTsvector:
    def test_to_tsvector(self):
        sql = "SELECT to_tsvector('english', body) FROM articles"
        assert "aurora-pg-05" in detect_pg_specific_features(sql)

    def test_plainto_tsquery(self):
        sql = "SELECT plainto_tsquery('english', 'search term') FROM dual"
        assert "aurora-pg-05" in detect_pg_specific_features(sql)

    def test_tsvector_match_operator(self):
        sql = "SELECT * FROM docs WHERE ts_col @@ to_tsquery('term')"
        assert "aurora-pg-05" in detect_pg_specific_features(sql)

    def test_tsvector_type_reference(self):
        sql = "ALTER TABLE articles ADD COLUMN search_vec tsvector"
        assert "aurora-pg-05" in detect_pg_specific_features(sql)


# ==========================================================================
# LATERAL joins  (aurora-pg-06)
# ==========================================================================


class TestLateralJoin:
    def test_join_lateral(self):
        sql = "SELECT * FROM orders o JOIN LATERAL (SELECT MAX(p) FROM payments WHERE oid = o.id) lp ON TRUE"
        assert "aurora-pg-06" in detect_pg_specific_features(sql)

    def test_comma_lateral(self):
        sql = "SELECT * FROM generate_series(1,10) gs, LATERAL (SELECT gs * 2) sub"
        assert "aurora-pg-06" in detect_pg_specific_features(sql)

    def test_lateral_paren(self):
        sql = "SELECT * FROM t, LATERAL(SELECT 1) sub"
        assert "aurora-pg-06" in detect_pg_specific_features(sql)

    def test_column_named_lateral_movement_does_not_trigger(self):
        sql = "SELECT lateral_movement, lateral_spread FROM threat_indicators WHERE lateral_movement > 0"
        assert "aurora-pg-06" not in detect_pg_specific_features(sql)

    def test_word_lateral_in_where_clause_does_not_trigger(self):
        sql = "SELECT * FROM incidents WHERE type = 'lateral'"
        assert "aurora-pg-06" not in detect_pg_specific_features(sql)


# ==========================================================================
# Upsert / ON CONFLICT  (aurora-pg-07)
# ==========================================================================


class TestUpsert:
    def test_on_conflict_do_update(self):
        sql = "INSERT INTO t (id, val) VALUES (1, 2) ON CONFLICT (id) DO UPDATE SET val = 2"
        assert "aurora-pg-07" in detect_pg_specific_features(sql)

    def test_on_conflict_do_nothing(self):
        sql = "INSERT INTO t (id) VALUES (1) ON CONFLICT DO NOTHING"
        assert "aurora-pg-07" in detect_pg_specific_features(sql)

    def test_returning_with_identifier(self):
        sql = "INSERT INTO orders (total) VALUES (100) RETURNING id"
        assert "aurora-pg-07" in detect_pg_specific_features(sql)

    def test_returning_star(self):
        sql = "UPDATE orders SET status = 'done' RETURNING *"
        assert "aurora-pg-07" in detect_pg_specific_features(sql)

    def test_returning_customer_in_where_does_not_trigger(self):
        # "returning_customer" is a column name in a WHERE clause — should NOT trigger
        sql = "SELECT * FROM users WHERE returning_customer = TRUE"
        assert "aurora-pg-07" not in detect_pg_specific_features(sql)

    def test_column_returning_in_select_list_does_not_trigger(self):
        # "returning" as an identifier alias without following whitespace+word
        sql = "SELECT customer_type AS returning FROM users"
        assert "aurora-pg-07" not in detect_pg_specific_features(sql)


# ==========================================================================
# Dead code: aurora-pg-08 never appears
# ==========================================================================


class TestDeadPatternPg08:
    def test_pg_08_never_in_results(self):
        queries = [
            "SELECT * FROM t",
            "WITH RECURSIVE cte AS (SELECT 1) SELECT * FROM cte",
            "SELECT ROW_NUMBER() OVER (PARTITION BY x) FROM t",
            "SELECT data->>'key' FROM t",
            "SELECT array_agg(id) FROM t",
            "SELECT to_tsvector('english', body) FROM t",
            "SELECT * FROM t JOIN LATERAL (SELECT 1) l ON TRUE",
            "INSERT INTO t VALUES (1) ON CONFLICT DO NOTHING",
            "SELECT * FROM t WHERE tags @> ARRAY['a']",
        ]
        for sql in queries:
            result = detect_pg_specific_features(sql)
            assert "aurora-pg-08" not in result, f"aurora-pg-08 appeared for SQL: {sql!r}"
