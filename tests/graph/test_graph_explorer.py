# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The experimental graph explorer (#308): its bounded views and its flag-gated routes.

Tables and engines are nodes; each query is an edge from each table it touches
to its engine, and the queries of one table and engine pair form a bundle.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.api.routes import graph_explorer as explorer_routes
from src.graph import explorer
from src.graph.explorer_overlays import EMPTY, build_overlays, query_path, wave_query_ids
from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_schema_design,
)
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.shared.engine_names import display_engine

FLAG = explorer_routes.EXPLORER_ENV_FLAG

# A synthesis report, assignment and access-pattern record shaped like the
# pipeline's, for the fixture's three queries: q1 (orders, 423 calls/s) and
# q3 (an INSERT into orders, 15) on dynamodb; q2 (a join of orders and
# customers, 89) on documentdb.
REPORT = {
    "facts": {
        "waves": [
            {"wave": 1, "engines": ["documentdb"], "migrating": True},
            {"wave": 2, "engines": ["elasticache"], "migrating": False},
            {"wave": 3, "engines": ["dynamodb"], "migrating": True},
        ],
        "engines": [
            {
                "engine": "dynamodb",
                "monthly_cost_usd": 76.75,
                "role": "Migration target",
                "workload_percent": 66.7,
                "rationale": "Key-value lookups fit DynamoDB.",
            },
            {"engine": "documentdb", "monthly_cost_usd": 192.12},
            {"engine": "elasticache", "monthly_cost_usd": None},
        ],
    },
    # The wave plan: a homogeneous first wave every moving query cuts over in.
    "migration_waves": [
        {
            "wave": 1,
            "title": "Move to DocumentDB",
            "rationale": "The whole database moves 1:1.",
            "gate": "Parity validated.",
            "engines": ["documentdb"],
            "homogeneity": "homogeneous",
            "cutover_query_count": 3,
        },
        {
            "wave": 2,
            "title": "Cache hot reads",
            "rationale": "A cache in front.",
            "engines": ["elasticache"],
            "cutover_query_count": None,
        },
        {
            "wave": 3,
            "title": "Move key-value queries to DynamoDB",
            "rationale": "Point lookups move.",
            "engines": ["dynamodb"],
            "cutover_query_count": None,
            "table_groups": [{"tables": ["orders"], "query_count": 2, "kind": "co_dependency"}],
        },
    ],
    "table_sources": [
        {
            "table": "orders",
            "source_of_truth": "dynamodb",
            "write_query_ids": ["q3"],
            "replicas": [{"engine": "documentdb", "read_query_ids": ["q2"]}],
        }
    ],
}
ASSIGNMENT = {
    "query_assignments": [
        {"query_id": "q1", "assigned_engine": "dynamodb", "cache_engine": "elasticache"},
        {"query_id": "q2", "assigned_engine": "documentdb"},
        {"query_id": "q3", "assigned_engine": "dynamodb"},
    ],
    "co_dependency_groups": [["q1", "q3"]],
    "write_groups": {"orders": ["q3"]},
}
ACCESS_PATTERNS = {
    "queries": [{"query_id": "q2", "class": "item_collection", "reason": "orders by customer"}]
}
OVERLAYS = build_overlays(ASSIGNMENT, REPORT, ACCESS_PATTERNS)


@pytest.fixture
def populated_store(
    tmp_path, sample_collector_output, sample_assignment, sample_schema_design_output
):
    store = GraphStore(str(tmp_path / "x.lbug"))
    initialize_schema(store)
    populate_from_collector(sample_collector_output, store)
    populate_from_assignment(sample_assignment, store)
    populate_from_schema_design(sample_schema_design_output, "documentdb", 1, store)
    yield store
    store.close()


@pytest.fixture
def leased():
    """Records whether a request leased the job's graph."""
    return []


@pytest.fixture
def client(populated_store, leased):
    app = FastAPI()
    app.include_router(explorer_routes.status_router)
    app.include_router(explorer_routes.router)

    def lease():
        leased.append(True)
        return (populated_store, "db")

    app.dependency_overrides[graph_routes.get_graph_for_job] = lease
    app.dependency_overrides[explorer_routes.get_overlays_for_job] = lambda: OVERLAYS
    yield TestClient(app)
    app.dependency_overrides.clear()


