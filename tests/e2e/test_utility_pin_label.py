"""A utility pin shows "pinned (utility)", and the mean fits leave pins out (#1373).

Maintainer decision on #1381 (2026-10-22): a query the first assignment pins as a
utility or DDL statement shows "pinned (utility)" instead of a percentage in the
assignment review, the query journey and the reports, and pins stay out of the
mean-fit figures. Checked on every sample's real pipeline run, through the
assignment review API, the graph's query journey and report.json, and on a copy
of the job saved before the ``pinned`` field existed.
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
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult


def _copy(r: PipelineResult, tmp_path: Path) -> LocalArtifactStore:
    root = tmp_path / "artifacts"
    shutil.copytree(r.job_dir(), root / r.db / r.job_id)
    return LocalArtifactStore(base_dir=str(root))


def _versions(store: LocalArtifactStore, r: PipelineResult) -> dict[int, dict]:
    prefix = f"{r.db}/{r.job_id}/assignment/"
    out = {}
    for key in store.list_prefix(prefix):
        if key.endswith("/assignment.json"):
            v = int(key[len(prefix) :].split("/")[0][1:])
            out[v] = store.read_json(key)
    return out


def _utility_pins(versions: dict[int, dict]) -> set[str]:
    """The queries the first assignment pinned as utility statements."""
    return {
        m["query_id"]
        for m in versions[min(versions)].get("moves") or []
        if m.get("reason_code") == "override.utility"
    }


def _served(store: LocalArtifactStore, r: PipelineResult, monkeypatch) -> dict[str, dict]:
    monkeypatch.setattr(assignments_route, "artifact_store", store)
    resp = TestClient(app).get(f"/api/v1/assessments/{r.job_id}/assignments?database_name={r.db}")
    assert resp.status_code == 200, resp.text
    return {qa["query_id"]: qa for qa in resp.json()["assignment"]["query_assignments"]}


def _journeys(store, r: PipelineResult, tmp_path: Path, qids) -> dict[str, dict]:
    graph = GraphStore(str(tmp_path / "context.lbug"))
    try:
        initialize_schema(graph)
        rebuild_graph(r.db, r.job_id, store, graph)
        return {q: (query_journey(graph, q) or {}).get("assignment") or {} for q in qids}
    finally:
        graph.close()


def _check_labels(store, r, tmp_path, monkeypatch) -> None:
    versions = _versions(store, r)
    pins = _utility_pins(versions)
    if not r.external:
        assert pins, (r.db, "every bundled sample has utility statements")
    served = _served(store, r, monkeypatch)
    on_pin_engine = {q for q in pins if served[q]["assigned_engine"]}
    assert {q for q, qa in served.items() if qa.get("pinned") == "utility"} == on_pin_engine
    scored = sorted(q for q in served if q not in pins)[:5]
    journeys = _journeys(store, r, tmp_path, sorted(on_pin_engine) + scored)
    for q in on_pin_engine:
        assert journeys[q]["pinned"] == "utility", q
    for q in scored:
        assert journeys[q]["pinned"] is None, q
        assert served[q].get("pinned") is None, q


def test_every_utility_pin_is_labelled_in_the_review_and_the_journey(
    run: PipelineResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _copy(run, tmp_path)
    # The first assignment writes the pin on the file itself, not only on read.
    versions = _versions(store, run)
    first = {qa["query_id"]: qa for qa in versions[min(versions)]["query_assignments"]}
    assert {q for q, qa in first.items() if qa.get("pinned") == "utility"} == _utility_pins(
        versions
    )
    _check_labels(store, run, tmp_path, monkeypatch)


def test_an_assignment_saved_before_the_field_still_labels_its_pins(
    run: PipelineResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _copy(run, tmp_path)
    for v, data in _versions(store, run).items():
        for qa in data["query_assignments"]:
            qa.pop("pinned", None)
        store.write_json(f"{run.db}/{run.job_id}/assignment/v{v}/assignment.json", data)
    _check_labels(store, run, tmp_path, monkeypatch)


@pytest.mark.deterministic
def test_discourses_catalog_reads_show_the_label_not_zero(
    discourse_run: PipelineResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discourse's 36 utility pins that score 0 on Aurora PostgreSQL (26 catalog reads
    on ``pg_*`` tables, 10 whose tables the collector could not name): their
    confidence stays that real score, and every view labels them as pins."""
    store = _copy(discourse_run, tmp_path)
    pins = _utility_pins(_versions(store, discourse_run))
    served = _served(store, discourse_run, monkeypatch)
    zero = {q for q in pins if served[q]["confidence"] == 0}
    assert len(zero) == 36, len(zero)
    catalog = {
        q for q in zero if all(t.startswith("pg_") for t in served[q]["source_tables"] or ["?"])
    }
    assert len(catalog) == 26, len(catalog)
    assert all(served[q]["pinned"] == "utility" for q in zero)
    journeys = _journeys(store, discourse_run, tmp_path, sorted(zero))
    assert {j["pinned"] for j in journeys.values()} == {"utility"}
    assert {j["confidence"] for j in journeys.values()} == {0}


