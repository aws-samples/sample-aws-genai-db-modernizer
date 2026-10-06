"""Unit tests for the capability registry."""

from src.agents.referee.capability_registry import (
    LIGHTWEIGHT_ALTERNATIVES,
    SIGNAL_TO_CAPABILITY,
    can_engine_serve_capability,
    detect_required_capabilities,
    is_dedup_only_group_by,
    requires_aggregation_capability,
    suggest_lightweight_alternative,
)


class TestDetectRequiredCapabilities:
    """Tests for detect_required_capabilities()."""

    def test_detects_inverted_index_from_like_wildcard(self):
        caps = detect_required_capabilities("SELECT * FROM posts WHERE title LIKE '%search%'", [])
        assert "inverted_index" in caps

    def test_detects_inverted_index_from_match_against(self):
        caps = detect_required_capabilities(
            "SELECT * FROM posts WHERE MATCH(title, body) AGAINST('search term')", []
        )
        assert "inverted_index" in caps

    def test_detects_inverted_index_from_tsvector(self):
        caps = detect_required_capabilities(
            "SELECT * FROM posts WHERE to_tsvector(body) @@ to_tsquery('search')", []
        )
        assert "inverted_index" in caps

    def test_detects_inverted_index_from_text_search_signal(self):
        caps = detect_required_capabilities("SELECT * FROM posts", ["text_search"])
        assert "inverted_index" in caps

    def test_detects_scan_engine_from_window_function(self):
        caps = detect_required_capabilities(
            "SELECT ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at) FROM posts",
            [],
        )
        assert "scan_engine" in caps

    def test_detects_scan_engine_from_recursive_cte(self):
        caps = detect_required_capabilities(
            "WITH RECURSIVE cte AS (SELECT id FROM categories) SELECT * FROM cte", []
        )
        assert "scan_engine" in caps

    def test_no_capabilities_for_simple_select(self):
        caps = detect_required_capabilities("SELECT id, name FROM users WHERE id = 42", [])
        assert caps == []

    def test_no_capabilities_for_empty_text(self):
        caps = detect_required_capabilities("", [])
        assert caps == []

    def test_no_capabilities_for_basic_signals(self):
        caps = detect_required_capabilities(
            "SELECT * FROM users WHERE id = ?",
            ["key_value_lookups", "high_frequency_reads"],
        )
        assert caps == []

    def test_multiple_capabilities_detected(self):
        """A query can require multiple hard capabilities."""
        caps = detect_required_capabilities(
            "WITH RECURSIVE tree AS (SELECT * FROM cats) "
            "SELECT * FROM tree WHERE title LIKE '%test%'",
            ["text_search"],
        )
        assert "inverted_index" in caps
        assert "scan_engine" in caps

    def test_case_insensitive_detection(self):
        caps = detect_required_capabilities("SELECT * FROM posts WHERE title like '%hello%'", [])
        assert "inverted_index" in caps


class TestCanEngineServeCapability:
    """Tests for can_engine_serve_capability()."""

    def test_no_requirements_always_passes(self):
        assert can_engine_serve_capability("dynamodb", []) is True
        assert can_engine_serve_capability("opensearch", []) is True
        assert can_engine_serve_capability("documentdb", []) is True

    def test_opensearch_has_inverted_index(self):
        assert can_engine_serve_capability("opensearch", ["inverted_index"]) is True

    def test_dynamodb_lacks_inverted_index(self):
        assert can_engine_serve_capability("dynamodb", ["inverted_index"]) is False

    def test_documentdb_lacks_inverted_index(self):
        assert can_engine_serve_capability("documentdb", ["inverted_index"]) is False

    def test_opensearch_has_scan_engine(self):
        assert can_engine_serve_capability("opensearch", ["scan_engine"]) is True

    def test_dynamodb_lacks_scan_engine(self):
        assert can_engine_serve_capability("dynamodb", ["scan_engine"]) is False

    def test_documentdb_has_multi_doc_acid(self):
        assert can_engine_serve_capability("documentdb", ["multi_doc_acid"]) is True

    def test_dynamodb_lacks_multi_doc_acid(self):
        assert can_engine_serve_capability("dynamodb", ["multi_doc_acid"]) is False

    def test_multiple_capabilities_all_required(self):
        """Engine must have ALL required capabilities, not just one."""
        # OpenSearch has both inverted_index and scan_engine
        assert can_engine_serve_capability("opensearch", ["inverted_index", "scan_engine"]) is True
        # DynamoDB has neither
        assert can_engine_serve_capability("dynamodb", ["inverted_index", "scan_engine"]) is False

    def test_unknown_engine_fails(self):
        assert can_engine_serve_capability("unknown", ["inverted_index"]) is False

    def test_strong_consistency_matrix(self):
        assert can_engine_serve_capability("dynamodb", ["strong_consistency"]) is True
        assert can_engine_serve_capability("documentdb", ["strong_consistency"]) is True
        assert can_engine_serve_capability("opensearch", ["strong_consistency"]) is False


