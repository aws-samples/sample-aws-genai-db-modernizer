"""get_synthesis_report reads the VERSIONED synthesis artifact (ADR-029 Layer A).

The synthesis writer produces synthesis/v{N}/report.json (keyed on the effective
assignment version), or referee-synthesis/report.json when no versioned
assignment exists. It never writes an unversioned synthesis/report.json, so the
reader must resolve the effective version and read the versioned key first, then
fall back. Before this fix the reader looked only at the unversioned key and
always reported "not available".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.atx_orchestrator import tools
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.assignment_models import Assignment, AssignmentSource, AssignmentStatus
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-synth"


def _assignment(version: int) -> dict:
    return Assignment(
        job_id=JOB,
        version=version,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    ).model_dump(mode="json")


@pytest.fixture
def store(tmp_path):
    return upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))


def _call() -> dict:
    result: dict = json.loads(tools.get_synthesis_report(JOB, DB))
    return result


class TestGetSynthesisReport:
    def test_reads_versioned_report_at_effective_version(self, store) -> None:
        store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", _assignment(2))
        store.write_json(
            f"{DB}/{JOB}/synthesis/v2/report.json", {"architecture_type": "HYBRID", "v": 2}
        )
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = _call()
        assert out["architecture_type"] == "HYBRID"
        assert out["v"] == 2

    def test_prefers_versioned_over_legacy_unversioned(self, store) -> None:
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", _assignment(1))
        store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", {"which": "versioned"})
        store.write_json(f"{DB}/{JOB}/synthesis/report.json", {"which": "legacy"})
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = _call()
        assert out["which"] == "versioned"

    def test_falls_back_to_referee_synthesis_when_no_assignment(self, store) -> None:
        # No assignment -> effective version 0 -> only the legacy referee key exists.
        store.write_json(f"{DB}/{JOB}/referee-synthesis/report.json", {"which": "referee"})
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = _call()
        assert out["which"] == "referee"

    def test_error_when_no_report_anywhere(self, store) -> None:
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", _assignment(1))
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = _call()
        assert "error" in out
        assert out["report_artifact"] == f"{DB}/{JOB}/synthesis/v1/report.json"
        assert f"{DB}/{JOB}/referee-synthesis/report.json" in out["searched"]
