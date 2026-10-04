"""Read-only handles, query timeout, row cap and gating of the raw query endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.graph import GraphStoreCache
from src.graph.store import GraphStore


def _make_graph(path: str, rows: int = 3) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(path)
    store.execute("CREATE NODE TABLE Foo(id STRING PRIMARY KEY, val INT64)")
    store.execute(
        "UNWIND range(1, $n) AS i CREATE (:Foo {id: 'f' + string(i), val: i})", {"n": rows}
    )
    store.close()


@pytest.fixture
def graph_path(tmp_path):
    path = str(tmp_path / "ro.lbug")
    _make_graph(path, rows=3)
    return path


def test_read_only_handle_rejects_writes(graph_path):
    store = GraphStore(graph_path, read_only=True)
    try:
        assert store.query("MATCH (f:Foo) RETURN count(f) AS c")[0]["c"] == 3
        with pytest.raises(RuntimeError, match="read-only"):
            store.execute("CREATE (:Foo {id: 'x', val: 0})")
        with pytest.raises(RuntimeError, match="read-only"):
            store.execute("MATCH (f:Foo) SET f.val = 0")
    finally:
        store.close()


def test_query_timeout_interrupts_long_queries(graph_path):
    store = GraphStore(graph_path, read_only=True, query_timeout_ms=100)
    try:
        with pytest.raises(RuntimeError, match="Interrupted"):
            store.query(
                "UNWIND range(1, 100000) AS x UNWIND range(1, 100000) AS y RETURN sum(x + y) AS s"
            )
    finally:
        store.close()


def test_query_bounded_caps_rows_and_reports_truncation(tmp_path):
    path = str(tmp_path / "many.lbug")
    _make_graph(path, rows=25)
    store = GraphStore(path, read_only=True)
    try:
        rows, truncated = store.query_bounded("MATCH (f:Foo) RETURN f.id AS id", max_rows=10)
        assert len(rows) == 10
        assert truncated is True
        rows, truncated = store.query_bounded("MATCH (f:Foo) RETURN f.id AS id", max_rows=25)
        assert len(rows) == 25
        assert truncated is False
    finally:
        store.close()


def test_cache_read_only_handles_carry_timeout_and_switch_modes(tmp_path):
    cache = GraphStoreCache(max_size=2, base_dir=str(tmp_path))
    _make_graph(cache.local_path("db", "job"))
    reader = cache.get("db", "job", read_only=True)
    assert reader.read_only is True
    assert cache.get("db", "job", read_only=True) is reader
    with pytest.raises(RuntimeError, match="read-only"):
        reader.execute("CREATE (:Foo {id: 'x', val: 0})")
    # Asking for a writer closes the reader (one handle per file per process).
    writer = cache.get("db", "job")
    assert writer.read_only is False
    assert cache.peek("db", "job") is writer
    cache.close_all()


# --- endpoint -----------------------------------------------------------------


@pytest.fixture
def raw_client(tmp_path):
    path = str(tmp_path / "ep.lbug")
    _make_graph(path, rows=1_005)
    store = GraphStore(path, read_only=True, query_timeout_ms=10_000)
    calls = {"graph": 0}

    def _graph():
        calls["graph"] += 1
        return store, "db"

    app = FastAPI()
    app.include_router(graph_routes.router)
    app.dependency_overrides[graph_routes.get_graph_for_job] = _graph
    yield TestClient(app), calls
    app.dependency_overrides.clear()
    store.close()


def _post(client, cypher, params=None):
    return client.post(
        "/api/v1/assessments/job-1/graph/query", json={"cypher": cypher, "params": params}
    )


def test_raw_query_disabled_by_default(raw_client, monkeypatch):
    client, calls = raw_client
    monkeypatch.delenv(graph_routes.RAW_QUERY_ENV_FLAG, raising=False)
    resp = _post(client, "MATCH (f:Foo) RETURN f.id AS id")
    assert resp.status_code == 403
    assert graph_routes.RAW_QUERY_ENV_FLAG in resp.json()["detail"]
    # The graph is not even resolved when the endpoint is disabled.
    assert calls["graph"] == 0


@pytest.mark.parametrize("value", ["0", "true", ""])
def test_raw_query_requires_exact_flag_value(raw_client, monkeypatch, value):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, value)
    assert _post(client, "MATCH (f:Foo) RETURN f.id AS id").status_code == 403


def test_raw_query_enabled_returns_capped_rows(raw_client, monkeypatch):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = _post(client, "MATCH (f:Foo) RETURN f.id AS id ORDER BY id")
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 1_000
    assert len(body["rows"]) == 1_000
    assert body["truncated"] is True
    assert body["columns"] == ["id"]


def test_raw_query_small_result_not_truncated(raw_client, monkeypatch):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = _post(client, "MATCH (f:Foo) WHERE f.val <= $n RETURN f.val AS v", {"n": 2})
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 2
    assert resp.json()["truncated"] is False


@pytest.mark.parametrize(
    "cypher",
    [
        "CREATE (:Foo {id: 'x', val: 0})",
        "MATCH (f:Foo) SET f.val = 0",
        "MATCH (f:Foo) RETURN f; MATCH (f:Foo) DELETE f",
        "LOAD FROM 'x.csv' RETURN *",
    ],
)
def test_raw_query_rejects_disallowed_statements(raw_client, monkeypatch, cypher):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = _post(client, cypher)
    assert resp.status_code == 400
    assert "read queries" in resp.json()["detail"] or "single statement" in resp.json()["detail"]
