# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The graph query catalog (#307): every entry is a tested, bounded read.

Each entry runs against graphs rebuilt from real job folders:

- ``old_jobs``: the v0.4.0-beta.2 wordpress job (analysis, assignment v2,
  reality check, synthesis; no schema design, no load test);
- ``real_runs``: a recorded wordpress run (schema designs at v2; no synthesis,
  no load test);
- ``real_runs`` plus a load test written by this test into its own copy, so
  the load-test entries return rows somewhere.

An entry either returns rows within its bound or returns nothing, and then
its declared needs name a stage the job did not run.
"""

from __future__ import annotations

import copy
import json
import math
import re
import shutil
from pathlib import Path

import pytest

from src.graph import catalog
from src.graph.cypher_guard import validate_read_only_cypher
from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_load_test,
    populate_from_schema_design,
    rebuild_graph,
)
from src.graph.schema import NODE_TABLES, initialize_schema
from src.graph.store import GraphStore
from src.storage.local_store import LocalArtifactStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OLD_JOB = FIXTURES / "old_jobs" / "v0.4.0-beta.2" / "wordpress" / "aa4e840a"
REAL_RUN = FIXTURES / "real_runs" / "wordpress-both" / "job"

NODE_LABELS = {ddl.split("EXISTS", 1)[1].split("(", 1)[0].strip() for ddl in NODE_TABLES}


def _rebuild(base: Path, path: Path) -> GraphStore:
    store = GraphStore(str(path))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)
    return store


def _copy_job(src: Path, tmp: Path) -> Path:
    base = tmp / "artifacts"
    shutil.copytree(src, base / "wordpress" / "job")
    return base


def _latency(p90: float) -> dict:
    return {
        "p50": p90 / 2,
        "p90": p90,
        "p95": p90 * 1.2,
        "p99": p90 * 1.5,
        "p999": p90 * 2,
        "min": p90 / 4,
        "max": p90 * 3,
    }


def _add_load_test(base: Path, store: GraphStore) -> None:
    """A DynamoDB load test at the effective version: one gain, one regression."""
    rows = store.query(
        "MATCH (q:Query)-[:PART_OF]->(ap:AccessPattern {engine: 'dynamodb'}) "
        "RETURN DISTINCT q.id AS q ORDER BY q LIMIT 2"
    )
    assert len(rows) == 2
    results = [
        {
            "query_id": rows[0]["q"],
            "operation_type": "GetItem",
            "source_latency_ms": _latency(40.0),
            "target_latency_ms": _latency(4.0),
            "improvement_factor": 10.0,
            "throughput_rps": 1000.0,
            "error_rate_pct": 0.0,
            "cost_per_operation_usd": 0.000001,
        },
        {
            "query_id": rows[1]["q"],
            "operation_type": "Query",
            "source_latency_ms": _latency(10.0),
            "target_latency_ms": _latency(20.0),
            "improvement_factor": 0.5,
            "throughput_rps": 500.0,
            "error_rate_pct": 0.1,
            "cost_per_operation_usd": 0.000002,
        },
    ]
    path = base / "wordpress" / "job" / "load-test-dynamodb" / "v2" / "results" / "summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"target_engine": "dynamodb", "version": 2, "pattern_results": results})
    )


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    """The three fixture graphs, rebuilt once for the module."""
    tmp = tmp_path_factory.mktemp("catalog")
    stores = {
        "old_jobs": _rebuild(_copy_job(OLD_JOB, tmp / "old"), tmp / "old.lbug"),
        "real_runs": _rebuild(_copy_job(REAL_RUN, tmp / "real"), tmp / "real.lbug"),
    }
    lt_base = _copy_job(REAL_RUN, tmp / "lt")
    _add_load_test(lt_base, stores["real_runs"])
    stores["load_test"] = _rebuild(lt_base, tmp / "lt.lbug")
    yield stores
    for store in stores.values():
        store.close()


ENTRIES = catalog.entries()
ENTRY_IDS = [e.id for e in ENTRIES]
FIXTURE_NAMES = ["old_jobs", "real_runs", "load_test"]


def _first_choices(store: GraphStore, entry: catalog.CatalogEntry) -> dict[str, str] | None:
    """Each parameter's first choice on this graph, or None when one has no choices."""
    values: dict[str, str] = {}
    for param in entry.params:
        choices = catalog.param_choices(store, param)
        if not choices:
            return None
        values[param.name] = choices[0]
    return values


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


