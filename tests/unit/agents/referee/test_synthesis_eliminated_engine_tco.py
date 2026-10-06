"""An eliminated engine's own analysed cost reaches ``tco_analysis`` (#380).

``data.engines`` has already dropped every engine the reality check
eliminated by the time ``build_tco_analysis`` runs (ADR-029 Layer E, #202),
so a saving the reality check's own recommendation names for it (e.g.
"$240.96/mo") used to appear nowhere in the TCO facts the report publishes --
a judge reading ``report.json``'s ``tco_analysis`` could not trace the
figure at all. ``run_synthesis_deterministic`` now reads that engine's own
``analysis-<engine>/analysis.json`` directly and carries its cost into
``tco_analysis.eliminated_engine_costs``.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-eliminated-tco"


@pytest.fixture
def store(tmp_path):
    s = LocalArtifactStore(base_dir=str(tmp_path))
    selected = ["dynamodb", "aurora_mysql", "opensearch"]
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in selected]},
    )
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"database_schema": {"tables": [{"table_id": "shop.orders"}]}, "queries": {}},
    )
    s.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json",
        {"workload_analysis": {}, "cost_estimate": {"monthly_cost_usd": 15.2}},
    )
    s.write_json(
        f"{DB}/{JOB}/analysis-aurora_mysql/analysis.json",
        {"workload_analysis": {}, "cost_estimate": {"monthly_cost_usd": 121.06}},
    )
    # Eliminated: no query in the effective (v2) assignment stays on opensearch,
    # but its own analysis is still on record from when it was still a candidate.
    s.write_json(
        f"{DB}/{JOB}/analysis-opensearch/analysis.json",
        {"workload_analysis": {}, "cost_estimate": {"monthly_cost_usd": 240.96}},
    )
    s.write_json(
        f"{DB}/{JOB}/schema-dynamodb/v2/schema_output.json",
        {"table_definitions": [{"table_name": "orders", "source_tables": ["shop.orders"]}]},
    )
    s.write_json(
        f"{DB}/{JOB}/assignment/v2/assignment.json",
        {
            "version": 2,
            "status": "reality_checked",
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True},
                {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
            ],
        },
    )
    s.write_json(
        f"{DB}/{JOB}/reality-check/output.json",
        {
            "consolidations": [
                {
                    "from_engine": "opensearch",
                    "to_engine": "dynamodb",
                    "query_count": 1,
                    "reason": "1 opensearch query moved to dynamodb",
                    "saved_cost_estimate": 450.0,
                    "action": "full",
                }
            ],
            "architectural_patterns": [],
        },
    )
    return s


def test_eliminated_engines_own_cost_is_traceable_in_tco_facts(store):
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    tco = result["tco_analysis"]
    assert tco["eliminated_engine_costs"] == [
        {"database": "opensearch", "monthly_cost_usd": 240.96}
    ]
    # The eliminated engine's own cost never counts toward the projected total --
    # only engines still in the effective architecture do.
    assert tco["projected_monthly_cost"] == pytest.approx(15.2 + 121.06, abs=0.01)
