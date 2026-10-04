"""Read-only handles, query timeout, row cap and gating of the raw query endpoint."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.graph.cypher_guard import DisallowedStatementError, validate_read_only_cypher
from src.graph.store import READ_QUERY_TIMEOUT_MS, GraphStore


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


def test_filter_covers_statements_the_read_only_handle_accepts(graph_path, tmp_path):
    """The read-only handle is not the control for these statements: the filter is.

    Each statement below runs on a read-only handle, so the raw endpoint must
    rely on validate_read_only_cypher to refuse it.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    statements = [
        f"COPY (MATCH (f:Foo) RETURN f.id) TO '{out_dir / 'rows.csv'}'",
        f"EXPORT DATABASE '{out_dir / 'db'}'",
        "CALL threads=2",
    ]
    store = GraphStore(graph_path, read_only=True)
    try:
        for cypher in statements:
            store.execute(cypher)  # accepted by the read-only handle
            with pytest.raises(DisallowedStatementError):
                validate_read_only_cypher(cypher)
    finally:
        store.close()


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
    assert resp.json()["detail"] == graph_routes.DISABLED_DETAIL
    assert graph_routes.RAW_QUERY_ENV_FLAG not in resp.text
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


@pytest.mark.parametrize(
    ("cypher", "rows", "truncated"),
    [
        ("MATCH (f:Foo) RETURN f.id AS id LIMIT 3", 3, False),
        ("MATCH (f:Foo) RETURN f.id AS id ORDER BY id LIMIT 5000", 1_000, True),
        ("MATCH (f:Foo) RETURN f.id AS id ORDER BY id SKIP 1000", 5, False),
        ("MATCH (f:Foo) RETURN f.id AS id // trailing comment", 1_000, True),
        ("MATCH (f:Foo) RETURN f.id AS id;", 1_000, True),
    ],
)
def test_raw_query_limit_reaches_the_engine(raw_client, monkeypatch, cypher, rows, truncated):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    seen = []
    original = graph_routes.run_raw_query

    def _spy(db_path, statement, *args):
        seen.append(statement)
        return original(db_path, statement, *args)

    monkeypatch.setattr(graph_routes, "run_raw_query", _spy)
    resp = _post(client, cypher)
    assert resp.status_code == 200, resp.text
    assert resp.json()["row_count"] == rows
    assert resp.json()["truncated"] is truncated
    # The statement sent to the engine carries a LIMIT of at most cap + 1.
    assert seen and seen[0].rstrip().split()[-2:][0].upper() == "LIMIT"
    assert int(seen[0].rstrip().split()[-1]) <= 1_001


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (f:Foo) RETURN f.id AS id LIMIT $n",
        "MATCH (f:Foo) RETURN f.id AS id UNION MATCH (f:Foo) RETURN f.id AS id",
        "RETURN " + "(" * 200 + "1" + ")" * 200,
        "RETURN " + "CASE WHEN true THEN " * 40 + "1" + " END" * 40,
        "MATCH (f:Foo) RETURN f.id AS id " + " " * 10_000,
    ],
)
def test_raw_query_rejects_unbounded_or_oversized_statements(raw_client, monkeypatch, cypher):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = _post(client, cypher, {"n": 5})
    assert resp.status_code == 400


def test_raw_query_rejects_large_request_bodies(raw_client, monkeypatch):
    client, calls = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    big = {
        "cypher": "RETURN 1 AS x",
        "params": {"pad": "x" * (graph_routes.MAX_QUERY_REQUEST_BYTES + 1)},
    }
    resp = client.post("/api/v1/assessments/job-1/graph/query", json=big)
    assert resp.status_code == 413


def test_raw_query_validates_the_body(raw_client, monkeypatch):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = client.post("/api/v1/assessments/job-1/graph/query", json={"params": {}})
    assert resp.status_code == 422


# --- live server: timeout fires and the server stays responsive ---------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int, path: str, payload: dict | None = None, timeout: float = 60
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        if payload is None:
            conn.request("GET", path)
        else:
            body = json.dumps(payload)
            conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def test_expensive_read_times_out_while_server_stays_responsive(tmp_path, monkeypatch):
    path = str(tmp_path / "slow.lbug")
    _make_graph(path, rows=2_000)
    store = GraphStore(path, read_only=True, query_timeout_ms=READ_QUERY_TIMEOUT_MS)
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")

    app = FastAPI()
    app.include_router(graph_routes.router)
    app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (store, "db")

    @app.get("/ping")
    def ping():
        return {"ok": True}

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="none")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        query_url = "/api/v1/assessments/job-1/graph/query"
        cartesian = (
            "MATCH (a:Foo), (b:Foo), (c:Foo), (d:Foo) "
            "RETURN sum(a.val * b.val + c.val - d.val) AS s"
        )
        results: list[tuple[tuple[int, dict], float]] = []

        def _slow():
            started = time.monotonic()
            reply = _request(port, query_url, {"cypher": cartesian})
            results.append((reply, time.monotonic() - started))

        workers = [
            threading.Thread(target=_slow) for _ in range(graph_routes.MAX_CONCURRENT_RAW_QUERIES)
        ]
        for worker in workers:
            worker.start()
        time.sleep(1.0)

        # Other requests are served meanwhile; extra raw queries are refused
        # quickly instead of piling up.
        for _ in range(3):
            started = time.monotonic()
            assert _request(port, "/ping", timeout=5) == (200, {"ok": True})
            status, body = _request(port, query_url, {"cypher": "RETURN 1 AS x"}, timeout=5)
            assert status == 429
            assert time.monotonic() - started < 2
            assert all(w.is_alive() for w in workers)

        for worker in workers:
            worker.join(60)
        timeout_s = READ_QUERY_TIMEOUT_MS / 1000
        assert len(results) == len(workers)
        for (status, body), elapsed in results:
            assert status == 504
            assert body["detail"] == graph_routes.TIMEOUT_DETAIL
            assert timeout_s - 1 <= elapsed < timeout_s + 10

        # Slots are released: a normal query works again.
        status, body = _request(port, query_url, {"cypher": "MATCH (f:Foo) RETURN count(f) AS c"})
        assert (status, body["rows"]) == (200, [{"c": 2_000}])
    finally:
        server.should_exit = True
        thread.join(10)
        store.close()