# --- overlays ---------------------------------------------------------------


def test_overlays_read_waves_units_writers_costs_and_classes():
    o = OVERLAYS
    assert o.views() == ["traffic", "moves-together", "who-writes", "cost"]
    # One move wave per query; the cache wave is where q1's cache goes in front.
    assert o.query_move_wave == {"q1": 3, "q2": 1, "q3": 3}
    assert o.query_cached_in == {"q1": 2}
    assert o.engine_wave == {"documentdb": 1, "elasticache": 2, "dynamodb": 3}
    assert o.cutover == {"wave": 1, "engines": ["documentdb"]}
    assert o.query_units == {"q1": ("unit-1", 2), "q3": ("unit-1", 2)}
    assert o.table_sources["orders"]["writers"] == {"q3"}
    # A cost the report leaves out is not invented.
    assert o.engine_costs == {"dynamodb": 76.75, "documentdb": 192.12}
    assert o.query_classes == {"q2": ("item_collection", "orders by customer")}


def test_missing_artifacts_leave_only_the_traffic_view():
    assert build_overlays(None, None).views() == ["traffic"]
    assert EMPTY.views() == ["traffic"]
    dropped = {
        "query_assignments": [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "cache_engine": "elasticache",
                "cache_dropped": True,
            }
        ]
    }
    # A dropped cache does not put the query in the cache wave.
    o = build_overlays(dropped, REPORT)
    assert o.query_move_wave == {"q1": 3} and o.query_cached_in == {}


# --- links: tables and engines as nodes, queries as bundled edges -----------


def test_options_list_tables_and_engines(populated_store):
    opts = explorer.explorer_options(populated_store, limit=10)
    assert {t.id: t.query_count for t in opts.tables} == {"customers": 1, "orders": 3}
    assert [e.id for e in opts.engines] == ["documentdb", "dynamodb"]
    assert explorer.explorer_options(populated_store, limit=1).tables_truncated


def test_a_table_links_to_one_bundle_per_engine_busiest_first(populated_store):
    out = explorer.links(populated_store, OVERLAYS, "table", "orders", limit=10)
    assert [(b.table, b.engine) for b in out.bundles] == [
        ("orders", "dynamodb"),
        ("orders", "documentdb"),
    ]
    ddb = out.bundles[0]
    # The bundle's figures come from the API: its query count and its calls/s total.
    assert ddb.query_count == 2 and ddb.calls_per_second == 438.0
    assert [q.query_id for q in ddb.queries] == ["q1", "q3"]
    assert ddb.key == "bundle:orders|dynamodb" and not ddb.queries_truncated
    assert {n.key for n in out.nodes} == {"table:orders", "engine:dynamodb", "engine:documentdb"}
    assert out.total_bundles == 2 and not out.truncated


def test_bundles_carry_waves_access_and_units(populated_store):
    ddb, docdb = explorer.links(populated_store, OVERLAYS, "table", "orders").bundles
    assert (ddb.wave, ddb.cached_in_wave, ddb.has_cached_reads, ddb.cutover_wave) == (3, 2, True, 1)
    assert (docdb.wave, docdb.cached_in_wave, docdb.has_cached_reads, docdb.cutover_wave) == (
        1,
        None,
        False,
        None,
    )
    assert ddb.access == "write" and docdb.access == "replica_read"
    q1, q3 = ddb.queries
    assert (q1.access, q3.access) == ("read", "write")
    assert q1.unit == "unit-1" and q1.unit_size == 2
    nodes = {n.key: n for n in explorer.links(populated_store, OVERLAYS, "table", "orders").nodes}
    assert nodes["table:orders"].source_of_truth == "dynamodb"
    assert nodes["engine:documentdb"].monthly_cost_usd == 192.12


def test_a_join_is_an_edge_from_each_of_its_tables_with_one_query_id(populated_store):
    orders = explorer.links(populated_store, OVERLAYS, "table", "orders").bundles[1]
    customers = explorer.links(populated_store, OVERLAYS, "table", "customers").bundles[0]
    assert orders.queries[0].query_id == customers.queries[0].query_id == "q2"
    assert orders.queries[0].tables == ["customers", "orders"]
    assert (orders.table, customers.table) == ("orders", "customers")


