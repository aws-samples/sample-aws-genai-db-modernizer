# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Risks in the context graph (#306): active and resolved Risk nodes, each linked
to the queries it names (EVIDENCED_BY) and the tables it impacts (IMPACTS)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from src.graph.populators import populate_from_collector, populate_from_synthesis, rebuild_graph
from src.graph.queries import risk_hotspots
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.local_store import LocalArtifactStore

FIXTURES = Path(__file__).parents[1] / "fixtures"
# A deterministic run (--llm-mode none) of the WordPress sample (old_jobs/README.md).
DETERMINISTIC_WORDPRESS = FIXTURES / "old_jobs/v0.4.0-beta.2/wordpress/aa4e840a"
# A real /modernize run of the WordPress sample; its report sits next to the job.
REAL_WORDPRESS = FIXTURES / "real_runs/wordpress-both"


def _report(risks=(), resolved=None):
    assessment: dict = {"overall_risk_level": "HIGH", "risks": list(risks)}
    if resolved is not None:
        assessment["resolved_risks"] = list(resolved)
    return {"risk_assessment": assessment}


def _active(risk_id, query_ids=("q1",), tables=("orders",), **extra):
    return {
        "risk_id": risk_id,
        "risk_type": "design",
        "severity": "HIGH",
        "description": f"{risk_id} description",
        "mitigation": "m",
        "affected_tables": list(tables),
        "query_ids": list(query_ids),
        **extra,
    }


def _resolved(query_ids=("q2",), tables=("customers",), engine="aurora_mysql"):
    return {
        "engine": engine,
        "severity": "MEDIUM",
        "description": f"[{engine}] resolved description",
        "affected_tables": list(tables),
        "query_ids": list(query_ids),
        "resolved_on": "dynamodb",
        "reason": "the analysis recommended DynamoDB for these queries",
    }


def _risks(store):
    rows = store.query(
        "MATCH (r:Risk) RETURN r.id AS id, r.status AS status, r.severity AS severity, "
        "r.rule AS rule, r.wave AS wave, r.engine AS engine, "
        "r.resolved_on AS resolved_on, r.reason AS reason ORDER BY id"
    )
    return {r["id"]: r for r in rows}


def _evidence(store):
    return sorted(
        (r["rid"], r["qid"])
        for r in store.query(
            "MATCH (r:Risk)-[:EVIDENCED_BY]->(q:Query) RETURN r.id AS rid, q.id AS qid"
        )
    )


def _impacts(store):
    return sorted(
        (r["rid"], r["tid"])
        for r in store.query(
            "MATCH (r:Risk)-[:IMPACTS]->(st:SourceTable) RETURN r.id AS rid, st.id AS tid"
        )
    )


def test_active_risk_is_evidenced_by_its_queries(graph_store, sample_collector_output):
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_synthesis(
        _report([_active("RISK-001", ("q1", "q3"), rule="design.trade_offs", wave=2)]),
        graph_store,
    )
    assert _evidence(graph_store) == [("RISK-001", "q1"), ("RISK-001", "q3")]
    risk = _risks(graph_store)["RISK-001"]
    assert (risk["status"], risk["rule"], risk["wave"]) == ("active", "design.trade_offs", 2)


def test_unknown_query_ids_are_skipped_and_counted(graph_store, sample_collector_output, caplog):
    populate_from_collector(sample_collector_output, graph_store)
    report = _report(
        [_active("RISK-001", ("q1", "q-gone"))], resolved=[_resolved(("q2", "q-missing"))]
    )
    with caplog.at_level(logging.INFO, logger="src.graph.populators"):
        populate_from_synthesis(report, graph_store)
    assert _evidence(graph_store) == [("RESOLVED-001", "q2"), ("RISK-001", "q1")]
    assert "2 risk query id(s) with no Query node" in caplog.text


def test_resolved_risks_are_marked_and_explained(graph_store, sample_collector_output):
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_synthesis(
        _report([_active("RISK-001")], resolved=[_resolved(), _resolved(engine="dynamodb")]),
        graph_store,
    )
    risks = _risks(graph_store)
    assert sorted(risks) == ["RESOLVED-001", "RESOLVED-002", "RISK-001"]
    assert risks["RISK-001"]["status"] == "active"
    first = risks["RESOLVED-001"]
    assert first["status"] == "resolved"
    assert (first["engine"], first["resolved_on"]) == ("aurora_mysql", "dynamodb")
    assert first["reason"] == "the analysis recommended DynamoDB for these queries"
    assert first["severity"] == "MEDIUM"
    assert first["rule"] is None and first["wave"] is None
    assert risks["RESOLVED-002"]["engine"] == "dynamodb"
    # IMPACTS for both kinds.
    assert _impacts(graph_store) == [
        ("RESOLVED-001", "customers"),
        ("RESOLVED-002", "customers"),
        ("RISK-001", "orders"),
    ]


