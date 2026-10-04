"""Resolution flow: local cache -> download -> build+upload (self-healing).

Every handle handed to a read endpoint is read-only; building uses a separate,
short-lived read-write handle that is closed before the upload.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.graph import GraphStoreCache
from src.graph.store import GraphStore


def _write_populated(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(path)
    store.execute("CREATE NODE TABLE Foo(id STRING PRIMARY KEY)")
    store.execute("CREATE (:Foo {id: 'a'})")
    store.close()


def _fake_rebuild(calls):
    def _rebuild(db_name, job_id, artifact_store, store):
        calls.append(store.read_only)
        store.execute("CREATE NODE TABLE IF NOT EXISTS Foo(id STRING PRIMARY KEY)")
        store.execute("MERGE (:Foo {id: 'built'})")
        return {"nodes_created": 1}

    return _rebuild


@pytest.fixture
def wired(monkeypatch, tmp_path):
    cache = GraphStoreCache(max_size=3, base_dir=str(tmp_path))
    persistence = MagicMock()
    persistence.download_if_exists.return_value = False
    rebuilds: list[bool] = []
    monkeypatch.setattr(graph_routes, "graph_cache", cache)
    monkeypatch.setattr(graph_routes, "artifact_store", MagicMock())
    monkeypatch.setattr(graph_routes, "graph_persistence", persistence)
    monkeypatch.setattr(graph_routes, "_resolve_db_name", lambda job_id: "db")
    monkeypatch.setattr(graph_routes, "_get_database_name", lambda job_id: "db")
    monkeypatch.setattr(graph_routes, "initialize_schema", lambda store: None)
    monkeypatch.setattr(graph_routes, "rebuild_graph", _fake_rebuild(rebuilds))
    yield cache, persistence, rebuilds
    cache.close_all()


def _ids(store):
    return [r["id"] for r in store.query("MATCH (f:Foo) RETURN f.id AS id ORDER BY id")]


def test_build_and_upload_on_download_miss(wired):
    """When nothing is cached, the graph is built on a writer, uploaded, then served read-only."""
    cache, persistence, rebuilds = wired

    with graph_routes.graph_lease("job-1") as (store, db_name):
        assert db_name == "db"
        assert store.read_only is True
        assert _ids(store) == ["built"]
    assert rebuilds == [False]
    persistence.upload.assert_called_once()


def test_download_hit_skips_build(wired):
    """When the graph is in the store, download and skip rebuild."""
    cache, persistence, rebuilds = wired

    def _download(db_name, job_id, local_path):
        _write_populated(local_path)
        return True

    persistence.download_if_exists.side_effect = _download

    with graph_routes.graph_lease("job-1") as (store, _):
        assert store.read_only is True
        assert _ids(store) == ["a"]
    assert rebuilds == []
    persistence.upload.assert_not_called()


def test_existing_local_graph_is_reused_read_only(wired):
    """A populated local graph is opened read-only without download or rebuild."""
    cache, persistence, rebuilds = wired
    _write_populated(cache.local_path("db", "job-1"))

    with graph_routes.graph_lease("job-1") as (first, _):
        pass
    with graph_routes.graph_lease("job-1") as (second, _):
        pass

    assert first is second
    assert first.read_only is True
    persistence.download_if_exists.assert_not_called()
    assert rebuilds == []


def test_unreadable_local_file_is_rebuilt(wired):
    cache, persistence, rebuilds = wired
    path = cache.local_path("db", "job-1")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"not a database")

    with graph_routes.graph_lease("job-1") as (store, _):
        assert _ids(store) == ["built"]
    assert rebuilds == [False]


def test_served_handle_rejects_writes(wired):
    with graph_routes.graph_lease("job-1") as (store, _):
        with pytest.raises(RuntimeError, match="read-only"):
            store.execute("CREATE (:Foo {id: 'x'})")


@pytest.fixture
def rebuild_client(wired):
    app = FastAPI()
    app.include_router(graph_routes.router)
    return TestClient(app)


def test_rebuild_endpoint_disabled_by_default(wired, rebuild_client, monkeypatch):
    _, persistence, rebuilds = wired
    monkeypatch.delenv(graph_routes.RAW_QUERY_ENV_FLAG, raising=False)

    resp = rebuild_client.post("/api/v1/assessments/job-1/graph/rebuild")

    assert resp.status_code == 403
    assert resp.json()["detail"] == graph_routes.DISABLED_DETAIL
    assert graph_routes.RAW_QUERY_ENV_FLAG not in resp.text
    assert rebuilds == []


def test_rebuild_endpoint_releases_writer(wired, rebuild_client, monkeypatch):
    """After a forced rebuild the next read gets a fresh read-only handle."""
    cache, persistence, rebuilds = wired
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    with graph_routes.graph_lease("job-1"):
        pass
    assert cache.is_open("db", "job-1")

    resp = rebuild_client.post("/api/v1/assessments/job-1/graph/rebuild")

    assert resp.status_code == 200
    assert resp.json()["status"] == "rebuilt"
    assert not cache.is_open("db", "job-1")
    assert persistence.upload.call_count == 2
    with graph_routes.graph_lease("job-1") as (store, _):
        assert store.read_only is True
    assert rebuilds == [False, False]
    assert os.path.exists(cache.local_path("db", "job-1"))