# --- request shape, value generators, concurrency, curated errors ------------


@pytest.mark.parametrize(
    "content_type",
    [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"],
)
@pytest.mark.parametrize("path", ["/graph/query", "/graph/rebuild"])
def test_post_endpoints_require_json_content_type(raw_client, monkeypatch, content_type, path):
    client, calls = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    headers = {"content-type": content_type} if content_type else {}
    body = json.dumps({"cypher": "RETURN 1 AS x"})
    resp = client.post(f"/api/v1/assessments/job-1{path}", content=body, headers=headers)
    if content_type is None:
        # httpx sends no Content-Type for raw content.
        assert "content-type" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 415
    assert calls["graph"] == 0


def test_json_content_type_with_charset_is_accepted(raw_client, monkeypatch):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = client.post(
        "/api/v1/assessments/job-1/graph/query",
        content=json.dumps({"cypher": "RETURN 1 AS x"}),
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"] == [{"x": 1}]


def _doubling(steps: int, seed: str, step: str) -> str:
    return (
        f"WITH {seed} AS s "
        + " ".join(f"WITH {step} AS s" for _ in range(steps))
        + " RETURN size(s) AS n"
    )


@pytest.mark.parametrize(
    "cypher",
    [
        _doubling(30, "'xxxxxxxxxxxxxxxx'", "s + s"),
        _doubling(26, "[1, 2, 3, 4, 5, 6, 7, 8]", "list_concat(s, s)"),
    ],
)
def test_raw_query_memory_is_capped_in_a_child_process(raw_client, monkeypatch, cypher):
    """Values that grow inside the query are stopped by the worker's memory cap."""
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    started = time.monotonic()
    resp = _post(client, cypher)
    assert resp.status_code == 400
    assert "memory" in resp.json()["detail"]
    assert time.monotonic() - started < 15


@pytest.mark.parametrize(
    "cypher",
    [
        "RETURN size(range(1, 200000000)) AS n",
        "UNWIND range(1, 1001 * 1000) AS x RETURN count(x) AS n",
        "RETURN size(repeat('x', 1000000000)) AS n",
        "RETURN size(lpad('x', 1000000000, 'y')) AS n",
        "RETURN size(rpad('x', 1000000000, 'y')) AS n",
    ],
)
def test_raw_query_rejects_large_generated_values(raw_client, monkeypatch, cypher):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = _post(client, cypher)
    assert resp.status_code == 400


def test_raw_query_small_generated_values_work(raw_client, monkeypatch):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    resp = _post(client, "UNWIND range(1, 10) AS x RETURN sum(x) AS s, repeat('ab', 3) AS r")
    assert resp.status_code == 200
    assert resp.json()["rows"] == [{"s": 55, "r": "ababab"}]


def test_raw_query_slots_return_429_when_full(raw_client, monkeypatch):
    client, _ = raw_client
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    held = []
    for _ in range(graph_routes.MAX_CONCURRENT_RAW_QUERIES):
        assert graph_routes._raw_query_slots.acquire(blocking=False)
        held.append(True)
    try:
        resp = _post(client, "RETURN 1 AS x")
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "1"
    finally:
        for _ in held:
            graph_routes._raw_query_slots.release()
    assert _post(client, "RETURN 1 AS x").status_code == 200


@pytest.mark.parametrize(
    ("message", "status", "detail"),
    [
        ("Interrupted.", 504, graph_routes.TIMEOUT_DETAIL),
        ("Buffer manager exception: Unable to allocate memory!", 503, graph_routes.MEMORY_DETAIL),
    ],
)
def test_curated_endpoint_maps_engine_limits(raw_client, monkeypatch, message, status, detail):
    client, _ = raw_client

    def _boom(store):
        raise RuntimeError(message)

    monkeypatch.setattr(graph_routes.graph_queries, "risk_hotspots", _boom)
    resp = client.get("/api/v1/assessments/job-1/graph/risks")
    assert resp.status_code == status
    assert resp.json()["detail"] == detail