# ---------------------------------------------------------------------------
# Shape of the catalog
# ---------------------------------------------------------------------------


def test_the_catalog_covers_every_cookbook_query():
    refs = {e.ref for e in ENTRIES}
    assert {str(n) for n in range(1, 11)} | set("ABCDEF") <= refs
    assert "6b" in refs  # the resolved-risks query #1779 added to the cookbook


def test_entry_ids_and_refs_are_unique():
    assert len(set(ENTRY_IDS)) == len(ENTRY_IDS)
    assert len({e.ref for e in ENTRIES}) == len(ENTRIES)
    assert catalog.get_entry(ENTRIES[0].id) is ENTRIES[0]
    with pytest.raises(KeyError):
        catalog.get_entry("no_such_entry")


@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
def test_every_entry_is_a_read_only_statement(entry):
    validate_read_only_cypher(entry.cypher)
    validate_read_only_cypher(catalog.bounded_cypher(entry))
    for param in entry.params:
        validate_read_only_cypher(param.choices_cypher)
        validate_read_only_cypher(param.exists_cypher)
        assert "$value" in param.exists_cypher


@pytest.mark.parametrize("need", catalog.NEEDS, ids=[n.id for n in catalog.NEEDS])
def test_every_stage_probe_is_a_bounded_read(need):
    validate_read_only_cypher(need.probe_cypher)
    assert catalog.bounded_probe(need).endswith(" LIMIT 2")


def test_unmet_needs_runs_the_bounded_probe(graphs, monkeypatch):
    seen = []
    store = graphs["old_jobs"]
    real = store.query_bounded

    def spy(cypher, params=None, max_rows=1000):
        seen.append((cypher, max_rows))
        return real(cypher, params, max_rows)

    monkeypatch.setattr(store, "query_bounded", spy)
    catalog.unmet_needs(store, catalog.get_entry("risk_hotspots"))
    assert seen == [(catalog.bounded_probe(n), 1) for n in (catalog.ANALYSIS, catalog.SYNTHESIS)]


def _final_order_keys(cypher: str) -> list[str]:
    tail = cypher.rsplit("ORDER BY", 1)[1]
    tail = re.sub(r"\s+LIMIT\s+\d+\s*$", "", tail)
    keys = [k.strip() for k in re.split(r",(?![^(]*\))", tail)]
    return [re.sub(r"\s+(DESC|ASC)$", "", k) for k in keys]


@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
def test_the_final_order_ends_with_the_row_key(entry):
    """The tie-breaker: rows that tie on the leading sort keys keep one order."""
    assert entry.row_key, f"{entry.id} declares no row key"
    keys = _final_order_keys(entry.cypher)
    assert keys[-len(entry.row_key) :] == list(entry.row_key), (entry.id, keys)


@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
def test_every_entry_is_well_formed(entry):
    assert entry.question.endswith("?"), entry.question
    assert entry.title
    assert entry.section in catalog.SECTIONS
    assert 1 <= entry.max_rows <= catalog.MAX_ROWS
    for param in entry.params:
        assert f"${param.name}" in entry.cypher
        assert param.kind in catalog.ParamKind
        assert param.example
    for node in entry.projection.nodes:
        assert node.label in NODE_LABELS, node
    columns = {n.column for n in entry.projection.nodes}
    for edge in entry.projection.edges:
        assert {edge.source, edge.target} <= columns, edge
        assert edge.label


def test_the_known_dialect_traps_are_gone():
    """The cookbook's errors on main: reserved ``AS table`` and ``labels(n)[0]``."""
    for entry in ENTRIES:
        assert " AS table " not in f"{entry.cypher} " and " AS table," not in entry.cypher
        assert "labels(" not in entry.cypher


def test_the_blast_radius_example_is_schema_qualified():
    (param,) = catalog.get_entry("table_blast_radius").params
    assert param.example == "wordpress.wp_posts"


