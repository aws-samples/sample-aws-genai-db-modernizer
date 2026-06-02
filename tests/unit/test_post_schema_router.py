"""Tests for post-schema router logic."""


from src.agents.referee.post_schema_router import (
    _extract_unsupported_query_ids,
    _parse_pe_routing_notes,
    _select_target_engine,
    route_unsupported_queries,
)


class TestParsePERoutingNotes:
    """Test parsing of [ROUTING] entries from PE notes."""

    def test_parses_single_routing_note(self):
        notes = ["[ROUTING] query_ids=[q-42,q-55] → opensearch | reason: full-text LIKE search"]
        result = _parse_pe_routing_notes(notes)
        assert len(result) == 1
        assert result[0]["query_ids"] == ["q-42", "q-55"]
        assert result[0]["target_engine"] == "opensearch"
        assert result[0]["reason"] == "full-text LIKE search"

    def test_parses_multiple_routing_notes(self):
        notes = [
            "[ROUTING] query_ids=[q-1] → opensearch | reason: text search",
            "Some other PE note about capacity",
            "[ROUTING] query_ids=[q-2,q-3] → documentdb | reason: complex joins",
        ]
        result = _parse_pe_routing_notes(notes)
        assert len(result) == 2
        assert result[0]["query_ids"] == ["q-1"]
        assert result[1]["query_ids"] == ["q-2", "q-3"]

    def test_ignores_non_routing_notes(self):
        notes = [
            "Consider monitoring hot partition on Users table",
            "Cost estimate: ~$45/month on-demand",
        ]
        result = _parse_pe_routing_notes(notes)
        assert len(result) == 0

    def test_handles_empty_notes(self):
        result = _parse_pe_routing_notes([])
        assert len(result) == 0

    def test_handles_whitespace_in_query_ids(self):
        notes = ["[ROUTING] query_ids=[q-1, q-2, q-3] → opensearch | reason: search"]
        result = _parse_pe_routing_notes(notes)
        assert result[0]["query_ids"] == ["q-1", "q-2", "q-3"]


class TestExtractUnsupportedQueryIds:
    """Test extraction of query IDs from unsupported patterns."""

    def test_dynamodb_format(self):
        schema_output = {
            "unsupported_patterns": [
                {
                    "query_ids": ["q-10", "q-11"],
                    "pattern_type": "text_search",
                    "recommendation": "Use OpenSearch",
                },
            ]
        }
        result = _extract_unsupported_query_ids(schema_output, "dynamodb")
        assert len(result) == 1
        assert result[0]["query_ids"] == ["q-10", "q-11"]

    def test_documentdb_format(self):
        schema_output = {
            "unsupported_patterns": [
                {
                    "source_query_ids": ["q-20"],
                    "reason": "graph traversal",
                    "recommendation": "Use Neptune",
                },
            ]
        }
        result = _extract_unsupported_query_ids(schema_output, "documentdb")
        assert len(result) == 1
        assert result[0]["query_ids"] == ["q-20"]

    def test_opensearch_format_with_query_ids(self):
        schema_output = {
            "unsupported_patterns": [
                {
                    "query_ids": ["q-30"],
                    "source_query": "INSERT INTO ...",
                    "reason": "transactional write",
                    "recommendation": "Use DynamoDB",
                },
            ]
        }
        result = _extract_unsupported_query_ids(schema_output, "opensearch")
        assert len(result) == 1
        assert result[0]["query_ids"] == ["q-30"]

    def test_opensearch_format_without_query_ids(self):
        """Backward compat: old OpenSearch outputs without query_ids field."""
        schema_output = {
            "unsupported_patterns": [
                {
                    "source_query": "INSERT INTO ...",
                    "reason": "transactional write",
                    "recommendation": "Use DynamoDB",
                },
            ]
        }
        result = _extract_unsupported_query_ids(schema_output, "opensearch")
        assert len(result) == 0

    def test_no_unsupported_patterns(self):
        schema_output = {"unsupported_patterns": []}
        result = _extract_unsupported_query_ids(schema_output, "dynamodb")
        assert len(result) == 0


class TestSelectTargetEngine:
    """Test engine selection logic."""

    def test_selects_first_available_engine(self):
        target = _select_target_engine(
            "q-1",
            "SELECT * FROM posts WHERE title LIKE '%term%'",
            "dynamodb",
            ["dynamodb", "opensearch", "documentdb"],
            set(),
        )
        assert target == "documentdb"

    def test_respects_exclusions(self):
        sql = "INSERT INTO t1 (col) SELECT col FROM t2"
        target = _select_target_engine(
            "q-1", sql, "dynamodb", ["dynamodb", "opensearch", "documentdb"], set()
        )
        assert target == "documentdb"

    def test_skips_from_engine(self):
        target = _select_target_engine(
            "q-1", "SELECT * FROM users WHERE id = 1", "dynamodb", ["dynamodb", "opensearch"], set()
        )
        assert target == "opensearch"

    def test_returns_none_when_no_candidate(self):
        target = _select_target_engine(
            "q-1", "SELECT * FROM users WHERE id = 1", "dynamodb", ["dynamodb"], set()
        )
        assert target is None

    def test_skips_already_failed_engines(self):
        target = _select_target_engine(
            "q-1",
            "SELECT * FROM users WHERE id = 1",
            "dynamodb",
            ["dynamodb", "opensearch", "documentdb"],
            already_failed={"documentdb"},
        )
        assert target == "opensearch"


