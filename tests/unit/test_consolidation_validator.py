"""Unit tests for the consolidation validator."""

import json

from src.agents.referee.consolidation_validator import (
    _build_validation_prompt,
    _parse_llm_response,
    apply_corrections,
)


class TestParseResponse:
    """Tests for _parse_llm_response()."""

    def test_parses_empty_array(self):
        result = _parse_llm_response("[]")
        assert result == []

    def test_parses_flagged_queries(self):
        response = json.dumps(
            [
                {"query_id": "q-1", "reason": "full-text search impossible"},
                {"query_id": "q-2", "reason": "complex join needs $lookup"},
            ]
        )
        result = _parse_llm_response(response)
        assert len(result) == 2
        assert result[0]["query_id"] == "q-1"
        assert result[1]["query_id"] == "q-2"

    def test_handles_markdown_code_fences(self):
        response = '```json\n[{"query_id": "q-1", "reason": "bad fit"}]\n```'
        result = _parse_llm_response(response)
        assert len(result) == 1
        assert result[0]["query_id"] == "q-1"

    def test_handles_invalid_json(self):
        result = _parse_llm_response("This is not JSON at all")
        assert result == []

    def test_handles_missing_query_id(self):
        response = json.dumps(
            [
                {"query_id": "q-1", "reason": "ok"},
                {"reason": "missing id"},  # No query_id
            ]
        )
        result = _parse_llm_response(response)
        assert len(result) == 1
        assert result[0]["query_id"] == "q-1"

    def test_handles_whitespace(self):
        result = _parse_llm_response("  \n  []  \n  ")
        assert result == []

    def test_adds_default_reason(self):
        response = json.dumps([{"query_id": "q-1"}])
        result = _parse_llm_response(response)
        assert result[0]["reason"] == "flagged"


class TestApplyCorrections:
    """Tests for apply_corrections()."""

    def test_no_corrections_returns_unchanged(self):
        assignments = [
            {"query_id": "q-1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
        ]
        consolidations = [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": 1,
                "reason": "test",
                "saved_cost_estimate": 500,
            },
        ]

        result_a, result_c = apply_corrections([], assignments, consolidations)
        assert result_a == assignments
        assert result_c == consolidations

    def test_moves_query_back_to_original_engine(self):
        assignments = [
            {
                "query_id": "q-1",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb",
            },
            {
                "query_id": "q-2",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb",
            },
        ]
        consolidations = [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": 2,
                "reason": "test",
                "saved_cost_estimate": 500,
            },
        ]

        corrections = [
            {"query_id": "q-1", "original_engine": "documentdb", "reason": "complex joins"},
        ]

        result_a, result_c = apply_corrections(corrections, assignments, consolidations)

        # q-1 should be back on documentdb
        q1 = next(qa for qa in result_a if qa["query_id"] == "q-1")
        assert q1["assigned_engine"] == "documentdb"
        assert "consolidation reversed" in q1["assignment_reason"]

        # q-2 should stay on dynamodb
        q2 = next(qa for qa in result_a if qa["query_id"] == "q-2")
        assert q2["assigned_engine"] == "dynamodb"

    def test_partial_reversal_updates_consolidation(self):
        assignments = [
            {
                "query_id": "q-1",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb",
            },
            {
                "query_id": "q-2",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb",
            },
            {
                "query_id": "q-3",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb",
            },
        ]
        consolidations = [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": 3,
                "reason": "test",
                "saved_cost_estimate": 500,
            },
        ]

        corrections = [
            {"query_id": "q-1", "original_engine": "documentdb", "reason": "bad fit"},
        ]

        _, result_c = apply_corrections(corrections, assignments, consolidations)

        assert len(result_c) == 1
        assert result_c[0]["query_count"] == 2  # 3 - 1 reversed
        assert result_c[0]["action"] == "partial"
        assert "q-1" in result_c[0]["queries_retained"]

    def test_full_reversal_removes_consolidation(self):
        assignments = [
            {
                "query_id": "q-1",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb",
            },
        ]
        consolidations = [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": 1,
                "reason": "test",
                "saved_cost_estimate": 500,
            },
        ]

        corrections = [
            {"query_id": "q-1", "original_engine": "documentdb", "reason": "bad fit"},
        ]

        _, result_c = apply_corrections(corrections, assignments, consolidations)

        # Consolidation should be removed entirely
        assert len(result_c) == 0


class TestBuildPrompt:
    """Tests for _build_validation_prompt()."""

    def test_includes_target_engine_context(self):
        prompt = _build_validation_prompt(
            from_engine="documentdb",
            to_engine="dynamodb",
            queries=[
                {
                    "query_id": "q-1",
                    "sql": "SELECT * FROM posts WHERE title LIKE '%test%'",
                    "type": "SELECT",
                    "tables": ["posts"],
                    "signals": ["text_search"],
                    "cps": 2.5,
                }
            ],
        )

        assert "DynamoDB" in prompt
        assert "key-value" in prompt
        assert "q-1" in prompt
        assert "text_search" in prompt

    def test_includes_query_details(self):
        prompt = _build_validation_prompt(
            from_engine="opensearch",
            to_engine="dynamodb",
            queries=[
                {
                    "query_id": "q-42",
                    "sql": "SELECT id FROM users WHERE id = ?",
                    "type": "SELECT",
                    "tables": ["users"],
                    "signals": [],
                    "cps": 10.0,
                }
            ],
        )

        assert "q-42" in prompt
        assert "users" in prompt
        assert "10.0 cps" in prompt
        assert "JSON" in prompt
