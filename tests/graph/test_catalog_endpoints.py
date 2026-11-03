# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The graph query catalog over the API (#307 slice 3).

``GET /graph/catalog`` lists the catalog's entries with their parameters and
each parameter's choices on the job's graph; ``POST /graph/catalog/{entry_id}``
runs one entry with bound parameters. Neither route takes Cypher.
"""

from __future__ import annotations

import dataclasses
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.graph import catalog
from src.graph import store as graph_store_module
from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_schema_design,
)
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore, ReadLimits

BASE = "/api/v1/assessments/job-1/graph/catalog"
JSON = {"Content-Type": "application/json"}
MARKUP = "<script>alert('x')</script> & \"quoted\" | pipe"


@pytest.fixture
def populated_store(
    tmp_path, sample_collector_output, sample_assignment, sample_schema_design_output
):
    store = GraphStore(str(tmp_path / "catalog.lbug"))
    initialize_schema(store)
    populate_from_collector(sample_collector_output, store)
    populate_from_assignment(sample_assignment, store)
    design = json.loads(json.dumps(sample_schema_design_output))
    design["trade_offs"][0]["description"] = MARKUP
    populate_from_schema_design(design, "documentdb", 1, store)
    yield store
    store.close()


def _client(store) -> TestClient:
    app = FastAPI()
    app.include_router(graph_routes.router)
    app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (store, "db")
    return TestClient(app)


@pytest.fixture
def client(populated_store):
    yield _client(populated_store)


def _run(client: TestClient, entry_id: str, params: dict | None = None, **kwargs):
    body = {} if params is None else {"params": params}
    return client.post(f"{BASE}/{entry_id}", content=json.dumps(body), headers=JSON, **kwargs)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_returns_every_entry_in_catalog_order(client):
    resp = client.get(BASE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == "job-1"
    assert [e["id"] for e in body["entries"]] == [e.id for e in catalog.entries()]
    census = body["entries"][0]
    assert census["id"] == "node_census"
    assert census["params"] == []
    assert census["max_rows"] == 50
    assert census["title"] and census["question"] and census["section"] == "summary"


def test_list_gives_each_parameter_its_choices_from_the_graph(client):
    entries = {e["id"]: e for e in client.get(BASE).json()["entries"]}
    (table,) = entries["table_blast_radius"]["params"]
    assert table["name"] == "table_id"
    assert table["kind"] == "table"
    assert table["choices"] == ["customers", "orders"]
    assert table["choices_truncated"] is False
    assert table["example"] and table["description"]
    (engine,) = entries["engine_destinations"]["params"]
    assert engine["choices"] == ["documentdb", "dynamodb"]
    assert entries["access_pattern_queries"]["params"][0]["choices"] == ["DDB-AP-1"]
    needs = entries["access_pattern_queries"]["needs"]
    assert needs == [
        {
            "id": "schema_design",
            "stage": "Schema design, per engine",
            "produces": "`AccessPattern` nodes",
        }
    ]


def test_list_never_carries_cypher(client):
    resp = client.get(BASE)
    assert resp.status_code == 200
    body = resp.text
    assert "cypher" not in body
    assert "MATCH" not in body


def test_list_reads_each_shared_parameter_once(populated_store, monkeypatch):
    calls: list[str] = []
    real = catalog.param_choices

    def spy(store, param):
        calls.append(param.choices_cypher)
        return real(store, param)

    monkeypatch.setattr(catalog, "param_choices", spy)
    assert _client(populated_store).get(BASE).status_code == 200
    assert len(calls) == len(set(calls))


def test_list_says_when_choices_are_cut(client, monkeypatch):
    monkeypatch.setattr(catalog, "MAX_CHOICES", 1)
    entries = {e["id"]: e for e in client.get(BASE).json()["entries"]}
    (table,) = entries["table_blast_radius"]["params"]
    assert table["choices"] == ["customers"]
    assert table["choices_truncated"] is True


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def test_run_an_entry_without_parameters(client):
    resp = _run(client, "engine_distribution")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entry"] == "engine_distribution"
    assert body["job_id"] == "job-1"
    assert body["params"] == {}
    assert body["columns"] == ["engine", "queries"]
    assert {r["engine"]: r["queries"] for r in body["rows"]} == {"dynamodb": 2, "documentdb": 1}
    assert body["row_count"] == 2
    assert body["truncated"] is False
    assert body["max_rows"] == 50
    assert body["unmet_needs"] == [] and body["explanation"] is None
    assert {n["id"] for n in body["projection"]["nodes"]} == {"dynamodb", "documentdb"}


def test_run_an_entry_with_a_bound_parameter(client):
    resp = _run(client, "table_blast_radius", {"table_id": "customers"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["params"] == {"table_id": "customers"}
    assert body["row_count"] == len(body["rows"]) >= 1
    assert {r["query_id"] for r in body["rows"]} == {"q2"}


def test_an_empty_body_runs_an_entry_without_parameters(client):
    resp = client.post(f"{BASE}/node_census", headers=JSON)
    assert resp.status_code == 200, resp.text
    assert resp.json()["row_count"] >= 1


def test_an_empty_result_names_the_missing_stage(client):
    resp = _run(client, "load_test_regressions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == [] and body["columns"] == []
    assert [n["id"] for n in body["unmet_needs"]] == ["load_test"]
    assert "Load test" in body["explanation"]


def test_customer_text_comes_back_as_data(client):
    resp = _run(client, "query_decisions", {"query_id": "q2"})
    assert resp.status_code == 200, resp.text
    assert [r["description"] for r in resp.json()["rows"]] == [MARKUP]
    assert resp.headers["content-type"].startswith("application/json")


def test_the_row_bound_cuts_and_says_so(client, monkeypatch):
    entry = catalog.get_entry("node_census")
    monkeypatch.setitem(catalog._BY_ID, "node_census", dataclasses.replace(entry, max_rows=1))
    body = _run(client, "node_census").json()
    assert body["row_count"] == 1 and len(body["rows"]) == 1
    assert body["truncated"] is True
    assert body["max_rows"] == 1


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def test_an_unknown_entry_is_404_and_not_echoed(client):
    resp = _run(client, "no_such_entry")
    assert resp.status_code == 404
    assert (
        resp.json()["detail"] == "Unknown catalog entry; GET .../graph/catalog lists the entry ids."
    )
    assert "no_such_entry" not in resp.text


@pytest.mark.parametrize("entry_id", ["Node_Census", "node-census", "a" * 65, "node%20census"])
def test_a_malformed_entry_id_is_refused(client, entry_id):
    resp = _run(client, entry_id)
    assert resp.status_code == 404
    assert resp.json()["detail"].startswith("Unknown catalog entry")
    assert entry_id not in resp.text


@pytest.mark.parametrize("value", [7, 1.5, True, None, ["orders"], {"id": "orders"}])
def test_a_parameter_that_is_not_a_string_is_422(client, value):
    resp = _run(client, "table_blast_radius", {"table_id": value})
    assert resp.status_code == 422, resp.text


def test_a_missing_parameter_is_422_and_named(client):
    resp = _run(client, "table_blast_radius", {})
    assert resp.status_code == 422
    assert "table_id" in resp.text


def test_an_unknown_parameter_is_422_and_not_echoed(client):
    resp = _run(client, "table_blast_radius", {"table_id": "orders", "drop_me_now": "x"})
    assert resp.status_code == 422
    assert "drop_me_now" not in resp.text
    assert "table_id" in resp.text


def test_a_value_outside_the_choices_is_422_and_not_echoed(client):
    resp = _run(client, "table_blast_radius", {"table_id": "not_a_table_here"})
    assert resp.status_code == 422
    assert "not_a_table_here" not in resp.text


def test_a_value_is_checked_when_the_choices_are_cut(client, monkeypatch):
    """Past the listed choices' bound a real value runs and an unknown one is still refused."""
    monkeypatch.setattr(catalog, "MAX_CHOICES", 1)
    resp = _run(client, "table_blast_radius", {"table_id": "orders"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["row_count"] >= 1
    assert _run(client, "table_blast_radius", {"table_id": "nope"}).status_code == 422


@pytest.fixture(scope="module")
def big_store(tmp_path_factory):
    """A synthetic job with more queries than a parameter lists choices."""
    count = catalog.MAX_CHOICES + 100
    store = GraphStore(str(tmp_path_factory.mktemp("big") / "big.lbug"))
    initialize_schema(store)
    populate_from_collector(
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": f"q{i:05d}",
                        "query_text": "SELECT * FROM orders WHERE id = ?",
                        "query_type": "SELECT",
                        "tables_accessed": ["orders"],
                        "calls_per_second": float(count - i),
                    }
                    for i in range(count)
                ]
            }
        },
        store,
    )
    yield store
    store.close()


