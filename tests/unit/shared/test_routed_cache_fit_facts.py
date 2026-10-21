"""The cache layer's "Avg cache fit" on the pattern analysis page is the routed figure (#1325).

The page used to print the analysis phase's average over every table the cache engine rated. The
reports state the fit of the reads actually routed to the cache layer (``routed_confidence`` of the
ranking's cache entry). The analysis API now serves that figure in the analysis ``facts``, computed
at read time from the latest assignment by the same ``routed_fits`` call the ranking uses, so a
saved job of any age answers the same way and the page only displays it (ADR-032).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agents.referee.synthesis_data import load_synthesis_data
from src.agents.referee.synthesis_report import build_ranking
from src.api.main import app
from src.api.routes import results
from src.api.services.local_execution import LocalExecutionService
from src.api.services.local_s3 import LocalS3Service
from src.shared.report_facts import analysis_overview_facts, routed_cache_fit_facts
from src.storage.local_store import LocalArtifactStore

DB = "db"
JOB = "job-1"


def _analysis(**scores: int) -> dict:
    return {
        "table_recommendations": [
            {"table_id": t, "confidence_score": s} for t, s in scores.items()
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        "cost_estimate": {"monthly_cost_usd": 10},
    }


def _q(qid: str, table: str) -> dict:
    return {
        "query_id": qid,
        "query_type": "SELECT",
        "tables_accessed": [table],
        "calls_per_second": 2.0,
    }


def _assignment(version: int, cached: int) -> dict:
    return {
        "version": version,
        "query_assignments": [
            {
                "query_id": f"k{i}",
                "assigned_engine": "dynamodb",
                "assignment_reason": "highest confidence",
                **(
                    {"cache_engine": "elasticache", "cache_pattern": "point_lookup"}
                    if i < cached
                    else {}
                ),
            }
            for i in range(6)
        ],
    }


def _write_job(store: LocalArtifactStore, assignments: dict[int, dict]) -> None:
    prefix = f"{DB}/{JOB}"
    store.write_json(
        f"{prefix}/referee-triage/triage.json",
        {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "elasticache"}],
            "signals": [{"signal": "key_value_lookups", "query_ids": [f"k{i}" for i in range(6)]}],
        },
    )
    store.write_json(
        f"{prefix}/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": t} for t in ("users", "a", "b")]},
            "queries": {"query_patterns": [_q(f"k{i}", "users") for i in range(6)]},
        },
    )
    # The cache engine rated users at 95 but two other tables at 5: the all-tables average is 35,
    # the fit of the reads it fronts (the users lookups) is 95.
    store.write_json(f"{prefix}/analysis-dynamodb/analysis.json", _analysis(users=90, a=40, b=40))
    store.write_json(f"{prefix}/analysis-elasticache/analysis.json", _analysis(users=95, a=5, b=5))
    for version, assignment in assignments.items():
        store.write_json(f"{prefix}/assignment/v{version}/assignment.json", assignment)


@pytest.fixture
def store(tmp_path: Path):
    store = LocalArtifactStore(str(tmp_path))
    results.sfn_service = LocalExecutionService(store)
    results.s3_service = LocalS3Service(store)
    yield store
    results.sfn_service = None
    results.s3_service = None


def _facts(engine: str = "elasticache") -> dict:
    resp = TestClient(app).get(f"/api/v1/assessments/{JOB}/analysis/{engine}")
    assert resp.status_code == 200, resp.text
    facts: dict = resp.json()["facts"]
    return facts


def test_the_cache_facts_carry_the_fit_of_the_routed_reads(store) -> None:
    _write_job(store, {1: _assignment(1, cached=2)})
    facts = _facts()

    assert facts["avg_confidence_score"] == 35  # the analysis average stays, for audit
    assert facts["routed_cache_fit_applicable"] is True
    assert facts["routed_cache_fit"] == 95
    assert facts["routed_cache_queries"] == 2


def test_the_figure_is_the_one_the_reports_state(store) -> None:
    """report.json's cache entry and the page read the same routed_fits call."""
    _write_job(store, {1: _assignment(1, cached=2)})
    data = load_synthesis_data(store, JOB, DB, 1)
    cache = next(r for r in build_ranking(data) if r.get("role") == "cache_layer")

    assert _facts()["routed_cache_fit"] == cache["routed_confidence"]
    assert _facts()["routed_cache_queries"] == cache["routed_queries"]


