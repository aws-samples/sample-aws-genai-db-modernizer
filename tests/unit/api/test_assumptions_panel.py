# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991: the results page's assumptions panel (``src.api.routes.assumptions``).

A change is checked by the assumptions file's own parser, recorded with origin ``ui`` and
re-runs only the phases it needs through the orchestrator's resume: synthesis and the
deliverables (schema design too for the peak, which a server without a model does not
run and says so). Only a finished job changes, one change to a job at a time. A re-run
this server can't make is refused before anything is written, and a failed one puts the
job back as it was. The route is guarded as every write route is, and needs a JSON
content type.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assumptions as route
from src.contracts.phase_models import Phase
from src.orchestrator.job_lock import job_lock
from src.shared.assumptions import (
    ASSUMPTIONS_FILE,
    SETTABLE,
    read_assumptions,
    record_assumptions,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-ui"
URL = f"/api/v1/assessments/{JOB}/assumptions"
REPORT = f"{DB}/{JOB}/synthesis/v1/report.json"
client = TestClient(app)


class _Execution:
    """The job's execution as LocalExecutionService describes it."""

    def __init__(self, status: str = "SUCCEEDED") -> None:
        self.status = status

    def describe_execution(self, job_id: str) -> dict | None:
        if job_id != JOB:
            return None
        return {"status": self.status, "input": {"database_name": DB}}


class _Orchestrator:
    """Records the phases resumed; ``fail_on`` makes one raise; a synthesis rewrites the
    report with the recorded growth, as the real one does."""

    def __init__(self, store, fail_on: Phase | None = None) -> None:
        self.llm_mode = "none"
        self.store = store
        self.fail_on = fail_on
        self.resumed: list[Phase] = []

    def resume(self, job_id: str, phase: Phase, scope: Any = None) -> None:
        assert job_id == JOB
        if phase == Phase.SYNTHESIS:
            growth = (read_assumptions(self.store, DB, JOB) or {}).get("growth_multiplier")
            report = self.store.read_json(REPORT)
            report["facts"]["assumptions"]["assumptions"][0]["setting"] = growth
            self.store.write_json(REPORT, report)
        if phase == self.fail_on:
            raise RuntimeError("boom /secret/path")
        self.resumed.append(phase)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    rendered: list[str] = []
    render_result = {"status": "complete"}

    def render(job_id, db, root, version, *, graph=True):
        rendered.append(f"{db}/{job_id}")
        (tmp_path / db / job_id / "deliverables").mkdir(parents=True, exist_ok=True)
        (tmp_path / db / job_id / "deliverables" / "engineering-report.md").write_text("new")
        return render_result

    monkeypatch.setattr("src.report.deliverables_folder.render_job_deliverables", render)
    orch = _Orchestrator(store)
    monkeypatch.setattr(route, "artifact_store", store)
    monkeypatch.setattr(route, "orchestrator", orch)
    monkeypatch.setattr(route, "sfn_service", _Execution())
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"queries": {"query_patterns": []}})
    return store, orch, rendered, render_result


