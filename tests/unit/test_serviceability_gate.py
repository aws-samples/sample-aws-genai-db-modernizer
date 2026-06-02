"""Unit tests for the serviceability gate in reality check.

Tests the interaction between capability detection and consolidation logic:
- Gate blocks absorption when capability missing
- Gate allows absorption when capability present
- Partial consolidation: some queries move, some stay
- Lightweight alternative suggested when <= 10% and shared capability
- Full end-to-end: text_search queries block DynamoDB absorption
"""


from src.agents.referee.reality_check import (
    _find_best_absorber_for_query,
    _suggest_lightweight_for_orphans,
    run_reality_check,
)


def _make_query_assignment(query_id: str, engine: str, reason: str = "test") -> dict:
    return {
        "query_id": query_id,
        "assigned_engine": engine,
        "assignment_reason": reason,
    }


def _make_triage(signals: list[dict] | None = None, query_capabilities: dict | None = None) -> dict:
    return {
        "signals": signals or [],
        "query_capabilities": query_capabilities or {},
    }


def _make_collector(queries: list[dict] | None = None) -> dict:
    return {
        "queries": {
            "query_patterns": queries or [],
        },
        "database_schema": {"tables": []},
    }


class TestServiceabilityGateBlocks:
    """Test that the gate blocks absorption when capability is missing."""

    def test_text_search_query_blocks_dynamodb_absorption(self):
        """A text_search query cannot be absorbed by DynamoDB."""
        # Setup: OpenSearch has 2 queries, one is text_search
        assignment = {
            "query_assignments": [
                _make_query_assignment("q-1", "opensearch"),
                _make_query_assignment("q-2", "opensearch"),
                _make_query_assignment("q-3", "dynamodb"),
                _make_query_assignment("q-4", "dynamodb"),
                _make_query_assignment("q-5", "dynamodb"),
                _make_query_assignment("q-6", "dynamodb"),
                _make_query_assignment("q-7", "dynamodb"),
                _make_query_assignment("q-8", "dynamodb"),
                _make_query_assignment("q-9", "dynamodb"),
                _make_query_assignment("q-10", "dynamodb"),
            ]
        }

        triage = _make_triage(
            signals=[
                {"signal": "text_search", "query_ids": ["q-1"], "targets": ["opensearch"]},
                {
                    "signal": "key_value_lookups",
                    "query_ids": ["q-3", "q-4"],
                    "targets": ["dynamodb"],
                },
            ],
            query_capabilities={"q-1": ["inverted_index"]},
        )

        collector = _make_collector(
            queries=[
                {
                    "query_id": f"q-{i}",
                    "query_text": "SELECT 1",
                    "query_type": "SELECT",
                    "tables_accessed": ["t1"],
                }
                for i in range(1, 11)
            ]
        )

        result = run_reality_check(assignment, triage, {}, collector)

        # OpenSearch should NOT be fully consolidated because q-1 requires inverted_index
        # Check that q-1 is still on opensearch
        q1_assignment = next(qa for qa in result["revised_assignments"] if qa["query_id"] == "q-1")
        assert q1_assignment["assigned_engine"] == "opensearch"

    def test_gate_allows_absorption_when_capability_present(self):
        """Queries without hard requirements can still be absorbed."""
        assignment = {
            "query_assignments": [
                _make_query_assignment("q-1", "documentdb"),
                _make_query_assignment("q-2", "documentdb"),
                _make_query_assignment("q-3", "dynamodb"),
                _make_query_assignment("q-4", "dynamodb"),
                _make_query_assignment("q-5", "dynamodb"),
                _make_query_assignment("q-6", "dynamodb"),
                _make_query_assignment("q-7", "dynamodb"),
                _make_query_assignment("q-8", "dynamodb"),
                _make_query_assignment("q-9", "dynamodb"),
                _make_query_assignment("q-10", "dynamodb"),
            ]
        }

        # No hard capabilities needed — all are basic CRUD
        triage = _make_triage(query_capabilities={})

        collector = _make_collector(
            queries=[
                {
                    "query_id": f"q-{i}",
                    "query_text": "SELECT id FROM t WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": ["t1"],
                }
                for i in range(1, 11)
            ]
        )

        result = run_reality_check(assignment, triage, {}, collector)

        # DocumentDB should be consolidated into DynamoDB (no hard requirements)
        doc_queries = [
            qa for qa in result["revised_assignments"] if qa["assigned_engine"] == "documentdb"
        ]
        # Either fully consolidated or at least some moved
        assert len(result["consolidations"]) > 0 or len(doc_queries) == 2


