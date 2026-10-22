"""#1328: the Assessments API's ``target_databases`` is the job's engine scope.

It is validated against the engine catalog (active engines only, a 422 naming the
field otherwise), normalized (case, blanks, duplicates), kept in the job metadata
(``_meta.json``) and recorded as ``decisions/engine_scope.json``, which triage reads.
Left out, the job has no scope and nothing is recorded, as before.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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


def _job(root: Path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(root))


class TestValidation:
    @pytest.mark.parametrize(
        ("value", "says"),
        [
            pytest.param([], "target_databases is empty: name at least one engine", id="empty"),
            pytest.param(
                ["aurora"],
                "the engine id 'aurora' is legacy and not accepted; use aurora_mysql or "
                "aurora_postgresql",
                id="legacy",
            ),
            pytest.param(["neptune"], "Neptune (neptune) is not available yet", id="future"),
            pytest.param(["keyspaces"], "is not available yet", id="future-keyspaces"),
            pytest.param(["dynamodb", "mysql"], "unknown engine id(s) mysql", id="unknown"),
        ],
    )
    def test_a_refused_scope_is_a_422_on_the_field(self, value, says, root) -> None:
        resp = client.post("/api/v1/assessments", json=_body(target_databases=value))
        assert resp.status_code == 422, resp.text
        errors = resp.json()["detail"]
        assert [e["loc"] for e in errors] == [["body", "target_databases"]]
        assert says in errors[0]["msg"]
        # Nothing is started or recorded for a refused request.
        assert not root.exists() or not list(root.rglob("*.json"))

    def test_a_scope_is_normalized(self, root) -> None:
        resp = client.post(
            "/api/v1/assessments",
            json=_body(target_databases=["DynamoDB", " aurora_postgresql", "dynamodb"]),
        )
        assert resp.status_code == 202, resp.text
        meta = _job(root).read_json("wordpress/job-1/_meta.json")
        assert meta["input"]["target_databases"] == ["aurora_postgresql", "dynamodb"]


class TestTheJobRecord:
    def test_a_scope_is_kept_in_the_job_metadata_and_recorded_as_the_decision(self, root) -> None:
        resp = client.post(
            "/api/v1/assessments", json=_body(target_databases=["dynamodb", "aurora_postgresql"])
        )
        assert resp.status_code == 202, resp.text
        store = _job(root)
        assert store.read_json("wordpress/job-1/_meta.json")["input"]["target_databases"] == [
            "aurora_postgresql",
            "dynamodb",
        ]
        decision = store.read_json("wordpress/job-1/decisions/engine_scope.json")
        assert decision["engines"] == ["aurora_postgresql", "dynamodb"]
        assert decision["via"] == "api"
        assert decision["decided_by"] == "customer"

    def test_left_out_is_no_scope_and_records_no_decision(self, root) -> None:
        resp = client.post("/api/v1/assessments", json=_body())
        assert resp.status_code == 202, resp.text
        store = _job(root)
        assert store.read_json("wordpress/job-1/_meta.json")["input"]["target_databases"] is None
        assert not store.exists("wordpress/job-1/decisions/engine_scope.json")

    @pytest.mark.passes_on_main  # guards main's behaviour: null records nothing
    def test_null_is_the_same_as_left_out(self, root) -> None:
        resp = client.post("/api/v1/assessments", json=_body(target_databases=None))
        assert resp.status_code == 202, resp.text
        assert not _job(root).exists("wordpress/job-1/decisions/engine_scope.json")

    def test_every_active_engine_is_a_valid_scope(self, root) -> None:
        from src.shared import catalog

        active = sorted(catalog.engine_ids(status=catalog.ACTIVE))
        resp = client.post("/api/v1/assessments", json=_body(target_databases=active))
        assert resp.status_code == 202, resp.text
        assert _job(root).read_json("wordpress/job-1/decisions/engine_scope.json")["engines"] == (
            active
        )


def test_the_route_hands_the_scope_on_unchanged_and_none_as_none() -> None:
    sfn = MagicMock()
    sfn.start_execution.return_value = {"execution_arn": "arn:local:job-1"}
    old = assessments.sfn_service, assessments.s3_service
    assessments.sfn_service, assessments.s3_service = sfn, MagicMock()
    try:
        client.post("/api/v1/assessments", json=_body(target_databases=["DYNAMODB"]))
        client.post("/api/v1/assessments", json=_body())
    finally:
        assessments.sfn_service, assessments.s3_service = old
    sent = [call.args[1]["target_databases"] for call in sfn.start_execution.call_args_list]
    assert sent == [["dynamodb"], None]


class TestAStartedJobKeepsItsScope:
    """#1328 review: a second POST for a job that was already started is a 409, so its
    scope can neither change nor be orphaned after triage has routed under it."""

    def _decision(self, root: Path) -> dict:
        return _job(root).read_json("wordpress/job-1/decisions/engine_scope.json")

    def test_a_second_start_with_another_scope_is_refused(self, root) -> None:
        assert (
            client.post(
                "/api/v1/assessments", json=_body(target_databases=["dynamodb"])
            ).status_code
            == 202
        )
        meta = _job(root).read_json("wordpress/job-1/_meta.json")
        again = client.post("/api/v1/assessments", json=_body(target_databases=["opensearch"]))
        assert again.status_code == 409, again.text
        assert "already started" in again.json()["detail"]
        assert "Start a new job" in again.json()["detail"]
        assert self._decision(root)["engines"] == ["dynamodb"]
        assert _job(root).read_json("wordpress/job-1/_meta.json") == meta

    def test_a_second_start_with_no_scope_leaves_no_stale_decision(self, root) -> None:
        assert (
            client.post(
                "/api/v1/assessments", json=_body(target_databases=["dynamodb"])
            ).status_code
            == 202
        )
        again = client.post("/api/v1/assessments", json=_body())
        assert again.status_code == 409, again.text
        # The recorded scope and the job metadata still agree.
        assert self._decision(root)["engines"] == ["dynamodb"]
        meta = _job(root).read_json("wordpress/job-1/_meta.json")
        assert meta["input"]["target_databases"] == ["dynamodb"]

    def test_a_job_already_triaged_without_the_api_is_refused(self, root) -> None:
        # A job a script ran up to triage (no _meta.json) cannot be rescoped either.
        store = _job(root)
        store.write_json(
            "wordpress/job-1/collector/output.json",
            {"database_schema": {"tables": []}, "queries": {"query_patterns": []}},
        )
        resp = client.post("/api/v1/assessments", json=_body(target_databases=["opensearch"]))
        assert resp.status_code == 409, resp.text
        assert not store.exists("wordpress/job-1/decisions/engine_scope.json")

    def test_a_prepared_job_with_only_its_upload_starts(self, root) -> None:
        upload = root / "wordpress" / "job-1" / "uploads"
        upload.mkdir(parents=True)
        (upload / "collector-output.json").write_text("{}")
        resp = client.post("/api/v1/assessments", json=_body(target_databases=["dynamodb"]))
        assert resp.status_code == 202, resp.text
