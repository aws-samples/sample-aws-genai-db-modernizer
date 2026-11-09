# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991: a change in the results page's assumptions panel re-runs only what it affects,
on a copy of the deterministic wordpress job, through the API as ``src/api/main.py``
wires it (its orchestrator runs without a model).

The job was made by the scripts, so it has no progression file: the real orchestrator
records the phases its report shows were done and runs only synthesis (a spy on
``LocalOrchestrator._run_phase``), never the assignment review or schema design.

- Growth 3: recorded with origin ``ui``; synthesis and the deliverables re-run, and the
  report, the engineering report and the results payload state "Growth: 3× (set in the
  UI)"; the routing is unchanged.
- Peak 2: schema design is not run (it designs nothing without a model, as in the job's
  own run, and the answer says so); synthesis and the deliverables re-run, and the report
  states the peak set in the UI.
- On the designed job (designs a model would write), a peak change is refused before
  anything is written: this server can't redesign them.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import _run_pipeline_cached
from tests.e2e.deliverable_figures import md_plain
from tests.e2e.pipeline import PipelineResult

pytestmark = [pytest.mark.e2e, pytest.mark.deterministic]


class _Api:
    """The FastAPI app's assumptions and results routes wired to one artifact root."""

    def __init__(self, root: Path) -> None:
        from fastapi.testclient import TestClient

        from src.api.main import app
        from src.api.routes import assumptions, results
        from src.api.services.local_execution import LocalExecutionService
        from src.api.services.local_s3 import LocalS3Service
        from src.orchestrator.local_orchestrator import LocalOrchestrator
        from src.storage.local_store import LocalArtifactStore

        store = LocalArtifactStore(base_dir=str(root))
        sfn = LocalExecutionService(store)
        self._modules: tuple[Any, Any] = (assumptions, results)
        self._saved = (
            assumptions.artifact_store,
            assumptions.orchestrator,
            assumptions.sfn_service,
            results.sfn_service,
            results.s3_service,
        )
        assumptions.artifact_store = store
        assumptions.orchestrator = LocalOrchestrator(store=store, llm_mode="none")
        assumptions.sfn_service = sfn
        results.sfn_service = sfn
        results.s3_service = LocalS3Service(store)
        self.client = TestClient(app)

    def close(self) -> None:
        assumptions: Any
        results: Any
        assumptions, results = self._modules
        (
            assumptions.artifact_store,
            assumptions.orchestrator,
            assumptions.sfn_service,
            results.sfn_service,
            results.s3_service,
        ) = self._saved

    def post(self, job: str, body: dict[str, Any]) -> Any:
        return self.client.post(f"/api/v1/assessments/{job}/assumptions", json=body)


def _copy(run: PipelineResult, base: Path) -> Path:
    root = base / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    meta = run.artifact_root / ".meta" / f"{run.job_id}.json"
    if meta.exists():
        (root / ".meta").mkdir(parents=True, exist_ok=True)
        shutil.copy2(meta, root / ".meta" / meta.name)
    return root


def _report(root: Path, run: PipelineResult) -> dict:
    reports = sorted((root / run.db / run.job_id / "synthesis").glob("v*/report.json"))
    report: dict = json.loads(reports[-1].read_text())
    return report


@pytest.fixture
def wordpress(
    e2e_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_Api, PipelineResult, Path]]:
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    run = _run_pipeline_cached("wordpress", e2e_root / "artifacts", job_id="e2e-word")
    root = _copy(run, tmp_path)
    assert not (root / ".meta" / f"{run.job_id}.json").exists()  # a script-made job
    real = LocalOrchestrator._run_phase
    ran: list[str] = []

    def spy(self, job_id, phase, config=None, scope=None):  # type: ignore[no-untyped-def]
        ran.append(phase.value)
        return real(self, job_id, phase, config=config, scope=scope)

    monkeypatch.setattr(LocalOrchestrator, "_run_phase", spy)
    api = _Api(root)
    api.ran = ran  # type: ignore[attr-defined]
    try:
        yield api, run, root
    finally:
        api.close()


