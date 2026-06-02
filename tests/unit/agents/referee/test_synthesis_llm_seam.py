"""Unit tests for the synthesis handler LLM seam functions.

Tests verify the three clean seam functions introduced to support the Skill Sync
initiative, plus backward compatibility of the refactored run_synthesis.

Seam functions:
- run_synthesis_deterministic — reads all artifacts, runs all builders, no LLM
- prepare_synthesis_llm_input — formats LLM request payload
- apply_synthesis_llm_output  — merges LLM output into deterministic result
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from src.agents.referee.synthesis_handler import (
    apply_synthesis_llm_output,
    prepare_synthesis_llm_input,
    run_synthesis,
    run_synthesis_deterministic,
)
from src.storage.artifact_store import ArtifactStore

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_TRIAGE = {
    "selected_agents": [
        {"agent_type": "dynamodb", "reasons": ["key-value"]},
    ],
    "signals": [],
    "query_capabilities": {},
}

_COLLECTOR = {
    "database_schema": {
        "tables": [
            {"table_id": "users"},
            {"table_id": "orders"},
        ]
    },
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
        ]
    },
}

_ANALYSIS_DDB = {
    "table_recommendations": [
        {"table_id": "users", "confidence_score": 85, "migration_complexity": "LOW"},
        {"table_id": "orders", "confidence_score": 78, "migration_complexity": "LOW"},
    ],
    "workload_analysis": {
        "patterns_detected": ["key-value lookup"],
        "anti_patterns_detected": [],
    },
    "cost_estimate": {
        "monthly_cost_usd": 120.0,
        "cost_components": {"pricing_mode": "on-demand"},
    },
    "aggregate_recommendations": [],
    "signals": [],
}

_SCHEMA_DDB = {
    "table_definitions": [
        {
            "table_name": "Users",
            "source_tables": ["users"],
            "aggregate_pattern": "single-table",
            "gsis": [],
            "item_count": 50000,
            "item_size_bytes": 512,
        },
        {
            "table_name": "Orders",
            "source_tables": ["orders"],
            "aggregate_pattern": "separate",
            "gsis": [{"index_name": "order-gsi"}],
            "item_count": 200000,
            "item_size_bytes": 256,
        },
    ],
    "access_patterns": [
        {
            "pattern_id": "DDB-AP-1",
            "pattern_group": "user-reads",
            "operation": "GetItem",
            "table_name": "Users",
            "key_condition": "PK = user_id",
            "design_rps": 100,
            "in_scope": True,
            "query_ids": ["DDB-AP-1"],
        },
        {
            "pattern_id": "DDB-AP-2",
            "pattern_group": "order-reads",
            "operation": "GetItem",
            "table_name": "Orders",
            "key_condition": "PK = order_id",
            "design_rps": 50,
            "in_scope": True,
            "query_ids": ["DDB-AP-2"],
        },
    ],
    "trade_offs": [
        {
            "description": "No JOINs across tables",
            "impact": "Application-level joins required",
            "source_tables": ["users", "orders"],
            "target_tables": ["Users"],
            "query_ids": [],
            "engine": "dynamodb",
        }
    ],
    "unsupported_patterns": [],
    "migration_notes": [],
    "validation_passed": True,
}


def _mock_store(with_reality_check: bool = False) -> MagicMock:
    """Build a mock ArtifactStore pre-loaded with the shared fixture data."""
    store = MagicMock(spec=ArtifactStore)
    written: dict = {}

    artifacts: dict[str, dict] = {
        "referee-triage/triage.json": _TRIAGE,
        "collector/output.json": _COLLECTOR,
        "analysis-dynamodb/analysis.json": _ANALYSIS_DDB,
        "schema-dynamodb/schema_output.json": _SCHEMA_DDB,
    }

    if with_reality_check:
        artifacts["reality-check/output.json"] = {
            "consolidations": [],
            "architectural_patterns": ["key-value dominance"],
            "recommendations": ["Consider DynamoDB global tables for multi-region."],
            "before_distribution": {"dynamodb": 2},
            "after_distribution": {"dynamodb": 2},
        }

    def read_json(path: str) -> dict:
        for pattern, data in artifacts.items():
            if pattern in path:
                return data
        raise FileNotFoundError(f"Artifact not found in mock store: {path}")

    def exists(path: str) -> bool:
        return any(pattern in path for pattern in artifacts)

    def write_json(path: str, data: dict) -> None:
        written[path] = data

    store.read_json.side_effect = read_json
    store.exists.side_effect = exists
    store.write_json.side_effect = write_json
    store._written = written
    return store


# ---------------------------------------------------------------------------
# Test: run_synthesis_deterministic
# ---------------------------------------------------------------------------


class TestRunSynthesisDeterministicProducesFullReport:
    """run_synthesis_deterministic must return a complete report dict without calling any LLM."""

    def test_returns_dict(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert isinstance(result, dict)

    def test_has_required_keys(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        required = {
            "job_id",
            "database_name",
            "timestamp",
            "needs_deeper_analysis",
            "ranking",
            "table_mappings",
            "query_groups",
            "tco_analysis",
            "risk_assessment",
            "architecture",
            "trade_offs",
            "assignment_summary",
            "reality_check_summary",
            "summary",
            "executive_summary",
            "data",
        }
        assert required.issubset(result.keys())

    def test_job_id_preserved(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-99", "mydb", store)
        assert result["job_id"] == "job-99"

    def test_database_name_preserved(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "testdb", store)
        assert result["database_name"] == "testdb"

    def test_ranking_is_non_empty_list(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert isinstance(result["ranking"], list)
        assert len(result["ranking"]) >= 1

    def test_ranking_entry_has_target(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert result["ranking"][0]["target"] == "dynamodb"

    def test_table_mappings_is_list(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert isinstance(result["table_mappings"], list)

    def test_table_mappings_non_empty(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert len(result["table_mappings"]) >= 1

    def test_query_groups_is_list(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert isinstance(result["query_groups"], list)

    def test_tco_analysis_has_projected_cost(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert "projected_monthly_cost" in result["tco_analysis"]

    def test_risk_assessment_has_overall_level(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert "overall_risk_level" in result["risk_assessment"]
        assert "risks" in result["risk_assessment"]

    def test_architecture_has_type(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert "architecture_type" in result["architecture"]

    def test_summary_is_non_empty_string(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert isinstance(result["summary"], str)
        assert len(result["summary"]) > 0

    def test_executive_summary_equals_deterministic_summary(self):
        """executive_summary must default to the deterministic summary (no LLM)."""
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert result["executive_summary"] == result["summary"]

    def test_trade_offs_is_list(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert isinstance(result["trade_offs"], list)

    def test_reality_check_summary_none_when_absent(self):
        store = _mock_store(with_reality_check=False)
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert result["reality_check_summary"] is None

    def test_reality_check_summary_populated_when_present(self):
        store = _mock_store(with_reality_check=True)
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert result["reality_check_summary"] is not None
        assert "consolidations" in result["reality_check_summary"]

    def test_data_key_holds_synthesis_data(self):
        """The 'data' key must hold the SynthesisData object for downstream use."""
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        from src.agents.referee.synthesis_data import SynthesisData

        assert isinstance(result["data"], SynthesisData)

    def test_no_llm_called(self):
        """generate_executive_summary must not be called by run_synthesis_deterministic."""
        store = _mock_store()
        with patch("src.agents.referee.synthesis_handler.generate_executive_summary") as mock_gen:
            run_synthesis_deterministic("job-1", "mydb", store)
        mock_gen.assert_not_called()

    def test_assignment_summary_none_without_assignment(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        # No assignment_version > 0 so data.assignment is None
        assert result["assignment_summary"] is None

    def test_needs_deeper_analysis_is_bool(self):
        store = _mock_store()
        result = run_synthesis_deterministic("job-1", "mydb", store)
        assert isinstance(result["needs_deeper_analysis"], bool)


# ---------------------------------------------------------------------------
# Test: prepare_synthesis_llm_input
# ---------------------------------------------------------------------------


class TestPrepareSynthesisLlmInputHasCorrectKeys:
    """prepare_synthesis_llm_input must return a dict with all expected keys."""

    def _det(self) -> dict:
        return run_synthesis_deterministic("job-1", "mydb", _mock_store())

    def test_returns_dict(self):
        payload = prepare_synthesis_llm_input(self._det())
        assert isinstance(payload, dict)

    def test_has_deterministic_summary(self):
        assert "deterministic_summary" in prepare_synthesis_llm_input(self._det())

    def test_has_ranking(self):
        assert "ranking" in prepare_synthesis_llm_input(self._det())

    def test_has_query_groups(self):
        assert "query_groups" in prepare_synthesis_llm_input(self._det())

    def test_has_tco_analysis(self):
        assert "tco_analysis" in prepare_synthesis_llm_input(self._det())

    def test_has_risk_assessment(self):
        assert "risk_assessment" in prepare_synthesis_llm_input(self._det())

    def test_has_table_mappings(self):
        assert "table_mappings" in prepare_synthesis_llm_input(self._det())

    def test_has_trade_offs(self):
        assert "trade_offs" in prepare_synthesis_llm_input(self._det())

    def test_exactly_seven_keys(self):
        payload = prepare_synthesis_llm_input(self._det())
        assert len(payload) == 7

    def test_deterministic_summary_matches_result(self):
        det = self._det()
        payload = prepare_synthesis_llm_input(det)
        assert payload["deterministic_summary"] == det["summary"]

    def test_ranking_matches_result(self):
        det = self._det()
        payload = prepare_synthesis_llm_input(det)
        assert payload["ranking"] is det["ranking"]

    def test_trade_offs_matches_result(self):
        det = self._det()
        payload = prepare_synthesis_llm_input(det)
        assert payload["trade_offs"] is det["trade_offs"]

    def test_no_internal_data_key_in_payload(self):
        """The internal 'data' key must not be exposed in the LLM payload."""
        payload = prepare_synthesis_llm_input(self._det())
        assert "data" not in payload


# ---------------------------------------------------------------------------
# Test: apply_synthesis_llm_output
# ---------------------------------------------------------------------------


class TestApplySynthesisLlmOutput:
    """apply_synthesis_llm_output must correctly merge LLM output into the result."""

    def _det(self) -> dict:
        return run_synthesis_deterministic("job-1", "mydb", _mock_store())

    def test_returns_dict(self):
        assert isinstance(apply_synthesis_llm_output(self._det(), {}), dict)

    def test_empty_llm_output_leaves_executive_summary_unchanged(self):
        det = self._det()
        original = det["executive_summary"]
        result = apply_synthesis_llm_output(det, {})
        assert result["executive_summary"] == original

    def test_executive_summary_set_from_llm_output(self):
        det = self._det()
        new_summary = "DynamoDB is the clear fit for this workload."
        result = apply_synthesis_llm_output(det, {"executive_summary": new_summary})
        assert result["executive_summary"] == new_summary

    def test_executive_summary_replaces_deterministic_fallback(self):
        det = self._det()
        original_summary = det["summary"]
        llm_summary = "Our analysis points squarely at DynamoDB."
        result = apply_synthesis_llm_output(det, {"executive_summary": llm_summary})
        # summary (deterministic) is unchanged; only executive_summary changes
        assert result["summary"] == original_summary
        assert result["executive_summary"] == llm_summary

    def test_other_keys_not_modified(self):
        det = self._det()
        original_ranking = det["ranking"]
        apply_synthesis_llm_output(det, {"executive_summary": "New summary."})
        assert det["ranking"] is original_ranking

    def test_mutates_and_returns_same_dict(self):
        det = self._det()
        returned = apply_synthesis_llm_output(det, {"executive_summary": "x"})
        assert returned is det

    def test_unknown_llm_output_keys_are_ignored(self):
        det = self._det()
        original = det["executive_summary"]
        # unexpected key should not raise and should not change executive_summary
        result = apply_synthesis_llm_output(det, {"unknown_key": "something"})
        assert result["executive_summary"] == original


# ---------------------------------------------------------------------------
# Test: run_synthesis backward compatibility
# ---------------------------------------------------------------------------


class TestRunSynthesisBackwardCompatible:
    """run_synthesis called without llm_mode must behave identically to before."""

    def test_signature_has_llm_mode_parameter(self):
        sig = inspect.signature(run_synthesis)
        assert "llm_mode" in sig.parameters

    def test_llm_mode_default_is_bedrock(self):
        sig = inspect.signature(run_synthesis)
        assert sig.parameters["llm_mode"].default == "bedrock"

    def test_assignment_version_default_is_zero(self):
        sig = inspect.signature(run_synthesis)
        assert sig.parameters["assignment_version"].default == 0

    def test_llm_mode_none_writes_report_without_llm(self):
        """llm_mode='none' must write the report without calling any LLM."""
        store = _mock_store()
        with patch("src.agents.referee.synthesis_handler.generate_executive_summary") as mock_gen:
            run_synthesis("job-1", "mydb", store, llm_mode="none")
        mock_gen.assert_not_called()
        written_keys = list(store._written.keys())
        assert any("referee-synthesis/report.json" in k for k in written_keys)

    def test_llm_mode_none_output_has_correct_structure(self):
        """Output written with llm_mode='none' must be contract-valid."""
        store = _mock_store()
        run_synthesis("job-1", "mydb", store, llm_mode="none")
        output_key = next(k for k in store._written if "report.json" in k)
        output = store._written[output_key]
        for key in (
            "ranking",
            "summary",
            "summary_deterministic",
            "recommended_architecture",
            "table_mappings",
            "query_groups",
            "tco_analysis",
            "risk_assessment",
        ):
            assert key in output, f"Missing key in report: {key}"

    def test_llm_mode_none_summary_equals_deterministic(self):
        """With llm_mode='none' the written summary must equal the deterministic summary."""
        store = _mock_store()
        run_synthesis("job-1", "mydb", store, llm_mode="none")
        output_key = next(k for k in store._written if "report.json" in k)
        output = store._written[output_key]
        assert output["summary"] == output["summary_deterministic"]

    def test_llm_mode_external_writes_llm_input(self):
        """llm_mode='external' must write llm_input.json."""
        store = _mock_store()
        run_synthesis("job-1", "mydb", store, llm_mode="external")
        written_keys = list(store._written.keys())
        assert any("llm_input.json" in k for k in written_keys)

    def test_llm_mode_external_still_writes_report(self):
        """llm_mode='external' must still write the deterministic report.json."""
        store = _mock_store()
        run_synthesis("job-1", "mydb", store, llm_mode="external")
        written_keys = list(store._written.keys())
        assert any("report.json" in k for k in written_keys)

    def test_llm_mode_external_llm_input_has_correct_keys(self):
        """The written llm_input.json must have all seven payload keys."""
        store = _mock_store()
        run_synthesis("job-1", "mydb", store, llm_mode="external")
        input_key = next(k for k in store._written if "llm_input.json" in k)
        llm_input = store._written[input_key]
        for key in (
            "deterministic_summary",
            "ranking",
            "query_groups",
            "tco_analysis",
            "risk_assessment",
            "table_mappings",
            "trade_offs",
        ):
            assert key in llm_input, f"Missing key in llm_input: {key}"

    def test_llm_mode_bedrock_calls_generate_executive_summary(self):
        """llm_mode='bedrock' (default) must call generate_executive_summary."""
        store = _mock_store()
        with patch(
            "src.agents.referee.synthesis_handler.generate_executive_summary",
            return_value="Mocked executive summary.",
        ) as mock_gen:
            run_synthesis("job-1", "mydb", store, llm_mode="bedrock")
        mock_gen.assert_called_once()

    def test_no_llm_mode_arg_defaults_to_bedrock(self):
        """Calling without llm_mode must use generate_executive_summary."""
        store = _mock_store()
        with patch(
            "src.agents.referee.synthesis_handler.generate_executive_summary",
            return_value="Summary from bedrock.",
        ) as mock_gen:
            run_synthesis("job-1", "mydb", store)
        mock_gen.assert_called_once()

    def test_llm_mode_bedrock_summary_set_in_report(self):
        """The report written with bedrock mode uses the LLM-generated summary."""
        store = _mock_store()
        with patch(
            "src.agents.referee.synthesis_handler.generate_executive_summary",
            return_value="CTO-ready summary from LLM.",
        ):
            run_synthesis("job-1", "mydb", store, llm_mode="bedrock")
        output_key = next(k for k in store._written if "report.json" in k)
        output = store._written[output_key]
        assert output["summary"] == "CTO-ready summary from LLM."

    def test_versioned_report_key_with_assignment_version(self):
        """When assignment_version > 0 the report is written under synthesis/vN/."""
        store = _mock_store()
        # Need to add versioned schema/assignment paths to the mock
        original_read = store.read_json.side_effect
        original_exists = store.exists.side_effect

        versioned_artifacts: dict[str, dict] = {
            "assignment/v2/assignment.json": {
                "version": 2,
                "status": "assigned",
                "query_assignments": [],
                "co_dependency_groups": [],
                "table_assignments": [],
            },
            "schema-dynamodb/v2/schema_output.json": _SCHEMA_DDB,
        }

        def versioned_read(path: str):  # type: ignore[no-untyped-def]
            for pattern, data in versioned_artifacts.items():
                if pattern in path:
                    return data
            return original_read(path)

        def versioned_exists(path: str):  # type: ignore[no-untyped-def]
            for pattern in versioned_artifacts:
                if pattern in path:
                    return True
            return original_exists(path)

        store.read_json.side_effect = versioned_read
        store.exists.side_effect = versioned_exists

        run_synthesis("job-1", "mydb", store, assignment_version=2, llm_mode="none")
        written_keys = list(store._written.keys())
        assert any("synthesis/v2/report.json" in k for k in written_keys)