def test_links_are_bounded(populated_store, monkeypatch):
    out = explorer.links(populated_store, OVERLAYS, "table", "orders", limit=1)
    assert len(out.bundles) == 1 and out.truncated and out.total_bundles == 2
    monkeypatch.setattr(explorer, "BUNDLE_QUERY_LIMIT", 1)
    ddb = explorer.links(populated_store, OVERLAYS, "table", "orders").bundles[0]
    assert [q.query_id for q in ddb.queries] == ["q1"] and ddb.queries_truncated
    assert ddb.query_count == 2
    with pytest.raises(ValueError):
        explorer.links(populated_store, OVERLAYS, "table", "orders", limit=0)


def test_an_engine_links_to_its_tables(populated_store):
    out = explorer.links(populated_store, OVERLAYS, "engine", "documentdb")
    assert sorted(b.table for b in out.bundles) == ["customers", "orders"]
    assert out.nodes[0].label == display_engine("documentdb")
    assert explorer.links(populated_store, OVERLAYS, "engine", "nope") is None
    assert explorer.links(populated_store, OVERLAYS, "table", "nope") is None


def test_a_wave_links_to_whole_bundles_of_what_moves_in_it(populated_store):
    out = explorer.links(populated_store, OVERLAYS, "wave", "3")
    assert [(b.table, b.engine) for b in out.bundles] == [("orders", "dynamodb")]
    assert out.bundles[0].query_count == 2
    # The cache wave: q1 is cached, so its bundle shows whole (q3 included).
    cached = explorer.links(populated_store, OVERLAYS, "wave", "2")
    assert cached.bundles[0].query_count == 2
    assert explorer.links(populated_store, OVERLAYS, "wave", "9") is None
    assert explorer.links(populated_store, OVERLAYS, "wave", "x") is None
    assert explorer.links(populated_store, EMPTY, "wave", "3") is None


def test_without_a_report_nothing_claims_who_writes(populated_store):
    ddb = explorer.links(populated_store, EMPTY, "table", "orders").bundles[0]
    assert ddb.access is None and ddb.queries[0].access is None and ddb.wave is None


def test_table_names_and_sql_come_back_as_data(tmp_path):
    """A table name that looks like markup comes back verbatim: the UI renders it as text."""
    store = GraphStore(str(tmp_path / "t.lbug"))
    initialize_schema(store)
    name = "<img src=x onerror=alert(1)>"
    populate_from_collector(
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT '<b>'",
                        "query_type": "SELECT",
                        "tables_accessed": [name],
                        "calls_per_second": 1.0,
                    }
                ]
            }
        },
        store,
    )
    populate_from_assignment(
        {
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "dynamodb",
                    "confidence": 0.9,
                    "assignment_reason": "test",
                    "source_tables": [name],
                }
            ]
        },
        store,
    )
    try:
        out = explorer.links(store, EMPTY, "table", name)
        assert out.nodes[0].label == name
        assert out.bundles[0].queries[0].sql_preview == "SELECT '<b>'"
    finally:
        store.close()


# --- search and details -----------------------------------------------------


def test_search_finds_queries_by_sql_or_id(populated_store):
    out = explorer.search(populated_store, OVERLAYS, "INSERT into")
    assert [q.query_id for q in out.results] == ["q3"]
    assert {n.key for n in out.nodes} == {"table:orders", "engine:dynamodb"}
    by_id = explorer.search(populated_store, OVERLAYS, "Q2")
    assert [q.query_id for q in by_id.results] == ["q2"]
    assert explorer.search(populated_store, OVERLAYS, "orders").results[0].query_id == "q1"
    assert explorer.search(populated_store, OVERLAYS, "nothing-like-this").results == []
    with pytest.raises(ValueError):
        explorer.search(populated_store, OVERLAYS, "x")


def test_search_is_bounded(populated_store, monkeypatch):
    monkeypatch.setattr(explorer, "SEARCH_LIMIT", 2)
    out = explorer.search(populated_store, OVERLAYS, "orders")
    assert [q.query_id for q in out.results] == ["q1", "q2"] and out.truncated