class TestSignalToCapability:
    """Tests for signal-to-capability derivation."""

    def test_text_search_maps_to_inverted_index(self):
        assert SIGNAL_TO_CAPABILITY["text_search"] == "inverted_index"

    def test_detect_from_signal_only(self):
        """Signal derivation works even without SQL text patterns."""
        caps = detect_required_capabilities("SELECT 1", ["text_search"])
        assert "inverted_index" in caps


class TestAggregationAndJoinCapabilities:
    """The serviceability gate never has an empty required_caps for these (#338)."""

    def test_aggregations_signal_maps_to_aggregation(self):
        assert SIGNAL_TO_CAPABILITY["aggregations"] == "aggregation"

    def test_complex_joins_signal_maps_to_complex_joins(self):
        assert SIGNAL_TO_CAPABILITY["complex_joins"] == "complex_joins"

    def test_detect_aggregation_from_signal(self):
        caps = detect_required_capabilities("SELECT 1", ["aggregations"])
        assert "aggregation" in caps

    def test_detect_complex_joins_from_signal(self):
        caps = detect_required_capabilities("SELECT 1", ["complex_joins"])
        assert "complex_joins" in caps

    def test_aurora_mysql_and_postgresql_serve_aggregation_and_joins(self):
        assert can_engine_serve_capability("aurora_mysql", ["aggregation", "complex_joins"])
        assert can_engine_serve_capability("aurora_postgresql", ["aggregation", "complex_joins"])

    def test_dynamodb_cannot_serve_aggregation_or_joins(self):
        assert can_engine_serve_capability("dynamodb", ["aggregation"]) is False
        assert can_engine_serve_capability("dynamodb", ["complex_joins"]) is False

    def test_documentdb_serves_aggregation_but_not_joins(self):
        assert can_engine_serve_capability("documentdb", ["aggregation"]) is True
        assert can_engine_serve_capability("documentdb", ["complex_joins"]) is False

    def test_opensearch_serves_aggregation_but_not_joins(self):
        assert can_engine_serve_capability("opensearch", ["aggregation"]) is True
        assert can_engine_serve_capability("opensearch", ["complex_joins"]) is False

    def test_woocommerce_sum_join_query_requires_aggregation_and_joins(self):
        """The exact shape from #338's reproduction: SUM(...) across a 3-table JOIN."""
        caps = detect_required_capabilities(
            "SELECT order_items.order_item_name, SUM(meta.meta_value) FROM wp_posts "
            "JOIN wp_woocommerce_order_items order_items ON ... "
            "JOIN wp_woocommerce_order_itemmeta meta ON ... GROUP BY order_items.order_item_name",
            ["aggregations", "complex_joins"],
        )
        assert "aggregation" in caps
        assert "complex_joins" in caps
        assert can_engine_serve_capability("dynamodb", caps) is False
        assert can_engine_serve_capability("aurora_mysql", caps) is True


class TestSuggestLightweightAlternative:
    """Tests for suggest_lightweight_alternative()."""

    def test_inverted_index_suggests_opensearch_serverless(self):
        alt = suggest_lightweight_alternative("inverted_index")
        assert alt is not None
        assert "OpenSearch Serverless" in alt["service"]

    def test_scan_engine_suggests_athena(self):
        alt = suggest_lightweight_alternative("scan_engine")
        assert alt is not None
        assert "Athena" in alt["service"]

    def test_multi_doc_acid_suggests_saga(self):
        alt = suggest_lightweight_alternative("multi_doc_acid")
        assert alt is not None
        assert "saga" in alt["service"].lower() or "Step Functions" in alt["pattern"]

    def test_unknown_capability_returns_none(self):
        alt = suggest_lightweight_alternative("nonexistent_cap")
        assert alt is None

    def test_all_alternatives_have_required_fields(self):
        for _cap, alt in LIGHTWEIGHT_ALTERNATIVES.items():
            assert "service" in alt
            assert "pattern" in alt
            assert "cost_profile" in alt
            assert "limitations" in alt


