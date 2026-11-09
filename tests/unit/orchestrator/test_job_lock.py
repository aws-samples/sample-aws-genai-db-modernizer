# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991 review: one API-driven change to a job at a time. ``/resume``, the schema
confirm routes and the assumptions panel take the same per-job lock
(``src.orchestrator.job_lock``), which the orchestrator's ``resume`` and
``confirm_schema_design`` hold; a second caller from another thread gets 409, and the
lock is reentrant within one thread."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import phases, schema_revisions
from src.contracts.phase_models import Phase
from src.orchestrator.job_lock import JobBusyError, job_lock
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore

client = TestClient(app)


@contextmanager
def _held_elsewhere(job_id: str) -> Iterator[None]:
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with job_lock(job_id):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        yield
    finally:
        release.set()
        t.join()


def test_the_lock_is_reentrant_in_one_thread_and_released() -> None:
    with job_lock("j"):
        with job_lock("j"):
            pass
    with job_lock("j"):  # released after both exits
        pass


def test_another_thread_is_refused_while_the_lock_is_held() -> None:
    with _held_elsewhere("j"):
        with pytest.raises(JobBusyError, match="Another change to job j"):
            with job_lock("j"):
                pass
        with job_lock("other"):  # another job is free
            pass


def test_the_orchestrators_resume_holds_the_jobs_lock(tmp_path) -> None:
    orch = LocalOrchestrator(LocalArtifactStore(base_dir=str(tmp_path)))
    with _held_elsewhere("j"):
        with pytest.raises(JobBusyError):
            orch.resume("j", Phase.SYNTHESIS)
        with pytest.raises(JobBusyError):
            orch.resume_lenient("j", Phase.SYNTHESIS)
        with pytest.raises(JobBusyError):
            orch.confirm_schema_design("j")


def test_the_resume_route_answers_409_while_another_change_runs(monkeypatch, tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    monkeypatch.setattr(phases, "orchestrator", LocalOrchestrator(store))
    monkeypatch.setattr(phases, "sfn_service", None)
    with _held_elsewhere("job-1"):
        resp = client.post("/api/v1/assessments/job-1/resume", json={"phase": "synthesis"})
    assert resp.status_code == 409 and "Another change" in resp.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/assessments/job-1/schema/confirm-all?database_name=mydb",
        "/api/v1/assessments/job-1/schema/dynamodb/confirm?database_name=mydb",
    ],
)
def test_the_confirm_routes_answer_409_while_another_change_runs(monkeypatch, path) -> None:
    store = MagicMock()
    prefix = "mydb/job-1/schema-dynamodb/"
    store.list_prefix.return_value = [f"{prefix}v1/schema_output.json"]
    store.read_json.return_value = {"selected_engines": ["dynamodb"]}
    store.exists.return_value = True
    sfn = MagicMock()
    sfn.is_cancelled.return_value = False
    monkeypatch.setattr(schema_revisions, "artifact_store", store)
    monkeypatch.setattr(schema_revisions, "orchestrator", MagicMock())
    monkeypatch.setattr(schema_revisions, "sfn_service", sfn)
    monkeypatch.setattr(schema_revisions, "_get_active_engines", lambda *a: ["dynamodb"])
    with _held_elsewhere("job-1"):
        resp = client.post(path, json={})
    assert resp.status_code == 409, resp.text
    assert not store.write_json.called
