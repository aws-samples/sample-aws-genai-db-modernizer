# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SCORED edges on every sample run (#306), deterministic or a headless run's job.

The expectations are rebuilt here from the job's own artifacts, independently of the
populator: each query's last recorded ladder walking the lineage, the edges of each
version it moves in and of the effective version, ``assigned`` from each version's
assignment, ``current`` on the effective version, and the recorded score dimensions
(#1471) only where their score is the edge's. Read from the run's artifacts, so
routing drift in a sample does not break it; a headless run adds a customer gate.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.shared.report_facts import detail_round
from src.storage.assignment_versioning import resolve_effective_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult


def _lineage(job: Path, effective: int) -> list[dict]:
    chain, version = [], effective
    while isinstance(version, int) and version >= 1:
        path = job / f"assignment/v{version}/assignment.json"
        if not path.exists():
            break
        assignment = json.loads(path.read_text())
        chain.append(assignment)
        version = assignment.get("previous_version")
    return list(reversed(chain))


def _record_entry(record: dict | None, qid: str, engine: str) -> dict | None:
    for row in (record or {}).get("queries") or []:
        if row.get("query_id") == qid:
            return (row.get("score_dimensions") or {}).get(engine)
    return None


def test_scored_edges_match_the_recorded_ladders(run: PipelineResult, tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    job = root / run.db / run.job_id
    shutil.copytree(run.job_dir(), job)
    store = LocalArtifactStore(base_dir=str(root))
    graph = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(graph)
    rebuild_graph(run.db, run.job_id, store, graph)

    effective = resolve_effective_assignment_version(store, run.db, run.job_id)
    expected, ladder, explained = {}, {}, 0
    for assignment in _lineage(job, effective):
        v = assignment["version"]
        record_path = job / f"assignment/v{v}/access_patterns.json"
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        moves = sorted(assignment.get("moves") or [], key=lambda m: m["seq"])
        for m in moves:
            if (m.get("evidence") or {}).get("candidates"):
                ladder[m["query_id"]] = (v, m)
        assigned = {qa["query_id"]: qa["assigned_engine"] for qa in assignment["query_assignments"]}
        wanted = {m["query_id"] for m in moves}
        if v == effective:
            wanted |= set(assigned)
        for qid in wanted & set(ladder):
            lv, m = ladder[qid]
            for rung in m["evidence"]["candidates"]:
                entry = _record_entry(record, qid, rung["engine"])
                recorded = detail_round(entry.get("fit", entry.get("base"))) if entry else None
                agrees = recorded is not None and recorded == detail_round(rung["score"])
                explained += agrees
                expected[(qid, v, rung["engine"])] = (
                    rung["score"],
                    rung["gate"],
                    rung["engine"] == assigned.get(qid),
                    v == effective,
                    lv,
                    entry if agrees else None,
                )

    rows = graph.query(
        "MATCH (q:Query)-[s:SCORED]->(e:Engine) RETURN q.id AS q, s.version AS v, e.id AS e, "
        "s.score AS score, s.gate AS gate, s.assigned AS assigned, s.current AS current, "
        "s.ladder_version AS lv, s.dimensions_json AS dims"
    )
    found = {}
    for r in rows:
        dims = json.loads(r["dims"]) if r["dims"] else None
        if dims:
            dims.pop("tables", None)
        found[(r["q"], r["v"], r["e"])] = (
            r["score"],
            r["gate"],
            r["assigned"],
            r["current"],
            r["lv"],
            dims,
        )
    assert len(rows) == len(found) == len(expected)
    assert found == expected
    # Exactly one assigned engine per query on its current ladder, when the ladder has it.
    current_assigned = {}
    for (q, _v, e), value in found.items():
        if value[3] and value[2]:
            current_assigned.setdefault(q, []).append(e)
    assert all(len(engines) == 1 for engines in current_assigned.values())
    if not run.external:
        # A fresh deterministic run writes version 4 records: the dimensions are there.
        assert explained > 0
    graph.close()
