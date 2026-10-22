"""The triage insight figures of the job summary page, computed on the server (#1319, ADR-032).

The page used to take ``signals.length`` and each signal's ``query_ids.length`` in the browser.
The triage endpoint now returns them as ``facts`` next to the stored fields.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import results
from src.shared.report_facts import triage_signal_facts

TRIAGE = {
    "signals": [
        {"signal": f"s{i}", "query_ids": [f"q{j}" for j in range(i)], "targets": ["dynamodb", "x"]}
        for i in range(7)
    ]
    + [{"signal": "no_ids", "targets": []}],
    "selected_agents": [{"agent_type": "dynamodb"}],
}


def test_the_signal_figures() -> None:
    facts = triage_signal_facts(TRIAGE)
    assert facts["signal_count"] == 8
    assert len(facts["top_signals"]) == 5
    assert facts["top_signals"][2] == {"name": "s2", "query_count": 2, "primary_target": "dynamodb"}


def test_a_signal_without_query_ids_or_targets_counts_zero_and_has_no_target() -> None:
    facts = triage_signal_facts({"signals": [{"signal": "a"}]})
    assert facts["top_signals"] == [{"name": "a", "query_count": 0, "primary_target": None}]


@pytest.mark.parametrize("empty", [{}, {"signals": None}, {"signals": []}, None, {"signals": "x"}])
def test_no_signals_is_zero_and_empty(empty) -> None:
    assert triage_signal_facts(empty) == {"signal_count": 0, "top_signals": []}


def test_entries_that_are_not_signals_are_not_counted() -> None:
    assert triage_signal_facts({"signals": [{"signal": "a"}, "junk", None]})["signal_count"] == 1


@pytest.fixture
def services():
    s3 = MagicMock()
    s3.read_triage.return_value = dict(TRIAGE)
    results.sfn_service = MagicMock()
    results.sfn_service.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": "test_db", "source_database_type": "mysql"},
    }
    results.s3_service = s3
    yield s3
    results.sfn_service = None
    results.s3_service = None


def test_the_triage_endpoint_returns_the_facts_and_the_stored_fields(services) -> None:
    resp = TestClient(app).get("/api/v1/assessments/job-1/triage")

    assert resp.status_code == 200
    body = resp.json()
    assert body["facts"]["signal_count"] == 8
    assert body["signals"] == TRIAGE["signals"]
    assert body["selected_agents"] == TRIAGE["selected_agents"]
