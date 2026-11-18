# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2326: the assignments response says whether the gate is recorded as passed
(``approval_recorded``), so the page says "Approved" only then. Past the gate, a read
never writes a new version (#992): the page states the version the job approved."""

from __future__ import annotations

import pytest

from src.api.routes import assignments
from tests.unit.agents.referee.test_one_writer_per_table import DB as SPLIT_DB
from tests.unit.agents.referee.test_one_writer_per_table import JOB as SPLIT_JOB
from tests.unit.agents.referee.test_one_writer_per_table import _local_legacy_split
from tests.unit.api.test_assignment_cache_reads import DB, JOB, _store, _url, client


def test_nothing_recorded_is_not_approved(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    assert client.get(_url()).json()["approval_recorded"] is False


def test_a_decision_record_is_an_approval(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    assignments.artifact_store.write_json(
        f"{DB}/{JOB}/decisions/assignment_review.json", {"status": "completed"}
    )
    assert client.get(_url()).json()["approval_recorded"] is True


def test_the_progressions_completed_review_is_an_approval(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    assignments.artifact_store.write_json(
        f".meta/{JOB}.json", {"phases": {"assignment_review": {"status": "completed"}}}
    )
    assert client.get(_url()).json()["approval_recorded"] is True


def test_a_review_still_running_is_not_an_approval(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    assignments.artifact_store.write_json(
        f".meta/{JOB}.json", {"phases": {"assignment_review": {"status": "in_progress"}}}
    )
    assert client.get(_url()).json()["approval_recorded"] is False


def _split_url() -> str:
    return f"/api/v1/assessments/{SPLIT_JOB}/assignments?database_name={SPLIT_DB}"


def test_a_read_past_the_gate_writes_no_new_version(tmp_path, monkeypatch) -> None:
    # An old saved job whose v1 split a table's writes, approved and with results: the
    # page shows v1, the version it approved, and nothing new is written.
    store = _local_legacy_split(tmp_path)
    store.write_json(
        f"{SPLIT_DB}/{SPLIT_JOB}/decisions/assignment_review.json", {"status": "completed"}
    )
    store.write_json(f"{SPLIT_DB}/{SPLIT_JOB}/synthesis/v1/report.json", {"facts": {}})
    monkeypatch.setattr(assignments, "artifact_store", store)
    body = client.get(_split_url()).json()
    assert body["assignment"]["version"] == 1
    assert body["results_version"] == 1
    assert not store.exists(f"{SPLIT_DB}/{SPLIT_JOB}/assignment/v2/assignment.json")


def test_results_alone_also_close_the_gate_to_a_reroute(tmp_path, monkeypatch) -> None:
    # A job saved before approvals were recorded: its results say the gate was passed.
    store = _local_legacy_split(tmp_path)
    store.write_json(f"{SPLIT_DB}/{SPLIT_JOB}/synthesis/v1/report.json", {"facts": {}})
    monkeypatch.setattr(assignments, "artifact_store", store)
    assert client.get(_split_url()).json()["assignment"]["version"] == 1
    assert not store.exists(f"{SPLIT_DB}/{SPLIT_JOB}/assignment/v2/assignment.json")


# Guards #992's existing behaviour, which main already has: the guard above keeps it.
@pytest.mark.passes_on_main
def test_at_the_gate_the_read_still_reroutes(tmp_path, monkeypatch) -> None:
    store = _local_legacy_split(tmp_path)
    monkeypatch.setattr(assignments, "artifact_store", store)
    assert client.get(_split_url()).json()["assignment"]["version"] == 2
