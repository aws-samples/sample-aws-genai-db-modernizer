"""Unit tests for schema design agent handler."""

import os
from unittest.mock import patch

import pytest

from src.storage.local_store import LocalArtifactStore


@pytest.fixture
def artifact_store(tmp_path):
    """Create a LocalArtifactStore backed by a temp directory."""
    store = LocalArtifactStore(str(tmp_path))

    # Write sample collector output
    store.write_json(
        "mydb/job-001/collector/output.json",
        {
            "contract_version": "3.0",
            "database_schema": {
                "tables": [
                    {
                        "table_id": "mydb.users",
                        "table_name": "users",
                        "row_count": 100,
                        "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                    }
                ]
            },
            "queries": {"query_patterns": []},
        },
    )

    # Write sample analysis output
    store.write_json(
        "mydb/job-001/analysis-dynamodb/analysis.json",
        {
            "contract_version": "2.1",
        },
    )

    return store


class TestSchemaDesignHandler:
    def test_reads_collector_and_analysis_from_store(self, artifact_store):
        """Handler reads both collector and analysis outputs from ArtifactStore."""
        with patch(
            "src.agents.schema_design.handler._dispatch_schema_agent",
            return_value=('{"test": true}', '{"iterations": []}'),
        ):
            from src.agents.schema_design.handler import run_schema_design

            run_schema_design("job-001", "mydb", "dynamodb", artifact_store)

        # If we got here without error, reads succeeded

    def test_writes_output_and_trace_to_store(self, artifact_store):
        """Handler writes schema_output.json and design_trace.json via ArtifactStore."""
        with patch(
            "src.agents.schema_design.handler._dispatch_schema_agent",
            return_value=('{"result": "ok"}', '{"iterations": []}'),
        ):
            from src.agents.schema_design.handler import run_schema_design

            run_schema_design("job-001", "mydb", "dynamodb", artifact_store)

        assert artifact_store.exists("mydb/job-001/schema-dynamodb/schema_output.json")
        assert artifact_store.exists("mydb/job-001/schema-dynamodb/design_trace.json")

    def test_versioned_output_path_when_assignment_version_set(self, artifact_store):
        """When assignment_version > 0, output goes to versioned path."""
        # Write assignment artifact with queries assigned to dynamodb
        artifact_store.write_json(
            "mydb/job-001/assignment/v2/assignment.json",
            {
                "job_id": "job-001",
                "version": 2,
                "query_assignments": [
                    {
                        "query_id": "q1",
                        "assigned_engine": "dynamodb",
                        "confidence": 80,
                        "source_tables": ["mydb.users"],
                        "assignment_reason": "test",
                        "in_scope": True,
                    }
                ],
                "table_assignments": [],
            },
        )

        with patch(
            "src.agents.schema_design.handler._dispatch_schema_agent",
            return_value=('{"result": "ok"}', '{"iterations": []}'),
        ):
            from src.agents.schema_design.handler import run_schema_design

            run_schema_design("job-001", "mydb", "dynamodb", artifact_store, assignment_version=2)

        assert artifact_store.exists("mydb/job-001/schema-dynamodb/v2/schema_output.json")
        assert artifact_store.exists("mydb/job-001/schema-dynamodb/v2/design_trace.json")

    def test_passes_paths_to_dispatch(self, artifact_store):
        """Handler passes collector_path and analysis_path to _dispatch_schema_agent."""
        captured_paths = {}

        def capture_dispatch(
            target_type, collector_path=None, analysis_path=None, revision_context_path=None
        ):
            captured_paths["collector"] = collector_path
            captured_paths["analysis"] = analysis_path
            return '{"result": "ok"}', '{"iterations": []}'

        with patch(
            "src.agents.schema_design.handler._dispatch_schema_agent",
            side_effect=capture_dispatch,
        ):
            from src.agents.schema_design.handler import run_schema_design

            run_schema_design("job-001", "mydb", "dynamodb", artifact_store)

        assert captured_paths["collector"] is not None
        assert captured_paths["analysis"] is not None
        assert captured_paths["collector"].endswith(".json")
        assert captured_paths["analysis"].endswith(".json")

    def test_cleans_up_temp_files_after_run(self, artifact_store):
        """Handler cleans up temp files after agent completes."""
        captured_paths = {}

        def capture_dispatch(
            target_type, collector_path=None, analysis_path=None, revision_context_path=None
        ):
            captured_paths["collector"] = collector_path
            captured_paths["analysis"] = analysis_path
            return "{}", "{}"

        with patch(
            "src.agents.schema_design.handler._dispatch_schema_agent",
            side_effect=capture_dispatch,
        ):
            from src.agents.schema_design.handler import run_schema_design

            run_schema_design("job-001", "mydb", "dynamodb", artifact_store)

        # Temp files should be deleted after the run
        assert not os.path.exists(captured_paths["collector"])
        assert not os.path.exists(captured_paths["analysis"])

    def test_placeholder_for_unimplemented_engine(self, artifact_store):
        """Unimplemented engines get a placeholder output, no trace."""
        # Write analysis for neptune
        artifact_store.write_json(
            "mydb/job-001/analysis-neptune/analysis.json",
            {
                "contract_version": "2.1",
            },
        )

        from src.agents.schema_design.handler import run_schema_design

        run_schema_design("job-001", "mydb", "neptune", artifact_store)

        assert artifact_store.exists("mydb/job-001/schema-neptune/schema_output.json")
        output = artifact_store.read_json("mydb/job-001/schema-neptune/schema_output.json")
        assert output["target_type"] == "neptune"
        assert output["status"] == "not_implemented"
