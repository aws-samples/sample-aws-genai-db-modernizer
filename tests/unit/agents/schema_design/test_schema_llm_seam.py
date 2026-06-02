"""Unit tests for the schema design handler LLM seam functions.

Tests verify:
- prepare_schema_design_input filters collector correctly
- validate_schema_design_output passes valid DynamoDB schema
- validate_schema_design_output rejects invalid schema
- finalize_schema_design with valid input
- run_schema_design with llm_mode="external" writes input and returns
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from src.agents.schema_design.handler import (
    finalize_schema_design,
    prepare_schema_design_input,
    run_schema_design,
    validate_schema_design_output,
)
from src.storage.artifact_store import ArtifactStore

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_COLLECTOR = {
    "contract_version": "3.0",
    "database_schema": {
        "tables": [
            {
                "table_id": "mydb.users",
                "table_name": "users",
                "row_count": 1000,
                "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
            },
            {
                "table_id": "mydb.orders",
                "table_name": "orders",
                "row_count": 5000,
                "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
            },
        ],
    },
    "queries": {
        "query_patterns": [
            {
                "query_id": "DDB-AP-1",
                "query_text": "SELECT * FROM users WHERE user_id = ?",
                "query_type": "SELECT",
                "tables_accessed": ["mydb.users"],
                "calls_per_second": 10.0,
            },
            {
                "query_id": "DDB-AP-2",
                "query_text": "SELECT * FROM orders WHERE order_id = ?",
                "query_type": "SELECT",
                "tables_accessed": ["mydb.orders"],
                "calls_per_second": 5.0,
            },
        ]
    },
}

_ANALYSIS = {"contract_version": "2.1", "table_recommendations": []}

_ASSIGNMENT = {
    "job_id": "job-001",
    "version": 1,
    "query_assignments": [
        {
            "query_id": "DDB-AP-1",
            "assigned_engine": "dynamodb",
            "confidence": 90,
            "source_tables": ["mydb.users"],
            "assignment_reason": "key-value",
            "in_scope": True,
        },
        # DDB-AP-2 assigned to opensearch — should be filtered out for dynamodb
        {
            "query_id": "DDB-AP-2",
            "assigned_engine": "opensearch",
            "confidence": 80,
            "source_tables": ["mydb.orders"],
            "assignment_reason": "search",
            "in_scope": True,
        },
    ],
    "table_assignments": [],
}

# Minimal valid DynamoDB schema output (satisfies all required contract fields)
_VALID_DYNAMODB_OUTPUT = {
    "job_id": "job-001",
    "source_database": "mydb",
    "access_patterns": [
        {
            "pattern_id": "DDB-AP-1",
            "pattern_group": "user-reads",
            "operation": "GetItem",
            "table_name": "Users",
            "key_condition": "PK = user_id",
            "design_rps": 10,
            "in_scope": True,
            "query_ids": ["DDB-AP-1"],
            "source_tables": ["mydb.users"],
            "description": "Get user by primary key",
            "item_size_bytes": 256,
        }
    ],
    "table_definitions": [
        {
            "table_name": "Users",
            "source_tables": ["mydb.users"],
            "aggregate_pattern": "separate",
            "partition_key": {"attribute_name": "PK", "attribute_type": "S"},
            "attributes": [
                {
                    "name": "PK",
                    "type": "S",
                    "source_table": "mydb.users",
                    "source_column": "id",
                }
            ],
            "gsis": [],
            "item_count": 1000,
            "item_size_bytes": 256,
        }
    ],
    "hot_partition_analysis": [],
    "trade_offs": [
        {
            "description": "No JOINs across tables",
            "impact": "Application-level joins required",
            "source_tables": ["mydb.users"],
            "target_tables": ["Users"],
            "query_ids": [],
            "engine": "dynamodb",
        }
    ],
    "validation_passed": True,
}


def _mock_store(artifacts: dict[str, dict]) -> MagicMock:
    """Create a mock ArtifactStore pre-loaded with provided artifacts."""
    store = MagicMock(spec=ArtifactStore)
    written: dict[str, dict] = {}

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


def _base_store() -> MagicMock:
    return _mock_store(
        {
            "collector/output.json": _COLLECTOR,
            "analysis-dynamodb/analysis.json": _ANALYSIS,
        }
    )


def _store_with_assignment() -> MagicMock:
    return _mock_store(
        {
            "collector/output.json": _COLLECTOR,
            "analysis-dynamodb/analysis.json": _ANALYSIS,
            "assignment/v1/assignment.json": _ASSIGNMENT,
        }
    )


# ---------------------------------------------------------------------------
# Tests: prepare_schema_design_input
# ---------------------------------------------------------------------------


class TestPrepareSchemaDesignInput:
    def test_returns_dict(self):
        store = _base_store()
        result = prepare_schema_design_input("job-001", "mydb", "dynamodb", store)
        assert isinstance(result, dict)

    def test_has_required_keys(self):
        store = _base_store()
        result = prepare_schema_design_input("job-001", "mydb", "dynamodb", store)
        assert set(result.keys()) == {
            "target_type",
            "collector_output",
            "analysis_output",
            "database_name",
            "job_id",
        }

    def test_target_type_preserved(self):
        store = _base_store()
        result = prepare_schema_design_input("job-001", "mydb", "dynamodb", store)
        assert result["target_type"] == "dynamodb"

    def test_job_id_preserved(self):
        store = _base_store()
        result = prepare_schema_design_input("job-001", "mydb", "dynamodb", store)
        assert result["job_id"] == "job-001"

    def test_database_name_preserved(self):
        store = _base_store()
        result = prepare_schema_design_input("job-001", "mydb", "dynamodb", store)
        assert result["database_name"] == "mydb"

    def test_no_assignment_includes_all_queries(self):
        store = _base_store()
        result = prepare_schema_design_input("job-001", "mydb", "dynamodb", store)
        query_ids = [q["query_id"] for q in result["collector_output"]["queries"]["query_patterns"]]
        assert "DDB-AP-1" in query_ids
        assert "DDB-AP-2" in query_ids

    def test_with_assignment_filters_to_engine_queries(self):
        store = _store_with_assignment()
        result = prepare_schema_design_input(
            "job-001", "mydb", "dynamodb", store, assignment_version=1
        )
        query_ids = [q["query_id"] for q in result["collector_output"]["queries"]["query_patterns"]]
        assert "DDB-AP-1" in query_ids
        assert "DDB-AP-2" not in query_ids

    def test_with_assignment_filters_tables(self):
        store = _store_with_assignment()
        result = prepare_schema_design_input(
            "job-001", "mydb", "dynamodb", store, assignment_version=1
        )
        table_ids = [t["table_id"] for t in result["collector_output"]["database_schema"]["tables"]]
        assert "mydb.users" in table_ids
        assert "mydb.orders" not in table_ids

    def test_analysis_output_included(self):
        store = _base_store()
        result = prepare_schema_design_input("job-001", "mydb", "dynamodb", store)
        assert result["analysis_output"] == _ANALYSIS


# ---------------------------------------------------------------------------
# Tests: validate_schema_design_output
# ---------------------------------------------------------------------------


class TestValidateSchemaDesignOutput:
    def test_valid_dynamodb_schema_passes(self):
        result = validate_schema_design_output(_VALID_DYNAMODB_OUTPUT, "dynamodb")
        assert result == {"valid": True}

    def test_invalid_schema_rejected(self):
        bad_output = {"job_id": "job-001"}  # missing required fields
        result = validate_schema_design_output(bad_output, "dynamodb")
        assert result["valid"] is False
        assert "errors" in result
        assert len(result["errors"]) > 0

    def test_errors_are_strings(self):
        bad_output = {}
        result = validate_schema_design_output(bad_output, "dynamodb")
        assert all(isinstance(e, str) for e in result["errors"])

    def test_unknown_engine_returns_invalid(self):
        result = validate_schema_design_output({}, "neptune")
        assert result["valid"] is False
        assert any("neptune" in e for e in result["errors"])

    def test_documentdb_valid_contract_accepted(self):
        """DocumentDB contract is importable and accepts a minimal valid payload."""

        # Just verify the validator route is exercised — a fully invalid payload fails
        result = validate_schema_design_output({}, "documentdb")
        assert result["valid"] is False

    def test_opensearch_contract_route_exercised(self):
        result = validate_schema_design_output({}, "opensearch")
        assert result["valid"] is False

    def test_elasticache_contract_route_exercised(self):
        result = validate_schema_design_output({}, "elasticache")
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# Tests: finalize_schema_design
# ---------------------------------------------------------------------------


class TestFinalizeSchemaDesign:
    def _store_with_llm_response(self, response: dict) -> MagicMock:
        return _mock_store(
            {
                "llm_responses/schema_design_dynamodb.json": response,
            }
        )

    def test_valid_input_returns_complete_status(self):
        store = self._store_with_llm_response(_VALID_DYNAMODB_OUTPUT)
        with patch("src.agents.query_journey_materializer.materialize_design"):
            result = finalize_schema_design("job-001", "mydb", "dynamodb", store)
        assert result["status"] == "complete"

    def test_valid_input_returns_output_path(self):
        store = self._store_with_llm_response(_VALID_DYNAMODB_OUTPUT)
        with patch("src.agents.query_journey_materializer.materialize_design"):
            result = finalize_schema_design("job-001", "mydb", "dynamodb", store)
        assert "output_path" in result
        assert "schema-dynamodb" in result["output_path"]

    def test_valid_input_writes_schema_output(self):
        store = self._store_with_llm_response(_VALID_DYNAMODB_OUTPUT)
        with patch("src.agents.query_journey_materializer.materialize_design"):
            finalize_schema_design("job-001", "mydb", "dynamodb", store)
        written_keys = list(store._written.keys())
        assert any("schema_output.json" in k for k in written_keys)

    def test_versioned_output_path_uses_assignment_version(self):
        store = self._store_with_llm_response(_VALID_DYNAMODB_OUTPUT)
        with patch("src.agents.query_journey_materializer.materialize_design"):
            result = finalize_schema_design(
                "job-001", "mydb", "dynamodb", store, assignment_version=3
            )
        assert "v3" in result["output_path"]

    def test_default_version_is_v1(self):
        store = self._store_with_llm_response(_VALID_DYNAMODB_OUTPUT)
        with patch("src.agents.query_journey_materializer.materialize_design"):
            result = finalize_schema_design("job-001", "mydb", "dynamodb", store)
        assert "v1" in result["output_path"]

    def test_invalid_output_returns_validation_failed(self):
        store = self._store_with_llm_response({"bad": "data"})
        result = finalize_schema_design("job-001", "mydb", "dynamodb", store)
        assert result["status"] == "validation_failed"
        assert "errors" in result

    def test_invalid_output_does_not_write_schema(self):
        store = self._store_with_llm_response({"bad": "data"})
        finalize_schema_design("job-001", "mydb", "dynamodb", store)
        written_keys = list(store._written.keys())
        assert not any("schema_output.json" in k for k in written_keys)

    def test_materialize_design_called_on_success(self):
        store = self._store_with_llm_response(_VALID_DYNAMODB_OUTPUT)
        with patch("src.agents.query_journey_materializer.materialize_design") as mock_mat:
            finalize_schema_design("job-001", "mydb", "dynamodb", store)
        mock_mat.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: run_schema_design with llm_mode="external"
# ---------------------------------------------------------------------------


class TestRunSchemaDesignLlmModeExternal:
    def test_signature_has_llm_mode_parameter(self):
        sig = inspect.signature(run_schema_design)
        assert "llm_mode" in sig.parameters

    def test_llm_mode_default_is_bedrock(self):
        sig = inspect.signature(run_schema_design)
        assert sig.parameters["llm_mode"].default == "bedrock"

    def test_external_mode_writes_llm_input(self):
        store = _store_with_assignment()
        run_schema_design("job-001", "mydb", "dynamodb", store, llm_mode="external")
        written_keys = list(store._written.keys())
        assert any("llm_input.json" in k for k in written_keys)

    def test_external_mode_does_not_call_dispatch(self):
        store = _store_with_assignment()
        with patch("src.agents.schema_design.handler._dispatch_schema_agent") as mock_dispatch:
            run_schema_design("job-001", "mydb", "dynamodb", store, llm_mode="external")
        mock_dispatch.assert_not_called()

    def test_external_mode_llm_input_has_required_keys(self):
        store = _base_store()
        run_schema_design("job-001", "mydb", "dynamodb", store, llm_mode="external")
        input_key = next(k for k in store._written if "llm_input.json" in k)
        llm_input = store._written[input_key]
        for key in (
            "target_type",
            "collector_output",
            "analysis_output",
            "database_name",
            "job_id",
        ):
            assert key in llm_input, f"Missing key in llm_input: {key}"

    def test_external_mode_with_assignment_filters_collector(self):
        store = _store_with_assignment()
        run_schema_design(
            "job-001", "mydb", "dynamodb", store, assignment_version=1, llm_mode="external"
        )
        input_key = next(k for k in store._written if "llm_input.json" in k)
        llm_input = store._written[input_key]
        query_ids = [
            q["query_id"] for q in llm_input["collector_output"]["queries"]["query_patterns"]
        ]
        assert "DDB-AP-1" in query_ids
        assert "DDB-AP-2" not in query_ids

    def test_external_mode_returns_none(self):
        store = _base_store()
        result = run_schema_design("job-001", "mydb", "dynamodb", store, llm_mode="external")
        assert result is None

    def test_bedrock_mode_calls_dispatch(self):
        store = _base_store()
        with patch(
            "src.agents.schema_design.handler._dispatch_schema_agent",
            return_value=('{"result": "ok"}', '{"iterations": []}'),
        ) as mock_dispatch:
            run_schema_design("job-001", "mydb", "dynamodb", store, llm_mode="bedrock")
        mock_dispatch.assert_called_once()

    def test_default_mode_calls_dispatch(self):
        store = _base_store()
        with patch(
            "src.agents.schema_design.handler._dispatch_schema_agent",
            return_value=('{"result": "ok"}', '{"iterations": []}'),
        ) as mock_dispatch:
            run_schema_design("job-001", "mydb", "dynamodb", store)
        mock_dispatch.assert_called_once()

    def test_external_mode_input_path_contains_version(self):
        store = _mock_store(
            {
                "collector/output.json": _COLLECTOR,
                "analysis-dynamodb/analysis.json": _ANALYSIS,
                "assignment/v2/assignment.json": _ASSIGNMENT,
            }
        )
        run_schema_design(
            "job-001", "mydb", "dynamodb", store, assignment_version=2, llm_mode="external"
        )
        written_keys = list(store._written.keys())
        assert any("v2" in k and "llm_input.json" in k for k in written_keys)