def _pdf_text(path: Path) -> str:
    """The PDF's text with its line breaks joined, as a reader reads it."""
    from pypdf import PdfReader

    return " ".join(" ".join(p.extract_text().split()) for p in PdfReader(str(path)).pages)


def test_the_mean_fit_leaves_pins_out_and_says_how_many(run: PipelineResult) -> None:
    report = json.loads((run.artifact_root / run.report["report"]).read_text())
    version = (report.get("query_traces") or {}).get("effective_version")
    assignment = json.loads(
        (run.job_dir() / "assignment" / f"v{version}" / "assignment.json").read_text()
    )
    in_scope = [qa for qa in assignment["query_assignments"] if qa.get("in_scope", True)]
    for entry in report["ranking"]:
        if entry.get("routed_confidence_basis") != "owned_queries":
            continue
        engine = entry["target"]
        mine = [qa for qa in in_scope if qa["assigned_engine"] == engine]
        pinned = sum(1 for qa in mine if qa.get("pinned"))
        assert entry["routed_pinned_queries"] == pinned, engine
        assert entry["routed_queries"] == len(mine) - pinned, engine
        if pinned:
            note = f"{pinned} more queries are pinned (utility), not in the mean"
            assert note in entry["rationale"], entry["rationale"]
            # Both deliverables print it, from the facts layer's rationale.
            md = (run.job_dir() / "deliverables" / "engineering-report.md").read_text()
            assert note in md, engine
            pdf = _pdf_text(run.job_dir() / "deliverables" / "executive-report.pdf")
            assert note in pdf, engine


def test_an_old_assignment_still_leaves_its_pins_out_of_the_mean(
    run: PipelineResult, tmp_path: Path
) -> None:
    """Synthesis reads a version saved before ``pinned`` existed with its pins set
    from the lineage's Moves, so the mean fits are the same as for a new run."""
    from src.agents.referee.synthesis_data import load_synthesis_data
    from src.agents.referee.synthesis_report import build_ranking

    store = _copy(run, tmp_path)
    for v, data in _versions(store, run).items():
        for qa in data["query_assignments"]:
            qa.pop("pinned", None)
        store.write_json(f"{run.db}/{run.job_id}/assignment/v{v}/assignment.json", data)
    report = json.loads((run.artifact_root / run.report["report"]).read_text())
    version = (report.get("query_traces") or {}).get("effective_version")
    synthesis = load_synthesis_data(store, run.job_id, run.db, assignment_version=version)
    keys = ("target", "routed_confidence", "routed_queries", "routed_pinned_queries")
    ranking = build_ranking(synthesis)
    rebuilt = [tuple(e.get(k) for k in keys) for e in ranking]
    assert any(e.get("routed_pinned_queries") for e in ranking), "the pins were found"
    assert sorted(rebuilt, key=str) == sorted(
        (tuple(e.get(k) for k in keys) for e in report["ranking"]), key=str
    )
