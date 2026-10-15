"""``Assignment.moves`` is per-version (#616, maintainer decision 2026-10-14).

Each assignment version holds only the Moves that produced it (design brief
Option 1A), never a cumulative copy of v1's. Replaying v1's moves rebuilds v1;
v1 plus each later version's own moves rebuilds that later version.

The writers that derive a version from an earlier one hold only their own
Moves: the reality check's and the customer gate's (#616 Track L PR2/PR3; an
edit that changes no engine records none). The post-schema router and its
rollback do not record theirs yet (Track L PR4), so they start the new version
with an empty ``moves`` list rather than inheriting v1's.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.agents.referee.assignment_overrides import apply_assignment_overrides
from src.agents.referee.reality_check_handler import write_reality_check_result
from src.contracts.assignment_models import Assignment
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.ledger_fakes import recorded_ledger
from tests.unit.tools.schema import test_pe_routing_flags as pe_routing


def _move(seq: int, qid: str, engine: str) -> dict:
    return {
        "seq": seq,
        "query_id": qid,
        "pass": "score",
        "to_engine": engine,
        "score_after": 80,
        "reason_code": "highest_score",
    }


def test_the_reality_check_version_does_not_inherit_v1_moves(tmp_path) -> None:
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
        "lightweight_recommendations": [],
        "collector_output": {},
        "analysis_outputs": {},
        "assignment": {
            "query_assignments": original,
            "moves": [_move(1, "q1", "opensearch"), _move(2, "q2", "aurora_mysql")],
        },
    }

    new_version = write_reality_check_result(store, "job-1", "mydb", result, assignment_version=1)

    assert new_version == 2
    v2 = store.read_json(f"mydb/job-1/assignment/v{new_version}/assignment.json")
    # Only the check's own Move (q1 off OpenSearch), never v1's two.
    assert [(m["seq"], m["query_id"], m["from_engine"], m["to_engine"]) for m in v2["moves"]] == [
        (1, "q1", "opensearch", "aurora_mysql")
    ]


V1 = {
    "job_id": "job-gate",
    "version": 1,
    "status": "auto_generated",
    "timestamp": "2026-10-14T00:00:00Z",
    "table_assignments": [],
    "co_dependency_groups": [],
    "validation_warnings": [],
    "query_assignments": [
        {
            "query_id": "q1",
            "assigned_engine": "aurora_mysql",
            "confidence": 80,
            "source_tables": ["orders"],
            "assignment_reason": "x",
        }
    ],
    "moves": [_move(1, "q1", "aurora_mysql")],
}


def test_a_customer_edit_does_not_inherit_v1_moves(tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
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
    store.write_json(f"{prefix}/assignment/v1/assignment.json", V1)
    assert len(Assignment.model_validate(V1).moves) == 1

    # Even a no-op edit is a new version, produced by the customer gate.
    result = apply_assignment_overrides(store, "shop", "job-gate", overrides=[])

    assert result.assignment.version == 2
    assert result.assignment.moves == []
    v2 = store.read_json("shop/job-gate/assignment/v2/assignment.json")
    assert v2["moves"] == []
    # v1 keeps its own ledger.
    v1 = store.read_json("shop/job-gate/assignment/v1/assignment.json")
    assert len(v1["moves"]) == 1


class TestThePostSchemaRouterDoesNotInheritV1Moves:
    # The seeded post-schema routing job (one flagged query moves) from #629's tests.
    case = pe_routing.TestTheOrchestratorRoutesAFlagByScore()

    def _seed_with_moves(self, store) -> str:
        self.case._seed(store)
        prefix = f"{self.case.DB}/{self.case.JOB}"
        key = f"{prefix}/assignment/v1/assignment.json"
        v1 = store.read_json(key)
        moves = [
            _move(i, qa["query_id"], qa["assigned_engine"])
            for i, qa in enumerate(v1["query_assignments"], start=1)
        ]
        store.write_json(key, {**v1, "moves": moves})
        return prefix

    def test_the_rerouted_version_starts_with_no_moves(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        prefix = self._seed_with_moves(store)
        self.case._run(store, [])

        v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
        assert v2["source"] == "post_schema_routing"
        assert v2["moves"] == []
        assert len(store.read_json(f"{prefix}/assignment/v1/assignment.json")["moves"]) == 4

    def test_the_rollback_version_starts_with_no_moves(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        prefix = self._seed_with_moves(store)
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
        assert v3["moves"] == []
