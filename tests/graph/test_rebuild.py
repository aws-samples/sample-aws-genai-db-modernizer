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
    graph_store.close()
