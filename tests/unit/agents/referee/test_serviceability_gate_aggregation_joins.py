"""The serviceability gate never consolidates onto an engine that can't run
an aggregation or a multi-table join (#338).

``capability_registry.py``'s hard-capability matrix used to have no
``aggregation``/``complex_joins`` entries at all, so a ``SUM(...)``/``GROUP
BY``/multi-table-join query got ``required_caps = []`` and sailed through the
reality check's serviceability gate onto any committed engine -- including
DynamoDB, which cannot run it. See the WooCommerce reporting query in #338's
reproduction: ``aurora_mysql`` -> ``dynamodb`` with reason "no unique value",
even though DynamoDB has no server-side ``SUM``/``GROUP BY``.
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


def _make_collector(query_ids: list[str]) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                for qid in query_ids
            ]
        }
    }


class TestServiceabilityGateAggregationAndJoins:
    def test_sum_join_query_blocked_from_dynamodb(self):
        """WooCommerce shape from #338: aurora_mysql looks redundant by fit score
        alone, but DynamoDB has no aggregation/complex_joins capability."""
        assignment = _make_assignment(
            [(f"dq{i}", "dynamodb") for i in range(20)]
            + [(f"aq{i}", "aurora_mysql") for i in range(4)]
            + [("agg1", "aurora_mysql")]
        )
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "aurora_mysql"}],
            "signals": [],
            "query_capabilities": {"agg1": ["aggregation", "complex_joins"]},
        }
        collector = _make_collector(
            [f"dq{i}" for i in range(20)] + [f"aq{i}" for i in range(4)] + ["agg1"]
        )
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "aurora_mysql": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["agg1"] == "aurora_mysql"
        # The ordinary queries with no hard capability requirement still move
        assert {engine_of[f"aq{i}"] for i in range(4)} == {"dynamodb"}
        blocked = next(c for c in result["consolidations"] if c["from_engine"] == "aurora_mysql")
        assert blocked["action"] == "partial"
        assert blocked["queries_retained"] == ["agg1"]

    def test_query_with_no_hard_capability_is_unaffected(self):
        """A plain query with no aggregation/join requirement still consolidates normally."""
        assignment = _make_assignment(
            [(f"dq{i}", "dynamodb") for i in range(20)] + [("aq1", "aurora_mysql")]
        )
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "aurora_mysql"}],
            "signals": [],
            "query_capabilities": {},
        }
        collector = _make_collector([f"dq{i}" for i in range(20)] + ["aq1"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "aurora_mysql": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["aq1"] == "dynamodb"

    def test_single_table_count_star_blocked_from_dynamodb(self):
        """A review of a related PR: single-table COUNT(*)/FOUND_ROWS() queries also
        moved from Aurora to DynamoDB, where their own DynamoDB schema design then
        marked them unsupported. No join or GROUP BY needed -- the aggregate
        function alone is enough to require relational capability."""
        assignment = _make_assignment(
            [(f"dq{i}", "dynamodb") for i in range(20)]
            + [(f"aq{i}", "aurora_mysql") for i in range(4)]
            + [("count1", "aurora_mysql"), ("found1", "aurora_mysql")]
        )
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "aurora_mysql"}],
            "signals": [],
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for qid in [f"dq{i}" for i in range(20)] + [f"aq{i}" for i in range(4)]
                ]
                + [
                    {
                        "query_id": "count1",
                        "tables_accessed": ["db.postmeta"],
                        "query_type": "SELECT",
                        "query_text": "SELECT COUNT(*) FROM wp_postmeta WHERE meta_key = ?",
                    },
                    {
                        "query_id": "found1",
                        "tables_accessed": ["db.postmeta"],
                        "query_type": "SELECT",
                        "query_text": "SELECT `FOUND_ROWS` ( )",
                    },
                ]
            }
        }
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 80},
                    {"table_id": "db.postmeta", "confidence_score": 80},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 80},
                    {"table_id": "db.postmeta", "confidence_score": 80},
                ]
            },
        }

        # The capability gate needs the triage-computed query_capabilities, not
        # the resolver's own detection -- build it the way the pipeline does.
        from src.agents.referee.triage import _detect_query_capabilities

        query_capabilities = _detect_query_capabilities(collector, [])
        assert "aggregation" in query_capabilities.get("count1", [])
        assert "aggregation" in query_capabilities.get("found1", [])
        triage["query_capabilities"] = query_capabilities

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["count1"] == "aurora_mysql"
        assert engine_of["found1"] == "aurora_mysql"