def test_a_query_past_the_listed_choices_is_still_checked(big_store):
    client = _client(big_store)
    entries = {e["id"]: e for e in client.get(BASE).json()["entries"]}
    (query,) = entries["query_provenance"]["params"]
    assert query["choices_truncated"] is True
    assert len(query["choices"]) == catalog.MAX_CHOICES
    last = f"q{catalog.MAX_CHOICES + 99:05d}"
    assert last not in query["choices"]
    resp = _run(client, "query_provenance", {"query_id": last})
    assert resp.status_code == 200, resp.text
    assert [r["query_id"] for r in resp.json()["rows"]] == [last]
    resp = _run(client, "query_provenance", {"query_id": "q99999"})
    assert resp.status_code == 422
    assert "q99999" not in resp.text


def test_an_overlong_value_is_422(client):
    resp = _run(client, "table_blast_radius", {"table_id": "x" * 2000})
    assert resp.status_code == 422
    assert "x" * 2000 not in resp.text


def test_raw_cypher_in_the_body_is_refused(client):
    resp = client.post(
        f"{BASE}/node_census",
        content=json.dumps({"cypher": "MATCH (n) DETACH DELETE n", "params": {}}),
        headers=JSON,
    )
    assert resp.status_code == 422
    assert "DETACH" not in resp.text


