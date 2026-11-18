# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2326: the assignments response says which assignment version the job's results were
written for (``results_version``), so the gate page can tell an approved version from a
later one. None until synthesis has run."""

from __future__ import annotations

from src.api.routes import assignments
from src.storage.assignment_versioning import latest_synthesis_version
from tests.unit.api.test_assignment_cache_reads import DB, JOB, _store, _url, client


def test_no_results_version_before_synthesis(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    assert client.get(_url()).json()["results_version"] is None


def test_the_results_version_is_the_version_synthesis_wrote_for(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    store = assignments.artifact_store
    store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", {"facts": {}})
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", {"facts": {}})
    assert client.get(_url()).json()["results_version"] == 2


def test_other_synthesis_files_are_not_versions(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    store = assignments.artifact_store
    store.write_json(f"{DB}/{JOB}/synthesis/v3/other.json", {})
    store.write_json(f"{DB}/{JOB}/synthesis/vx/report.json", {})
    assert latest_synthesis_version(store, DB, JOB) is None