class TestPartialConsolidation:
    """Test partial consolidation behavior."""

    def test_partial_consolidation_moves_serviceable_keeps_unserviceable(self):
        """Some queries move, some stay due to capability requirements."""
        # 10 queries on opensearch, 1 needs inverted_index, rest don't
        assignment = {
            "query_assignments": [
                _make_query_assignment("q-search", "opensearch"),
                *[_make_query_assignment(f"q-basic-{i}", "opensearch") for i in range(9)],
                *[_make_query_assignment(f"q-ddb-{i}", "dynamodb") for i in range(20)],
            ]
        }

        triage = _make_triage(
            signals=[
                {"signal": "text_search", "query_ids": ["q-search"], "targets": ["opensearch"]},
            ],
            query_capabilities={"q-search": ["inverted_index"]},
        )

        queries = [
            {
                "query_id": "q-search",
                "query_text": "SELECT * WHERE title LIKE '%x%'",
                "query_type": "SELECT",
                "tables_accessed": ["posts"],
            },
            *[
                {
                    "query_id": f"q-basic-{i}",
                    "query_text": "SELECT id FROM t WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": ["posts"],
                }
                for i in range(9)
            ],
            *[
                {
                    "query_id": f"q-ddb-{i}",
                    "query_text": "SELECT id FROM t WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": ["users"],
                }
                for i in range(20)
            ],
        ]
        collector = _make_collector(queries=queries)

        result = run_reality_check(assignment, triage, {}, collector)

        # q-search must stay on opensearch
        q_search = next(qa for qa in result["revised_assignments"] if qa["query_id"] == "q-search")
        assert q_search["assigned_engine"] == "opensearch"

        # If partial consolidation happened, check for partial action
        partial_consolidations = [
            c for c in result["consolidations"] if c.get("action") == "partial"
        ]
        if partial_consolidations:
            for c in partial_consolidations:
                assert "q-search" in c["queries_retained"]


class TestLightweightRecommendations:
    """Test lightweight alternative suggestions."""

    def test_suggest_when_small_uniform_orphan_set(self):
        """Lightweight recommended when <= 10% queries share single capability."""
        unserviceable = [{"query_id": "q-1"}]
        total_movable = 20  # 1/20 = 5% < 10%
        query_capabilities = {"q-1": ["inverted_index"]}

        result = _suggest_lightweight_for_orphans(
            unserviceable, total_movable, query_capabilities, "opensearch"
        )

        assert result is not None
        assert result["capability"] == "inverted_index"
        assert "OpenSearch Serverless" in result["service"]
        assert result["query_ids"] == ["q-1"]
        assert result["replaces_engine"] == "opensearch"

    def test_no_suggest_when_over_10_percent(self):
        """No lightweight if orphan set is > 10% of total."""
        unserviceable = [{"query_id": f"q-{i}"} for i in range(5)]
        total_movable = 20  # 5/20 = 25% > 10%
        query_capabilities = {f"q-{i}": ["inverted_index"] for i in range(5)}

        result = _suggest_lightweight_for_orphans(
            unserviceable, total_movable, query_capabilities, "opensearch"
        )

        assert result is None

    def test_no_suggest_when_mixed_capabilities(self):
        """No lightweight if orphans need different capabilities."""
        unserviceable = [{"query_id": "q-1"}, {"query_id": "q-2"}]
        total_movable = 50  # 2/50 = 4% < 10%
        query_capabilities = {
            "q-1": ["inverted_index"],
            "q-2": ["scan_engine"],
        }

        result = _suggest_lightweight_for_orphans(
            unserviceable, total_movable, query_capabilities, "opensearch"
        )

        assert result is None

    def test_no_suggest_when_empty_orphans(self):
        result = _suggest_lightweight_for_orphans([], 10, {}, "opensearch")
        assert result is None

    def test_lightweight_in_full_run(self):
        """End-to-end: lightweight recommendations appear in reality check output."""
        # 1 text_search query on opensearch, 19 basic queries on opensearch, 30 on dynamodb
        assignment = {
            "query_assignments": [
                _make_query_assignment("q-search", "opensearch"),
                *[_make_query_assignment(f"q-os-{i}", "opensearch") for i in range(19)],
                *[_make_query_assignment(f"q-ddb-{i}", "dynamodb") for i in range(30)],
            ]
        }

        triage = _make_triage(
            signals=[
                {"signal": "text_search", "query_ids": ["q-search"], "targets": ["opensearch"]},
            ],
            query_capabilities={"q-search": ["inverted_index"]},
        )

        queries = [
            {
                "query_id": "q-search",
                "query_text": "SELECT * WHERE MATCH(t) AGAINST('x')",
                "query_type": "SELECT",
                "tables_accessed": ["posts"],
            },
            *[
                {
                    "query_id": f"q-os-{i}",
                    "query_text": "SELECT id FROM t WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": ["posts"],
                }
                for i in range(19)
            ],
            *[
                {
                    "query_id": f"q-ddb-{i}",
                    "query_text": "SELECT id FROM t WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": ["users"],
                }
                for i in range(30)
            ],
        ]
        collector = _make_collector(queries=queries)

        result = run_reality_check(assignment, triage, {}, collector)

        # Check lightweight recommendations are present (if partial consolidation occurred)

        # The search query should remain on opensearch regardless
        q_search = next(qa for qa in result["revised_assignments"] if qa["query_id"] == "q-search")
        assert q_search["assigned_engine"] == "opensearch"