def test_a_deeply_nested_body_is_422_not_500(client):
    """A body of nothing but ``[`` overflows the JSON parser's recursion, not the server."""
    body = b"[" * (graph_routes.MAX_QUERY_REQUEST_BYTES - 1)
    resp = client.post(f"{BASE}/node_census", content=body, headers=JSON)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "The request body is not valid JSON."


def test_a_body_that_is_not_json_is_422(client):
    resp = client.post(f"{BASE}/node_census", content=b"{not json", headers=JSON)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Request limits
# ---------------------------------------------------------------------------


def test_a_non_json_content_type_is_415(client):
    resp = client.post(f"{BASE}/node_census", content=b"{}", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415


def test_a_declared_oversize_body_is_413(client):
    big = json.dumps({"params": {"table_id": "x" * graph_routes.MAX_QUERY_REQUEST_BYTES}})
    resp = client.post(f"{BASE}/table_blast_radius", content=big, headers=JSON)
    assert resp.status_code == 413


def test_a_streamed_oversize_body_is_413(client):
    chunk = b" " * 4096

    def body():
        for _ in range(graph_routes.MAX_QUERY_REQUEST_BYTES // len(chunk) + 2):
            yield chunk

    resp = client.post(f"{BASE}/node_census", content=body(), headers=JSON)
    assert resp.status_code == 413


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_full_set_of_catalog_slots_is_429(client, method, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(graph_routes, "_catalog_slots", slots)
    if method == "GET":
        resp = client.get(BASE)
    else:
        resp = _run(client, "node_census")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1"


def test_the_catalog_cap_leaves_read_slots_to_the_curated_views():
    """Half the handle's read slots at most, and at least one."""
    assert [graph_routes.catalog_request_cap(n) for n in (1, 2, 3, 4, 8)] == [1, 1, 1, 2, 4]
    reads = ReadLimits.from_env().max_concurrent_reads
    assert graph_routes.MAX_CONCURRENT_CATALOG_REQUESTS == graph_routes.catalog_request_cap(reads)
    assert graph_routes.MAX_CONCURRENT_CATALOG_REQUESTS < reads


def test_a_ui_view_answers_while_every_catalog_slot_is_busy(
    tmp_path, sample_collector_output, sample_assignment, monkeypatch
):
    """Catalog runs that hold read slots never take the ones a curated view needs."""
    path = str(tmp_path / "ro.lbug")
    writer = GraphStore(path)
    initialize_schema(writer)
    populate_from_collector(sample_collector_output, writer)
    populate_from_assignment(sample_assignment, writer)
    writer.close()
    limits = ReadLimits(buffer_pool_bytes=64 * 1024 * 1024, query_threads=1, max_concurrent_reads=4)
    reader = GraphStore(path, read_only=True, read_limits=limits)
    cap = graph_routes.catalog_request_cap(limits.max_concurrent_reads)
    monkeypatch.setattr(graph_routes, "_catalog_slots", threading.BoundedSemaphore(cap))
    # A view that cannot get a read slot gives up fast (503) instead of after 30 s.
    monkeypatch.setattr(graph_store_module, "READ_SLOT_WAIT_S", 0.5)
    entered = threading.Semaphore(0)
    release = threading.Event()

    def holding_run(store, entry, params=None):
        with store._connection():  # holds one read slot, like a long statement
            entered.release()
            release.wait(10)
        return catalog.CatalogResult(rows=[], truncated=False)

    monkeypatch.setattr(catalog, "run_entry", holding_run)
    monkeypatch.setattr(catalog, "unmet_needs", lambda store, entry: [])
    client = _client(reader)
    statuses: list[int] = []

    def run_one() -> None:
        statuses.append(_run(client, "node_census").status_code)

    workers = [threading.Thread(target=run_one) for _ in range(limits.max_concurrent_reads)]
    try:
        for worker in workers:
            worker.start()
        for _ in range(cap):
            assert entered.acquire(timeout=10), "a catalog run never started"
        assert _run(client, "node_census").status_code == 429
        view = client.get("/api/v1/assessments/job-1/graph/risks")
        assert view.status_code == 200, view.text
    finally:
        release.set()
        for worker in workers:
            worker.join(10)
        reader.close()
    assert sorted(statuses) == [200] * cap + [429] * (len(workers) - cap)


# ---------------------------------------------------------------------------
# Engine errors never echo the statement
# ---------------------------------------------------------------------------


class _FailingStore:
    def __init__(self, message: str):
        self.message = message

    def query_bounded(self, cypher, params=None, max_rows=1000):
        raise RuntimeError(f"{self.message}: {cypher}")


@pytest.mark.parametrize(
    ("message", "status"),
    [("Interrupted", 504), ("Buffer manager exception", 503)],
)
def test_engine_limits_map_to_neutral_errors(message, status):
    resp = _run(_client(_FailingStore(message)), "node_census")
    assert resp.status_code == status
    assert "MATCH" not in resp.text


def test_an_unexpected_engine_error_does_not_echo_the_statement():
    client = TestClient(_app_with(_FailingStore("Binder exception")), raise_server_exceptions=False)
    resp = _run(client, "node_census")
    assert resp.status_code == 500
    assert "MATCH" not in resp.text
    assert "Traceback" not in resp.text


def _app_with(store) -> FastAPI:
    app = FastAPI()
    app.include_router(graph_routes.router)
    app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (store, "db")
    return app


_BAD_REQUESTS = [
    ("unknown entry", "no_such_entry", json.dumps({}).encode(), JSON, 404),
    ("non-string value", "table_blast_radius", b'{"params": {"table_id": 7}}', JSON, 422),
    ("unknown parameter", "table_blast_radius", b'{"params": {"nope": "x"}}', JSON, 422),
    ("missing parameter", "table_blast_radius", b'{"params": {}}', JSON, 422),
    ("invalid JSON", "node_census", b"{not json", JSON, 422),
    ("deep nesting", "node_census", b"[" * 60_000, JSON, 422),
    (
        "oversize body",
        "node_census",
        b" " * (graph_routes.MAX_QUERY_REQUEST_BYTES + 1),
        JSON,
        413,
    ),
    ("wrong content type", "node_census", b"{}", {"Content-Type": "text/plain"}, 415),
]


@pytest.mark.parametrize(
    ("entry_id", "body", "headers", "status"),
    [case[1:] for case in _BAD_REQUESTS],
    ids=[case[0] for case in _BAD_REQUESTS],
)
def test_a_bad_request_takes_no_slot_and_leases_no_graph(
    populated_store, monkeypatch, entry_id, body, headers, status
):
    """Validation answers before a catalog slot is taken or the job's graph is opened."""
    leased: list[bool] = []

    def lease():
        leased.append(True)
        return (populated_store, "db")

    # Every catalog slot is taken: a request that needed one would get 429.
    full = threading.BoundedSemaphore(1)
    full.acquire()
    monkeypatch.setattr(graph_routes, "_catalog_slots", full)
    app = FastAPI()
    app.include_router(graph_routes.router)
    app.dependency_overrides[graph_routes.get_graph_for_job] = lease
    resp = TestClient(app).post(f"{BASE}/{entry_id}", content=body, headers=headers)
    assert resp.status_code == status, resp.text
    assert leased == []


class _CountingStore:
    """Counts the statements a request runs on the graph."""

    def __init__(self, store: GraphStore):
        self.store = store
        self.statements = 0

    def query_bounded(self, cypher, params=None, max_rows=1000):
        self.statements += 1
        return self.store.query_bounded(cypher, params, max_rows=max_rows)


def test_a_request_runs_the_statement_count_the_docs_state(populated_store):
    """The README and cookbook bound a request's time by these counts (10 s each)."""
    counting = _CountingStore(populated_store)
    client = _client(counting)
    entries = client.get(BASE).json()["entries"]
    distinct = {p for e in catalog.entries() for p in e.params}
    assert counting.statements == len(distinct) == 6, "update the docs' '6 today'"
    for entry in entries:
        params = {p["name"]: (p["choices"] or [p["example"]])[0] for p in entry["params"]}
        counting.statements = 0
        resp = _run(client, entry["id"], params)
        assert resp.status_code in (200, 422), resp.text
        assert counting.statements <= 3, f"{entry['id']}: update the docs' 'at most 3'"