class TestSingleTableAggregates:
    """An aggregate function needs aggregation capability on one table too.

    A review of a related PR flagged single-table COUNT(*)/FOUND_ROWS()
    queries moving from Aurora to DynamoDB: the serviceability gate's
    required_caps was [] for them because the registry's old detectors only
    fired on GROUP BY+aggregate or join+aggregate, never a bare aggregate call.
    """

    def test_bare_count_star_requires_aggregation(self):
        caps = detect_required_capabilities(
            "SELECT COUNT(*) FROM wp_postmeta WHERE meta_key = ? AND post_id = ?", []
        )
        assert "aggregation" in caps

    def test_bare_sum_requires_aggregation(self):
        caps = detect_required_capabilities("SELECT SUM(amount) FROM orders WHERE id = ?", [])
        assert "aggregation" in caps

    def test_found_rows_requires_aggregation(self):
        caps = detect_required_capabilities("SELECT `FOUND_ROWS` ( )", [])
        assert "aggregation" in caps

    def test_sql_calc_found_rows_requires_aggregation(self):
        caps = detect_required_capabilities(
            "SELECT SQL_CALC_FOUND_ROWS wp_users.ID FROM wp_users", []
        )
        assert "aggregation" in caps

    def test_dynamodb_cannot_serve_bare_count(self):
        caps = detect_required_capabilities("SELECT COUNT(*) FROM wp_postmeta", [])
        assert can_engine_serve_capability("dynamodb", caps) is False

    def test_aurora_mysql_can_serve_bare_count(self):
        caps = detect_required_capabilities("SELECT COUNT(*) FROM wp_postmeta", [])
        assert can_engine_serve_capability("aurora_mysql", caps) is True


class TestIsDedupOnlyGroupByIntegration:
    """is_dedup_only_group_by (src.shared.unsupported_pattern, #336) is re-exported
    here and feeds the "aggregation" capability detector (#338); its own
    semantics are tested in tests/unit/shared/test_unsupported_pattern.py."""

    def test_reexported_from_capability_registry(self):
        assert is_dedup_only_group_by is not None

    def test_dedup_group_by_does_not_require_aggregation_capability(self):
        text = (
            "SELECT wp_posts.ID FROM wp_posts WHERE post_type = ? GROUP BY wp_posts.ID "
            "ORDER BY wp_posts.post_date"
        )
        assert is_dedup_only_group_by(text) is True
        assert "aggregation" not in detect_required_capabilities(text, [])


class TestAggregationCoversStringAndWindowFunctions:
    """GROUP_CONCAT/STRING_AGG/ARRAY_AGG and window functions need aggregation
    capability too (review finding 6)."""

    def test_group_concat_requires_aggregation(self):
        assert (
            requires_aggregation_capability("SELECT x, GROUP_CONCAT(y) FROM t GROUP BY x") is True
        )

    def test_string_agg_requires_aggregation(self):
        assert requires_aggregation_capability("SELECT STRING_AGG(name, ',') FROM t") is True

    def test_array_agg_requires_aggregation(self):
        assert requires_aggregation_capability("SELECT ARRAY_AGG(id) FROM t GROUP BY x") is True

    def test_window_function_requires_aggregation(self):
        assert (
            requires_aggregation_capability(
                "SELECT SUM(amount) OVER (PARTITION BY customer_id) FROM orders"
            )
            is True
        )

    def test_row_number_window_requires_aggregation(self):
        assert (
            requires_aggregation_capability("SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t") is True
        )

    def test_dynamodb_cannot_serve_group_concat(self):
        caps = detect_required_capabilities("SELECT GROUP_CONCAT(y) FROM t", [])
        assert can_engine_serve_capability("dynamodb", caps) is False

    def test_dedup_only_group_by_still_excluded(self):
        text = "SELECT id FROM t GROUP BY id ORDER BY id"
        assert requires_aggregation_capability(text) is False


class TestComplexJoinsThreeTables:
    """A 3-table join (2 JOIN clauses) is complex, not just 4+ tables (review finding 3)."""

    def test_two_join_clauses_is_complex(self):
        from src.agents.referee.triage import _detect_query_signals

        co = {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": (
                            "SELECT a.id FROM t1 a JOIN t2 b ON a.id=b.id JOIN t3 c ON b.id=c.id"
                        ),
                        "query_type": "SELECT",
                        "has_joins": True,
                        "join_count": 2,
                        "tables_accessed": ["t1", "t2", "t3"],
                        "rows_returned_avg": 10,
                        "calls_per_second": 1,
                    }
                ]
            }
        }
        signals = _detect_query_signals(co)
        complex_joins_signal = next((s for s in signals if s.signal == "complex_joins"), None)
        assert complex_joins_signal is not None
        assert "q1" in complex_joins_signal.query_ids


