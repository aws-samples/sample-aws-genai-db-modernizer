# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for rebuild_graph — full graph reconstruction from artifacts."""

import json
from pathlib import Path

import pytest

from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.local_store import LocalArtifactStore


@pytest.fixture
def artifact_dir(tmp_path, sample_collector_output, sample_triage_output, sample_assignment):
    """Write sample artifacts to a local store directory."""
    db_name = "testdb"
    job_id = "job-123"
    base = tmp_path / "artifacts"
    base.mkdir()

    def _write(path, data):
        full = base / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(data))

    _write(f"{db_name}/{job_id}/collector/output.json", sample_collector_output)
    _write(f"{db_name}/{job_id}/referee-triage/triage.json", sample_triage_output)
    _write(f"{db_name}/{job_id}/assignment/v1/assignment.json", sample_assignment)

    return base, db_name, job_id


def test_rebuild_graph_populates_from_artifacts(artifact_dir):
    """rebuild_graph reads artifacts and populates the graph."""
    base, db_name, job_id = artifact_dir
    store = LocalArtifactStore(str(base))
    graph_path = str(base / db_name / job_id / "graph" / "context.lbug")
    Path(graph_path).parent.mkdir(parents=True, exist_ok=True)
    graph_store = GraphStore(graph_path)
    initialize_schema(graph_store)

    stats = rebuild_graph(db_name, job_id, store, graph_store)

    assert stats["nodes_created"] > 0
    queries = graph_store.query("MATCH (q:Query) RETURN COUNT(q) AS c")
    assert queries[0]["c"] == 3
    signals = graph_store.query("MATCH (s:Signal) RETURN COUNT(s) AS c")
    assert signals[0]["c"] == 2
    graph_store.close()


def test_rebuild_graph_skips_missing_artifacts(artifact_dir):
    """rebuild_graph doesn't fail when some artifacts are missing."""
    base, db_name, job_id = artifact_dir
    store = LocalArtifactStore(str(base))
    graph_path = str(base / db_name / job_id / "graph" / "context.lbug")
    Path(graph_path).parent.mkdir(parents=True, exist_ok=True)
    graph_store = GraphStore(graph_path)
    initialize_schema(graph_store)

    # No schema-design or load-test artifacts exist — should not raise
    stats = rebuild_graph(db_name, job_id, store, graph_store)
    assert stats["nodes_created"] > 0
    graph_store.close()


def test_rebuild_graph_reads_real_pipeline_paths(
    artifact_dir,
    sample_schema_design_output,
    sample_load_test_output,
    sample_synthesis_output,
):
    """rebuild_graph reads schema, load-test, and synthesis at the paths the pipeline writes.

    Regression guard: these three populators used to read paths that did not
    match what the pipeline emits, so they were silent no-ops. This test writes
    artifacts at the real paths and asserts their nodes land in the graph.
    """
    base, db_name, job_id = artifact_dir

    def _write(path, data):
        full = base / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(data))

    _write(
        f"{db_name}/{job_id}/schema-dynamodb/v1/schema_output.json",
        sample_schema_design_output,
    )
    _write(
        f"{db_name}/{job_id}/load-test-dynamodb/v1/results/summary.json",
        sample_load_test_output,
    )
    _write(f"{db_name}/{job_id}/referee-synthesis/report.json", sample_synthesis_output)

    store = LocalArtifactStore(str(base))
    graph_path = str(base / db_name / job_id / "graph" / "context.lbug")
    Path(graph_path).parent.mkdir(parents=True, exist_ok=True)
    graph_store = GraphStore(graph_path)
    initialize_schema(graph_store)

    rebuild_graph(db_name, job_id, store, graph_store)

    access_patterns = graph_store.query("MATCH (ap:AccessPattern) RETURN COUNT(ap) AS c")
    assert access_patterns[0]["c"] == 1
    runs = graph_store.query("MATCH (lt:LoadTestRun) RETURN COUNT(lt) AS c")
    assert runs[0]["c"] == 1
    risks = graph_store.query("MATCH (r:Risk) RETURN COUNT(r) AS c")
    assert risks[0]["c"] == 1
    agents = graph_store.query("MATCH (a:Agent) RETURN COUNT(a) AS c")
    assert agents[0]["c"] > 0
    produced = graph_store.query("MATCH ()-[:PRODUCED_BY]->(:Agent) RETURN COUNT(*) AS c")
    assert produced[0]["c"] > 0
    graph_store.close()


def _rebuild_with_files(artifact_dir, files):
    base, db_name, job_id = artifact_dir
    for path, data in files.items():
        full = base / db_name / job_id / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(data))
    store = LocalArtifactStore(str(base))
    graph_path = str(base / db_name / job_id / "graph" / "context.lbug")
    Path(graph_path).parent.mkdir(parents=True, exist_ok=True)
    graph_store = GraphStore(graph_path)
    initialize_schema(graph_store)
    rebuild_graph(db_name, job_id, store, graph_store)
    return base, db_name, job_id, store, graph_store


