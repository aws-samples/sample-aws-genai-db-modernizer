# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Risks in the context graph on every deterministic sample (#306).

The graph built from each sample's deterministic run holds the report's open
risks (``status = 'active'``) and the risks the assignment resolved
(``status = 'resolved'``), each linked by EVIDENCED_BY to the queries it names.
The counts are exact for the deterministic runs: a change to them is a change to
the synthesis risks or to the graph, and must be explained.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.assignment_versioning import resolve_effective_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic


def test_graph_risks_and_their_evidence(run: PipelineResult, tmp_path: Path) -> None:
    """Per status, one Risk node per report risk, and one EVIDENCED_BY edge per
    (risk, query id) pair the report names whose Query node exists. Read from the
    run's own report, so routing drift in a sample does not break it."""
    root = tmp_path / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    store = LocalArtifactStore(base_dir=str(root))
    graph = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(graph)
    rebuild_graph(run.db, run.job_id, store, graph)

    version = resolve_effective_assignment_version(store, run.db, run.job_id)
    report = json.loads(
        (root / run.db / run.job_id / f"synthesis/v{version}/report.json").read_text()
    )
    assessment = report["risk_assessment"]
    collector = json.loads((root / run.db / run.job_id / "collector/output.json").read_text())
    known = {q["query_id"] for q in collector["queries"]["query_patterns"]}

    risks = {
        r["s"]: r["c"] for r in graph.query("MATCH (r:Risk) RETURN r.status AS s, COUNT(r) AS c")
    }
    edges = {
        r["s"]: r["c"]
        for r in graph.query(
            "MATCH (r:Risk)-[e:EVIDENCED_BY]->(:Query) RETURN r.status AS s, COUNT(e) AS c"
        )
    }
    for status, key in (("active", "risks"), ("resolved", "resolved_risks")):
        listed = assessment.get(key) or []
        named = sum(len(set(r.get("query_ids") or []) & known) for r in listed)
        assert risks.get(status, 0) == len(listed), status
        assert edges.get(status, 0) == named, status
    assert risks.get("resolved", 0) > 0 and sum(edges.values()) > 0

    # The resolved risks' explanation, from the report synthesis wrote in this run:
    # a key renamed in synthesis_report._resolved_risk leaves a column null here.
    nodes = {
        r["id"]: r
        for r in graph.query(
            "MATCH (r:Risk {status: 'resolved'}) RETURN r.id AS id, r.engine AS engine, "
            "r.resolved_on AS resolved_on, r.reason AS reason"
        )
    }
    for n, entry in enumerate(assessment["resolved_risks"], start=1):
        node = nodes[f"RESOLVED-{n:03d}"]
        for field in ("engine", "resolved_on", "reason"):
            assert node[field] and node[field] == entry.get(field), field
    graph.close()
