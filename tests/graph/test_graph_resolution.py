"""Resolution flow: local cache -> download -> build+upload (self-healing)."""

from unittest.mock import MagicMock

from src.api.routes import graph as graph_routes


def test_build_and_upload_on_download_miss(monkeypatch, tmp_path):
    """When nothing is cached, the graph is built then uploaded."""
    store = MagicMock()
    store.is_populated.return_value = False
    cache = MagicMock()
    cache.get.return_value = store
    cache.local_path.return_value = str(tmp_path / "context.lbug")

    persistence = MagicMock()
    persistence.download_if_exists.return_value = False  # cache miss

    built = {}
    monkeypatch.setattr(graph_routes, "graph_cache", cache)
    monkeypatch.setattr(graph_routes, "artifact_store", MagicMock())
    monkeypatch.setattr(graph_routes, "graph_persistence", persistence)
    monkeypatch.setattr(graph_routes, "_resolve_db_name", lambda job_id: "db")
    monkeypatch.setattr(
        graph_routes,
        "rebuild_graph",
        lambda *a, **k: built.setdefault("called", True) or {"nodes_created": 1},
    )

    graph_routes._get_graph("job-1")

    assert built.get("called") is True
    persistence.upload.assert_called_once()


def test_download_hit_skips_build(monkeypatch, tmp_path):
    """When the graph is in the store, download and skip rebuild."""
    store = MagicMock()
    # Not populated on first check (fresh handle); populated after reopen.
    store.is_populated.return_value = True
    cache = MagicMock()
    cache.get.return_value = store
    cache.reopen.return_value = store
    cache.local_path.return_value = str(tmp_path / "context.lbug")

    persistence = MagicMock()
    persistence.download_if_exists.return_value = True  # cache hit

    monkeypatch.setattr(graph_routes, "graph_cache", cache)
    monkeypatch.setattr(graph_routes, "artifact_store", MagicMock())
    monkeypatch.setattr(graph_routes, "graph_persistence", persistence)
    monkeypatch.setattr(graph_routes, "_resolve_db_name", lambda job_id: "db")

    called = {"rebuild": False}
    monkeypatch.setattr(
        graph_routes,
        "rebuild_graph",
        lambda *a, **k: called.__setitem__("rebuild", True),
    )

    # Force the "not locally populated yet" path so download is attempted:
    # first is_populated() call returns False, then True after reopen.
    store.is_populated.side_effect = [False, True]

    graph_routes._get_graph("job-1")

    assert called["rebuild"] is False
    persistence.upload.assert_not_called()