def test_no_routed_reads_is_not_applicable_rather_than_the_analysis_average(store) -> None:
    _write_job(store, {1: _assignment(1, cached=0)})
    facts = _facts()

    assert facts["routed_cache_fit_applicable"] is False
    assert facts["routed_cache_fit"] is None
    assert facts["routed_cache_queries"] == 0
    assert facts["avg_confidence_score"] == 35


def test_a_job_without_an_assignment_is_not_applicable(store) -> None:
    _write_job(store, {})
    facts = _facts()

    assert facts["routed_cache_fit_applicable"] is False
    assert facts["routed_cache_fit"] is None


def test_the_latest_assignment_decides(store) -> None:
    """A later version that drops the cached reads makes the figure not applicable."""
    _write_job(store, {1: _assignment(1, cached=2), 2: _assignment(2, cached=0)})

    assert _facts()["routed_cache_fit_applicable"] is False


def test_an_owner_engine_carries_no_routed_cache_fit(store) -> None:
    _write_job(store, {1: _assignment(1, cached=2)})
    facts = _facts("dynamodb")

    assert "routed_cache_fit" not in facts
    assert "routed_cache_fit_applicable" not in facts


def test_unreadable_artifacts_do_not_take_the_analysis_away(store) -> None:
    _write_job(store, {1: _assignment(1, cached=2)})
    (Path(store.base_dir) / DB / JOB / "referee-triage" / "triage.json").write_text("{not json")
    facts = _facts()

    assert facts["avg_confidence_score"] == 35
    assert facts["routed_cache_fit_applicable"] is False


def test_the_overview_without_a_routed_fit_has_no_cache_keys() -> None:
    assert "routed_cache_fit" not in analysis_overview_facts({})


@pytest.mark.parametrize(
    ("confidence", "queries", "expected"),
    [
        (
            95,
            2,
            {
                "routed_cache_fit": 95,
                "routed_cache_queries": 2,
                "routed_cache_fit_applicable": True,
            },
        ),
        (
            0,
            1,
            {"routed_cache_fit": 0, "routed_cache_queries": 1, "routed_cache_fit_applicable": True},
        ),
        (
            None,
            0,
            {
                "routed_cache_fit": None,
                "routed_cache_queries": 0,
                "routed_cache_fit_applicable": False,
            },
        ),
        (
            None,
            3,
            {
                "routed_cache_fit": None,
                "routed_cache_queries": 3,
                "routed_cache_fit_applicable": False,
            },
        ),
    ],
)
def test_the_routed_cache_fit_facts(confidence, queries, expected) -> None:
    assert routed_cache_fit_facts(confidence, queries) == expected


def test_a_withheld_triage_does_not_withhold_the_cache_fit() -> None:
    """The signal figures are withheld with the triage; the routed cache fit has its own reader."""
    from unittest.mock import MagicMock

    from src.shared.contract_versioning import ContractVersionError

    s3 = MagicMock()
    s3.read_analysis.return_value = _analysis(users=95, a=5, b=5)
    s3.read_triage.side_effect = ContractVersionError(
        artifact="triage.json", contract="TriageOutput", stored="9.0", expected="1.0"
    )
    s3.read_routed_cache_fit.return_value = (95, 2)
    results.sfn_service = MagicMock()
    results.sfn_service.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": DB, "source_database_type": "mysql"},
    }
    results.s3_service = s3
    try:
        facts = _facts()
    finally:
        results.sfn_service = None
        results.s3_service = None

    assert "contract version" in facts["signals_withheld_reason"]
    assert facts["routed_cache_fit"] == 95
    assert facts["routed_cache_fit_applicable"] is True