# ---------------------------------------------------------------------------
# Every entry runs on every fixture graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
def test_every_entry_runs_or_its_needs_explain_the_empty_result(graphs, fixture, entry):
    store = graphs[fixture]
    unmet = catalog.unmet_needs(store, entry)
    params = _first_choices(store, entry)
    if params is None:
        assert unmet, f"{entry.id}: a parameter has no choices on {fixture} and no need explains it"
        return
    result = catalog.run_entry(store, entry, params)
    assert len(result.rows) <= entry.max_rows
    if not result.rows:
        assert unmet, f"{entry.id} is empty on {fixture} and no declared need explains it"
        return
    for row in result.rows:
        assert not any(_is_nan(v) for v in row.values()), (entry.id, row)
    expected = {n.column for n in entry.projection.nodes} | set(entry.row_key)
    assert expected <= set(result.rows[0]), (entry.id, sorted(result.rows[0]))
    keys = [tuple(row[c] for c in entry.row_key) for row in result.rows]
    assert len(set(keys)) == len(keys), f"{entry.id}: row key {entry.row_key} repeats"
    if entry.projection.nodes:
        assert catalog.project(entry, result.rows)["nodes"]


@pytest.mark.parametrize("entry", ENTRIES, ids=ENTRY_IDS)
def test_every_entry_returns_rows_on_some_fixture(graphs, entry):
    for name in FIXTURE_NAMES:
        params = _first_choices(graphs[name], entry)
        if params is not None and catalog.run_entry(graphs[name], entry, params).rows:
            return
    pytest.fail(f"{entry.id} returns no rows on any fixture")


def test_every_parameter_has_choices_on_some_fixture(graphs):
    for entry in ENTRIES:
        for param in entry.params:
            assert any(
                catalog.param_choices(graphs[name], param) for name in FIXTURE_NAMES
            ), f"{entry.id}.{param.name} has no choices on any fixture"


def test_every_documented_example_is_a_real_choice(graphs):
    for entry in ENTRIES:
        for param in entry.params:
            assert any(
                param.example in catalog.param_choices(graphs[name], param)
                for name in FIXTURE_NAMES
            ), f"{entry.id}.{param.name}: example {param.example!r} is not a choice"


def test_the_choices_are_strings_in_a_stable_order(graphs):
    (param,) = catalog.get_entry("engine_destinations").params
    choices = catalog.param_choices(graphs["old_jobs"], param)
    # The effective version's engines, the cache-only ElastiCache included; the
    # DocumentDB and OpenSearch of assignment v1 are no longer targets.
    assert choices == ["aurora_mysql", "dynamodb", "elasticache"]
    (table,) = catalog.get_entry("table_blast_radius").params
    tables = catalog.param_choices(graphs["old_jobs"], table)
    assert "wordpress.wp_posts" in tables and tables == sorted(tables)


def test_choices_say_when_they_are_cut(graphs, monkeypatch):
    param = catalog.QUERY_PARAM
    full = catalog.param_choices(graphs["old_jobs"], param)
    assert len(full) == 107 and full.truncated is False
    monkeypatch.setattr(catalog, "MAX_CHOICES", 5)
    cut = catalog.param_choices(graphs["old_jobs"], param)
    assert list(cut) == list(full[:5]) and cut.truncated is True


def _distinct_params() -> list:
    seen: dict = {}
    for entry in ENTRIES:
        for param in entry.params:
            seen.setdefault(param, None)
    return list(seen)


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_the_existence_probe_agrees_with_the_choices(graphs, fixture):
    """A value exists exactly when it is a choice: every choice is found, nothing else is."""
    store = graphs[fixture]
    for param in _distinct_params():
        choices = catalog.param_choices(store, param)
        assert not choices.truncated
        for value in choices:
            assert catalog.value_exists(store, param, value), f"{param.name}: {value!r}"
        assert not catalog.value_exists(store, param, "not-a-real-value")
        assert not catalog.value_exists(store, param, "")


def test_the_decided_query_probe_needs_a_decision(graphs):
    """A query no decision names is a query, but not a decided-query choice."""
    store = graphs["old_jobs"]
    decided = set(catalog.param_choices(store, catalog.DECIDED_QUERY_PARAM))
    undecided = [q for q in catalog.param_choices(store, catalog.QUERY_PARAM) if q not in decided]
    assert undecided
    assert catalog.value_exists(store, catalog.QUERY_PARAM, undecided[0])
    assert not catalog.value_exists(store, catalog.DECIDED_QUERY_PARAM, undecided[0])


