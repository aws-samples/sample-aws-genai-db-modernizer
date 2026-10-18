"""#950: synthesis raises the migration risks before the summary counts them.

End to end through ``run_synthesis_deterministic`` and the report writer: the
risks from the waves and the decision traces are in ``risk_assessment``, the
summary and the facts count them, the strategies name them by wave gate, and a
report written before ``Risk.rule``/``Risk.wave`` still loads.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.synthesis_output import SynthesisOutputContract
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-risks"
FIXTURES = Path(__file__).resolve().parents[2] / "report" / "fixtures"


def _query(qid: str, table: str, query_type: str = "SELECT") -> dict:
    verb = "UPDATE" if query_type == "UPDATE" else "SELECT * FROM"
    return {
        "query_id": qid,
        "query_text": f"{verb} {table} WHERE id = ?",  # nosec B608 -- fixture text, never executed
        "query_type": query_type,
        "calls_per_second": 3.0,
        "rows_returned_avg": 1,
        "tables_accessed": [table],
    }


QUERIES = [
    _query("kv1", "users"),
    _query("kv2", "sessions"),
    _query("w_orders", "orders", "UPDATE"),
    _query("rel1", "orders"),
    _query("rel_users", "users"),
]


def _qa(qid: str, engine: str, table: str) -> dict:
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "source_tables": [table],
        "assignment_reason": f"highest confidence for {engine}",
    }


ASSIGNMENT = {
    "job_id": JOB,
    "version": 2,
    "query_assignments": [
        _qa("kv1", "dynamodb", "users"),
        _qa("kv2", "dynamodb", "sessions"),
        _qa("w_orders", "dynamodb", "orders"),
        _qa("rel1", "aurora_mysql", "orders"),
        _qa("rel_users", "aurora_mysql", "users"),
    ],
    "table_assignments": [
        {
            "table_id": "users",
            "primary_engine": "dynamodb",
            "engines": ["dynamodb", "aurora_mysql"],
            "query_count": 2,
        },
        {
            "table_id": "sessions",
            "primary_engine": "dynamodb",
            "engines": ["dynamodb"],
            "query_count": 1,
        },
        {
            "table_id": "orders",
            "primary_engine": "aurora_mysql",
            "engines": ["aurora_mysql", "dynamodb"],
            "query_count": 2,
        },
    ],
    "co_dependency_groups": [],
}


@pytest.fixture
def store(tmp_path):
    s = LocalArtifactStore(base_dir=str(tmp_path))
    engines = ["dynamodb", "aurora_mysql"]
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
    )
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {
                "tables": [{"table_id": "users"}, {"table_id": "sessions"}, {"table_id": "orders"}]
            },
            "queries": {"query_patterns": QUERIES},
        },
    )
    for engine, conf in (("dynamodb", 90), ("aurora_mysql", 60)):
        s.write_json(
            f"{DB}/{JOB}/analysis-{engine}/analysis.json",
            {
                "table_recommendations": [
                    {"table_id": t, "confidence_score": conf}
                    for t in ("users", "sessions", "orders")
                ],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
                "cost_estimate": {"monthly_cost_usd": 10},
            },
        )
    s.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", ASSIGNMENT)
    return s


def _rules(risks: list[dict]) -> set[str]:
    return {r.get("rule") for r in risks if r.get("rule")}


def test_the_wave_risks_are_raised_and_the_summary_counts_them(store) -> None:
    """An assignment without write groups: the wave's kept table, which a captured
    query writes, is a CDC copy (one MEDIUM sync risk for the wave)."""
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    risks = result["risk_assessment"]["risks"]
    sync = [r for r in risks if r.get("rule") == "sync.wave"]
    assert len(sync) == 1
    assert sync[0]["severity"] == "MEDIUM"
    assert sync[0]["affected_tables"] == ["orders"]
    assert sync[0]["wave"] == 2
    level = result["risk_assessment"]["overall_risk_level"]
    noun = "risk" if len(risks) == 1 else "risks"
    assert f"{len(risks)} open migration {noun} (overall: {level})" in result["summary"]
    strategies = result["risk_assessment"]["mitigation_strategies"]
    gate_lines = [s for s in strategies if s.startswith("Before the Wave 2 gate, close ")]
    assert len(gate_lines) == 1
    assert sync[0]["risk_id"] in gate_lines[0]
    # The wave gate line comes before the post-migration checks.
    assert strategies.index(gate_lines[0]) < next(
        i for i, s in enumerate(strategies) if s.startswith("After wave ")
    )


def test_the_written_report_carries_the_rules_and_the_facts_count_them(store) -> None:
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    _write_synthesis_report(store, result, assignment_version=2)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
    risks = report["risk_assessment"]["risks"]
    assert "sync.wave" in _rules(risks)
    assert all(r["wave"] == 2 for r in risks if r.get("rule") == "sync.wave")
    assert report["facts"]["open_risk_count"] == len(risks)
    assert report["facts"]["open_risks_by_severity"].get("MEDIUM", 0) >= 1
    # The decision traces the risks read are the ones written (built once).
    assert report["query_traces"] == result["query_traces"]


def test_a_report_without_the_new_risk_fields_still_loads() -> None:
    old = json.loads((FIXTURES / "wordpress_report.json").read_text())
    assert all("rule" not in r for r in old["risk_assessment"]["risks"])
    report = SynthesisOutputContract.model_validate(old)
    assert all(r.rule is None and r.wave is None for r in report.risk_assessment.risks)


def test_a_risk_wave_must_be_a_wave_number() -> None:
    old = json.loads((FIXTURES / "wordpress_report.json").read_text())
    bad = copy.deepcopy(old)
    bad["risk_assessment"]["risks"] = [
        {"severity": "HIGH", "description": "[dynamodb] x", "rule": "sync.wave", "wave": 0}
    ]
    with pytest.raises(ValueError):
        SynthesisOutputContract.model_validate(bad)


def test_with_write_groups_the_sync_risks_follow_the_sources_of_truth(store) -> None:
    """#992: the assignment names each table's one writer; the risks read the
    same ``table_sources`` report.json carries, built once."""
    assignment = {**ASSIGNMENT, "write_groups": {"orders": ["w_orders"]}}
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", assignment)
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    risks = result["risk_assessment"]["risks"]
    sync = next(r for r in risks if r.get("rule") == "sync.wave")
    assert sync["severity"] == "MEDIUM"
    assert "orders" in sync["affected_tables"]
    assert "rel1" in sync["query_ids"]
    assert sync["description"].startswith(
        "[dynamodb] Wave 2: 1 read runs on a copy of 1 table that CDC keeps in sync from DynamoDB"
    )
    _write_synthesis_report(store, result, assignment_version=2)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
    assert report["table_sources"] == result["table_sources"]
    assert report["facts"]["table_sources"]["replicated_table_count"] == 1
