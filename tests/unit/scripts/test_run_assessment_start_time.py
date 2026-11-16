# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A script run records when it started, where the API reads it (#2304).

A job from ``run_assessment.py`` (the CLI and ``/modernize``) used to have no
``_meta.json``, so the API took its start from the job folder's mtime, which
moves forward as later phases add folders: the start could land after the end
and the dashboard showed a negative duration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.api.services.local_execution import LocalExecutionService
from src.storage.local_store import LocalArtifactStore
from tests.unit.assessment.conftest import sample_collection


def _collect(tmp: Path) -> tuple[LocalArtifactStore, str, str]:
    import scripts.run_assessment as run_assessment

    collection = tmp / "wordpress-collection.json"
    collection.write_text(json.dumps(sample_collection("wordpress")), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp / "artifacts"))
    mp = pytest.MonkeyPatch()
    mp.chdir(tmp)
    try:
        job_id, name = run_assessment.phase_collect(str(collection), None, store)
    finally:
        mp.undo()
    return store, job_id, name


def test_collect_records_the_start_time_the_api_reads(tmp_path: Path) -> None:
    before = datetime.now(UTC)
    store, job_id, name = _collect(tmp_path)
    after = datetime.now(UTC)

    meta = store.read_json(f"{name}/{job_id}/_meta.json")
    started = datetime.fromisoformat(meta["started_at"])
    assert before <= started <= after
    assert meta["job_id"] == job_id and meta["database_name"] == name

    execution = LocalExecutionService(store).describe_execution(job_id)
    assert execution is not None
    assert execution["started_at"] == meta["started_at"]


def test_the_start_time_does_not_move_with_the_job_folder(tmp_path: Path) -> None:
    import os

    store, job_id, name = _collect(tmp_path)
    recorded = store.read_json(f"{name}/{job_id}/_meta.json")["started_at"]
    # Later phases add folders, which moves the job folder's mtime forward.
    job_dir = tmp_path / "artifacts" / name / job_id
    os.utime(job_dir, (datetime.now(UTC).timestamp() + 3600,) * 2)
    execution = LocalExecutionService(store).describe_execution(job_id)
    assert execution is not None and execution["started_at"] == recorded