class TestUtilityStatementRequiresSqlAdmin:
    """A utility statement needs "sql_admin", which only Aurora has (review finding 1)."""

    def test_show_statement_requires_sql_admin(self):
        caps = detect_required_capabilities("SHOW FULL FIELDS FROM wp_options", [])
        assert "sql_admin" in caps

    def test_set_session_requires_sql_admin(self):
        caps = detect_required_capabilities("SET SESSION SQL_BIG_SELECTS = ?", [])
        assert "sql_admin" in caps

    def test_dynamodb_cannot_serve_sql_admin(self):
        caps = detect_required_capabilities("SHOW FULL FIELDS FROM wp_options", [])
        assert can_engine_serve_capability("dynamodb", caps) is False

    def test_aurora_mysql_and_postgresql_serve_sql_admin(self):
        caps = detect_required_capabilities("SHOW FULL FIELDS FROM wp_options", [])
        assert can_engine_serve_capability("aurora_mysql", caps) is True
        assert can_engine_serve_capability("aurora_postgresql", caps) is True

    def test_ordinary_query_does_not_require_sql_admin(self):
        caps = detect_required_capabilities("SELECT * FROM wp_posts WHERE id = ?", [])
        assert "sql_admin" not in caps

    def test_catalog_only_table_access_requires_sql_admin(self):
        caps = detect_required_capabilities("SELECT 1", ["aggregations"], ["pg_class"])
        assert "sql_admin" in caps


class TestComputedJoinCapability:
    """A join on a computed expression needs an engine that can evaluate it
    (review finding B2), e.g. lower(groups.name) = users.username_lower."""

    def test_computed_expression_join_requires_capability(self):
        from src.agents.referee.capability_registry import requires_computed_join_capability

        text = (
            'SELECT "users".* FROM "users" LEFT JOIN groups ON '
            "lower(groups.name) = users.username_lower WHERE groups.id IS NOT NULL"
        )
        assert requires_computed_join_capability(text) is True
        caps = detect_required_capabilities(text, [])
        assert "computed_join" in caps

    def test_computed_expression_on_the_right_side_requires_capability(self):
        """A second review of #375: the function can be on either side of the
        comparison -- `ON u.x = lower(g.name)` was previously missed because
        only the left side was checked."""
        from src.agents.referee.capability_registry import requires_computed_join_capability

        text = (
            'SELECT "users".* FROM "users" LEFT JOIN groups ON '
            "users.username_lower = lower(groups.name) WHERE groups.id IS NOT NULL"
        )
        assert requires_computed_join_capability(text) is True
        caps = detect_required_capabilities(text, [])
        assert "computed_join" in caps

    def test_function_compared_against_a_parameter_inside_on_does_not_require_capability(self):
        """A second review of #375: `ON ... AND lower(g.name) = $1` tests a
        computed value against a filter value -- it is not a join on a
        computed key, so it must not require the capability."""
        from src.agents.referee.capability_registry import requires_computed_join_capability

        text = (
            "SELECT * FROM users JOIN groups ON users.group_id = groups.id "
            "AND lower(groups.name) = $1"
        )
        assert requires_computed_join_capability(text) is False
        caps = detect_required_capabilities(text, [])
        assert "computed_join" not in caps

    def test_plain_key_join_does_not_require_capability(self):
        from src.agents.referee.capability_registry import requires_computed_join_capability

        text = (
            'SELECT $1 FROM "users" INNER JOIN "group_users" ON '
            '"users"."id" = "group_users"."user_id" WHERE "group_users"."group_id" = $2'
        )
        assert requires_computed_join_capability(text) is False
        caps = detect_required_capabilities(text, [])
        assert "computed_join" not in caps

    def test_function_call_outside_the_join_condition_does_not_count(self):
        """A function call in an unrelated WHERE clause isn't part of the join condition."""
        from src.agents.referee.capability_registry import requires_computed_join_capability

        text = (
            "SELECT * FROM t1 JOIN t2 ON t1.id = t2.id " "WHERE lower(t1.name) = ? GROUP BY t1.id"
        )
        assert requires_computed_join_capability(text) is False

    def test_dynamodb_cannot_serve_computed_join(self):
        caps = ["computed_join"]
        assert can_engine_serve_capability("dynamodb", caps) is False
        assert can_engine_serve_capability("documentdb", caps) is False
        assert can_engine_serve_capability("opensearch", caps) is False

    def test_aurora_serves_computed_join(self):
        caps = ["computed_join"]
        assert can_engine_serve_capability("aurora_mysql", caps) is True
        assert can_engine_serve_capability("aurora_postgresql", caps) is True

    def test_no_join_at_all_does_not_require_capability(self):
        from src.agents.referee.capability_registry import requires_computed_join_capability

        assert requires_computed_join_capability("SELECT lower(name) FROM users") is False