def test_needs_name_the_missing_stage(graphs):
    load_tests = catalog.get_entry("load_test_regressions")
    assert [n.id for n in catalog.unmet_needs(graphs["real_runs"], load_tests)] == ["load_test"]
    assert catalog.unmet_needs(graphs["load_test"], load_tests) == []
    risks = catalog.get_entry("risk_register")
    assert [n.id for n in catalog.unmet_needs(graphs["real_runs"], risks)] == ["synthesis"]
    assert catalog.unmet_needs(graphs["old_jobs"], risks) == []
    resolved = catalog.get_entry("resolved_risks")
    assert [n.id for n in catalog.unmet_needs(graphs["real_runs"], resolved)] == ["resolved_risks"]
    assert catalog.unmet_needs(graphs["old_jobs"], resolved) == []
    patterns = catalog.get_entry("access_patterns_per_engine")
    assert [n.id for n in catalog.unmet_needs(graphs["old_jobs"], patterns)] == ["schema_design"]


def test_the_risk_probes_count_only_their_own_status(graph_store):
    """Only resolved risks: 6's need is unmet and 6b's is met; only active: the reverse."""
    graph_store.execute("CREATE (:Risk {id: 'RESOLVED-001', status: 'resolved', severity: 'LOW'})")
    active = catalog.get_entry("risk_register")
    assert [n.id for n in catalog.unmet_needs(graph_store, active)] == ["synthesis"]
    assert catalog.unmet_needs(graph_store, catalog.get_entry("resolved_risks")) == []
    graph_store.execute("MATCH (r:Risk) SET r.status = 'active'")
    assert catalog.unmet_needs(graph_store, active) == []
    assert [
        n.id for n in catalog.unmet_needs(graph_store, catalog.get_entry("resolved_risks"))
    ] == ["resolved_risks"]


# ---------------------------------------------------------------------------
# Parameters and bounds
# ---------------------------------------------------------------------------


def test_run_entry_rejects_missing_unknown_or_untyped_parameters(graphs):
    entry = catalog.get_entry("table_blast_radius")
    store = graphs["old_jobs"]
    with pytest.raises(ValueError, match="missing"):
        catalog.run_entry(store, entry, {})
    with pytest.raises(ValueError, match="unknown"):
        catalog.run_entry(store, entry, {"table_id": "wordpress.wp_posts", "x": "y"})
    with pytest.raises(ValueError, match="string"):
        catalog.run_entry(store, entry, {"table_id": 7})


def test_run_entry_applies_the_row_bound(graphs, monkeypatch):
    entry = catalog.get_entry("top_traffic_queries")
    small = catalog.CatalogEntry(**{**entry.__dict__, "max_rows": 3})
    result = catalog.run_entry(graphs["old_jobs"], small, {})
    assert len(result.rows) == 3
    assert result.truncated is True
    full = catalog.run_entry(graphs["old_jobs"], entry, {})
    assert len(full.rows) == 20 and full.truncated is False


# ---------------------------------------------------------------------------
# The dialect fixes return the right answers
# ---------------------------------------------------------------------------


def test_node_census_names_each_label(graphs):
    rows = catalog.run_entry(graphs["old_jobs"], catalog.get_entry("node_census"), {}).rows
    counts = {r["type"]: r["n"] for r in rows}
    assert counts["Query"] == 107
    assert counts["SourceTable"] == 23
    assert "" not in counts


def test_anti_pattern_severity_is_the_mean_of_the_type(graphs):
    store = graphs["old_jobs"]
    rows = catalog.run_entry(store, catalog.get_entry("anti_patterns_by_type"), {}).rows
    got = {r["type"]: (r["queries"], round(r["avg_severity"], 6)) for r in rows}
    expected: dict[str, list[float]] = {}
    for r in store.query(
        "MATCH (a:AntiPattern) RETURN a.anti_pattern_type AS t, a.severity_weight AS w"
    ):
        expected.setdefault(r["t"], []).append(r["w"])
    assert {t: v[1] for t, v in got.items()} == {
        t: round(sum(ws) / len(ws), 6) for t, ws in expected.items()
    }
    assert got["no-relational-need"][0] == 21


