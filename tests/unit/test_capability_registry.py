"""Unit tests for the capability registry."""

from src.agents.referee.capability_registry import (
    LIGHTWEIGHT_ALTERNATIVES,
    SIGNAL_TO_CAPABILITY,
    can_engine_serve_capability,
    detect_required_capabilities,
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
