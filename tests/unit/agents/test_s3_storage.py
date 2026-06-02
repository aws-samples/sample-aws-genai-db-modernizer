"""Tests for S3 storage manager — ADR-016 path convention."""

import json
from unittest.mock import MagicMock

from src.tools.aws.credentials import AWSCredentialManager
from src.tools.aws.s3_storage import (
    ensure_bucket,
    get_account_id,
    init_storage,
    load_json,
    save_json,
)


def _mock_cred_mgr(region: str = "us-east-1") -> MagicMock:
    mgr = MagicMock(spec=AWSCredentialManager)
    mgr.region = region
    return mgr


def test_get_account_id() -> None:
    mgr = _mock_cred_mgr()
    mgr.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    assert get_account_id(mgr) == "123456789012"


def test_ensure_bucket_already_exists() -> None:
    mgr = _mock_cred_mgr()
    mock_s3 = MagicMock()
    mgr.client.return_value = mock_s3
    mock_s3.head_bucket.return_value = {}
    name = ensure_bucket(mgr, "123456789012")
    assert name == "db-modernizer-123456789012"
    mock_s3.create_bucket.assert_not_called()


def test_init_storage_adr016_paths() -> None:
    """Verify ADR-016 path convention: {database_name}/{job_id}/{agent}/"""
    mgr = _mock_cred_mgr()
    mock_s3 = MagicMock()
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": "999"}

    def client_factory(service: str) -> MagicMock:
        return mock_sts if service == "sts" else mock_s3

    mgr.client.side_effect = client_factory
    mock_s3.head_bucket.return_value = {}

    result = init_storage(mgr, "endpoint.rds.amazonaws.com", "mydb", "job-abc123")

    assert result["bucket"] == "db-modernizer-999"
    assert result["prefix"] == "endpoint/mydb/job-abc123/"
    assert result["collector_prefix"] == "endpoint/mydb/job-abc123/collector/"
    assert result["analysis_prefix"] == "endpoint/mydb/job-abc123/analysis/"
    assert result["referee_prefix"] == "endpoint/mydb/job-abc123/referee/"
    assert result["schema_design_prefix"] == "endpoint/mydb/job-abc123/schema-design/"


def test_save_json() -> None:
    mgr = _mock_cred_mgr()
    mock_s3 = MagicMock()
    mgr.client.return_value = mock_s3
    uri = save_json(mgr, "my-bucket", "mydb/job-1/collector/output.json", {"key": "value"})
    assert uri == "s3://my-bucket/mydb/job-1/collector/output.json"
    body = json.loads(mock_s3.put_object.call_args[1]["Body"].decode())
    assert body["key"] == "value"


def test_load_json() -> None:
    mgr = _mock_cred_mgr()
    mock_s3 = MagicMock()
    mgr.client.return_value = mock_s3
    mock_s3.get_object.return_value = {
        "Body": MagicMock(read=MagicMock(return_value=b'{"key": "value"}'))
    }
    data = load_json(mgr, "my-bucket", "path/data.json")
    assert data == {"key": "value"}