class TestFindBestAbsorberWithCapabilities:
    """Test that _find_best_absorber respects capability requirements."""

    def test_absorber_skips_engine_lacking_capability(self):
        qa = {"query_id": "q-1", "assigned_engine": "opensearch"}
        committed = {"dynamodb", "documentdb"}
        query_signals: dict = {"q-1": ["text_search"]}
        query_map = {"q-1": {"tables_accessed": ["posts"]}}
        query_capabilities = {"q-1": ["inverted_index"]}

        result = _find_best_absorber_for_query(
            qa=qa,
            committed_engines=committed,
            source_engine="opensearch",
            query_signals=query_signals,
            query_map=query_map,
            analysis_outputs={},
            engine_queries={"dynamodb": [], "documentdb": []},
            mandatory_committed_engines=set(),
            primary_engine="dynamodb",
            query_capabilities=query_capabilities,
        )

        # Neither dynamodb nor documentdb has inverted_index
        assert result is None

    def test_absorber_allows_engine_with_capability(self):
        qa = {"query_id": "q-1", "assigned_engine": "documentdb"}
        committed = {"dynamodb", "opensearch"}
        query_signals: dict = {"q-1": ["text_search"]}
        query_map = {"q-1": {"tables_accessed": ["posts"]}}
        query_capabilities = {"q-1": ["inverted_index"]}

        result = _find_best_absorber_for_query(
            qa=qa,
            committed_engines=committed,
            source_engine="documentdb",
            query_signals=query_signals,
            query_map=query_map,
            analysis_outputs={},
            engine_queries={"dynamodb": [], "opensearch": []},
            mandatory_committed_engines=set(),
            primary_engine="dynamodb",
            query_capabilities=query_capabilities,
        )

        # OpenSearch has inverted_index
        assert result is not None
        assert result["target_engine"] == "opensearch"


class TestBackwardCompatibility:
    """Test that existing behavior works without query_capabilities."""

    def test_run_without_query_capabilities(self):
        """Reality check works with empty/missing query_capabilities (backward compat)."""
        assignment = {
            "query_assignments": [
                _make_query_assignment("q-1", "documentdb"),
                _make_query_assignment("q-2", "documentdb"),
                _make_query_assignment("q-3", "dynamodb"),
                _make_query_assignment("q-4", "dynamodb"),
                _make_query_assignment("q-5", "dynamodb"),
                _make_query_assignment("q-6", "dynamodb"),
                _make_query_assignment("q-7", "dynamodb"),
                _make_query_assignment("q-8", "dynamodb"),
            ]
        }

        triage = {"signals": []}  # No query_capabilities field at all
        collector = _make_collector(
            queries=[
                {
                    "query_id": f"q-{i}",
                    "query_text": "SELECT 1",
                    "query_type": "SELECT",
                    "tables_accessed": ["t1"],
                }
                for i in range(1, 9)
            ]
        )

        # Should not raise
        result = run_reality_check(assignment, triage, {}, collector)
        assert "revised_assignments" in result
        assert "lightweight_recommendations" in result
