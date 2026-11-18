# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A finished job whose end precedes its start has no known duration (#2304)."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments, dashboard

client = TestClient(app)

START = "2026-02-23T10:00:05+00:00"
END_BEFORE_START = "2026-02-23T10:00:03+00:00"


@pytest.fixture
def sfn():
    svc = MagicMock()
    dashboard.sfn_service = svc
    yield svc
    dashboard.sfn_service = None


def test_dashboard_average_is_null_without_a_real_duration(sfn):
    done = {
        "job_id": "j1",
        "status": "SUCCEEDED",
        "started_at": START,
        "stopped_at": END_BEFORE_START,
    }
    sfn.list_executions.side_effect = lambda status_filter=None, max_results=100: (
        [done] if status_filter in (None, "SUCCEEDED") else []
    )
    data = client.get("/api/v1/dashboard/stats").json()
    assert data["average_duration_hours"] is None


def test_dashboard_average_ignores_negative_durations(sfn):
    good = {
        "job_id": "j1",
        "status": "SUCCEEDED",
        "started_at": "2026-02-23T10:00:00+00:00",
        "stopped_at": "2026-02-23T12:00:00+00:00",
    }
    bad = {
        "job_id": "j2",
        "status": "SUCCEEDED",
        "started_at": START,
        "stopped_at": END_BEFORE_START,
    }
    sfn.list_executions.side_effect = lambda status_filter=None, max_results=100: (
        [good, bad] if status_filter in (None, "SUCCEEDED") else []
    )
    data = client.get("/api/v1/dashboard/stats").json()
    assert data["average_duration_hours"] == 2.0


def test_assessment_list_duration_is_null_when_end_precedes_start(monkeypatch):
    svc = MagicMock()
    svc.read_source_engine.return_value = None
    svc.list_executions.return_value = [
        {
            "job_id": "j1",
            "database_name": "d",
            "status": "SUCCEEDED",
            "started_at": START,
            "stopped_at": END_BEFORE_START,
        },
        {
            "job_id": "j2",
            "database_name": "d",
            "status": "SUCCEEDED",
            "started_at": "2026-02-23T10:00:00+00:00",
            "stopped_at": "2026-02-23T10:15:00+00:00",
        },
    ]
    monkeypatch.setattr(assessments, "_require_services", lambda: (svc, MagicMock()))
    items = client.get("/api/v1/assessments").json()["assessments"]
    by_id = {a["job_id"]: a for a in items}
    assert by_id["j1"]["duration_seconds"] is None
    assert by_id["j2"]["duration_seconds"] == 900


# A stage whose end precedes its start has no known duration either (#2338 review).
BACKWARDS_HISTORY = [
    {
        "name": "RunCollector",
        "status": "completed",
        "started_at": "2026-02-23T14:15:00+00:00",
        "completed_at": "2026-02-23T14:00:00+00:00",
    },
    {
        "name": "RunRefereeTriage",
        "status": "completed",
        "started_at": "2026-02-23T14:15:00+00:00",
        "completed_at": "2026-02-23T14:30:00+00:00",
    },
]
RUNNING_EXECUTION = {
    "status": "RUNNING",
    "started_at": "2026-02-23T14:00:00Z",
    "stopped_at": None,
    "input": {"database_name": "test_db", "source_database_type": "mysql"},
}


@pytest.fixture
def stage_services():
    sfn, s3 = MagicMock(), MagicMock()
    sfn.describe_execution.return_value = RUNNING_EXECUTION
    sfn.get_execution_history.return_value = BACKWARDS_HISTORY
    sfn._execution_arn.return_value = "arn:test"
    assessments.sfn_service, assessments.s3_service = sfn, s3
    yield
    assessments.sfn_service, assessments.s3_service = None, None


def test_progress_stage_duration_is_null_when_end_precedes_start(stage_services):
    stages = client.get("/api/v1/assessments/job-1").json()["progress"]["stages"]
    assert stages[0]["duration_seconds"] is None
    assert stages[1]["duration_seconds"] == 900


def test_agent_duration_is_null_when_end_precedes_start(stage_services):
    agents = client.get("/api/v1/assessments/job-1/agents").json()["agents"]
    assert agents[0]["duration_seconds"] is None
    assert agents[1]["duration_seconds"] == 900
