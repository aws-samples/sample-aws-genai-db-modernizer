"""#877: a co-dependency group no engine can serve keeps its group entry.

A group that takes the Aurora fallback because no analyzed engine can serve
every member (``fallback.no_group_owner``, #787) is still a co-dependency
group: ADR-031 Decision 4 moves it as one unit in every later pass. The
routing trace used to record no group for it, so the reality check placed
its members one by one and could split it: a member with real scores of its
own moved to another committed engine while the gated member stayed behind.
"""

from __future__ import annotations

from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
from src.agents.referee.move_ledger import group_key
from src.agents.referee.reality_check import run_reality_check
from src.contracts.move_ledger import ReasonCode

SELF_JOIN = "SELECT * FROM t1 a JOIN t1 b ON a.id = b.id"
RECURSIVE_JOIN = (
    "WITH RECURSIVE tree AS (SELECT id FROM t1) SELECT * FROM t1 JOIN tree ON t1.id = tree.id"
)


def _join(qid: str, text: str) -> dict:
    # A join filtered on its joined table is significant, so two such queries
    # on the same table form a co-dependency group (``is_significant_join``).
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "tables_accessed": ["db.t1"],
        "join_count": 1,
        "has_joins": True,
        "has_aggregations": False,
        "filter_tables": ["db.t1"],
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }


def _lookup(qid: str) -> dict:
    return {
        "query_id": qid,
        "query_text": "SELECT * FROM t2 WHERE id = ?",
        "query_type": "SELECT",
        "tables_accessed": ["db.t2"],
        "calls_per_second": 1.0,
        "rows_returned_avg": 1,
    }


def _inputs() -> tuple[dict, dict, dict]:
    """``qa`` scores 95 on DynamoDB with no gate; ``qb``'s recursive CTE gates
    DynamoDB, so the group {qa, qb} falls back to Aurora PostgreSQL. The
    lookups on ``t2`` keep DynamoDB committed."""
    collector = {
        "job_id": "t",
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {
            "tables": [
                {"table_id": "db.t1", "table_name": "t1"},
                {"table_id": "db.t2", "table_name": "t2"},
            ]
        },
        "queries": {
            "query_patterns": [_join("qa", SELF_JOIN), _join("qb", RECURSIVE_JOIN)]
            + [_lookup(f"k{i}") for i in range(3)]
        },
    }
    triage = {
        "selected_agents": [{"agent_type": e} for e in ("dynamodb", "aurora_postgresql")],
        "signals": [],
    }
    analysis = {
        "dynamodb": {
            "table_recommendations": [
                {"table_id": "db.t1", "confidence_score": 95},
                {"table_id": "db.t2", "confidence_score": 95},
            ],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        }
    }
    return triage, analysis, collector


class TestTheTraceRecordsTheFallbackGroup:
    def test_every_member_carries_the_group_entry_and_its_fallback_reason(self):
        triage, analysis, collector = _inputs()
        trace = RoutingTrace()
        result = AssignmentResolver().resolve(triage, analysis, collector, routing_trace=trace)

        engine_of = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
        assert engine_of["qa"] == engine_of["qb"] == "aurora_postgresql"
        for qid in ("qa", "qb"):
            group = trace.by_query[qid].get("group")
            assert group == {
                "key": group_key(["qa", "qb"]),
                "scores": {},
                "fallback": ReasonCode.FALLBACK_NO_GROUP_OWNER.value,
            }
        # The key matches the key the fallback Moves carry.
        for move in result.moves:
            if move.query_id in ("qa", "qb"):
                assert move.group_key == group_key(["qa", "qb"])

    def test_a_group_with_a_candidate_has_no_fallback_key(self):
        triage, analysis, collector = _inputs()
        analysis["aurora_postgresql"] = {
            "table_recommendations": [
                {"table_id": "db.t1", "confidence_score": 60},
                {"table_id": "db.t2", "confidence_score": 60},
            ],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        }
        trace = RoutingTrace()
        AssignmentResolver().resolve(triage, analysis, collector, routing_trace=trace)

        group = trace.by_query["qa"]["group"]
        assert "fallback" not in group
        assert set(group["scores"]) == {"aurora_postgresql"}


class TestTheRealityCheckMovesTheFallbackGroupAsOne:
    def test_a_member_with_real_scores_does_not_leave_its_gated_partner(self):
        triage, analysis, collector = _inputs()
        v1 = AssignmentResolver().resolve(triage, analysis, collector)

        result = run_reality_check(v1.model_dump(mode="json"), triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["qa"] == engine_of["qb"] == "aurora_postgresql"
