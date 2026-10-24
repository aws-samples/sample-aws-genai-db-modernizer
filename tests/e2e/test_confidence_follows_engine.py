"""A query's confidence is its score on the engine it ends on, everywhere (#1373).

The reality check consolidates queries off engines it removes. Each such query's
``confidence`` in the version it writes is its score on the engine it lands on
(the score its Move records, the report's ``query_traces[].final_score``), not
the score of the engine it left: in the assignment file, the assignment review
API (``GET .../assignments``) and the query journey the graph serves (the UI's
"Workload Fit"). An assignment saved before the fix is corrected on read from
its lineage's Moves. Checked on every sample's real pipeline run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assignments as assignments_route
from src.graph.populators import rebuild_graph
from src.graph.queries import query_journey
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.shared.query_trace import assignment_lineage
from src.storage.assignment_versioning import resolve_effective_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult


def _store(r: PipelineResult) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(r.artifact_root))


def _copy(r: PipelineResult, tmp_path: Path) -> tuple[PipelineResult, LocalArtifactStore]:
    """A private copy of the job: the API and the graph never touch the shared run."""
    root = tmp_path / "artifacts"
    shutil.copytree(r.job_dir(), root / r.db / r.job_id)
    copy = PipelineResult(
        db=r.db, job_id=r.job_id, artifact_root=root, steps=r.steps, external=r.external
    )
    return copy, LocalArtifactStore(base_dir=str(root))


def _lineage(store: LocalArtifactStore, r: PipelineResult) -> list[dict]:
    version = resolve_effective_assignment_version(store, r.db, r.job_id)
    path = f"{r.db}/{r.job_id}/assignment/v{{}}/assignment.json"
    return assignment_lineage(
        lambda v: store.read_json(path.format(v)) if store.exists(path.format(v)) else None,
        version,
    )


def _moved(effective: dict) -> dict[str, dict]:
    """The queries the effective version's own Moves took to another engine."""
    return {
        m["query_id"]: m
        for m in effective.get("moves") or []
        if m.get("from_engine") and m["from_engine"] != m["to_engine"]
    }


def _score(move: dict) -> int:
    """The query's score on the engine its Move lands on, by the ledger's own rule
    (``query_score``: the query's ladder rung, else ``score_after``)."""
    from src.agents.referee.move_ledger import query_score
    from src.contracts.move_ledger import Move

    score = query_score(Move.model_validate(move))
    assert score is not None, move["query_id"]
    return round(score)


def _final_scores(r: PipelineResult) -> dict[str, float]:
    """``query_traces[].final_score`` of every query report.json traces."""
    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    return {
        t["query_id"]: t["final_score"]
        for t in (report.get("query_traces") or {}).get("queries") or []
        if t.get("final_score") is not None
    }


def test_every_version_holds_its_engines_score(run: PipelineResult) -> None:
    """Every version's confidences already equal their Moves' scores: reading the
    lineage's Moves back corrects nothing, and each traced query's confidence is
    the report's final score for it."""
    from src.agents.referee.move_ledger import confidence_from_moves

    store = _store(run)
    lineage = _lineage(store, run)
    assert lineage, "no assignment"
    for i, version in enumerate(lineage):
        copy = json.loads(json.dumps(version))
        assert confidence_from_moves(copy, lineage[: i + 1]) == [], (run.db, version["version"])
    effective = {qa["query_id"]: qa for qa in lineage[-1]["query_assignments"]}
    finals = _final_scores(run)
    assert finals, "report.json traces no query"
    wrong = {
        qid: (effective[qid]["confidence"], score)
        for qid, score in finals.items()
        if effective[qid]["confidence"] != round(score)
    }
    assert not wrong, (run.db, "confidence vs report final_score", wrong)


def test_a_consolidated_query_shows_its_new_engines_score(
    run: PipelineResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the assignment file, the review API and the graph's query journey."""
    copy, store = _copy(run, tmp_path)
    lineage = _lineage(store, run)
    moved = _moved(lineage[-1])
    if not run.external:
        assert moved, (run.db, "the reality check consolidates queries on every sample")
    effective = {qa["query_id"]: qa for qa in lineage[-1]["query_assignments"]}
    expected = {qid: _score(m) for qid, m in moved.items()}
    assert {q: effective[q]["confidence"] for q in moved} == expected, run.db

    monkeypatch.setattr(assignments_route, "artifact_store", store)
    resp = TestClient(app).get(
        f"/api/v1/assessments/{run.job_id}/assignments?database_name={run.db}"
    )
    assert resp.status_code == 200, resp.text
    served = {qa["query_id"]: qa for qa in resp.json()["assignment"]["query_assignments"]}
    assert {q: served[q]["confidence"] for q in moved} == expected, run.db

    journeys = _journey_confidences(copy, store, tmp_path, list(moved))
    assert journeys == expected, run.db


def _journey_confidences(
    r: PipelineResult, store: LocalArtifactStore, tmp_path: Path, qids: list[str]
) -> dict[str, int]:
    graph = GraphStore(str(tmp_path / "context.lbug"))
    try:
        initialize_schema(graph)
        rebuild_graph(r.db, r.job_id, store, graph)
        out: dict[str, int] = {}
        for qid in qids:
            journey = query_journey(graph, qid)
            assert journey is not None, qid
            out[qid] = journey["assignment"]["confidence"]
        return out
    finally:
        graph.close()


def test_an_assignment_saved_before_the_fix_is_corrected_on_read(
    run: PipelineResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy of the job whose effective version carries, as main wrote it, each
    consolidated query's score on the engine it left: the review API and the
    journey still show the engine it is on's score; the file is not rewritten."""
    copy, store = _copy(run, tmp_path)
    lineage = _lineage(store, copy)
    effective = lineage[-1]
    moved = {
        q: m
        for q, m in _moved(effective).items()
        if m.get("score_before") is not None and round(m["score_before"]) != _score(m)
    }
    if not run.external:
        assert moved, (run.db, "a consolidation that changes the score")
    expected = {qid: _score(m) for qid, m in moved.items()}
    for qa in effective["query_assignments"]:
        if qa["query_id"] in moved:
            qa["confidence"] = round(moved[qa["query_id"]]["score_before"])
    path = f"{run.db}/{run.job_id}/assignment/v{effective['version']}/assignment.json"
    store.write_json(path, effective)

    monkeypatch.setattr(assignments_route, "artifact_store", store)
    resp = TestClient(app).get(
        f"/api/v1/assessments/{run.job_id}/assignments?database_name={run.db}"
    )
    assert resp.status_code == 200, resp.text
    served = {qa["query_id"]: qa for qa in resp.json()["assignment"]["query_assignments"]}
    assert {q: served[q]["confidence"] for q in moved} == expected, run.db
    assert _journey_confidences(copy, store, tmp_path, list(moved)) == expected, run.db
    on_disk = {
        qa["query_id"]: qa["confidence"] for qa in store.read_json(path)["query_assignments"]
    }
    assert all(on_disk[q] != expected[q] for q in moved), "the stored file was rewritten"
