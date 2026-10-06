"""A mandatory engine with a tiny workload share and a high fixed cost is not
exempt from the cost review forever (#167).

A signal override makes an engine "mandatory" through the reality check's
Pass 0-2, which protects its queries even when it carries almost none of the
workload. #326 added a dedicated justification floor for OpenSearch; this is
the same idea generalized to any other mandatory engine, judged on cost share
alone: an engine whose fixed monthly cost (``ENGINE_BASE_COST``) is high and
whose share of in-scope queries is tiny is not exempt just because a signal
made it mandatory.
"""

from __future__ import annotations

from src.agents.referee.reality_check import run_reality_check


def _make_collector(query_ids: list[str]) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                for qid in query_ids
            ]
        }
    }


class TestMandatoryEngineCostShareFloor:
    def test_tiny_share_high_cost_mandatory_engine_is_dropped(self):
        """A hypothetical mandatory engine (not OpenSearch) serving 1/40 queries."""
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(39)
            ]
            + [
                {
                    "query_id": "doc1",
                    "assigned_engine": "documentdb",
                    "assignment_reason": "signal override: nested_document → documentdb",
                    "signal_override": "nested_document",
                }
            ],
        }
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
            "signals": [],
        }
        collector = _make_collector([f"dq{i}" for i in range(39)] + ["doc1"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["doc1"] == "dynamodb"
        moved = next(c for c in result["consolidations"] if c["from_engine"] == "documentdb")
        assert "167" in moved["reason"] or "justification floor" in moved["reason"]