def _risk_ids(graph_store):
    return sorted(r["id"] for r in graph_store.query("MATCH (r:Risk) RETURN r.id AS id"))


def _report(risk_id):
    return {
        "risk_assessment": {
            "risks": [
                {
                    "risk_id": risk_id,
                    "risk_type": "t",
                    "severity": "high",
                    "description": "d",
                    "mitigation": "m",
                }
            ]
        }
    }


def test_rebuild_graph_reads_versioned_synthesis_report(artifact_dir, sample_assignment):
    """#1520: the report synthesis writes at synthesis/v<N>/report.json feeds Risk nodes."""
    files = {
        "assignment/v2/assignment.json": sample_assignment,
        "synthesis/v2/report.json": _report("risk-v2"),
        "synthesis/v1/report.json": _report("risk-v1"),
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    assert _risk_ids(graph_store) == ["risk-v2"]
    graph_store.close()


def test_rebuild_graph_versioned_report_wins_over_legacy(artifact_dir, sample_assignment):
    files = {
        "assignment/v1/assignment.json": sample_assignment,
        "synthesis/v1/report.json": _report("risk-v1"),
        "referee-synthesis/report.json": _report("risk-legacy"),
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    assert _risk_ids(graph_store) == ["risk-v1"]
    graph_store.close()


@pytest.mark.passes_on_main
def test_rebuild_graph_old_layout_falls_back_to_legacy_report(artifact_dir, sample_assignment):
    """An old job (assignment, legacy synthesis key only) still gets its Risk nodes."""
    files = {
        "assignment/v1/assignment.json": sample_assignment,
        "referee-synthesis/report.json": _report("risk-legacy"),
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    assert _risk_ids(graph_store) == ["risk-legacy"]
    graph_store.close()


def test_inputs_digest_covers_versioned_synthesis_report(artifact_dir, sample_assignment):
    """#1520: rewriting synthesis/v<N>/report.json changes the digest, so the graph is rebuilt."""
    from src.graph.inputs_digest import graph_inputs_digest

    base, db_name, job_id, store, graph_store = _rebuild_with_files(
        artifact_dir,
        {
            "assignment/v1/assignment.json": sample_assignment,
            "synthesis/v1/report.json": _report("risk-a"),
        },
    )
    before = graph_inputs_digest(store, db_name, job_id)
    (base / db_name / job_id / "synthesis/v1/report.json").write_text(json.dumps(_report("risk-b")))
    assert graph_inputs_digest(store, db_name, job_id) != before
    graph_store.close()


# --- Schema design and load test: every engine, the effective version (#306) ---


def _aurora_design(engine, query_id="q1"):
    """An Aurora schema output as the schema-design merge writes it: one pattern
    per source query, keyed on the query id, linked by ``source_query_ids``."""
    return {
        "target_engine": engine,
        "migration_strategy": "carry_over",
        "access_patterns": [
            {
                "pattern_id": query_id,
                "description": "Orders by customer",
                "operation": "SELECT",
                "sql_text": "SELECT * FROM orders WHERE customer_id = ?",
                "source_tables": ["orders"],
                "source_query_ids": [query_id],
                "index_used": "idx_orders_customer_id",
                "design_rps": 423.0,
                "script_derived": True,
                "in_scope": True,
                "load_testable": True,
            }
        ],
        "trade_offs": [
            {
                "description": "Keep the orders table as is",
                "impact": "No application change",
                "query_ids": [query_id],
            }
        ],
    }


def _design(pattern_id, query_ids=("q1",)):
    return {
        "access_patterns": [
            {
                "pattern_id": pattern_id,
                "description": pattern_id,
                "operation": "GetItem",
                "design_rps": 10.0,
                "query_ids": list(query_ids),
                "in_scope": True,
            }
        ]
    }


def _patterns(graph_store):
    return sorted(
        (r["id"], r["engine"], r["ver"])
        for r in graph_store.query(
            "MATCH (ap:AccessPattern) RETURN ap.id AS id, ap.engine AS engine, "
            "ap.schema_version AS ver"
        )
    )


def _load_test_runs(graph_store):
    return sorted(
        (r["engine"], r["ver"])
        for r in graph_store.query(
            "MATCH (lt:LoadTestRun) RETURN lt.engine AS engine, lt.schema_version AS ver"
        )
    )


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_rebuild_graph_reads_aurora_schema_design(artifact_dir, engine):
    """An Aurora design's access patterns and trade-offs land in the graph,
    linked to their source queries."""
    *_, graph_store = _rebuild_with_files(
        artifact_dir, {f"schema-{engine}/v1/schema_output.json": _aurora_design(engine)}
    )
    assert _patterns(graph_store) == [("q1", engine, 1)]
    linked = graph_store.query(
        "MATCH (q:Query)-[:PART_OF]->(ap:AccessPattern) RETURN q.id AS q, ap.id AS ap"
    )
    assert linked == [{"q": "q1", "ap": "q1"}]
    informed = graph_store.query(
        "MATCH (d:Decision {category: 'trade_off'})-[:INFORMED_BY]->(q:Query) "
        "RETURN d.id AS d, q.id AS q"
    )
    assert informed == [{"d": f"trade_off-{engine}-0", "q": "q1"}]
    graph_store.close()


# The engines schema design can design (scripts/run_schema_design.py's
# _ALL_ENGINES), pinned here so a wrong catalog filter can't pass the tests below.
_DESIGN_ENGINES = [
    "aurora_mysql",
    "aurora_postgresql",
    "documentdb",
    "dynamodb",
    "elasticache",
    "opensearch",
]


@pytest.mark.passes_on_main
def test_the_catalog_engines_are_the_designable_engines():
    """The rebuild reads the catalog's active engines: exactly the six schema design
    designs, so no designed engine is skipped and no other engine is read."""
    from scripts.run_schema_design import _ALL_ENGINES
    from src.shared import catalog

    assert sorted(catalog.engine_ids(status=catalog.ACTIVE)) == _DESIGN_ENGINES
    assert sorted(_ALL_ENGINES) == _DESIGN_ENGINES


def test_rebuild_graph_reads_the_design_of_every_catalog_engine(artifact_dir):
    """Every designable engine's design is read, so a new engine is never skipped
    by a hard-coded list; a legacy or future catalog engine's is not."""
    files = {
        f"schema-{engine}/v1/schema_output.json": _design(f"AP-{engine}")
        for engine in [*_DESIGN_ENGINES, "aurora", "keyspaces", "neptune"]
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    assert _patterns(graph_store) == sorted((f"AP-{e}", e, 1) for e in _DESIGN_ENGINES)
    graph_store.close()


@pytest.mark.passes_on_main
def test_rebuild_graph_reads_the_analysis_of_every_catalog_engine(artifact_dir):
    """The analysis loop reads the same engine list as schema design and load test."""
    files = {
        f"analysis-{engine}/analysis.json": {
            "workload_analysis": {
                "anti_patterns_detected": [
                    {
                        "anti_pattern_id": f"AP-{engine}",
                        "anti_pattern_type": "t",
                        "query_ids": ["q1"],
                    }
                ]
            }
        }
        for engine in _DESIGN_ENGINES
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    rows = graph_store.query(
        "MATCH (ap:AntiPattern)-[:PRODUCED_BY]->(a:Agent) RETURN ap.id AS ap, a.id AS agent"
    )
    assert sorted((r["ap"], r["agent"]) for r in rows) == [
        (f"AP-{e}", f"analysis-{e}") for e in _DESIGN_ENGINES
    ]
    graph_store.close()


def test_rebuild_graph_reads_design_and_load_test_past_version_10(
    artifact_dir, sample_assignment, sample_load_test_output
):
    """No cap at v10: the effective version v11's design and load test are read."""
    files = {
        "assignment/v11/assignment.json": {**sample_assignment, "version": 11},
        "schema-dynamodb/v11/schema_output.json": _design("DDB-AP-11"),
        "load-test-dynamodb/v11/results/summary.json": sample_load_test_output,
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    assert _patterns(graph_store) == [("DDB-AP-11", "dynamodb", 11)]
    assert _load_test_runs(graph_store) == [("dynamodb", 11)]
    graph_store.close()


def test_rebuild_graph_after_a_rollback_reads_the_restated_design(
    artifact_dir, sample_assignment, sample_load_test_output
):
    """v2 is a post-schema reroute whose redesign failed; v3 rolls it back and
    restates v1 with its designs. The graph reads v3's (v1's) designs, never the
    abandoned v2's, even for an engine v3 has no design for."""
    files = {
        "assignment/v2/assignment.json": {
            **sample_assignment,
            "version": 2,
            "previous_version": 1,
            "status": "rerouted_after_approval",
        },
        "assignment/v3/assignment.json": {
            **sample_assignment,
            "version": 3,
            "previous_version": 2,
            "status": "reroute_rolled_back",
            "rolled_back_version": 2,
        },
        "schema-dynamodb/v1/schema_output.json": _design("DDB-AP-1"),
        "schema-dynamodb/v2/schema_output.json": _design("DDB-AP-ABANDONED"),
        "schema-dynamodb/v3/schema_output.json": _design("DDB-AP-1"),
        # The abandoned reroute's redesign of an engine v3 does not restate.
        "schema-documentdb/v2/schema_output.json": _design("DOC-AP-ABANDONED"),
        "load-test-documentdb/v2/results/summary.json": sample_load_test_output,
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    assert _patterns(graph_store) == [("DDB-AP-1", "dynamodb", 3)]
    assert _load_test_runs(graph_store) == []
    graph_store.close()


def test_rebuild_graph_ignores_a_design_of_an_older_version(artifact_dir, sample_assignment):
    """A customer edit made v2 and schema design has not run on it yet: v1's
    design is stale (ADR-028), so the graph does not present it as current."""
    files = {
        "assignment/v2/assignment.json": {**sample_assignment, "version": 2},
        "schema-dynamodb/v1/schema_output.json": _design("DDB-AP-1"),
    }
    *_, graph_store = _rebuild_with_files(artifact_dir, files)
    assert _patterns(graph_store) == []
    graph_store.close()


@pytest.mark.passes_on_main
def test_rebuild_graph_without_a_design_has_no_design_nodes(artifact_dir):
    """A job with no schema design or load test builds as before: complete, with
    no access pattern or load-test nodes."""
    base, db_name, job_id = artifact_dir
    store = LocalArtifactStore(str(base))
    graph_path = str(base / db_name / job_id / "graph" / "context.lbug")
    Path(graph_path).parent.mkdir(parents=True, exist_ok=True)
    graph_store = GraphStore(graph_path)
    initialize_schema(graph_store)
    stats = rebuild_graph(db_name, job_id, store, graph_store)
    assert stats["complete"] is True
    assert _patterns(graph_store) == []
    assert _load_test_runs(graph_store) == []
    assert graph_store.query("MATCH (q:Query) RETURN COUNT(q) AS c")[0]["c"] == 3
    graph_store.close()


@pytest.mark.passes_on_main
def test_rebuild_graph_without_an_assignment_reads_the_v1_design(tmp_path, sample_collector_output):
    """A job with no assignment yet: schema design writes v1 (ADR-026), and it is read."""
    base = tmp_path / "artifacts"
    for path, data in {
        "collector/output.json": sample_collector_output,
        "schema-dynamodb/v1/schema_output.json": _design("DDB-AP-1"),
    }.items():
        full = base / "testdb" / "job-1" / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(data))
    graph_store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(graph_store)
    rebuild_graph("testdb", "job-1", LocalArtifactStore(str(base)), graph_store)
    assert _patterns(graph_store) == [("DDB-AP-1", "dynamodb", 1)]
    graph_store.close()


_REAL_RUN = Path(__file__).parents[1] / "fixtures/real_runs/wordpress-both/job"


def test_rebuild_graph_reads_the_aurora_design_of_a_real_run(tmp_path):
    """A real wordpress run (effective v2): its Aurora MySQL design's 68 access
    patterns land at v2, each linked to its source query."""
    import shutil

    base = tmp_path / "artifacts"
    shutil.copytree(_REAL_RUN, base / "wordpress" / "job")
    design = json.loads((_REAL_RUN / "schema-aurora_mysql/v2/schema_output.json").read_text())
    expected = sorted(
        (qid, ap["pattern_id"])
        for ap in design["access_patterns"]
        for qid in ap["source_query_ids"]
    )
    graph_store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(graph_store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), graph_store)

    aurora = [p for p in _patterns(graph_store) if p[1] == "aurora_mysql"]
    assert len(aurora) == 68
    assert {p[2] for p in aurora} == {2}
    linked = graph_store.query(
        "MATCH (q:Query)-[:PART_OF]->(ap:AccessPattern {engine: 'aurora_mysql'}) "
        "RETURN q.id AS q, ap.id AS ap"
    )
    assert len(expected) == 68
    assert sorted((r["q"], r["ap"]) for r in linked) == expected
    graph_store.close()


def test_rebuilding_twice_on_one_handle_gives_the_same_graph(artifact_dir):
    """``rebuild_graph`` clears the graph and creates its tables again. The engine's
    connection caches each prepared statement by its text, pointing at the dropped
    tables, so a second rebuild on the same handle used to write nothing: the
    graph came out empty. ``clear`` now replaces the connection."""
    base, db_name, job_id = artifact_dir
    store = LocalArtifactStore(str(base))
    graph_store = GraphStore(str(base / "twice.lbug"))
    initialize_schema(graph_store)
    first = rebuild_graph(db_name, job_id, store, graph_store)
    second = rebuild_graph(db_name, job_id, store, graph_store)
    assert first["nodes_created"] > 3
    assert (second["nodes_created"], second["edges_created"]) == (
        first["nodes_created"],
        first["edges_created"],
    )
    assert graph_store.query("MATCH (q:Query) RETURN COUNT(q) AS c")[0]["c"] == 3
    graph_store.close()