class TestRouteUnsupportedQueries:
    """Integration tests for the full router."""

    def test_no_unsupported_returns_empty(self):
        schema_outputs = {
            "dynamodb": {"unsupported_patterns": []},
            "opensearch": {"unsupported_patterns": []},
        }
        result = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=["dynamodb", "opensearch"],
        )
        assert len(result.routings) == 0
        assert len(result.terminal_queries) == 0

    def test_routes_unsupported_to_next_engine(self):
        schema_outputs = {
            "dynamodb": {
                "unsupported_patterns": [
                    {
                        "query_ids": ["q-1", "q-2"],
                        "pattern_type": "text_search",
                        "recommendation": "Use OpenSearch",
                    },
                ]
            },
            "opensearch": {"unsupported_patterns": []},
        }
        result = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=["dynamodb", "opensearch"],
            query_texts={
                "q-1": "SELECT * FROM posts WHERE title LIKE '%x%'",
                "q-2": "SELECT * FROM posts WHERE body LIKE '%y%'",
            },
        )
        assert len(result.routings) == 2
        for r in result.routings:
            assert r.to_engine == "opensearch"
            assert r.from_engine == "dynamodb"

    def test_pe_routing_notes_take_priority(self):
        schema_outputs = {
            "dynamodb": {"unsupported_patterns": []},
        }
        pe_notes = {
            "dynamodb": ["[ROUTING] query_ids=[q-5] → opensearch | reason: full-text search"]
        }
        result = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=["dynamodb", "opensearch"],
            pe_notes_by_engine=pe_notes,
        )
        assert len(result.routings) == 1
        assert result.routings[0].query_id == "q-5"
        assert result.routings[0].to_engine == "opensearch"

    def test_cascade_depth_exceeded_marks_terminal(self):
        schema_outputs = {
            "dynamodb": {
                "unsupported_patterns": [
                    {
                        "query_ids": ["q-99"],
                        "pattern_type": "aggregation",
                        "recommendation": "complex OLAP",
                    },
                ]
            },
        }
        result = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=["dynamodb"],
            cascade_depth=2,
            max_depth=2,
        )
        assert len(result.terminal_queries) == 1
        assert "q-99" in result.terminal_queries

    def test_deduplicates_across_engines(self):
        """Same query unsupported by two engines — only routed once."""
        schema_outputs = {
            "dynamodb": {
                "unsupported_patterns": [
                    {
                        "query_ids": ["q-1"],
                        "pattern_type": "text_search",
                        "recommendation": "use opensearch",
                    },
                ]
            },
            "documentdb": {
                "unsupported_patterns": [
                    {
                        "source_query_ids": ["q-1"],
                        "reason": "text search",
                        "recommendation": "use opensearch",
                    },
                ]
            },
        }
        result = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=["dynamodb", "documentdb", "opensearch"],
            query_texts={"q-1": "SELECT * FROM posts WHERE title LIKE '%x%'"},
        )
        routed_ids = [r.query_id for r in result.routings]
        assert routed_ids.count("q-1") == 1

    def test_already_routed_queries_skipped(self):
        schema_outputs = {
            "dynamodb": {
                "unsupported_patterns": [
                    {
                        "query_ids": ["q-1"],
                        "pattern_type": "text_search",
                        "recommendation": "use opensearch",
                    },
                ]
            },
        }
        result = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=["dynamodb", "opensearch"],
            already_routed={"q-1"},
        )
        assert len(result.routings) == 0
        assert len(result.terminal_queries) == 0

    def test_respects_hard_exclusions_on_target(self):
        """Router should not route to an engine that has a hard exclusion."""
        schema_outputs = {
            "documentdb": {
                "unsupported_patterns": [
                    {
                        "source_query_ids": ["q-1"],
                        "reason": "needs search",
                        "recommendation": "use opensearch",
                    },
                ]
            },
        }
        result = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=["documentdb", "opensearch", "dynamodb"],
            query_texts={"q-1": "INSERT INTO t1 (c) SELECT c FROM t2 WHERE x = 1"},
        )
        if result.routings:
            assert result.routings[0].to_engine == "dynamodb"