def _status(store, risk_ids):
    rows = store.query(
        "MATCH (r:Risk) WHERE r.id IN $ids RETURN DISTINCT r.status AS s", {"ids": risk_ids}
    )
    return {r["s"] for r in rows}


def test_the_risk_register_lists_only_active_risks(graphs):
    store = graphs["old_jobs"]
    assert _status(store, ["RESOLVED-001"]) == {"resolved"}  # the fixture has both kinds
    rows = catalog.run_entry(store, catalog.get_entry("risk_register"), {}).rows
    ids = [r["risk_id"] for r in rows]
    assert ids and _status(store, ids) == {"active"}
    assert sorted(ids) == ["RISK-001", "RISK-002", "RISK-003", "RISK-004"]


def test_the_risk_register_orders_by_severity_rank(graphs):
    rows = catalog.run_entry(graphs["old_jobs"], catalog.get_entry("risk_register"), {}).rows
    rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    got = [(rank[r["severity"]], r["risk_id"]) for r in rows]
    assert got == sorted(got)
    # Alphabetical order would put LOW before MEDIUM.
    assert [r["severity"] for r in rows] == ["MEDIUM", "MEDIUM", "MEDIUM", "LOW"]


def test_resolved_risks_lists_only_resolved_risks(graphs):
    store = graphs["old_jobs"]
    rows = catalog.run_entry(store, catalog.get_entry("resolved_risks"), {}).rows
    ids = [r["risk_id"] for r in rows]
    assert ids == [f"RESOLVED-00{i}" for i in range(1, 6)]
    assert _status(store, ids) == {"resolved"}
    assert all(r["resolved_on"] and r["reason"] for r in rows)


def test_decision_examples_are_the_first_three_by_id(graphs):
    store = graphs["real_runs"]
    rows = catalog.run_entry(store, catalog.get_entry("decisions_summary"), {}).rows
    by_category = {r["category"]: r["examples"] for r in rows}
    decisions = store.query(
        "MATCH (d:Decision) RETURN d.id AS id, d.category AS c, d.description AS text"
    )
    for category, examples in by_category.items():
        mine = sorted((d["id"], d["text"]) for d in decisions if d["c"] == category)
        assert examples == [text for _, text in mine[:3]], category
    assert len(by_category["trade_off"]) == 3


def test_risk_hotspot_traffic_is_not_multiplied_by_risks(graphs):
    store = graphs["old_jobs"]
    rows = catalog.run_entry(store, catalog.get_entry("risk_hotspots"), {}).rows
    traffic = {
        r["t"]: r["cps"]
        for r in store.query(
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query) "
            "RETURN st.id AS t, sum(q.calls_per_second) AS cps"
        )
    }
    assert rows
    for row in rows:
        assert row["total_cps"] == pytest.approx(traffic[row["table_id"]]), row
    top = rows[0]
    assert top["table_id"] == "wordpress.wp_options"
    assert (top["risks"], top["anti_patterns"]) == (2, 3)


def test_blast_radius_of_a_qualified_table(graphs):
    store = graphs["real_runs"]
    entry = catalog.get_entry("table_blast_radius")
    rows = catalog.run_entry(store, entry, {"table_id": "wordpress.wp_posts"}).rows
    assert rows and {r["table_id"] for r in rows} == {"wordpress.wp_posts"}
    assert any(r["access_patterns"] for r in rows)
    graph = catalog.project(entry, rows)
    labels = {n["label"] for n in graph["nodes"]}
    assert {"SourceTable", "Query", "Destination", "AccessPattern"} <= labels
    assert {e["label"] for e in graph["edges"]} >= {"READS_FROM", "MIGRATES_TO", "PART_OF"}


