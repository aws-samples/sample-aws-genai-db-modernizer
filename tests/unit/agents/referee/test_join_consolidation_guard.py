"""Review of #375, finding B2: the reality check never moves a join onto
DynamoDB from a non-Aurora engine, unless a denormalised design is
explicitly proposed (no such mechanism exists yet, so this is
unconditional). Discourse's documentdb -> dynamodb consolidation of
`-4845005` and `-7745666` reproduced this exactly.
"""

from __future__ import annotations

from src.agents.referee.reality_check import run_reality_check


def _make_assignment(query_engine_pairs: list[tuple[str, str]]) -> dict:
    return {
        "version": 1,
        "query_assignments": [
            {"query_id": qid, "assigned_engine": engine, "assignment_reason": "test"}
            for qid, engine in query_engine_pairs
        ],
    }


class TestJoinNeverConsolidatedOntoDynamoDBFromNonAurora:
    def test_two_table_join_stays_off_dynamodb_when_consolidated_from_documentdb(self):
        assignment = _make_assignment(
            [(f"dq{i}", "dynamodb") for i in range(20)] + [("join1", "documentdb")]
        )
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
            "signals": [],
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": f"dq{i}", "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for i in range(20)
                ]
                + [
                    {
                        "query_id": "join1",
                        "tables_accessed": ["db.users", "db.groups"],
                        "query_type": "SELECT",
                        "has_joins": True,
                        "join_count": 1,
                        "query_text": (
                            "SELECT users.* FROM users INNER JOIN group_users ON "
                            "users.id = group_users.user_id WHERE group_users.group_id = ?"
                        ),
                    }
                ]
            }
        }
        # DynamoDB scores very highly on the join's tables -- without the guard
        # it would win the absorber ranking outright.
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 95},
                    {"table_id": "db.groups", "confidence_score": 95},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 50},
                    {"table_id": "db.groups", "confidence_score": 50},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["join1"] != "dynamodb"

    def test_two_table_join_already_on_dynamodb_at_v1_is_unaffected(self):
        """The guard only blocks a *consolidation* onto DynamoDB; a join a v1 fit
        score already placed there on its own merits is untouched."""
        assignment = _make_assignment(
            [(f"dq{i}", "dynamodb") for i in range(19)] + [("join1", "dynamodb")]
        )
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "aurora_mysql"}],
            "signals": [],
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": f"dq{i}", "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for i in range(19)
                ]
                + [
                    {
                        "query_id": "join1",
                        "tables_accessed": ["db.users", "db.groups"],
                        "query_type": "SELECT",
                        "has_joins": True,
                        "join_count": 1,
                        "query_text": (
                            "SELECT users.* FROM users INNER JOIN group_users ON "
                            "users.id = group_users.user_id WHERE group_users.group_id = ?"
                        ),
                    }
                ]
            }
        }
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 90},
                    {"table_id": "db.groups", "confidence_score": 90},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["join1"] == "dynamodb"