def test_query_detail(populated_store):
    d = explorer.query_detail(populated_store, OVERLAYS, "q2")
    assert d.operation == "SELECT" and d.calls_per_second == 89.0
    assert d.engine == "documentdb" and d.wave == 1 and d.cached_in_wave is None
    assert [(s.wave, s.engine, s.kind) for s in d.path] == [(1, "documentdb", "move")]
    assert d.access_pattern_class == "item_collection"
    assert d.assignment_reason == "Join pattern suited for document model"
    assert [(a.table, a.access) for a in d.accesses] == [
        ("customers", "read"),
        ("orders", "replica_read"),
    ]
    assert d.sql_preview.startswith("SELECT o.*, c.name FROM orders o")
    assert explorer.query_detail(populated_store, OVERLAYS, "nope") is None


# --- labels -----------------------------------------------------------------


def test_a_signed_integer_query_id_keeps_its_results_page_short_form():
    """PostgreSQL queryids are signed 64-bit integers: cut at 8 characters with no
    ellipsis they read as small negative numbers ("SELECT -5048447")."""
    qid = "-5048447123456789012"
    assert explorer.short_query_id(qid) == "-50484471234…"
    assert explorer.query_label(qid, "SELECT") == "SELECT -50484471234…"
    assert explorer.short_query_id("q1") == "q1"


def test_sql_preview_is_one_line_and_bounded():
    assert explorer.sql_preview("SELECT *\n  FROM t") == "SELECT * FROM t"
    long = explorer.sql_preview("x" * 500)
    assert len(long) == explorer.SQL_PREVIEW_CHARS and long.endswith("…")
    assert explorer.sql_preview(None) is None


# --- traffic ----------------------------------------------------------------


def test_scales(populated_store):
    t = explorer.traffic_scale(populated_store)
    assert (t.min, t.max) == (15.0, 423.0) and t.min < t.samples[1] < t.max
    b = explorer.bundle_traffic_scale(populated_store)
    # Bundles: customers->documentdb 89, orders->documentdb 89, orders->dynamodb 438.
    assert (b.min, b.max) == (89.0, 438.0)
    assert explorer.cost_scale(OVERLAYS.engine_costs).samples == [76.75, 192.12]
    assert explorer.scale_of([0, None, -1]) is None
    assert explorer.scale_of([1.0, 100.0]).samples == [1.0, 10.0, 100.0]


def test_below_traffic_queries_and_bundles(populated_store):
    out = explorer.below_traffic(
        populated_store, ["q1", "q2", "q3"], [("orders", "dynamodb"), ("orders", "documentdb")], 100
    )
    assert out.hidden_query_ids == ["q2", "q3"]
    assert [(b.table, b.engine) for b in out.hidden_bundles] == [("orders", "documentdb")]
    assert out.hidden_count == 3
    assert explorer.below_traffic(populated_store, ["q1"], [], 0).hidden_count == 0
    too_many = [f"q{i}" for i in range(explorer.MAX_VIEW_QUERIES + 1)]
    with pytest.raises(ValueError):
        explorer.below_traffic(populated_store, too_many, [], 1)


def test_bundle_refs():
    assert explorer.parse_bundle_ref("db.t|x|dynamodb") == ("db.t|x", "dynamodb")
    for bad in ("nobar", "|dynamodb", "t|"):
        with pytest.raises(ValueError):
            explorer.parse_bundle_ref(bad)


# --- routes -----------------------------------------------------------------

PROBE = "/api/v1/experimental/graph-explorer"
ROUTES = (
    "/api/v1/assessments/job-1/graph/explore/options",
    "/api/v1/assessments/job-1/graph/explore/links?kind=table&id=orders",
    "/api/v1/assessments/job-1/graph/explore/search?text=orders",
    "/api/v1/assessments/job-1/graph/explore/query?id=q1",
    "/api/v1/assessments/job-1/graph/explore/overlays",
    "/api/v1/assessments/job-1/graph/explore/below-traffic?min_calls_per_second=1&query_id=q1",
)