def test_load_test_rows_stay_on_their_own_engine_and_version(graphs):
    store = graphs["load_test"]
    rows = catalog.run_entry(store, catalog.get_entry("load_test_per_pattern"), {}).rows
    assert rows and {r["engine"] for r in rows} == {"dynamodb"}
    regressions = catalog.run_entry(store, catalog.get_entry("load_test_regressions"), {}).rows
    assert [r["improvement_factor"] for r in regressions] == [0.5]


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def test_projection_dedupes_nodes_and_skips_nulls():
    entry = catalog.get_entry("table_blast_radius")
    rows = [
        {
            "table_id": "t",
            "query_id": "q1",
            "destinations": ["d", None],
            "access_patterns": [],
        },
        {"table_id": "t", "query_id": "q2", "destinations": ["d"], "access_patterns": None},
    ]
    graph = catalog.project(entry, rows)
    assert sorted((n["label"], n["id"]) for n in graph["nodes"]) == [
        ("Destination", "d"),
        ("Query", "q1"),
        ("Query", "q2"),
        ("SourceTable", "t"),
    ]
    assert sorted((e["source"], e["target"], e["label"]) for e in graph["edges"]) == [
        ("q1", "d", "MIGRATES_TO"),
        ("q1", "t", "READS_FROM"),
        ("q2", "d", "MIGRATES_TO"),
        ("q2", "t", "READS_FROM"),
    ]


def test_projection_is_empty_for_a_count_only_entry():
    graph = catalog.project(catalog.get_entry("node_census"), [{"type": "Query", "n": 3}])
    assert graph == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# Load-test runs join only their own engine and schema version
# ---------------------------------------------------------------------------


def _run(query_id: str) -> dict:
    return {
        "query_id": query_id,
        "source_latency_ms": _latency(40.0),
        "target_latency_ms": _latency(4.0),
        "improvement_factor": 10.0,
        "throughput_rps": 100.0,
        "error_rate_pct": 0.0,
        "cost_per_operation_usd": 0.000001,
    }


@pytest.fixture
def mixed_runs(
    graph_store, sample_collector_output, sample_assignment, sample_schema_design_output
):
    """DDB-AP-1 (q1, q3) and EC-AP-1 (q1) at v2; q1 tested on DynamoDB v2, q3 on v1."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    populate_from_schema_design(sample_schema_design_output, "dynamodb", 2, graph_store)
    cache = copy.deepcopy(sample_schema_design_output)
    cache["trade_offs"] = []
    cache["access_patterns"][0].update(pattern_id="EC-AP-1", query_ids=["q1"])
    populate_from_schema_design(cache, "elasticache", 2, graph_store)
    populate_from_load_test({"pattern_results": [_run("q1")]}, "dynamodb", 2, graph_store)
    populate_from_load_test({"pattern_results": [_run("q3")]}, "dynamodb", 1, graph_store)
    return graph_store


def test_access_pattern_queries_keep_runs_on_their_engine_and_version(mixed_runs):
    entry = catalog.get_entry("access_pattern_queries")
    ddb = catalog.run_entry(mixed_runs, entry, {"access_pattern_id": "DDB-AP-1"}).rows
    assert sorted((r["query_id"], r["run_id"]) for r in ddb) == [
        ("q1", "lt-dynamodb-v2-q1"),
        ("q3", None),  # its only run is DynamoDB v1, not the pattern's v2
    ]
    cache = catalog.run_entry(mixed_runs, entry, {"access_pattern_id": "EC-AP-1"}).rows
    assert [(r["query_id"], r["run_id"]) for r in cache] == [("q1", None)]


def test_load_test_per_pattern_keeps_runs_on_their_engine_and_version(mixed_runs):
    rows = catalog.run_entry(mixed_runs, catalog.get_entry("load_test_per_pattern"), {}).rows
    assert [(r["pattern"], r["engine"], r["queries"]) for r in rows] == [
        ("DDB-AP-1", "dynamodb", 1)
    ]


# ---------------------------------------------------------------------------
# The assignment version chain (#306)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["old_jobs", "real_runs"])
def test_the_version_chain_lists_each_version_and_what_it_supersedes(graphs, fixture):
    rows = catalog.run_entry(graphs[fixture], catalog.get_entry("version_chain"), {}).rows
    assert [
        (r["version_id"], r["kind"], r["effective"], r["supersedes"], r["restores"]) for r in rows
    ] == [
        ("assignment-v1", "first_assignment", False, None, None),
        ("assignment-v2", "reality_check", True, "assignment-v1", None),
    ]


def test_version_changes_lists_the_queries_a_version_moved(graphs):
    store = graphs["old_jobs"]
    entry = catalog.get_entry("version_changes")
    v2 = catalog.run_entry(store, entry, {"version_id": "assignment-v2"}).rows
    assert sorted({r["role"] for r in v2}) == ["moved", "tie"]
    assert sum(r["role"] == "moved" for r in v2) == 11
    moved = [r for r in v2 if r["role"] == "moved"]
    assert all(r["to_engines"] for r in moved)
    # The reality check moved queries off DocumentDB and OpenSearch only.
    assert {e for r in moved for e in r["from_engines"]} <= {"documentdb", "opensearch"}
    assert {e for r in moved for e in r["to_engines"]} <= {"aurora_mysql", "dynamodb"}
    v1 = catalog.run_entry(store, entry, {"version_id": "assignment-v1"}).rows
    assert len(v1) == 107 and {r["role"] for r in v1} == {"moved"}


def test_version_choices_are_newest_first(graphs):
    assert list(catalog.param_choices(graphs["old_jobs"], catalog.VERSION_PARAM)) == [
        "assignment-v2",
        "assignment-v1",
    ]


def test_check_params_is_public_so_callers_validate_before_opening_a_graph():
    """The CLI checks parameters before it opens (or builds) the job's graph (#307)."""
    entry = catalog.get_entry("table_blast_radius")
    assert catalog.check_params(entry, {"table_id": "wordpress.wp_posts"}) == {
        "table_id": "wordpress.wp_posts"
    }
    with pytest.raises(ValueError, match="missing"):
        catalog.check_params(entry, {})


