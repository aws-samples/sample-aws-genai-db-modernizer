"""Unit tests for analysis agent handler."""

from unittest.mock import MagicMock

from src.storage.artifact_store import ArtifactStore

MINIMAL_COLLECTOR_OUTPUT = {
    "contract_version": "3.0",
    "job_id": "job-001",
    "metadata": {
        "collection_timestamp": "2026-03-13T00:00:00Z",
        "collector_version": "1.0.0",
        "source_database": {
            "engine": "mysql",
            "version": "8.0.44",
            "hostname": "test-host",
        },
    },
    "database_schema": {
        "tables": [
            {
                "table_id": "mydb.users",
                "table_name": "users",
                "row_count": 500,
                "size_mb": 1.0,
                "columns": [
                    {
                        "column_name": "id",
                        "data_type": "int",
                        "nullable": False,
                    }
                ],
                "primary_key": ["id"],
            }
        ]
    },
    "queries": {
        "query_patterns": [
            {
                "query_id": "q1",
                "query_text": "SELECT * FROM users WHERE id = ?",
                "query_type": "SELECT",
                "frequency_per_hour": 100,
                "calls_per_second": 2.0,
                "tables_accessed": ["mydb.users"],
                "rows_returned_avg": 1,
                "filter_columns": ["id"],
            }
        ]
    },
    "metrics": {
        "performance_metrics": {"avg_query_time_ms": 1.0},
    },
}


def _mock_store(collector_output=None) -> MagicMock:
    """Create a mock ArtifactStore that returns collector output on read."""
    store = MagicMock(spec=ArtifactStore)
    store.read_json.return_value = collector_output or MINIMAL_COLLECTOR_OUTPUT
    written = {}

    def _write_json(path, data):
        written[path] = data

    store.write_json.side_effect = _write_json
    store._written = written
    return store


def test_dynamodb_analysis_writes_contract_output():
    """DynamoDB agent produces AnalysisOutputContract format."""
    store = _mock_store()

    from src.agents.analysis.handler import run_analysis

    run_analysis("job-001", "mydb", "dynamodb", store)

    # Find the analysis.json write call
    analysis_call = None
    for call in store.write_json.call_args_list:
        if call[0][0] == "mydb/job-001/analysis-dynamodb/analysis.json":
            analysis_call = call
            break

    assert analysis_call is not None
    body = analysis_call[0][1]
    assert body["contract_version"] == "2.1"
    assert body["agent_metadata"]["agent_name"] == "dynamodb-analysis-agent"
    assert body["agent_metadata"]["target_database"] == "dynamodb"
    assert len(body["table_recommendations"]) == 1
    assert body["table_recommendations"][0]["table_id"] == "mydb.users"
    assert body["table_recommendations"][0]["confidence_score"] > 0


def test_opensearch_analysis_writes_contract_output():
    """OpenSearch agent produces AnalysisOutputContract format."""
    store = _mock_store()

    from src.agents.analysis.handler import run_analysis

    run_analysis("job-001", "mydb", "opensearch", store)

    # Find the analysis.json write call
    analysis_call = None
    for call in store.write_json.call_args_list:
        if call[0][0] == "mydb/job-001/analysis-opensearch/analysis.json":
            analysis_call = call
            break

    assert analysis_call is not None
    body = analysis_call[0][1]
    assert body["contract_version"] == "2.1"
    assert body["agent_metadata"]["agent_name"] == "opensearch-analysis-agent"
    assert body["agent_metadata"]["target_database"] == "opensearch"
    assert len(body["table_recommendations"]) == 1
    assert body["table_recommendations"][0]["table_id"] == "mydb.users"


def test_placeholder_agent_writes_output():
    """Non-implemented agents still produce placeholder AnalysisOutputContract."""
    store = _mock_store()

    from src.agents.analysis.handler import run_analysis

    run_analysis("job-001", "mydb", "neptune", store)

    # Find the analysis.json write call
    analysis_call = None
    for call in store.write_json.call_args_list:
        if call[0][0] == "mydb/job-001/analysis-neptune/analysis.json":
            analysis_call = call
            break

    assert analysis_call is not None
    body = analysis_call[0][1]
    assert body["contract_version"] == "2.1"
    assert body["agent_metadata"]["agent_name"] == "neptune-analysis-agent"
    assert body["agent_metadata"]["agent_version"] == "0.0.1"
