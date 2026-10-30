# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A stored artifact at another contract MAJOR reaches the user as a 409 (#971).

``LocalS3Service`` and the assessment summaries used to catch every exception
and answer "not available" (404), or build a stand-in reality check from the
assignment. Each route below reads one refused file and must answer 409 with
the plain message, never 404, 200 or a stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments, results
from src.api.services.local_execution import LocalExecutionService
from src.api.services.local_s3 import LocalS3Service
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job1"
MESSAGE = "This assessment was saved by an incompatible version of the tool"
_ASSIGNMENT = {
    "contract_version": "1.15",
    "job_id": JOB,
    "version": 1,
    "status": "auto_generated",
    "timestamp": "2026-10-01T12:00:00Z",
    "query_assignments": [
        {
            "query_id": "q1",
            "assigned_engine": "dynamodb",
            "confidence": 80,
            "source_tables": ["app.users"],
            "assignment_reason": "key-value",
        }
    ],
    "table_assignments": [],
    "co_dependency_groups": [],
    "validation_warnings": [],
}


@pytest.fixture
def job(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "artifacts"
    (root / DB / JOB / "collector").mkdir(parents=True)
    store = LocalArtifactStore(str(root))
    saved = (results.sfn_service, results.s3_service)
    saved_a = (assessments.sfn_service, assessments.s3_service)
    sfn, s3 = LocalExecutionService(store), LocalS3Service(store)
    results.sfn_service, results.s3_service = sfn, s3
    assessments.sfn_service, assessments.s3_service = sfn, s3
    yield root / DB / JOB
    results.sfn_service, results.s3_service = saved
    assessments.sfn_service, assessments.s3_service = saved_a


def _write(job_dir: Path, rel: str, data: dict) -> None:
    path = job_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _get(route: str) -> tuple[int, object]:
    resp = TestClient(app).get(f"/api/v1/assessments/{JOB}{route}", params={"database_name": DB})
    return resp.status_code, resp.json()


def _assert_refused(route: str) -> None:
    status, body = _get(route)
    assert status == 409, (route, status, body)
    assert isinstance(body, dict) and body["detail"].startswith(MESSAGE)
    assert "/" not in body["detail"].split("(")[0]  # no artifact path


@pytest.mark.parametrize(
    ("rel", "routes"),
    [
        ("referee-synthesis/report.json", ["/results", "/results/table-mappings"]),
        ("referee-triage/triage.json", ["/triage"]),
        ("collector/output.json", ["/collector"]),
        ("analysis-dynamodb/analysis.json", ["/analysis/dynamodb"]),
    ],
)
def test_a_refused_artifact_is_a_409_not_not_available(job: Path, rel: str, routes: list) -> None:
    _write(job, rel, {"contract_version": "9.0"})
    for route in routes:
        _assert_refused(route)


def test_a_refused_versioned_synthesis_report_is_a_409(job: Path) -> None:
    _write(job, "assignment/v1/assignment.json", _ASSIGNMENT)
    _write(job, "synthesis/v1/report.json", {"contract_version": "9.0"})
    for route in ("/results", "/results/table-mappings"):
        _assert_refused(route)


def test_a_refused_reality_check_is_a_409_not_a_stand_in(job: Path) -> None:
    _write(job, "assignment/v1/assignment.json", _ASSIGNMENT)
    _write(job, "reality-check/output.json", {"contract_version": "9.0"})
    _assert_refused("/reality-check")


def test_a_refused_assignment_is_never_a_stand_in_reality_check(job: Path) -> None:
    _write(job, "assignment/v1/assignment.json", {**_ASSIGNMENT, "contract_version": "9.0"})
    _assert_refused("/reality-check")


def test_a_refused_schema_design_is_a_409_not_a_missing_design(job: Path) -> None:
    _write(job, "schema-dynamodb/v1/schema_output.json", {"contract_version": "9.0"})
    _assert_refused("/schema-designs")


def test_a_readable_job_still_answers(job: Path) -> None:
    """Control: the same routes at the current MAJOR are not refused."""
    _write(job, "assignment/v1/assignment.json", _ASSIGNMENT)
    status, body = _get("/reality-check")
    assert status == 200 and isinstance(body, dict)
    _write(job, "referee-triage/triage.json", {"contract_version": "1.0", "selected_agents": []})
    assert _get("/triage")[0] == 200


def test_a_refused_progression_shows_the_job_as_failed_with_the_message(job: Path) -> None:
    """The job list must not 409 for one job: its status says FAILED, with the message."""
    meta = job.parent.parent / ".meta" / f"{JOB}.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"contract_version": "9.0", "job_id": JOB}))
    status, body = _get("")
    assert status == 200, body
    assert isinstance(body, dict) and body["status"] == "FAILED"
    assert MESSAGE in json.dumps(body)
    listing = TestClient(app).get("/api/v1/assessments")
    assert listing.status_code == 200
    rows = [a for a in listing.json()["assessments"] if a["job_id"] == JOB]
    assert rows and rows[0]["status"] == "FAILED"


@pytest.mark.parametrize(
    ("agent", "rel"),
    [
        ("collector", "collector/output.json"),
        ("referee-triage", "referee-triage/triage.json"),
        ("referee-synthesis", "referee-synthesis/report.json"),
        ("analysis-dynamodb", "analysis-dynamodb/analysis.json"),
    ],
)
def test_the_agents_summary_lets_a_refusal_through(job: Path, agent: str, rel: str) -> None:
    """``/agents`` summaries (``_extract_artifact_summary``) used to answer None."""
    from src.shared.contract_versioning import ContractVersionError

    _write(job, rel, {"contract_version": "9.0"})
    with pytest.raises(ContractVersionError):
        assessments._extract_artifact_summary(DB, JOB, agent)