def _designed_engine_graph(path: Path, queries: int, objects: int) -> None:
    """A graph sized like a designed discourse job's Aurora PostgreSQL (#1974):
    ``queries`` queries, each with one effective TARGETS edge to one of ``objects``
    design objects, plus a cache edge, an access pattern and a source table each."""
    store = GraphStore(str(path))
    initialize_schema(store)
    oids = [f"aurora_postgresql:table:t{i}" for i in range(objects)]
    qids = [f"q{i}" for i in range(queries)]
    store.execute(
        "UNWIND $rows AS r CREATE (:TargetObject {id: r, engine: 'aurora_postgresql', "
        "kind: 'table', name: r, fallback: false})",
        {"rows": oids},
    )
    store.execute("UNWIND $rows AS r CREATE (:Query {id: r})", {"rows": qids})
    store.execute("UNWIND $rows AS r CREATE (:AccessPattern {id: 'ap-' + r})", {"rows": qids})
    store.execute("UNWIND range(0, 99) AS i CREATE (:SourceTable {id: 's' + string(i)})")
    rows = [
        {"q": q, "o": oids[i % objects], "s": f"s{i % 100}", "cache": i % 10 == 0}
        for i, q in enumerate(qids)
    ]
    store.execute(
        "UNWIND $rows AS r MATCH (q:Query {id: r.q}), (o:TargetObject {id: r.o}) "
        "CREATE (q)-[:TARGETS {version: 1, effective: true, cache: r.cache}]->(o)",
        {"rows": rows},
    )
    store.execute(
        "UNWIND $rows AS r MATCH (q:Query {id: r.q}), (a:AccessPattern {id: 'ap-' + r.q}), "
        "(s:SourceTable {id: r.s}) CREATE (q)-[:PART_OF]->(a), (q)-[:READS_FROM]->(s)",
        {"rows": rows},
    )
    store.close()


def test_engine_destinations_fits_the_read_pool_on_a_large_design(tmp_path):
    """#1974: entry B on a design with hundreds of objects stays inside the default
    read handle's buffer pool, and still counts every query and object once."""
    path = tmp_path / "designed.lbug"
    _designed_engine_graph(path, queries=800, objects=530)
    store = GraphStore(str(path), read_only=True)
    try:
        entry = catalog.get_entry("engine_destinations")
        (row,) = catalog.run_entry(store, entry, {"engine": "aurora_postgresql"}).rows
    finally:
        store.close()
    assert (row["destination"], row["queries"], row["cache_queries"]) == (
        "aurora_postgresql",
        800,
        80,
    )
    assert len(row["targets"]) == 530 and row["fallbacks"] is None
    assert len(row["source_tables"]) == 100 and len(row["access_patterns"]) == 800
    counts = {c["id"]: (c["queries"], c["cache_queries"]) for c in row["target_counts"]}
    assert len(counts) == 530 and sum(q for q, _ in counts.values()) == 800
    assert sum(c for _, c in counts.values()) == 80