def test_routes_are_404_and_never_lease_the_graph_when_the_flag_is_off(client, leased, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    for path in ROUTES:
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert resp.json() == {"detail": "Not Found"}
    # The probe answers, without reading any job.
    assert client.get(PROBE).json() == {"enabled": False}
    assert leased == []


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_only_1_turns_the_flag_on(client, monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert client.get(ROUTES[0]).status_code == 404
    assert client.get(PROBE).json() == {"enabled": False}


def test_every_route_answers_when_the_flag_is_on(client, monkeypatch):
    # The views are registered before the catch-all, so a GET to one still reaches it.
    monkeypatch.setenv(FLAG, "1")
    for path in (PROBE, *ROUTES):
        assert client.get(path).status_code == 200, path
    status = client.get(PROBE).json()
    assert status == {"enabled": True, "max_links": 60, "max_options": 1000}


def test_links_route(client, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    base = "/api/v1/assessments/job-1/graph/explore/links"
    body = client.get(base, params={"kind": "table", "id": "orders", "limit": 1}).json()
    assert body["truncated"] is True and body["bundles"][0]["calls_per_second"] == 438.0
    assert client.get(base, params={"kind": "wave", "id": "3"}).json()["anchor_kind"] == "wave"
    assert client.get(base, params={"kind": "table", "id": "nope"}).status_code == 404
    assert client.get(base, params={"kind": "decision", "id": "q1"}).status_code == 422
    too_many = explorer.MAX_LINK_LIMIT + 1
    assert (
        client.get(base, params={"kind": "table", "id": "o", "limit": too_many}).status_code == 422
    )


def test_search_and_query_routes(client, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    base = "/api/v1/assessments/job-1/graph/explore"
    found = client.get(f"{base}/search", params={"text": "insert"}).json()
    assert [q["query_id"] for q in found["results"]] == ["q3"]
    assert client.get(f"{base}/search", params={"text": "x"}).status_code == 422
    assert client.get(f"{base}/search", params={"text": "x" * 201}).status_code == 422
    detail = client.get(f"{base}/query", params={"id": "q2"}).json()
    assert detail["access_pattern_class"] == "item_collection"
    assert client.get(f"{base}/query", params={"id": "nope"}).status_code == 404


def test_overlays_route(client, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    body = client.get("/api/v1/assessments/job-1/graph/explore/overlays").json()
    assert body["views"] == ["traffic", "moves-together", "who-writes", "cost"]
    assert [w["wave"] for w in body["waves"]] == [1, 2, 3]
    assert body["waves"][2]["engine_labels"] == [display_engine("dynamodb")]
    assert body["traffic"]["max"] == 423.0 and body["bundle_traffic"]["max"] == 438.0
    assert body["cost"]["samples"] == [76.75, 192.12]


def test_below_traffic_route_is_bounded(client, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    base = "/api/v1/assessments/job-1/graph/explore/below-traffic"
    body = client.get(
        base,
        params={"min_calls_per_second": 100, "query_id": ["q2"], "bundle": ["orders|documentdb"]},
    ).json()
    assert body["hidden_count"] == 2 and body["hidden_query_ids"] == ["q2"]
    too_many = [f"q{i}" for i in range(explorer.MAX_VIEW_QUERIES + 1)]
    for params in (
        {"min_calls_per_second": 1, "query_id": too_many},
        {"min_calls_per_second": 1, "bundle": [f"t{i}|e" for i in range(151)]},
        {"min_calls_per_second": -1},
        {"min_calls_per_second": 1, "query_id": ["x" * 1025]},
        {"min_calls_per_second": 1, "bundle": ["no-engine"]},
    ):
        assert client.get(base, params=params).status_code == 422, params


def test_explorer_routes_are_read_only(client, monkeypatch):
    # Every route but the one that refuses the other methods is a GET.
    refusal = explorer_routes.graph_explore_other_requests
    routes = [*explorer_routes.status_router.routes, *explorer_routes.router.routes]
    assert {m for r in routes if r.endpoint is not refusal for m in r.methods} == {"GET"}
    monkeypatch.setenv(FLAG, "1")
    for path in ROUTES:
        assert client.post(path.split("?")[0]).status_code == 405, path


def test_the_collectors_placeholder_table_is_marked_not_named_as_a_table(tmp_path):
    """A statement with no table (SET SESSION, SELECT FOUND_ROWS()) reaches the
    graph under the collector's placeholder "unknown"; the explorer marks it so
    the UI does not show it as a source table."""
    store = GraphStore(str(tmp_path / "p.lbug"))
    initialize_schema(store)
    populate_from_collector(
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
                        "query_type": "OTHER",
                        "tables_accessed": ["unknown"],
                        "calls_per_second": 1.0,
                    }
                ]
            }
        },
        store,
    )
    try:
        node = explorer.links(store, EMPTY, "table", "unknown").nodes[0]
        assert node.placeholder is True
        real = explorer.links(store, EMPTY, "table", "unknown")
        assert real.anchor_id == "unknown"
    finally:
        store.close()


def test_real_tables_are_not_placeholders(populated_store):
    nodes = explorer.links(populated_store, OVERLAYS, "table", "orders").nodes
    assert not [n for n in nodes if n.placeholder]


def test_overlays_name_the_placeholder_tables(client, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    body = client.get("/api/v1/assessments/job-1/graph/explore/overlays").json()
    assert body["placeholder_tables"] == ["unknown"]


# --- round 5: one move wave per query, the cache wave apart, the wave path ---


def test_a_cached_dynamodb_query_moves_in_wave_3_and_is_cached_in_wave_2(populated_store):
    q1 = explorer.links(populated_store, OVERLAYS, "table", "orders").bundles[0].queries[0]
    assert q1.query_id == "q1"
    assert (q1.wave, q1.cached_in_wave) == (3, 2)
    # Its path: on the cutover engine from wave 1, then to DynamoDB in wave 3.
    assert [(s.wave, s.engine, s.kind) for s in q1.path] == [
        (1, "documentdb", "cutover"),
        (3, "dynamodb", "move"),
    ]
    assert q1.path[1].engine_label == display_engine("dynamodb")


def test_no_query_has_two_move_waves():
    for qid, wave in OVERLAYS.query_move_wave.items():
        assert isinstance(wave, int), qid
    for qid in OVERLAYS.query_move_wave:
        moves = [s for s in query_path(OVERLAYS, qid) if s["kind"] == "move"]
        assert len(moves) == 1, qid


def test_an_engine_in_two_waves_claims_no_wave():
    """The single-wave mapping relies on the plan listing each engine once; an
    engine listed twice gets no wave (fails closed) instead of two."""
    report = {
        "facts": {
            "waves": [
                {"wave": 1, "engines": ["dynamodb"]},
                {"wave": 3, "engines": ["dynamodb", "documentdb"]},
            ]
        },
        "migration_waves": [{"wave": 1, "engines": ["dynamodb"], "cutover_query_count": 3}],
    }
    o = build_overlays(ASSIGNMENT, report)
    assert o.ambiguous_engines == {"dynamodb"}
    assert "dynamodb" not in o.engine_wave and o.engine_wave["documentdb"] == 3
    assert o.query_move_wave == {"q2": 3}
    # The cutover count no longer matches the queries with a move wave: no path.
    assert o.cutover is None


def test_the_path_needs_the_plans_cutover_to_match():
    mismatch = {
        **REPORT,
        "migration_waves": [{**REPORT["migration_waves"][0], "cutover_query_count": 99}],
    }
    o = build_overlays(ASSIGNMENT, mismatch)
    assert o.cutover is None
    assert [s["kind"] for s in query_path(o, "q1")] == ["move"]
    no_plan = build_overlays(
        ASSIGNMENT, {k: v for k, v in REPORT.items() if k != "migration_waves"}
    )
    assert no_plan.cutover is None


def test_a_wave_start_lists_what_moves_or_is_cached_in_it():
    assert wave_query_ids(OVERLAYS, 3) == ["q1", "q3"]
    assert wave_query_ids(OVERLAYS, 2) == ["q1"]
    # The cutover wave lists what moves in it, not the whole workload passing through.
    assert wave_query_ids(OVERLAYS, 1) == ["q2"]


def test_the_move_step_names_the_querys_own_engine_when_a_wave_has_two():
    """Discourse's wave 3 moves queries to DynamoDB and to DocumentDB: each
    query's move step names its own engine, not the wave's first."""
    report = {
        "facts": {
            "waves": [
                {"wave": 1, "engines": ["aurora"]},
                {"wave": 3, "engines": ["dynamodb", "documentdb"]},
            ]
        },
        "migration_waves": [{"wave": 1, "engines": ["aurora"], "cutover_query_count": 3}],
    }
    o = build_overlays(ASSIGNMENT, report)
    assert query_path(o, "q2") == [
        {"wave": 1, "engine": "aurora", "kind": "cutover"},
        {"wave": 3, "engine": "documentdb", "kind": "move"},
    ]
    assert query_path(o, "q1")[-1]["engine"] == "dynamodb"


# --- round 6: walkthroughs ---


def test_the_migration_wave_by_wave_follows_the_plan(populated_store):
    out = explorer.walkthroughs(populated_store, OVERLAYS)
    waves = next(w for w in out.walkthroughs if w.id == "waves")
    assert [(s.kind, s.start_kind, s.start_id) for s in waves.steps] == [
        ("wave", "wave", "1"),
        ("wave", "wave", "2"),
        ("wave", "wave", "3"),
        ("group", "group", "3:0"),
    ]
    first, _, _, group = waves.steps
    # Text and figures are the plan's own.
    assert (first.title, first.rationale, first.gate) == (
        "Move to DocumentDB",
        "The whole database moves 1:1.",
        "Parity validated.",
    )
    assert first.view == "moves-together" and first.wave_filter == 1
    assert (group.group_index, group.group_total, group.group_kind) == (1, 1, "co_dependency")
    assert (group.table_count, group.query_count, group.tables) == (1, 2, ["orders"])
    # Not about one query: it starts with every pair as one collapsed bundle.
    assert group.fan_out is False
    assert not any(s.fan_out for s in waves.steps)


def test_a_plan_group_is_a_start_point(populated_store):
    out = explorer.links(populated_store, OVERLAYS, "group", "3:0")
    assert [(b.table, b.engine) for b in out.bundles] == [("orders", "dynamodb")]
    for bad in ("3:5", "9:0", "x", "3:x"):
        assert explorer.links(populated_store, OVERLAYS, "group", bad) is None


def test_the_busiest_queries_walkthrough(populated_store):
    busiest = next(
        w
        for w in explorer.walkthroughs(populated_store, OVERLAYS).walkthroughs
        if w.id == "busiest"
    )
    assert [s.start_id for s in busiest.steps] == ["q1", "q2", "q3"]
    step = busiest.steps[0]
    assert step.select_query == "q1" and step.fan_out
    assert step.query.calls_per_second == 423.0 and step.query.path[-1].engine == "dynamodb"
    assert step.assignment_reason == "Key-value lookup pattern with high traffic"
    q = explorer.links(populated_store, OVERLAYS, "query", "q2")
    assert sorted((b.table, b.engine) for b in q.bundles) == [
        ("customers", "documentdb"),
        ("orders", "documentdb"),
    ]
    assert explorer.links(populated_store, OVERLAYS, "query", "nope") is None


def test_why_each_engine_takes_the_reports_engine_facts(populated_store):
    engines = next(
        w
        for w in explorer.walkthroughs(populated_store, OVERLAYS).walkthroughs
        if w.id == "engines"
    )
    ddb = next(s for s in engines.steps if s.engine == "dynamodb")
    assert (ddb.role, ddb.rationale, ddb.workload_percent, ddb.monthly_cost_usd) == (
        "Migration target",
        "Key-value lookups fit DynamoDB.",
        66.7,
        76.75,
    )
    assert [(t.table, t.query_count, t.calls_per_second) for t in ddb.busiest_tables] == [
        ("orders", 2, 438.0)
    ]


def test_walkthroughs_without_their_data_are_left_out(populated_store):
    # No report: no wave plan, no engine facts; the busiest queries need only the graph.
    out = explorer.walkthroughs(populated_store, EMPTY)
    assert [w.id for w in out.walkthroughs] == ["busiest"]


def test_walkthroughs_route(client, leased, monkeypatch):
    path = "/api/v1/assessments/job-1/graph/explore/walkthroughs"
    monkeypatch.delenv(FLAG, raising=False)
    assert client.get(path).status_code == 404 and leased == []
    monkeypatch.setenv(FLAG, "1")
    body = client.get(path).json()
    assert [w["id"] for w in body["walkthroughs"]] == ["waves", "busiest", "engines"]