def _assignments(root: Path, run: PipelineResult) -> list[bytes]:
    job = root / run.db / run.job_id
    return [p.read_bytes() for p in sorted(job.glob("assignment/v*/assignment.json"))]


def test_a_growth_change_reruns_synthesis_and_the_deliverables(wordpress) -> None:
    api, run, root = wordpress
    before = _report(root, run)
    assignments = _assignments(root, run)
    resp = api.post(run.job_id, {"growth": 3})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reran"] == ["synthesis", "report"]
    assert api.ran == ["synthesis"]  # type: ignore[attr-defined]
    assert resp.json()["recorded_done"] == ["assignment_review", "schema_design", "synthesis"]
    assert _assignments(root, run) == assignments
    assert resp.json()["deliverables"] in ("complete", "partial")
    decision = json.loads((root / run.db / run.job_id / "decisions/assumptions.json").read_text())
    assert decision["origins"] == {"growth_multiplier": "ui"}
    after = _report(root, run)
    growth = after["facts"]["assumptions"]["assumptions"][0]
    assert (growth["setting"], growth["origin"]) == (3.0, "ui")
    assert growth["line"].startswith("Growth: 3× (set in the UI)")
    assert after["facts"]["growth_scenarios"]["origin"] == "ui"
    # Growth reaches no routing figure.
    assert (
        after["facts"]["projected_monthly_cost_usd"]
        == before["facts"]["projected_monthly_cost_usd"]
    )
    md = md_plain(
        (root / run.db / run.job_id / "deliverables" / "engineering-report.md").read_text()
    )
    assert md.count(f"{growth['line']}.") == 1
    panel = api.client.get(f"/api/v1/assessments/{run.job_id}/assumptions").json()
    assert panel["assumptions"][0]["value"] == 3.0
    assert panel["assumptions"][0]["origin_label"] == "set in the UI"
    payload = api.client.get(f"/api/v1/assessments/{run.job_id}/results").json()
    assert payload["synthesis"]["facts"]["assumptions"]["lines"][0] == growth["line"]


def test_a_peak_change_runs_no_schema_design_without_a_model_and_says_so(wordpress) -> None:
    api, run, root = wordpress
    assignments = _assignments(root, run)
    resp = api.post(run.job_id, {"peak": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reran"] == ["synthesis", "report"] and body["skipped"] == ["schema_design"]
    assert api.ran == ["synthesis"]  # type: ignore[attr-defined]
    assert _assignments(root, run) == assignments
    assert "no design was written" in body["notes"][0]
    peak = _report(root, run)["facts"]["assumptions"]["assumptions"][1]
    assert peak["line"] == "Peak: 2× peak-to-average ratio (set in the UI)"
    progression = json.loads((root / ".meta" / f"{run.job_id}.json").read_text())
    assert progression["phases"]["schema_design"]["status"] == "skipped"
    assert not list((root / run.db / run.job_id).glob("schema-*/v*/schema_output.json"))


def test_designs_a_model_wrote_are_never_redesigned_here(
    designed_run: PipelineResult, tmp_path: Path
) -> None:
    root = _copy(designed_run, tmp_path)
    api = _Api(root)
    try:
        designs = sorted((root / designed_run.db / designed_run.job_id).glob("schema-*/v*/*.json"))
        before = {p: p.read_bytes() for p in designs}
        resp = api.post(designed_run.job_id, {"peak": 2})
        assert resp.status_code == 409, resp.text
        assert "Nothing was changed" in resp.json()["detail"]
        job = root / designed_run.db / designed_run.job_id
        assert not (job / "decisions" / "assumptions.json").exists()
        assert {p: p.read_bytes() for p in designs} == before
    finally:
        api.close()
