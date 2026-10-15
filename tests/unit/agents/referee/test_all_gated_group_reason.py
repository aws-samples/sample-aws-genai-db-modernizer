"""#787: a co-dependency group no engine can serve says so, and names the gates.

When every analyzed engine is gated for at least one member of a co-dependency
group (the capability or exclusion gate, or the ownership gate), the group has
no candidate (ADR-031 Decisions 2 and 4) and takes the Aurora fallback. Its
members' ``assignment_reason`` used to read "no analysis available", even for
a member with real, ungated scores. The reason now says the group fell back
because no analyzed engine can serve every member, and names each engine's
gate (for "a member query": the ids stay in the ledger); the Move records the
member and gate per engine in ``evidence.group_gates`` (rule
``fallback.no_group_owner``).
"""

from __future__ import annotations

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.contracts.assignment_models import Assignment, QueryAssignment
from src.contracts.move_ledger import GroupGate, MovePass, ReasonCode
from src.shared.move_reasons import render_move_reason
from tests.e2e.routing_snapshot import categorize

SELF_JOIN = "SELECT * FROM t1 a JOIN t1 b ON a.id = b.id"
RECURSIVE_JOIN = (
    "WITH RECURSIVE tree AS (SELECT id FROM t1) SELECT * FROM t1 JOIN tree ON t1.id = tree.id"
)


def _query(qid: str, text: str, qtype: str = "SELECT") -> dict:
    # A join filtered on its joined table is significant, so two such queries
    # on the same table form a co-dependency group (``is_significant_join``).
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "tables_accessed": ["db.t1"],
        "join_count": 1,
        "has_joins": True,
        "has_aggregations": False,
        "filter_tables": ["db.t1"],
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }


