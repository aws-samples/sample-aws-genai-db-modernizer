"""#1071: the assessment request carries ``engines_in_use`` to the collect step.

The UI's offline form lets a user declare the other AWS databases the application runs
today. The request validates the ids like ``--engines-in-use`` does, keeps them in the job
metadata (``_meta.json``), and the collector merges them with the file's own list.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments
from src.api.services.local_execution import LocalExecutionService
from src.storage.local_store import LocalArtifactStore

client = TestClient(app)


def _body(**overrides: object) -> dict:
    return {
        "source_database_type": "mysql",
        "database_name": "wordpress",
        "collection_mode": "offline",
        "offline_s3_key": "wordpress/job-1/uploads/collector-output.json",
        "job_id": "job-1",
        **overrides,
    }


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    monkeypatch.setattr(assessments, "sfn_service", LocalExecutionService(store))
    monkeypatch.setattr(assessments, "s3_service", MagicMock())
    return tmp_path / "artifacts"


class TestRequest:
    def test_the_ids_are_normalized_and_kept_in_the_job_metadata(self, root: Path) -> None:
        resp = client.post(
            "/api/v1/assessments",
            json=_body(engines_in_use=["OpenSearch", " documentdb", "opensearch"]),
        )
        assert resp.status_code == 202, resp.text
        store = LocalArtifactStore(base_dir=str(root))
        meta = store.read_json("wordpress/job-1/_meta.json")
        assert meta["input"]["engines_in_use"] == ["opensearch", "documentdb"]

    def test_left_out_declares_nothing(self, root: Path) -> None:
        resp = client.post("/api/v1/assessments", json=_body())
        assert resp.status_code == 202, resp.text
        meta = LocalArtifactStore(base_dir=str(root)).read_json("wordpress/job-1/_meta.json")
        assert meta["input"]["engines_in_use"] == []

    @pytest.mark.parametrize(
        ("value", "says"),
        [
            pytest.param(["redis"], "redis", id="unknown"),
            pytest.param(["aurora"], "legacy", id="legacy"),
            pytest.param(["neptune"], "neptune", id="future"),
        ],
    )
    def test_a_refused_id_is_a_422_on_the_field(self, value: list[str], says: str, root) -> None:
        resp = client.post("/api/v1/assessments", json=_body(engines_in_use=value))
        assert resp.status_code == 422, resp.text
        errors = resp.json()["detail"]
        assert [e["loc"] for e in errors] == [["body", "engines_in_use"]]
        assert says in errors[0]["msg"]
        assert not root.exists() or not list(root.rglob("*.json"))


class TestCollectStep:
    @patch("src.agents.collector.handler._dispatch_collect")
    def test_the_collector_reads_the_declared_engines_from_the_job_metadata(
        self, mock_dispatch: MagicMock, tmp_path: Path
    ) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        store.write_json(
            "mydb/job-001/_meta.json",
            {"job_id": "job-001", "input": {"engines_in_use": ["opensearch"]}},
        )
        result = MagicMock()
        result.model_dump_json.return_value = json.dumps(
            {"metadata": {"engines_in_use": ["documentdb"]}}
        )
        mock_dispatch.return_value = result
        env = {
            "CLUSTER_ENDPOINT": "mydb.example.internal",
            "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:mydb",  # pragma: allowlist secret
            "ENGINE": "mysql",
            "COLLECTION_MODE": "live",
        }
        with patch.dict("os.environ", env, clear=True):
            from src.agents.collector.handler import run_collector

            run_collector("job-001", "mydb", store)

        assert mock_dispatch.call_args[0][1].engines_in_use == ["opensearch"]
        written = store.read_json("mydb/job-001/collector/output.json")
        assert written["metadata"]["engines_in_use"] == ["documentdb", "opensearch"]
