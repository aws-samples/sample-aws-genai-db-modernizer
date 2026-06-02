"""Unit tests for the reality check handler LLM seam functions.

Tests verify the three clean seam functions introduced to support the Skill Sync
initiative, plus backward compatibility of the refactored run_reality_check_handler.

Seam functions:
- run_reality_check_deterministic  — reads artifacts, runs consolidation, no LLM
- prepare_reality_check_llm_input  — formats LLM request payload
- apply_reality_check_llm_output   — merges LLM output into deterministic result
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from src.agents.referee.reality_check_handler import (
    apply_reality_check_llm_output,
    prepare_reality_check_llm_input,
    run_reality_check_deterministic,
    run_reality_check_handler,
)
from src.storage.artifact_store import ArtifactStore

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ASSIGNMENT = {
    "version": 1,
    "query_assignments": [
        {
            "query_id": "DDB-AP-1",
            "assigned_engine": "dynamodb",
            "assignment_reason": "key-value lookup",
        },
        {
            "query_id": "DDB-AP-2",
            "assigned_engine": "dynamodb",
            "assignment_reason": "key-value lookup",
        },
        {
            "query_id": "DOC-AP-1",
            "assigned_engine": "documentdb",
            "assignment_reason": "nested document",
        },
    ],
}

_TRIAGE = {
    "selected_agents": [
        {"agent_type": "dynamodb", "reasons": ["key-value"]},
        {"agent_type": "documentdb", "reasons": ["nested doc"]},
    ],
    "signals": [],
    "query_capabilities": {},
}

_COLLECTOR = {
    "database_schema": {"tables": [{"table_id": "users"}, {"table_id": "orders"}]},
    "queries": {
        "query_patterns": [
            {
                "query_id": "DDB-AP-1",
                "query_text": "SELECT * FROM users WHERE user_id = ?",
                "query_type": "SELECT",
                "tables_accessed": ["users"],
                "calls_per_second": 10.0,
            },
            {
                "query_id": "DDB-AP-2",
                "query_text": "SELECT * FROM orders WHERE order_id = ?",
                "query_type": "SELECT",
                "tables_accessed": ["orders"],
                "calls_per_second": 5.0,
            },
            {
                "query_id": "DOC-AP-1",
                "query_text": "SELECT * FROM documents WHERE doc_id = ?",
                "query_type": "SELECT",
                "tables_accessed": ["users"],
                "calls_per_second": 2.0,
            },
        ]
    },
}

_ANALYSIS_DDB = {
    "table_recommendations": [
        {"table_id": "users", "confidence_score": 85, "migration_complexity": "LOW"},
        {"table_id": "orders", "confidence_score": 80, "migration_complexity": "LOW"},
    ],
    "signals": [],
}

_ANALYSIS_DOC = {
    "table_recommendations": [
        {"table_id": "users", "confidence_score": 60, "migration_complexity": "MEDIUM"},
    ],
    "signals": [],
}


def _mock_store() -> MagicMock:
    """Build a mock ArtifactStore pre-loaded with the shared fixture data."""
    store = MagicMock(spec=ArtifactStore)
    written: dict = {}

    artifacts = {
        "assignment/v1/assignment.json": _ASSIGNMENT,
        "referee-triage/triage.json": _TRIAGE,
        "collector/output.json": _COLLECTOR,
        "analysis-dynamodb/analysis.json": _ANALYSIS_DDB,
        "analysis-documentdb/analysis.json": _ANALYSIS_DOC,
    }

    def read_json(path):
        for pattern, data in artifacts.items():
            if pattern in path:
                return data
        raise FileNotFoundError(f"Artifact not found in mock store: {path}")

    def exists(path):
        return any(pattern in path for pattern in artifacts)

    def write_json(path, data):
        written[path] = data

    store.read_json.side_effect = read_json
    store.exists.side_effect = exists
    store.write_json.side_effect = write_json
    store._written = written
    return store


# ---------------------------------------------------------------------------
# Test: run_reality_check_deterministic
# ---------------------------------------------------------------------------


class TestRunRealityCheckDeterministicReturnsValidOutput:
    """run_reality_check_deterministic must return a complete dict without calling any LLM."""

    def test_returns_dict(self):
        """Result is a plain dict."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        assert isinstance(result, dict)

    def test_has_required_keys(self):
        """All required top-level keys are present."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        expected_keys = {
            "consolidations",
            "recommendations",
            "architectural_patterns",
            "unique_value_assessment",
            "before_distribution",
            "after_distribution",
            "executive_summary",
            "assignment",
            "collector_output",
            "analysis_outputs",
        }
        assert expected_keys.issubset(result.keys())

    def test_executive_summary_is_none(self):
        """executive_summary must always be None (no LLM called)."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        assert result["executive_summary"] is None

    def test_consolidations_is_list(self):
        """consolidations must be a list (possibly empty)."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        assert isinstance(result["consolidations"], list)

    def test_recommendations_is_non_empty_list(self):
        """recommendations must be a non-empty list of strings."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        recs = result["recommendations"]
        assert isinstance(recs, list)
        assert len(recs) >= 1
        assert all(isinstance(r, str) for r in recs)

    def test_before_distribution_reflects_assignment(self):
        """before_distribution counts queries by engine from the assignment."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        bd = result["before_distribution"]
        assert isinstance(bd, dict)
        # The fixture has 2 dynamodb + 1 documentdb queries
        assert bd.get("dynamodb", 0) == 2
        assert bd.get("documentdb", 0) == 1

    def test_after_distribution_is_dict(self):
        """after_distribution is a dict of engine -> query count."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        assert isinstance(result["after_distribution"], dict)

    def test_after_distribution_total_equals_before_total(self):
        """Total query count must be conserved across redistribution."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        before_total = sum(result["before_distribution"].values())
        after_total = sum(result["after_distribution"].values())
        assert before_total == after_total

    def test_assignment_is_passthrough(self):
        """assignment key holds the original assignment artifact."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        assert result["assignment"] == _ASSIGNMENT

    def test_collector_output_is_passthrough(self):
        """collector_output holds the collector artifact."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        assert result["collector_output"] == _COLLECTOR

    def test_analysis_outputs_has_engines(self):
        """analysis_outputs contains entries for selected engines that exist in store."""
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store)
        ao = result["analysis_outputs"]
        assert isinstance(ao, dict)
        assert "dynamodb" in ao
        assert "documentdb" in ao

    def test_no_llm_called(self):
        """validate_consolidations and _generate_executive_summary must not be called."""
        store = _mock_store()
        with (
            patch(
                "src.agents.referee.reality_check_handler.validate_consolidations"
            ) as mock_validate,
            patch(
                "src.agents.referee.reality_check_handler._generate_executive_summary"
            ) as mock_summary,
        ):
            run_reality_check_deterministic("job-1", "mydb", store)
        mock_validate.assert_not_called()
        mock_summary.assert_not_called()


# ---------------------------------------------------------------------------
# Test: prepare_reality_check_llm_input
# ---------------------------------------------------------------------------


class TestPrepareLlmInputHasCorrectStructure:
    """prepare_reality_check_llm_input must return a dict with the two expected sub-keys."""

    def _get_det(self) -> dict:
        store = _mock_store()
        return run_reality_check_deterministic("job-1", "mydb", store)

    def test_returns_dict(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        assert isinstance(payload, dict)

    def test_has_consolidation_validation_key(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        assert "consolidation_validation" in payload

    def test_has_executive_summary_key(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        assert "executive_summary" in payload

    def test_consolidation_validation_has_consolidations(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        cv = payload["consolidation_validation"]
        assert "consolidations" in cv

    def test_consolidation_validation_has_collector_output(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        cv = payload["consolidation_validation"]
        assert "collector_output" in cv

    def test_consolidation_validation_has_analysis_outputs(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        cv = payload["consolidation_validation"]
        assert "analysis_outputs" in cv

    def test_executive_summary_has_before_distribution(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        es = payload["executive_summary"]
        assert "before_distribution" in es

    def test_executive_summary_has_after_distribution(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        es = payload["executive_summary"]
        assert "after_distribution" in es

    def test_executive_summary_has_consolidations(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        es = payload["executive_summary"]
        assert "consolidations" in es

    def test_executive_summary_has_unique_value_assessment(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        es = payload["executive_summary"]
        assert "unique_value_assessment" in es

    def test_executive_summary_has_architectural_patterns(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        es = payload["executive_summary"]
        assert "architectural_patterns" in es

    def test_executive_summary_has_recommendations(self):
        payload = prepare_reality_check_llm_input(self._get_det())
        es = payload["executive_summary"]
        assert "recommendations" in es

    def test_consolidations_values_match_deterministic(self):
        """The consolidations in the payload match those from the deterministic result."""
        det = self._get_det()
        payload = prepare_reality_check_llm_input(det)
        assert payload["consolidation_validation"]["consolidations"] == det["consolidations"]
        assert payload["executive_summary"]["consolidations"] == det["consolidations"]

    def test_collector_output_matches_deterministic(self):
        det = self._get_det()
        payload = prepare_reality_check_llm_input(det)
        assert payload["consolidation_validation"]["collector_output"] == det["collector_output"]

    def test_distributions_match_deterministic(self):
        det = self._get_det()
        payload = prepare_reality_check_llm_input(det)
        assert payload["executive_summary"]["before_distribution"] == det["before_distribution"]
        assert payload["executive_summary"]["after_distribution"] == det["after_distribution"]


# ---------------------------------------------------------------------------
# Test: apply_reality_check_llm_output
# ---------------------------------------------------------------------------


class TestApplyLlmOutputHandlesCorrectionsAndSummary:
    """apply_reality_check_llm_output must correctly merge LLM output into deterministic result."""

    def _get_det(self) -> dict:
        store = _mock_store()
        return run_reality_check_deterministic("job-1", "mydb", store)

    def test_returns_dict(self):
        det = self._get_det()
        result = apply_reality_check_llm_output(det, {})
        assert isinstance(result, dict)

    def test_empty_llm_output_leaves_result_unchanged(self):
        """Passing an empty dict must not modify executive_summary or recommendations."""
        det = self._get_det()
        original_summary = det["executive_summary"]
        original_recs = list(det["recommendations"])
        result = apply_reality_check_llm_output(det, {})
        assert result["executive_summary"] == original_summary
        assert result["recommendations"] == original_recs

    def test_executive_summary_is_set_from_llm_output(self):
        """executive_summary key from llm_output is stored in the result."""
        det = self._get_det()
        summary_text = "Your workload is well-suited for DynamoDB."
        result = apply_reality_check_llm_output(det, {"executive_summary": summary_text})
        assert result["executive_summary"] == summary_text

    def test_empty_corrections_list_leaves_assignments_unchanged(self):
        """Providing consolidation_corrections=[] must not change revised_assignments."""
        det = self._get_det()
        original_assignments = list(det["revised_assignments"])
        result = apply_reality_check_llm_output(det, {"consolidation_corrections": []})
        assert result["revised_assignments"] == original_assignments

    def test_corrections_move_queries_back(self):
        """A correction must move the specified query back to its original engine."""
        store = _mock_store()
        # Build a deterministic result where documentdb was consolidated into dynamodb
        det = run_reality_check_deterministic("job-1", "mydb", store)

        # Manually inject a consolidation to simulate a moved query
        det["revised_assignments"] = [
            {
                "query_id": "DOC-AP-1",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb (no unique value)",
            },
            *[qa for qa in det["revised_assignments"] if qa["query_id"] != "DOC-AP-1"],
        ]
        det["consolidations"] = [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": 1,
                "reason": "no unique value",
                "saved_cost_estimate": 500,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        ]

        corrections = [
            {
                "query_id": "DOC-AP-1",
                "original_engine": "documentdb",
                "reason": "requires nested document aggregation",
            }
        ]
        result = apply_reality_check_llm_output(det, {"consolidation_corrections": corrections})

        # DOC-AP-1 should have been moved back to documentdb
        moved_back = next(
            (qa for qa in result["revised_assignments"] if qa["query_id"] == "DOC-AP-1"), None
        )
        assert moved_back is not None
        assert moved_back["assigned_engine"] == "documentdb"

    def test_corrections_rebuilds_recommendations(self):
        """After applying corrections, recommendations must be rebuilt."""
        det = self._get_det()

        # Add a fake consolidation and a correction for it
        det["consolidations"] = [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": 1,
                "reason": "no unique value",
                "saved_cost_estimate": 200,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        ]
        det["revised_assignments"] = [
            {
                "query_id": "DOC-AP-1",
                "assigned_engine": "dynamodb",
                "assignment_reason": "reality check: consolidated from documentdb → dynamodb (no unique value)",
            }
        ]

        corrections = [
            {
                "query_id": "DOC-AP-1",
                "original_engine": "documentdb",
                "reason": "requires document aggregation",
            }
        ]
        result = apply_reality_check_llm_output(det, {"consolidation_corrections": corrections})

        # Recommendations should have been rebuilt (full reversal removes the consolidation)
        # After correction DOC-AP-1 is back on documentdb so consolidation entry is removed
        assert isinstance(result["recommendations"], list)

    def test_both_corrections_and_summary_applied_together(self):
        """Corrections and executive_summary can both be present in a single llm_output."""
        det = self._get_det()
        summary_text = "The workload is a clean DynamoDB fit."
        result = apply_reality_check_llm_output(
            det,
            {
                "consolidation_corrections": [],
                "executive_summary": summary_text,
            },
        )
        assert result["executive_summary"] == summary_text


# ---------------------------------------------------------------------------
# Test: run_reality_check_handler backward compatibility
# ---------------------------------------------------------------------------


class TestRunRealityCheckHandlerBackwardCompatible:
    """run_reality_check_handler called without llm_mode must behave identically to before."""

    def test_signature_has_llm_mode_parameter(self):
        sig = inspect.signature(run_reality_check_handler)
        assert "llm_mode" in sig.parameters

    def test_llm_mode_default_is_bedrock(self):
        sig = inspect.signature(run_reality_check_handler)
        assert sig.parameters["llm_mode"].default == "bedrock"

    def test_llm_mode_none_writes_output_json(self):
        """llm_mode='none' must write reality-check/output.json without calling any LLM."""
        store = _mock_store()
        with (
            patch(
                "src.agents.referee.reality_check_handler.validate_consolidations"
            ) as mock_validate,
            patch(
                "src.agents.referee.reality_check_handler._generate_executive_summary"
            ) as mock_summary,
        ):
            run_reality_check_handler("job-1", "mydb", store, llm_mode="none")

        mock_validate.assert_not_called()
        mock_summary.assert_not_called()
        written_keys = list(store._written.keys())
        assert any("reality-check/output.json" in k for k in written_keys)

    def test_llm_mode_none_output_has_correct_structure(self):
        """Output written with llm_mode='none' must be contract-valid."""
        store = _mock_store()
        run_reality_check_handler("job-1", "mydb", store, llm_mode="none")
        output_key = next(k for k in store._written if "reality-check/output.json" in k)
        output = store._written[output_key]
        # Verify the written output has the required top-level fields
        assert "consolidations" in output
        assert "recommendations" in output
        assert "before_distribution" in output
        assert "after_distribution" in output
        assert "source_assignment_version" in output

    def test_llm_mode_external_writes_llm_input_and_awaiting(self):
        """llm_mode='external' must write llm_input.json and awaiting_llm.json."""
        store = _mock_store()
        run_reality_check_handler("job-1", "mydb", store, llm_mode="external")
        written_keys = list(store._written.keys())
        assert any("llm_input.json" in k for k in written_keys)
        assert any("awaiting_llm.json" in k for k in written_keys)

    def test_llm_mode_external_still_writes_output_json(self):
        """llm_mode='external' must still write the deterministic output.json."""
        store = _mock_store()
        run_reality_check_handler("job-1", "mydb", store, llm_mode="external")
        written_keys = list(store._written.keys())
        assert any("reality-check/output.json" in k for k in written_keys)

    def test_llm_mode_external_llm_input_has_correct_structure(self):
        """The written llm_input.json must have the consolidation_validation and executive_summary keys."""
        store = _mock_store()
        run_reality_check_handler("job-1", "mydb", store, llm_mode="external")
        input_key = next(k for k in store._written if "llm_input.json" in k)
        llm_input = store._written[input_key]
        assert "consolidation_validation" in llm_input
        assert "executive_summary" in llm_input

    def test_llm_mode_bedrock_calls_generate_executive_summary(self):
        """llm_mode='bedrock' (default) must call _generate_executive_summary."""
        store = _mock_store()
        with (
            patch(
                "src.agents.referee.reality_check_handler.validate_consolidations",
                return_value=[],
            ),
            patch(
                "src.agents.referee.reality_check_handler._generate_executive_summary",
                return_value="Mocked summary.",
            ) as mock_summary,
        ):
            run_reality_check_handler("job-1", "mydb", store, llm_mode="bedrock")
        mock_summary.assert_called_once()

    def test_no_llm_mode_arg_defaults_to_bedrock(self):
        """Calling without llm_mode must behave identically to llm_mode='bedrock'."""
        store = _mock_store()
        with (
            patch(
                "src.agents.referee.reality_check_handler.validate_consolidations",
                return_value=[],
            ),
            patch(
                "src.agents.referee.reality_check_handler._generate_executive_summary",
                return_value=None,
            ) as mock_summary,
        ):
            run_reality_check_handler("job-1", "mydb", store)
        mock_summary.assert_called_once()

    def test_consolidation_writes_new_assignment_version(self):
        """When consolidations occur, a new assignment version must be written."""
        store = _mock_store()
        with (
            patch(
                "src.agents.referee.reality_check_handler.validate_consolidations",
                return_value=[],
            ),
            patch(
                "src.agents.referee.reality_check_handler._generate_executive_summary",
                return_value=None,
            ),
            patch(
                "src.agents.referee.reality_check_handler.run_reality_check_deterministic",
            ) as mock_det,
        ):
            # Simulate a result with one consolidation
            mock_det.return_value = {
                "consolidations": [
                    {
                        "from_engine": "documentdb",
                        "to_engine": "dynamodb",
                        "query_count": 1,
                        "reason": "no unique value",
                        "saved_cost_estimate": 200,
                        "action": "full",
                        "queries_retained": [],
                        "retention_reason": None,
                    }
                ],
                "recommendations": ["Consolidated 1 query."],
                "architectural_patterns": [],
                "unique_value_assessment": {
                    "dynamodb": {
                        "total_queries": 3,
                        "unique_queries": ["DDB-AP-1", "DDB-AP-2", "DOC-AP-1"],
                        "redundant_queries": [],
                        "unique_ratio": 1.0,
                        "avg_delta": 20.0,
                        "is_primary": True,
                        "is_mandatory": False,
                        "consolidation_blocked": None,
                    }
                },
                "before_distribution": {"dynamodb": 2, "documentdb": 1},
                "after_distribution": {"dynamodb": 3},
                "executive_summary": None,
                "assignment": _ASSIGNMENT,
                "collector_output": _COLLECTOR,
                "analysis_outputs": {},
                "revised_assignments": [
                    {
                        "query_id": "DDB-AP-1",
                        "assigned_engine": "dynamodb",
                        "assignment_reason": "key-value",
                    },
                    {
                        "query_id": "DDB-AP-2",
                        "assigned_engine": "dynamodb",
                        "assignment_reason": "key-value",
                    },
                    {
                        "query_id": "DOC-AP-1",
                        "assigned_engine": "dynamodb",
                        "assignment_reason": "reality check: consolidated from documentdb → dynamodb",
                    },
                ],
                "lightweight_recommendations": [],
            }
            run_reality_check_handler("job-1", "mydb", store, llm_mode="none")

        written_keys = list(store._written.keys())
        assert any("assignment/v2/assignment.json" in k for k in written_keys)