def _resolve(queries: list[dict], analyzed: list[str], selected: list[str]) -> Assignment:
    collector = {
        "job_id": "t",
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [{"table_id": "db.t1", "table_name": "t1"}]},
        "queries": {"query_patterns": queries},
    }
    triage = {"selected_agents": [{"agent_type": e} for e in selected], "signals": []}
    analysis = {
        engine: {
            "table_recommendations": [{"table_id": "db.t1", "confidence_score": 95}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        }
        for engine in analyzed
    }
    return AssignmentResolver().resolve(triage, analysis, collector)


def _qa(result: Assignment, qid: str) -> QueryAssignment:
    return next(qa for qa in result.query_assignments if qa.query_id == qid)


class TestEveryEngineGatedByOneMember:
    """The issue's repro: ``qa`` scores 95 on DocumentDB and DynamoDB with no
    gate, but ``qb``'s recursive CTE gates both, so no engine can host the
    group."""

    def _result(self) -> Assignment:
        return _resolve(
            [_query("qa", SELF_JOIN), _query("qb", RECURSIVE_JOIN)],
            ["documentdb", "dynamodb"],
            ["documentdb", "dynamodb", "aurora_postgresql"],
        )

    def test_a_member_with_real_scores_does_not_say_no_analysis_available(self):
        qa = _qa(self._result(), "qa")

        assert qa.assigned_engine == "aurora_postgresql"
        assert "no analysis available" not in qa.assignment_reason
        assert qa.assignment_reason == (
            "co-dependency group → Aurora PostgreSQL (fallback: no analyzed engine can "
            "serve every member; DocumentDB and DynamoDB lack a capability a member "
            "query needs)"
        )

    def test_the_gated_member_keeps_its_own_gate_notes(self):
        qb = _qa(self._result(), "qb")

        assert qb.assignment_reason.startswith(
            "co-dependency group → Aurora PostgreSQL (fallback: no analyzed engine can "
            "serve every member;"
        )
        assert "DocumentDB lacks required capability" in qb.assignment_reason

    def test_the_move_records_the_gates_as_evidence(self):
        result = self._result()

        for qid in ("qa", "qb"):
            [move] = [m for m in result.moves if m.query_id == qid]
            assert move.pass_ is MovePass.AURORA_FALLBACK
            assert move.reason_code is ReasonCode.FALLBACK_NO_GROUP_OWNER
            assert move.group_key is not None
            assert move.evidence.group_gates == [
                GroupGate(engine="documentdb", query_id="qb", gate="capability"),
                GroupGate(engine="dynamodb", query_id="qb", gate="capability"),
            ]
        # Each member's own best engine is still in its ladder (Decision 4).
        [qa_move] = [m for m in result.moves if m.query_id == "qa"]
        assert {c.engine: c.score for c in qa_move.evidence.candidates} == {
            "documentdb": 95.0,
            "dynamodb": 95.0,
        }

    def test_the_ledger_renderer_names_the_same_gates(self):
        """The move ledger's one renderer (``src/shared/move_reasons.py``) words
        the Move from its rule and ``group_gates``, the same clause the
        assignment reason carries."""
        result = self._result()
        [move] = [m for m in result.moves if m.query_id == "qa"]

        assert render_move_reason(move) == (
            "No analyzed engine can serve every query in its co-dependency group "
            "(DocumentDB and DynamoDB lack a capability a member query needs), so it "
            "falls back to Aurora PostgreSQL."
        )

    def test_the_routing_snapshot_has_a_category_for_it(self):
        reason = _qa(self._result(), "qa").assignment_reason
        assert categorize(reason) == "co_dependency_fallback"


class TestOwnershipGate:
    """An engine that is not a system of record cannot own a write member
    (Decision 2 step 3): that is named as well."""

    def test_the_write_gate_is_named(self):
        update = _query("qa", "UPDATE t1 a JOIN t1 b ON a.id = b.id SET a.x = 1", "UPDATE")
        result = _resolve(
            [update, _query("qb", RECURSIVE_JOIN)],
            ["opensearch", "dynamodb"],
            ["opensearch", "dynamodb", "aurora_postgresql"],
        )
        qa = _qa(result, "qa")

        assert qa.assigned_engine == "aurora_postgresql"
        assert "OpenSearch cannot own a member query" in qa.assignment_reason
        assert "DynamoDB lacks a capability a member query needs" in qa.assignment_reason
        assert "query qa" not in qa.assignment_reason
        assert "query qb" not in qa.assignment_reason
        [move] = [m for m in result.moves if m.query_id == "qa"]
        assert GroupGate(engine="opensearch", query_id="qa", gate="write_gate") in (
            move.evidence.group_gates
        )


class TestHardExclusion:
    """The scorer's ``hard_exclusion`` gate has its own wording (#817 review):
    an excluded engine reads as ruled out, never as "gated"."""

    def test_an_excluded_engine_is_named_as_excluded(self):
        like = _query("qa", "SELECT * FROM t1 a JOIN t1 b ON a.id = b.id WHERE a.name LIKE '%term'")
        result = _resolve(
            [like, _query("qb", SELF_JOIN)],
            ["dynamodb"],
            ["dynamodb", "aurora_postgresql"],
        )
        qa = _qa(result, "qa")

        [move] = [m for m in result.moves if m.query_id == "qa"]
        assert move.evidence.group_gates == [
            GroupGate(engine="dynamodb", query_id="qa", gate="hard_exclusion")
        ]
        assert "DynamoDB is ruled out for a member query" in qa.assignment_reason
        assert "gated" not in qa.assignment_reason.split(" | ")[0]


class TestAGroupWithACandidateIsUnchanged:
    def test_no_group_gates_when_an_engine_hosts_the_group(self):
        result = _resolve(
            [_query("qa", SELF_JOIN), _query("qb", RECURSIVE_JOIN)],
            ["documentdb", "aurora_postgresql"],
            ["documentdb", "aurora_postgresql"],
        )

        for qid in ("qa", "qb"):
            qa = _qa(result, qid)
            assert qa.assignment_reason.startswith("co-dependency group → Aurora PostgreSQL")
            assert "fallback" not in qa.assignment_reason
            [move] = [m for m in result.moves if m.query_id == qid]
            assert move.evidence.group_gates == []
