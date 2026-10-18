"""Every assignment version written from an earlier one carries today's
``contract_version`` (#972, ADR-008 Decision 1).

The reality check and the post-schema router (reroute and rollback) build the
new ``assignment.json`` by spreading the earlier version's dict. Spreading
alone would copy that file's ``contract_version`` (an older one, or none for a
file written before 1.15), so the new file would claim a version it was not
written at. Each writer stamps ``Assignment``'s own default instead. The
customer gate writes through ``Assignment.model_dump`` and is covered too.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.agents.referee.assignment_overrides import apply_assignment_overrides
from src.agents.referee.reality_check_handler import write_reality_check_result
from src.contracts.assignment_models import Assignment
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee import test_moves_are_per_version as per_version
from tests.unit.agents.referee.ledger_fakes import recorded_ledger
from tests.unit.tools.schema import test_pe_routing_flags as pe_routing

CURRENT = Assignment.model_fields["contract_version"].default

# A file written before 1.15 (no field) and one written at an older MINOR.
STORED_VERSIONS = [None, "1.14"]


def _stamp(record: dict, stored: str | None) -> dict:
    out = {k: v for k, v in record.items() if k != "contract_version"}
    if stored is not None:
        out["contract_version"] = stored
    return out


@pytest.mark.parametrize("stored", STORED_VERSIONS, ids=["pre-1.15", "1.14"])
def test_the_reality_check_version_carries_the_current_version(tmp_path, stored) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    original = [
        {"query_id": "q1", "assigned_engine": "opensearch", "in_scope": True},
        {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
    ]
    revised = [
        {"query_id": "q1", "assigned_engine": "aurora_mysql", "in_scope": True},
        {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
    ]
    result = {
        "revised_assignments": revised,
        "move_ledger": recorded_ledger(original, revised),
        "consolidations": [
            {
                "from_engine": "opensearch",
                "to_engine": "aurora_mysql",
                "query_count": 1,
                "reason": "test consolidation",
                "saved_cost_estimate": 1.0,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        ],
        "architectural_patterns": [],
        "recommendations": [],
        "unique_value_assessment": {},
        "executive_summary": None,
        "before_distribution": {"opensearch": 1, "aurora_mysql": 1},
        "after_distribution": {"aurora_mysql": 2},
        "collector_output": {},
        "analysis_outputs": {},
        "assignment": _stamp({"query_assignments": original}, stored),
    }

    new_version = write_reality_check_result(store, "job-1", "mydb", result, assignment_version=1)

    v2 = store.read_json(f"mydb/job-1/assignment/v{new_version}/assignment.json")
    assert v2["contract_version"] == CURRENT


def _seed_customer_gate_job(store, v1: dict) -> None:
    """The one-query job of ``test_moves_are_per_version``'s customer-gate case."""
    prefix = "shop/job-gate"
    store.write_json(
        f"{prefix}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": "aurora_mysql"}], "signals": []},
    )
    store.write_json(
        f"{prefix}/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": "orders"}]},
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM orders WHERE id = ?",  # nosec B608 -- test fixture, never executed
                        "query_type": "SELECT",
                        "calls_per_second": 1.0,
                        "rows_returned_avg": 1,
                        "tables_accessed": ["orders"],
                    }
                ]
            },
        },
    )
    store.write_json(
        f"{prefix}/analysis-aurora_mysql/analysis.json",
        {
            "table_recommendations": [{"table_id": "orders", "confidence_score": 80}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            "cost_estimate": {"monthly_cost_usd": 10},
        },
    )
    store.write_json(f"{prefix}/assignment/v1/assignment.json", v1)


@pytest.mark.parametrize("stored", STORED_VERSIONS, ids=["pre-1.15", "1.14"])
def test_the_customer_gate_version_carries_the_current_version(tmp_path, stored) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed_customer_gate_job(store, _stamp(per_version.V1, stored))

    apply_assignment_overrides(store, "shop", "job-gate", overrides=[])

    v1 = store.read_json("shop/job-gate/assignment/v1/assignment.json")
    assert v1.get("contract_version") == stored
    v2 = store.read_json("shop/job-gate/assignment/v2/assignment.json")
    assert v2["contract_version"] == CURRENT


class TestThePostSchemaRouterVersionsCarryTheCurrentVersion:
    case = pe_routing.TestTheOrchestratorRoutesAFlagByScore()

    def _seed(self, store, stored: str | None) -> str:
        self.case._seed(store)
        prefix = f"{self.case.DB}/{self.case.JOB}"
        key = f"{prefix}/assignment/v1/assignment.json"
        store.write_json(key, _stamp(store.read_json(key), stored))
        return prefix

    @pytest.mark.parametrize("stored", STORED_VERSIONS, ids=["pre-1.15", "1.14"])
    def test_the_rerouted_version(self, tmp_path, stored) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        prefix = self._seed(store, stored)
        self.case._run(store, [])

        v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
        assert v2["source"] == "post_schema_routing"
        assert v2["contract_version"] == CURRENT
        router = store.read_json(f"{prefix}/post-schema-router/router_output.json")
        assert router["contract_version"] == "1.4"

    @pytest.mark.parametrize("stored", STORED_VERSIONS, ids=["pre-1.15", "1.14"])
    def test_the_rollback_version(self, tmp_path, stored) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        prefix = self._seed(store, stored)
        orch = LocalOrchestrator(store, llm_mode="bedrock")
        with (
            patch(
                "src.agents.schema_design.handler.run_schema_design_auto",
                side_effect=RuntimeError("designer down"),
            ),
            pytest.raises(RuntimeError, match="designer down"),
        ):
            orch._run_post_schema_routing(self.case.JOB, self.case.DB)

        v3 = store.read_json(f"{prefix}/assignment/v3/assignment.json")
        assert v3["status"] == "reroute_rolled_back"
        assert v3["contract_version"] == CURRENT
