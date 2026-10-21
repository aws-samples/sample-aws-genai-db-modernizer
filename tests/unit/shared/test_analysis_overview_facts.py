"""The engine overview figures of an analysis, computed once on the server (#1312, ADR-032).

The pattern analysis page used to average the tables' fit scores, count patterns and sum the
triage signals in the browser. The API now returns them as ``facts`` of the analysis output,
and the agent summary the job page reads is built from the same function.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments, results
from src.shared.contract_versioning import ContractVersionError
from src.shared.report_facts import analysis_overview_facts

ANALYSIS = {
    "table_recommendations": [
        {"table_id": "a", "confidence_score": 90},
        {"table_id": "b", "confidence_score": 71},
        {"table_id": "c", "confidence_score": 80},
    ],
    "cost_estimate": {"monthly_cost_usd": 123.456},
    "workload_analysis": {
        "patterns_detected": [
            {"pattern_id": "p1", "query_ids": ["q1", "q2"]},
            {"pattern_id": "p2", "query_ids": ["q2", "q3"]},
            {"pattern_id": "p3"},
        ],
        "anti_patterns_detected": [{"anti_pattern_id": "x"}],
    },
}
SIGNALS = [
    {"signal": "s1", "query_count": 5, "targets": ["dynamodb"]},
    {"signal": "s2", "query_count": 7, "targets": ["dynamodb", "elasticache"]},
    {"signal": "s3", "query_count": 100, "targets": ["elasticache"]},
]


def test_the_overview_figures() -> None:
    assert analysis_overview_facts(ANALYSIS) == {
        "avg_confidence_score": 80,
        "tables_analyzed": 3,
        "monthly_cost_usd": 123.456,
        "patterns_detected": 3,
        "anti_patterns_detected": 1,
        "queries_covered": 3,
        "anti_pattern_query_counts": [0],
    }


@pytest.mark.parametrize("empty", [{}, {"table_recommendations": None, "workload_analysis": None}])
def test_an_empty_analysis_is_all_zero(empty) -> None:
    facts = analysis_overview_facts(empty)
    assert facts["avg_confidence_score"] == 0
    assert facts["tables_analyzed"] == 0
    assert facts["monthly_cost_usd"] == 0
    assert facts["patterns_detected"] == facts["anti_patterns_detected"] == 0
    assert facts["queries_covered"] == 0


def test_a_table_without_a_score_counts_as_zero() -> None:
    facts = analysis_overview_facts(
        {"table_recommendations": [{"confidence_score": 90}, {"table_id": "x"}]}
    )
    assert facts["avg_confidence_score"] == 45
    assert facts["tables_analyzed"] == 2


def test_the_signal_total_counts_only_this_engines_signals() -> None:
    assert analysis_overview_facts(ANALYSIS, SIGNALS, "dynamodb")["signal_count"] == 2
    assert analysis_overview_facts(ANALYSIS, SIGNALS, "dynamodb")["signal_query_total"] == 12
    assert analysis_overview_facts(ANALYSIS, SIGNALS, "elasticache")["signal_query_total"] == 107
    assert analysis_overview_facts(ANALYSIS, SIGNALS, "opensearch")["signal_query_total"] == 0
    assert "signal_query_total" not in analysis_overview_facts(ANALYSIS)


def _refusal(artifact: str, contract: str) -> ContractVersionError:
    return ContractVersionError(artifact=artifact, contract=contract, stored="9.0", expected="1.0")


@pytest.fixture
def services():
    s3 = MagicMock()
    s3.read_analysis.return_value = dict(ANALYSIS)
    s3.read_triage.return_value = {"signals": SIGNALS}
    results.sfn_service = MagicMock()
    results.sfn_service.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": "test_db", "source_database_type": "mysql"},
    }
    results.s3_service = s3
    yield s3
    results.sfn_service = None
    results.s3_service = None


def test_the_analysis_endpoint_returns_the_facts(services) -> None:
    resp = TestClient(app).get("/api/v1/assessments/job-1/analysis/dynamodb")

    assert resp.status_code == 200
    facts = resp.json()["facts"]
    assert facts["avg_confidence_score"] == 80
    assert facts["queries_covered"] == 3
    assert facts["signal_query_total"] == 12
    assert resp.json()["table_recommendations"] == ANALYSIS["table_recommendations"]


def test_an_old_job_without_triage_signals_still_gets_its_facts(services) -> None:
    services.read_triage.return_value = None
    resp = TestClient(app).get("/api/v1/assessments/job-1/analysis/dynamodb")

    assert resp.status_code == 200
    assert resp.json()["facts"]["avg_confidence_score"] == 80
    assert resp.json()["facts"]["signal_query_total"] == 0


def test_the_agent_summary_reads_the_same_average() -> None:
    s3 = MagicMock()
    s3.read_analysis.return_value = dict(ANALYSIS)
    assessments.s3_service = s3
    try:
        summary = assessments._extract_artifact_summary("test_db", "job-1", "analysis-dynamodb")
    finally:
        assessments.s3_service = None
    assert (
        summary["avg_confidence_score"] == analysis_overview_facts(ANALYSIS)["avg_confidence_score"]
    )


def test_a_mean_of_exactly_x_point_5_rounds_as_python_does() -> None:
    """Half to even, as the agent summary and report.json always did (the old page rounded half up)."""

    def mean(*scores: int) -> int:
        recs = [{"confidence_score": s} for s in scores]
        facts = analysis_overview_facts({"table_recommendations": recs})
        return int(facts["avg_confidence_score"])

    assert (mean(80, 81), mean(81, 82), mean(0, 1), mean(1, 2)) == (80, 82, 0, 2)


@pytest.mark.parametrize("cost", [None, "12.5", [], {"usd": 1}, True])
def test_a_non_number_monthly_cost_is_zero(cost) -> None:
    got = analysis_overview_facts({"cost_estimate": {"monthly_cost_usd": cost}})["monthly_cost_usd"]
    # a bool is an int to Python; it must not pass for a cost either way it is read
    assert got in (0, True) if cost is True else got == 0


def test_the_query_count_of_each_anti_pattern_follows_their_order() -> None:
    analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {"query_ids": ["a", "b"]},
                {"query_ids": None},
                {},
                "not a record",
            ]
        }
    }
    assert analysis_overview_facts(analysis)["anti_pattern_query_counts"] == [2, 0, 0, 0]


def test_a_triage_at_another_contract_version_does_not_take_the_analysis_away(services) -> None:
    services.read_triage.side_effect = _refusal("triage.json", "TriageOutput")
    resp = TestClient(app).get("/api/v1/assessments/job-1/analysis/dynamodb")

    assert resp.status_code == 200
    body = resp.json()
    assert body["table_recommendations"] == ANALYSIS["table_recommendations"]
    assert body["facts"]["avg_confidence_score"] == 80
    assert body["facts"]["signal_query_total"] == 0
    assert body["facts"]["signal_count"] == 0
    assert "contract version" in body["facts"]["signals_withheld_reason"]


def test_an_unreadable_triage_does_not_take_the_analysis_away(services) -> None:
    services.read_triage.side_effect = OSError("disk")
    resp = TestClient(app).get("/api/v1/assessments/job-1/analysis/dynamodb")

    assert resp.status_code == 200
    assert resp.json()["facts"]["signals_withheld_reason"]


def test_an_analysis_at_another_contract_version_is_still_refused(services) -> None:
    services.read_analysis.side_effect = _refusal("analysis.json", "AnalysisOutput")
    resp = TestClient(app).get("/api/v1/assessments/job-1/analysis/dynamodb")

    assert resp.status_code == 409


@pytest.mark.parametrize("bad", [None, "90", 90.0, True])
def test_report_json_and_the_overview_agree_on_every_kind_of_score(bad) -> None:
    """build_ranking (report.json) reads the engine's figures from the same function."""
    from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
    from src.agents.referee.synthesis_report import build_ranking

    analysis = {
        "table_recommendations": [
            {"table_id": "a", "confidence_score": 71},
            {"table_id": "b", "confidence_score": bad},
        ],
        "workload_analysis": {
            "patterns_detected": [{"p": 1}, {"p": 2}],
            "anti_patterns_detected": [{"a": 1}],
        },
        "cost_estimate": {"monthly_cost_usd": 10},
    }
    data = SynthesisData(
        job_id="j",
        database_name="db",
        triage={"selected_agents": [{"agent_type": "dynamodb"}]},
        collector={"database_schema": {"tables": []}, "queries": {"query_patterns": []}},
        engines={"dynamodb": EngineArtifacts(engine="dynamodb", analysis=analysis)},
        assignment=None,
    )
    entry = build_ranking(data)[0]
    facts = analysis_overview_facts(analysis)

    assert (
        entry["analysis_confidence"] == entry["confidence_score"] == facts["avg_confidence_score"]
    )
    assert entry["tables_analyzed"] == facts["tables_analyzed"]
    assert entry["patterns_detected"] == facts["patterns_detected"]
    assert entry["anti_patterns_detected"] == facts["anti_patterns_detected"]
