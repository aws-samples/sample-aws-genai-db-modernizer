"""Unit tests for collector agent handler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.storage.artifact_store import ArtifactStore

ENV_VARS = {
    "S3_BUCKET": "test-bucket",
    "ENGINE": "mysql",
    "CLUSTER_ENDPOINT": "mydb.abc123.us-east-1.rds.amazonaws.com",
    "PORT": "3306",
    "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:mydb",
    "AUTOMATION_INSTANCE_ID": "i-0abc123",
    "AWS_REGION": "us-east-1",
    "DB_INSTANCE_IDENTIFIER": "mydb",
    "COLLECTION_MODE": "live",
}


def _mock_store() -> MagicMock:
    """Create a mock ArtifactStore."""
    store = MagicMock(spec=ArtifactStore)
    return store


@patch("src.agents.collector.handler._dispatch_collect")
def test_collector_writes_output_to_store(mock_dispatch):
    """Handler calls collect() and writes output via ArtifactStore."""
    mock_result = MagicMock()
    mock_result.model_dump_json.return_value = json.dumps(
        {"job_id": "job-001", "status": "ok", "queries": {"query_patterns": []}}
    )
    mock_result.database_schema.tables = [MagicMock(), MagicMock()]
    mock_result.queries.query_patterns = [MagicMock()]
    mock_dispatch.return_value = mock_result

    store = _mock_store()

    with patch.dict("os.environ", ENV_VARS):
        from src.agents.collector.handler import run_collector

        run_collector("job-001", "mydb", store)

    store.write_json.assert_called_once()
    call_args = store.write_json.call_args
    assert call_args[0][0] == "mydb/job-001/collector/output.json"


@patch("src.agents.collector.handler._dispatch_collect")
def test_collector_passes_correct_input(mock_dispatch):
    """Handler builds CollectorInput correctly from env vars."""
    mock_result = MagicMock()
    mock_result.model_dump_json.return_value = json.dumps({"queries": {"query_patterns": []}})
    mock_result.database_schema.tables = []
    mock_result.queries.query_patterns = []
    mock_dispatch.return_value = mock_result

    store = _mock_store()

    with patch.dict("os.environ", ENV_VARS):
        from src.agents.collector.handler import run_collector

        run_collector("job-001", "mydb", store)

    # Verify dispatch was called with correct engine and CollectorInput
    mock_dispatch.assert_called_once()
    engine = mock_dispatch.call_args[0][0]
    input_contract = mock_dispatch.call_args[0][1]
    assert engine == "mysql"
    assert input_contract.job_id == "job-001"
    assert input_contract.database_name == "mydb"
    assert input_contract.engine.value == "mysql"
    assert input_contract.cluster_endpoint == "mydb.abc123.us-east-1.rds.amazonaws.com"
    assert (
        input_contract.live_config.secret_arn
        == "arn:aws:secretsmanager:us-east-1:123:secret:mydb"  # nosec B105 — fake ARN in test fixture
    )
    assert input_contract.live_config.automation_instance_id == "i-0abc123"


def test_collector_exits_without_required_env_vars():
    """Handler exits with error if CLUSTER_ENDPOINT or SECRET_ARN missing."""
    store = _mock_store()

    with patch.dict("os.environ", {"S3_BUCKET": "test-bucket"}, clear=True):
        from src.agents.collector.handler import run_collector

        with pytest.raises(SystemExit) as exc_info:
            run_collector("job-001", "mydb", store)
        assert exc_info.value.code == 1