def test_resolved_risk_ids_never_collide_with_active_ids(graph_store):
    populate_from_synthesis(
        _report(
            [_active("RISK-001"), _active("RESOLVED-001")],
            resolved=[_resolved(), _resolved()],
        ),
        graph_store,
    )
    risks = _risks(graph_store)
    assert len(risks) == 4
    assert risks["RESOLVED-001"]["status"] == "active"
    assert [risks[i]["status"] for i in ("RESOLVED-002", "RESOLVED-003")] == ["resolved"] * 2


def test_resolved_risks_produced_by_synthesis(graph_store):
    populate_from_synthesis(_report(resolved=[_resolved()]), graph_store)
    rows = graph_store.query(
        "MATCH (r:Risk)-[:PRODUCED_BY]->(a:Agent) RETURN r.id AS id, a.id AS agent"
    )
    assert rows == [{"id": "RESOLVED-001", "agent": "referee-synthesis"}]


@pytest.mark.passes_on_main
def test_risk_hotspots_count_only_active_risks(graph_store, sample_collector_output):
    """A resolved risk is in the graph for the audit trail; it is no hotspot."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_synthesis(
        _report([_active("RISK-001", tables=("orders",))], resolved=[_resolved()]),
        graph_store,
    )
    hotspots = {h.table_id: h.risk_count for h in risk_hotspots(graph_store).hotspots}
    assert hotspots == {"orders": 1}


def test_risk_hotspot_traffic_counts_each_query_once_per_table(
    graph_store, sample_collector_output
):
    """Three risks on one table must not triple its traffic (#1782): orders is read by
    q1, q2 and q3 (423 + 89 + 15 calls/s)."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_synthesis(
        _report([_active(f"RISK-00{n}", tables=("orders",)) for n in (1, 2, 3)]),
        graph_store,
    )
    (hotspot,) = risk_hotspots(graph_store).hotspots
    assert hotspot.table_id == "orders"
    assert hotspot.risk_count == 3
    assert hotspot.total_calls_per_second == pytest.approx(527.0)


def test_risk_hotspot_traffic_ignores_the_risks_of_other_tables(
    graph_store, sample_collector_output
):
    """A table with one risk keeps its own traffic: customers is read by q2 only."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_synthesis(
        _report(
            [
                _active("RISK-001", tables=("orders", "customers")),
                _active("RISK-002", tables=("orders",)),
            ]
        ),
        graph_store,
    )
    by_table = {h.table_id: h for h in risk_hotspots(graph_store).hotspots}
    assert by_table["orders"].total_calls_per_second == pytest.approx(527.0)
    assert by_table["customers"].total_calls_per_second == pytest.approx(89.0)
    assert (by_table["orders"].risk_count, by_table["customers"].risk_count) == (2, 1)


def test_risk_hotspots_on_wordpress_report_each_tables_real_traffic(tmp_path):
    """On the WordPress run wp_options carries 2 risks and 3 anti-patterns and is read
    at 93.13 calls/s; it was reported as 558.77 (6x). Every hotspot equals the
    table's summed query traffic."""
    store = _rebuild(tmp_path, DETERMINISTIC_WORDPRESS)
    traffic = {
        r["t"]: r["cps"]
        for r in store.query(
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query) "
            "RETURN st.id AS t, sum(q.calls_per_second) AS cps"
        )
    }
    hotspots = {h.table_id: h for h in risk_hotspots(store).hotspots}
    options = hotspots["wordpress.wp_options"]
    assert options.total_calls_per_second == pytest.approx(93.13, abs=0.005)
    assert (options.risk_count, options.anti_pattern_count) == (2, 3)
    for table_id, hotspot in hotspots.items():
        assert hotspot.total_calls_per_second == pytest.approx(traffic[table_id]), table_id
    store.close()


def _rebuild(tmp_path, job: Path, report: Path | None = None) -> GraphStore:
    base = tmp_path / "artifacts"
    shutil.copytree(job, base / "wordpress" / "job")
    if report is not None:
        dest = base / "wordpress" / "job" / "synthesis" / "v2" / "report.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(report, dest)
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)
    return store


def _status_counts(store):
    rows = store.query("MATCH (r:Risk) RETURN r.status AS s, COUNT(r) AS c")
    return {r["s"]: r["c"] for r in rows}


def _evidence_by_status(store):
    rows = store.query(
        "MATCH (r:Risk)-[e:EVIDENCED_BY]->(:Query) RETURN r.status AS s, COUNT(e) AS c"
    )
    return {r["s"]: r["c"] for r in rows}


def test_deterministic_wordpress_run(tmp_path):
    """The deterministic WordPress run: 4 active and 5 resolved risks, 101 and 46
    EVIDENCED_BY edges (every query id in the report names a collected query)."""
    store = _rebuild(tmp_path, DETERMINISTIC_WORDPRESS)
    assert _status_counts(store) == {"active": 4, "resolved": 5}
    assert _evidence_by_status(store) == {"active": 101, "resolved": 46}
    store.close()


