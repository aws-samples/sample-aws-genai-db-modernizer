"""Unit tests for schema revision API endpoints.

Tests cover:
- GET  /{job_id}/schema/{engine}              — fetch latest or specific version
- GET  /{job_id}/schema/{engine}/versions     — list version metadata
- PUT  /{job_id}/schema/{engine}/revisions    — optimistic concurrency check + 501 stub
- POST /{job_id}/schema/{engine}/confirm      — per-engine confirmation
- POST /{job_id}/schema/confirm-all           — bulk confirmation
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from src.api.main import app
    from src.api.routes import schema_revisions

    store = MagicMock()
    schema_revisions.artifact_store = store
    schema_revisions.orchestrator = None
    yield TestClient(app), store
    # reset after test
    schema_revisions.artifact_store = None
    schema_revisions.orchestrator = None


# ---------------------------------------------------------------------------
# Helpers — build minimal artifact payloads
# ---------------------------------------------------------------------------

_TS = "2026-05-01T12:00:00+00:00"


def _version_meta(version: int, engine: str = "dynamodb") -> dict:
    return {
        "version": version,
        "base_version": version - 1 if version > 1 else None,
        "initiated_by": "system" if version == 1 else "customer",
        "timestamp": _TS,
        "modifications": None,
        "redesigned_groups": [],
        "verification": {
            "passed": True,
            "hard_errors": [],
            "warnings": [],
        },
        "changelog": [],
    }


def _schema_output() -> dict:
    return {
        "access_patterns": [],
        "index_designs": [],
        "collection_designs": [],
    }


def _triage_output(engines: list[str]) -> dict:
    return {
        "job_id": "job-1",
        "database_name": "mydb",
        "agent_type": "referee-triage",
        "selected_agents": [{"agent_type": e, "reasons": ["test"]} for e in engines],
        "skipped_agents": [],
        "baseline": {},
        "deferred_agents": [],
        "signals": [],
        "query_capabilities": {},
        "confidence_score": 80,
        "timestamp": _TS,
    }


def _setup_schema_v1(store: MagicMock, db: str, job_id: str, engine: str) -> None:
    """Configure the mock store so that a v1 schema exists for *engine*."""
    prefix = f"{db}/{job_id}/schema-{engine}/"
    store.list_prefix.return_value = [
        f"{prefix}v1/schema_output.json",
        f"{prefix}v1/version_meta.json",
    ]
    store.read_json.side_effect = lambda path: (
        _schema_output()
        if path.endswith("schema_output.json")
        else _version_meta(1, engine)
        if path.endswith("version_meta.json")
        else {}
    )
    store.exists.return_value = True


# ---------------------------------------------------------------------------
# GET /{job_id}/schema/{engine}
# ---------------------------------------------------------------------------


class TestGetSchema:
    def test_returns_200_and_schema_when_latest(self, client):
        tc, store = client
        _setup_schema_v1(store, "mydb", "job-1", "dynamodb")

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "schema_output" in data
        assert "meta" in data

    def test_returns_latest_version_when_no_version_param(self, client):
        tc, store = client
        # Two versions exist
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
            f"{prefix}v1/version_meta.json",
            f"{prefix}v2/schema_output.json",
            f"{prefix}v2/version_meta.json",
        ]
        store.read_json.side_effect = lambda path: (
            _schema_output()
            if path.endswith("schema_output.json")
            else _version_meta(2, "dynamodb")
        )
        store.exists.return_value = True

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        assert resp.json()["meta"]["version"] == 2

    def test_returns_specific_version_when_provided(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
            f"{prefix}v1/version_meta.json",
            f"{prefix}v2/schema_output.json",
            f"{prefix}v2/version_meta.json",
        ]

        def _read(path: str) -> dict:
            if "v1/version_meta.json" in path:
                return _version_meta(1)
            if "v2/version_meta.json" in path:
                return _version_meta(2)
            return _schema_output()

        store.read_json.side_effect = _read
        store.exists.return_value = True

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb",
            params={"database_name": "mydb", "version": "1"},
        )

        assert resp.status_code == 200
        assert resp.json()["meta"]["version"] == 1

    def test_returns_404_when_no_schema_exists(self, client):
        tc, store = client
        store.list_prefix.return_value = []

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 404

    def test_returns_404_when_requested_version_not_found(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
        ]
        store.exists.return_value = False  # v3 does not exist

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb",
            params={"database_name": "mydb", "version": "3"},
        )

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /{job_id}/schema/{engine}/versions
# ---------------------------------------------------------------------------


class TestGetVersions:
    def test_returns_200_with_versions_list(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
            f"{prefix}v1/version_meta.json",
            f"{prefix}v2/schema_output.json",
            f"{prefix}v2/version_meta.json",
        ]
        store.read_json.side_effect = lambda path: (
            _version_meta(1) if "v1/version_meta" in path else _version_meta(2)
        )
        store.exists.return_value = True

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb/versions",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "versions" in data
        assert isinstance(data["versions"], list)
        assert len(data["versions"]) == 2

    def test_versions_ordered_ascending(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v2/version_meta.json",
            f"{prefix}v1/version_meta.json",
        ]
        store.read_json.side_effect = lambda path: (
            _version_meta(1) if "v1/version_meta" in path else _version_meta(2)
        )
        store.exists.return_value = True

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb/versions",
            params={"database_name": "mydb"},
        )

        versions = [v["version"] for v in resp.json()["versions"]]
        assert versions == sorted(versions)

    def test_returns_empty_list_when_no_versions(self, client):
        tc, store = client
        store.list_prefix.return_value = []

        resp = tc.get(
            "/api/v1/assessments/job-1/schema/dynamodb/versions",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        assert resp.json()["versions"] == []


# ---------------------------------------------------------------------------
# PUT /{job_id}/schema/{engine}/revisions
# ---------------------------------------------------------------------------


_REVISION_BODY = {
    "base_version": 1,
    "pattern_modifications": [],
    "table_modifications": [],
    "new_patterns": [],
}


class TestPutRevisions:
    def test_returns_409_when_base_version_is_stale(self, client):
        tc, store = client
        # Latest is v2, request sends base_version=1 → stale
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
            f"{prefix}v2/schema_output.json",
        ]

        body = {**_REVISION_BODY, "base_version": 1}
        resp = tc.put(
            "/api/v1/assessments/job-1/schema/dynamodb/revisions",
            params={"database_name": "mydb"},
            json=body,
        )

        assert resp.status_code == 409

    def test_returns_404_when_no_schema_exists_for_engine(self, client):
        tc, store = client
        store.list_prefix.return_value = []

        resp = tc.put(
            "/api/v1/assessments/job-1/schema/dynamodb/revisions",
            params={"database_name": "mydb"},
            json=_REVISION_BODY,
        )

        assert resp.status_code == 404

    def test_returns_200_with_schema_when_base_version_matches(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
        ]
        store.read_json.return_value = _schema_output()

        body = {**_REVISION_BODY, "base_version": 1}
        resp = tc.put(
            "/api/v1/assessments/job-1/schema/dynamodb/revisions",
            params={"database_name": "mydb"},
            json=body,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "schema_output" in data
        assert "meta" in data
        assert data["meta"]["version"] == 2
        assert data["meta"]["base_version"] == 1
        assert data["meta"]["initiated_by"] == "customer"

    def test_drop_removes_pattern_from_schema(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
        ]
        store.read_json.return_value = {
            "access_patterns": [
                {"pattern_id": "AP-1", "query_ids": ["q1"]},
                {"pattern_id": "AP-2", "query_ids": ["q2"]},
            ],
            "index_designs": [],
            "collection_designs": [],
        }

        body = {
            "base_version": 1,
            "pattern_modifications": [
                {"pattern_id": "AP-1", "action": "DROP"},
            ],
            "table_modifications": [],
            "new_patterns": [],
        }
        resp = tc.put(
            "/api/v1/assessments/job-1/schema/dynamodb/revisions",
            params={"database_name": "mydb"},
            json=body,
        )

        assert resp.status_code == 200
        patterns = resp.json()["schema_output"]["access_patterns"]
        pattern_ids = [p["pattern_id"] for p in patterns]
        assert "AP-1" not in pattern_ids
        assert "AP-2" in pattern_ids

    def test_writes_version_artifacts(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
        ]
        store.read_json.return_value = _schema_output()

        body = {**_REVISION_BODY, "base_version": 1}
        tc.put(
            "/api/v1/assessments/job-1/schema/dynamodb/revisions",
            params={"database_name": "mydb"},
            json=body,
        )

        write_paths = [str(call) for call in store.write_json.call_args_list]
        assert any("v2/schema_output.json" in p for p in write_paths)
        assert any("v2/version_meta.json" in p for p in write_paths)

    def test_409_detail_mentions_stale(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
            f"{prefix}v2/schema_output.json",
        ]

        body = {**_REVISION_BODY, "base_version": 1}
        resp = tc.put(
            "/api/v1/assessments/job-1/schema/dynamodb/revisions",
            params={"database_name": "mydb"},
            json=body,
        )

        assert resp.status_code == 409
        detail = resp.json().get("detail", "")
        assert "stale" in detail.lower() or "conflict" in detail.lower() or "2" in detail


# ---------------------------------------------------------------------------
# POST /{job_id}/schema/{engine}/confirm
# ---------------------------------------------------------------------------


class TestPostConfirm:
    def test_returns_200_with_confirmed_version_and_engine(self, client):
        tc, store = client
        _setup_schema_v1(store, "mydb", "job-1", "dynamodb")

        resp = tc.post(
            "/api/v1/assessments/job-1/schema/dynamodb/confirm",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "confirmed_version" in data
        assert "engine" in data
        assert data["engine"] == "dynamodb"

    def test_confirmed_version_matches_latest(self, client):
        tc, store = client
        prefix = "mydb/job-1/schema-dynamodb/"
        store.list_prefix.return_value = [
            f"{prefix}v1/schema_output.json",
            f"{prefix}v2/schema_output.json",
        ]
        store.exists.return_value = True

        resp = tc.post(
            "/api/v1/assessments/job-1/schema/dynamodb/confirm",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        assert resp.json()["confirmed_version"] == 2

    def test_writes_confirmed_json(self, client):
        tc, store = client
        _setup_schema_v1(store, "mydb", "job-1", "dynamodb")

        tc.post(
            "/api/v1/assessments/job-1/schema/dynamodb/confirm",
            params={"database_name": "mydb"},
        )

        # Verify write_json was called for the confirmed.json path
        write_calls = [str(call) for call in store.write_json.call_args_list]
        assert any("confirmed.json" in c for c in write_calls)

    def test_returns_404_when_no_schema_exists(self, client):
        tc, store = client
        store.list_prefix.return_value = []

        resp = tc.post(
            "/api/v1/assessments/job-1/schema/dynamodb/confirm",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /{job_id}/schema/confirm-all
# ---------------------------------------------------------------------------


class TestPostConfirmAll:
    def _setup_multi_engine(
        self, store: MagicMock, db: str, job_id: str, engines: list[str]
    ) -> None:
        """Configure store for multiple engines, each with v1."""
        triage_path = f"{db}/{job_id}/referee-triage/triage.json"

        def _list_prefix(prefix: str) -> list[str]:
            for eng in engines:
                if f"schema-{eng}/" in prefix:
                    return [f"{prefix}v1/schema_output.json"]
            return []

        def _read_json(path: str) -> dict:
            if path == triage_path:
                return _triage_output(engines)
            return {}

        store.list_prefix.side_effect = _list_prefix
        store.read_json.side_effect = _read_json
        store.exists.return_value = True

    def test_returns_200_with_dict_of_engine_to_version(self, client):
        tc, store = client
        self._setup_multi_engine(store, "mydb", "job-1", ["dynamodb", "opensearch"])

        resp = tc.post(
            "/api/v1/assessments/job-1/schema/confirm-all",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "confirmed" in data
        assert isinstance(data["confirmed"], dict)

    def test_all_active_engines_appear_in_response(self, client):
        tc, store = client
        self._setup_multi_engine(store, "mydb", "job-1", ["dynamodb", "opensearch"])

        resp = tc.post(
            "/api/v1/assessments/job-1/schema/confirm-all",
            params={"database_name": "mydb"},
        )

        confirmed = resp.json()["confirmed"]
        assert "dynamodb" in confirmed
        assert "opensearch" in confirmed

    def test_writes_confirmed_json_for_each_engine(self, client):
        tc, store = client
        self._setup_multi_engine(store, "mydb", "job-1", ["dynamodb", "opensearch"])

        tc.post(
            "/api/v1/assessments/job-1/schema/confirm-all",
            params={"database_name": "mydb"},
        )

        write_calls = [str(call) for call in store.write_json.call_args_list]
        confirmed_writes = [c for c in write_calls if "confirmed.json" in c]
        assert len(confirmed_writes) == 2

    def test_returns_400_when_no_engines_found(self, client):
        tc, store = client
        # Triage returns empty selected_agents
        store.read_json.return_value = _triage_output([])
        store.exists.return_value = True

        resp = tc.post(
            "/api/v1/assessments/job-1/schema/confirm-all",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 400

    def test_calls_orchestrator_confirm_when_available(self, client):
        tc, store = client
        from src.api.routes import schema_revisions

        mock_orch = MagicMock()
        schema_revisions.orchestrator = mock_orch
        self._setup_multi_engine(store, "mydb", "job-1", ["dynamodb"])

        tc.post(
            "/api/v1/assessments/job-1/schema/confirm-all",
            params={"database_name": "mydb"},
        )

        mock_orch.confirm_schema_design.assert_called_once_with("job-1")
        schema_revisions.orchestrator = None

    def test_confirmed_versions_are_latest_per_engine(self, client):
        tc, store = client
        engines = ["dynamodb"]
        triage_path = "mydb/job-1/referee-triage/triage.json"

        def _list_prefix(prefix: str) -> list[str]:
            if "schema-dynamodb/" in prefix:
                return [
                    f"{prefix}v1/schema_output.json",
                    f"{prefix}v2/schema_output.json",
                ]
            return []

        store.list_prefix.side_effect = _list_prefix
        store.read_json.side_effect = lambda path: (
            _triage_output(engines) if path == triage_path else {}
        )
        store.exists.return_value = True

        resp = tc.post(
            "/api/v1/assessments/job-1/schema/confirm-all",
            params={"database_name": "mydb"},
        )

        assert resp.status_code == 200
        assert resp.json()["confirmed"]["dynamodb"] == 2