def _report(store, *, summary_source: str = "deterministic", designs: bool = False) -> None:
    items = [
        {"key": "growth", "setting": 5.0, "origin": "default", "basis": None, "line": "Growth: 5×"},
        {
            "key": "peak",
            "setting": None,
            "origin": "default",
            "basis": "not_measured",
            "line": "Peak: not measured",
        },
    ]
    store.write_json(
        REPORT,
        {
            "summary_source": summary_source,
            "facts": {"assumptions": {"assumptions": items, "lines": []}},
        },
    )
    if designs:
        store.write_json(f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json", {"tables": []})


def _post(body: str, content_type: str = "application/json"):
    return client.post(URL, content=body, headers={"content-type": content_type})


def _nothing_written(store) -> bool:
    return not store.exists(f"{DB}/{JOB}/{ASSUMPTIONS_FILE}")


def test_a_growth_change_is_recorded_as_ui_and_reruns_synthesis_and_the_report(wired) -> None:
    store, orch, rendered, _ = wired
    _report(store)
    resp = _post('{"growth": 3}')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"] == {"growth": 3.0} and body["origins"] == {"growth": "ui"}
    assert body["reran"] == ["synthesis", "report"] and body["skipped"] == []
    assert orch.resumed == [Phase.SYNTHESIS]
    assert rendered == [f"{DB}/{JOB}"]
    decision = read_assumptions(store, DB, JOB)
    assert decision["growth_multiplier"] == 3.0
    assert decision["origins"] == {"growth_multiplier": "ui"}


def test_a_peak_change_without_a_model_runs_no_schema_design_and_says_so(wired) -> None:
    store, orch, rendered, _ = wired
    _report(store)
    resp = _post('{"peak": 2.5}')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reran"] == ["synthesis", "report"]
    assert body["skipped"] == ["schema_design"]
    assert "no design was written" in body["notes"][0]
    assert orch.resumed == [Phase.SYNTHESIS]
    assert read_assumptions(store, DB, JOB)["peak_to_avg_ratio"] == 2.5


def test_another_writers_value_keeps_its_origin(wired) -> None:
    store, _, _, _ = wired
    _report(store)
    record_assumptions(store, DB, JOB, {"peak_to_avg_ratio": 2.0}, via="file")
    assert _post('{"growth": 4}').status_code == 200
    origins = read_assumptions(store, DB, JOB)["origins"]
    assert origins == {"peak_to_avg_ratio": "file", "growth_multiplier": "ui"}


@pytest.mark.parametrize(
    "body,why",
    [
        ('{"grwth": 3}', "the request has an unknown key"),
        ('{"rate_window": 3}', "collected"),
        ('{"growth": 0}', "growth must be a number between 0 (excluded) and 100"),
        ('{"growth": 101}', "growth must be a number between"),
        ('{"peak": 0.5}', "peak must be a number between 1 (included) and 100"),
        ('{"growth": NaN}', "growth must be a number between"),
        ('{"growth": "3"}', "growth must be a number between"),
        ('{"growth": true}', "growth must be a number between"),
        ('{"growth": 3, "growth": 4}', "the request sets a key more than once"),
        ("[3]", "JSON object"),
        ("{}", "sets no assumption"),
        ("not json", "not valid JSON"),
    ],
)
def test_the_assumptions_files_rules_refuse_the_body_and_nothing_is_written(
    wired, body, why
) -> None:
    store, orch, rendered, _ = wired
    _report(store)
    resp = _post(body)
    assert resp.status_code == 422, resp.text
    assert why in resp.json()["detail"]
    assert _nothing_written(store) and orch.resumed == [] and rendered == []


@pytest.mark.parametrize(
    "body,sent",
    [
        ('{"<img src=x onerror=alert(1)>": 3}', "<img"),
        ('{"growth": 4567}', "4567"),
        ('{"peak": 0.123456}', "0.123456"),
    ],
)
def test_a_refusal_names_the_field_never_what_was_sent(wired, body, sent) -> None:
    store, _, _, _ = wired
    _report(store)
    resp = _post(body)
    assert resp.status_code == 422
    assert sent not in resp.json()["detail"]


def test_a_body_that_is_not_declared_json_is_refused(wired) -> None:
    """A browser page elsewhere can send text/plain without a preflight: refused."""
    store, _, _, _ = wired
    _report(store)
    for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
        assert _post('{"growth": 3}', content_type).status_code == 415
    assert _nothing_written(store)


def test_a_body_larger_than_an_assumptions_file_is_refused(wired) -> None:
    store, _, _, _ = wired
    _report(store)
    resp = _post('{"growth": 3' + " " * (17 * 1024) + "}")
    assert resp.status_code == 413
    assert _nothing_written(store)


def test_a_chunked_body_larger_than_an_assumptions_file_is_refused(wired) -> None:
    """A chunked body sends no length; the body as read is capped too."""
    store, _, _, _ = wired
    _report(store)

    def chunks():
        yield b'{"growth": 3'
        yield b" " * (17 * 1024)
        yield b"}"

    resp = client.post(URL, content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 413
    assert _nothing_written(store)


def test_a_request_from_another_host_name_is_refused(wired) -> None:
    store, _, _, _ = wired
    _report(store)
    resp = client.post(
        URL,
        content='{"growth": 3}',
        headers={"content-type": "application/json", "host": "evil.example"},
    )
    assert resp.status_code == 400
    assert _nothing_written(store)


def test_a_model_written_summary_is_not_rewritten_without_a_model(wired) -> None:
    store, orch, rendered, _ = wired
    _report(store, summary_source="llm")
    resp = _post('{"growth": 3}')
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "written with a model" in detail and "Nothing was changed" in detail
    assert "start a new run with these assumptions" in detail and "scripts/" not in detail
    assert _nothing_written(store) and orch.resumed == [] and rendered == []


def test_a_summary_the_model_wrote_and_was_set_aside_is_named_so(wired) -> None:
    store, orch, _, _ = wired
    _report(store, summary_source="deterministic_fallback")
    resp = _post('{"growth": 3}')
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "set aside and the deterministic one kept" in detail
    assert "was written with a model" not in detail
    assert _nothing_written(store) and orch.resumed == []


def test_designs_are_not_redesigned_without_a_model(wired) -> None:
    store, orch, _, _ = wired
    _report(store, designs=True)
    resp = _post('{"peak": 2}')
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "only a model can do that" in detail and "start a new run" in detail
    assert _nothing_written(store) and orch.resumed == []
    # Growth reaches no design: the same job can still change it.
    assert _post('{"growth": 2}').status_code == 200


def test_a_job_without_results_is_refused(wired) -> None:
    store, _, _, _ = wired
    resp = _post('{"growth": 3}')
    assert resp.status_code == 409 and "no results yet" in resp.json()["detail"]
    assert _nothing_written(store)


@pytest.mark.parametrize("status", ["RUNNING", "WAITING", "CANCELLED", "FAILED"])
def test_a_job_that_is_not_finished_is_refused(wired, monkeypatch, status) -> None:
    """A job with an earlier report that is running or waiting again (an assignment
    edit), cancelled or failed is not changed."""
    store, orch, _, _ = wired
    _report(store)
    monkeypatch.setattr(route, "sfn_service", _Execution(status))
    resp = _post('{"growth": 3}')
    assert resp.status_code == 409 and "not finished" in resp.json()["detail"]
    assert _nothing_written(store) and orch.resumed == []
    rows = client.get(URL).json()["assumptions"]
    assert [r["available"] for r in rows] == [False, False]


def test_a_change_while_another_holds_the_jobs_lock_is_refused(wired) -> None:
    """``/resume`` and the schema confirm routes take the same lock (another thread)."""
    import threading

    store, orch, _, _ = wired
    _report(store)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with job_lock(JOB):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        resp = _post('{"growth": 3}')
    finally:
        release.set()
        t.join()
    assert resp.status_code == 409 and "Another change" in resp.json()["detail"]
    assert _nothing_written(store) and orch.resumed == []


def test_an_unknown_job_is_404(wired) -> None:
    resp = client.post(
        "/api/v1/assessments/nope/assumptions",
        content='{"growth": 3}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 404


def test_a_failed_synthesis_puts_the_job_back_as_it_was(wired, monkeypatch, tmp_path) -> None:
    store, _, rendered, _ = wired
    _report(store)
    record_assumptions(store, DB, JOB, {"growth_multiplier": 2.0}, via="file")
    before = {
        "decision": (tmp_path / DB / JOB / ASSUMPTIONS_FILE).read_bytes(),
        "report": (tmp_path / REPORT).read_bytes(),
    }
    monkeypatch.setattr(route, "orchestrator", _Orchestrator(store, fail_on=Phase.SYNTHESIS))
    resp = _post('{"growth": 3}')
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "Re-running synthesis failed" in detail and "undone" in detail
    assert "/secret/path" not in detail
    assert (tmp_path / DB / JOB / ASSUMPTIONS_FILE).read_bytes() == before["decision"]
    assert (tmp_path / REPORT).read_bytes() == before["report"]
    assert rendered == []
    # The panel, the gate and the deliverables still agree on the value as it was.
    assert client.get(URL).json()["assumptions"][0]["value"] == 5.0


def test_a_failed_deliverables_render_puts_the_report_back_too(wired, tmp_path) -> None:
    """Synthesis wrote the new report, then the deliverables failed: the report, the
    decision and the deliverables folder are all as they were."""
    store, orch, _, render_result = wired
    _report(store)
    folder = tmp_path / DB / JOB / "deliverables"
    folder.mkdir(parents=True)
    (folder / "engineering-report.md").write_text("old")
    before = (tmp_path / REPORT).read_bytes()
    render_result["status"] = "error"
    render_result["message"] = "boom"
    resp = _post('{"growth": 3}')
    assert resp.status_code == 500
    assert "Re-running the deliverables failed" in resp.json()["detail"]
    assert orch.resumed == [Phase.SYNTHESIS]  # synthesis ran and rewrote the report
    assert (tmp_path / REPORT).read_bytes() == before
    assert json.loads(before)["facts"]["assumptions"]["assumptions"][0]["setting"] == 5.0
    assert (folder / "engineering-report.md").read_text() == "old"
    assert _nothing_written(store)


def test_the_panel_states_each_settable_assumption_with_the_files_ranges(wired) -> None:
    store, _, _, _ = wired
    _report(store)
    rows = client.get(URL).json()["assumptions"]
    assert [(r["name"], r["key"], r["min"], r["min_included"], r["max"]) for r in rows] == [
        (name, key, low, low_ok, high) for key, (name, low, low_ok, high) in SETTABLE.items()
    ]
    growth, peak = rows
    assert growth["value"] == 5.0 and growth["origin"] == "default"
    assert growth["origin_label"] == "default" and growth["line"] == "Growth: 5×"
    assert growth["reruns"] == ["synthesis", "report"] and growth["available"] is True
    assert peak["value"] is None and peak["reruns"] == ["schema_design", "synthesis", "report"]
    assert peak["available"] is True and "no design was written" in peak["notes"][0]


def test_the_panel_says_why_a_change_is_unavailable(wired) -> None:
    store, _, _, _ = wired
    _report(store, summary_source="llm", designs=True)
    growth, peak = client.get(URL).json()["assumptions"]
    assert growth["available"] is False and "executive summary" in growth["unavailable_reason"]
    assert peak["available"] is False and "redesigning" in peak["unavailable_reason"]


def test_a_report_written_before_the_block_still_loads(wired) -> None:
    store, _, _, _ = wired
    store.write_json(REPORT, {"summary_source": "deterministic"})
    rows = client.get(URL).json()["assumptions"]
    assert [r["value"] for r in rows] == [None, None]
    assert [r["origin"] for r in rows] == [None, None]


# ---------------------------------------------------------------------------
# #1991 review: the job id and database name are checked by every route's rule, and
# the job's folder must resolve inside the artifact root, before any store call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job", ["..", "%2e%2e", "%2Fetc%2Fpasswd", "a%2fb", "a%5cb", "/etc"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_a_traversing_job_id_is_refused_before_the_store(wired, job, method) -> None:
    store, orch, _, _ = wired
    _report(store)
    url = f"/api/v1/assessments/{job}/assumptions"
    if method == "get":
        resp = client.get(url)
    else:
        resp = client.post(
            url, content='{"growth": 3}', headers={"content-type": "application/json"}
        )
    assert resp.status_code in (404, 405, 422), resp.text
    assert _nothing_written(store) and orch.resumed == []


@pytest.mark.parametrize("db", ["..", "/etc", "a/../..", "shop%2f..", "a\\b"])
def test_a_database_name_that_traverses_is_no_job(wired, monkeypatch, db) -> None:
    store, orch, _, _ = wired
    _report(store)
    execution = _Execution()
    monkeypatch.setattr(
        execution,
        "describe_execution",
        lambda job_id: {"status": "SUCCEEDED", "input": {"database_name": db}},
    )
    monkeypatch.setattr(route, "sfn_service", execution)
    assert client.get(URL).status_code == 404
    assert _post('{"growth": 3}').status_code == 404
    assert _nothing_written(store) and orch.resumed == []


def test_a_symlinked_job_folder_escaping_the_root_is_no_job(tmp_path, monkeypatch) -> None:
    import os

    root, outside = tmp_path / "root", tmp_path / "outside"
    (outside / JOB / "synthesis" / "v1").mkdir(parents=True)
    (outside / JOB / "synthesis" / "v1" / "report.json").write_text(
        '{"summary_source": "deterministic"}'
    )
    root.mkdir()
    os.symlink(outside, root / DB)
    store = LocalArtifactStore(base_dir=str(root))
    orch = _Orchestrator(store)
    monkeypatch.setattr(route, "artifact_store", store)
    monkeypatch.setattr(route, "orchestrator", orch)
    monkeypatch.setattr(route, "sfn_service", _Execution())
    assert client.get(URL).status_code == 404
    assert _post('{"growth": 3}').status_code == 404
    assert not (outside / JOB / "decisions").exists() and orch.resumed == []


def test_the_library_refuses_a_traversing_job_before_any_path(tmp_path) -> None:
    from src.orchestrator.assumption_change import apply_assumption_change

    store = LocalArtifactStore(base_dir=str(tmp_path))
    for db, job in [("..", JOB), (DB, ".."), ("/etc", JOB), (DB, "a%2fb")]:
        with pytest.raises(ValueError):
            apply_assumption_change(
                _Orchestrator(store), store, db, job, {"growth_multiplier": 3.0}, str(tmp_path)
            )
    assert list(tmp_path.iterdir()) == []