def test_real_wordpress_run(tmp_path):
    """The real /modernize WordPress run: 9 active and 5 resolved risks, 158 and 46
    EVIDENCED_BY edges, one per (risk, query id) in the report."""
    report = json.loads((REAL_WORDPRESS / "report.json").read_text())["risk_assessment"]
    store = _rebuild(tmp_path, REAL_WORDPRESS / "job", REAL_WORDPRESS / "report.json")
    assert _status_counts(store) == {"active": 9, "resolved": 5}
    assert _evidence_by_status(store) == {"active": 158, "resolved": 46}
    expected_active = sorted((r["risk_id"], q) for r in report["risks"] for q in r["query_ids"])
    assert [e for e in _evidence(store) if e[0].startswith("RISK-")] == expected_active
    store.close()


# The fields synthesis writes for a resolved risk (synthesis_report._resolved_risk)
# and the Risk columns that carry them.
_RESOLVED_FIELDS = ("engine", "resolved_on", "reason", "severity")


@pytest.mark.parametrize("which", ["deterministic", "real"])
def test_resolved_risk_columns_match_the_report(tmp_path, which):
    """On both WordPress reports, each resolved Risk node carries the report's
    engine, resolved_on, reason and severity, in report order: a key renamed in
    the synthesis report leaves a column null and fails here."""
    if which == "deterministic":
        report_path = DETERMINISTIC_WORDPRESS / "synthesis/v2/report.json"
        store = _rebuild(tmp_path, DETERMINISTIC_WORDPRESS)
    else:
        report_path = REAL_WORDPRESS / "report.json"
        store = _rebuild(tmp_path, REAL_WORDPRESS / "job", report_path)
    resolved = json.loads(report_path.read_text())["risk_assessment"]["resolved_risks"]
    assert resolved
    nodes = _risks(store)
    for n, entry in enumerate(resolved, start=1):
        node = nodes[f"RESOLVED-{n:03d}"]
        assert node["status"] == "resolved"
        for field in _RESOLVED_FIELDS:
            assert entry.get(field), (field, entry)
            assert node[field] == entry[field], field
    store.close()


def test_a_boolean_wave_is_not_a_wave(graph_store):
    populate_from_synthesis(_report([_active("RISK-001", wave=True)]), graph_store)
    assert _risks(graph_store)["RISK-001"]["wave"] is None


COOKBOOK = Path(__file__).parents[2] / "docs/guides/context-graph-query-cookbook.md"


def _cookbook_risk_queries() -> list[str]:
    """The cookbook's Cypher queries that read Risk nodes by status."""
    return [
        json.loads(line)["cypher"]
        for line in COOKBOOK.read_text().splitlines()
        if line.startswith('{"cypher":') and "Risk {status:" in line
    ]


def test_cookbook_risk_queries_run(graph_store, sample_collector_output):
    """The cookbook's risk register, resolved-risk and hotspot queries run as written,
    and each reads only its own kind of risk."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_synthesis(
        _report([_active("RISK-001", tables=("orders",))], resolved=[_resolved()]),
        graph_store,
    )
    register, resolved, hotspots = _cookbook_risk_queries()
    assert [r["description"] for r in graph_store.query(register)] == ["RISK-001 description"]
    rows = graph_store.query(resolved)
    # The catalog names the column risk_id, as in the risk register (#307).
    assert [(r["risk_id"], r["raised_by"], r["resolved_on"], r["queries"]) for r in rows] == [
        ("RESOLVED-001", "aurora_mysql", "dynamodb", ["q2"])
    ]
    assert {r["table_id"]: r["risks"] for r in graph_store.query(hotspots)} == {"orders": 1}


def _cookbook_query(heading: str) -> str:
    """The Cypher of the cookbook query under ``### <heading>``."""
    lines = COOKBOOK.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"### {heading}"))
    line = next(line for line in lines[start:] if line.startswith('{"cypher":'))
    return json.loads(line)["cypher"]


# fixture -> (tables in query 3, tables in query E)
_TABLE_QUERY_COUNTS = {
    "deterministic": (23, 16),
    "real": (23, 16),
}


@pytest.mark.parametrize("which", sorted(_TABLE_QUERY_COUNTS))
def test_cookbook_table_queries_run_on_wordpress(tmp_path, which):
    """Cookbook queries 3 (table to engine mapping) and E (cross-engine tables) run
    as written on both WordPress fixtures, and agree: E lists exactly the tables 3
    maps to more than one engine."""
    if which == "deterministic":
        store = _rebuild(tmp_path, DETERMINISTIC_WORDPRESS)
    else:
        store = _rebuild(tmp_path, REAL_WORDPRESS / "job", REAL_WORDPRESS / "report.json")
    mapping = store.query(_cookbook_query("3."))
    cross = store.query(_cookbook_query("E."))
    assert (len(mapping), len(cross)) == _TABLE_QUERY_COUNTS[which]
    assert {r["table_id"]: r["engine_count"] for r in cross} == {
        r["table_id"]: len(r["engines"]) for r in mapping if len(r["engines"]) > 1
    }
    store.close()
