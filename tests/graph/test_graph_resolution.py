"""Resolution flow: local cache -> download -> build+upload (self-healing).

Every handle handed to a read endpoint is read-only; building uses a separate,
short-lived read-write handle that is closed before the upload.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    monkeypatch.setattr(graph_routes, "initialize_schema", lambda store: None)
    monkeypatch.setattr(graph_routes, "rebuild_graph", _fake_rebuild(rebuilds))
    yield cache, persistence, rebuilds
    cache.close_all()


def test_build_and_upload_on_download_miss(wired):
    """When nothing is cached, the graph is built on a writer, uploaded, then served read-only."""
    cache, persistence, rebuilds = wired

    store, db_name = graph_routes._get_graph("job-1")

    assert db_name == "db"
    assert rebuilds == [False]
    persistence.upload.assert_called_once()
    assert store.read_only is True
    assert store.query("MATCH (f:Foo) RETURN f.id AS id") == [{"id": "built"}]


def test_download_hit_skips_build(wired):
    """When the graph is in the store, download and skip rebuild."""
    cache, persistence, rebuilds = wired

    def _download(db_name, job_id, local_path):
        _write_populated(local_path)
        return True

    persistence.download_if_exists.side_effect = _download

    store, _ = graph_routes._get_graph("job-1")

    assert rebuilds == []
    persistence.upload.assert_not_called()
    assert store.read_only is True
    assert store.query("MATCH (f:Foo) RETURN f.id AS id") == [{"id": "a"}]


def test_existing_local_graph_is_reused_read_only(wired):
    """A populated local graph is opened read-only without download or rebuild."""
    cache, persistence, rebuilds = wired
    _write_populated(cache.local_path("db", "job-1"))

    first, _ = graph_routes._get_graph("job-1")
    second, _ = graph_routes._get_graph("job-1")

    assert first is second
    assert first.read_only is True
    persistence.download_if_exists.assert_not_called()
    assert rebuilds == []


def test_served_handle_rejects_writes(wired):
    store, _ = graph_routes._get_graph("job-1")
    with pytest.raises(RuntimeError, match="read-only"):
        store.execute("CREATE (:Foo {id: 'x'})")


def test_rebuild_endpoint_releases_writer(wired, monkeypatch):
    """After a forced rebuild the next read gets a read-only handle."""
    import asyncio

    cache, persistence, rebuilds = wired
    monkeypatch.setattr(graph_routes, "_get_database_name", lambda job_id: "db")

    result = asyncio.run(graph_routes.rebuild_assessment_graph("job-1"))

    assert result["status"] == "rebuilt"
    assert cache.peek("db", "job-1") is None
    persistence.upload.assert_called_once()
    store, _ = graph_routes._get_graph("job-1")
    assert store.read_only is True
    assert rebuilds == [False]
