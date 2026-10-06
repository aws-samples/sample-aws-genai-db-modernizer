"""Unit tests for the reality check agent core logic."""

from src.agents.referee.reality_check import (
    BASIC_CRUD_SCORE,
    SIGNAL_MATCH_BONUS,
    TINY_MANDATORY_QUERY_THRESHOLD,
    _can_engine_serve_query,
    _engine_fit_score,
    _run_aurora_absorption_pass,
    run_reality_check,
)


def _make_assignment(query_engine_pairs: list[tuple[str, str]]) -> dict:
    """Build a minimal assignment dict."""
    return {
        "version": 1,
        "query_assignments": [
            {
                "query_id": qid,
                "assigned_engine": engine,
                "assignment_reason": "test",
            }
            for qid, engine in query_engine_pairs
        ],
    }


def _make_triage(signals: list[dict] | None = None) -> dict:
    return {
        "selected_agents": [
            {"agent_type": "dynamodb"},
            {"agent_type": "documentdb"},
            {"agent_type": "opensearch"},
        ],
        "signals": signals or [],
    }


def _make_collector(query_ids: list[str], extra: dict[str, dict] | None = None) -> dict:
    extra = extra or {}
    patterns = []
    for qid in query_ids:
        pattern = {
            "query_id": qid,
            "tables_accessed": ["db.users"],
            "query_type": "SELECT",
        }
        pattern.update(extra.get(qid, {}))
        patterns.append(pattern)
    return {"queries": {"query_patterns": patterns}}


# A query_text/calls_per_second pair that clears the OpenSearch justification
# floor (#326): real search depth (MATCH...AGAINST) at traffic above
# OPENSEARCH_MIN_CALLS_PER_SECOND, with no source engine metadata (so the
# native-source-engine override does not apply). Tests that assert OpenSearch
# keeps a mandatory query use this so they test mandatory protection itself,
# not the justification floor (covered separately below).
_REAL_SEARCH_DEPTH_QUERY = {
    "query_text": "SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)",
    "calls_per_second": 5.0,
}


def _make_engine_queries(engine_query_map: dict[str, list[str]]) -> dict[str, list[dict]]:
    """Build engine_queries dict: engine -> list of assignment dicts."""
    result = {}
    for engine, qids in engine_query_map.items():
        result[engine] = [
            {"query_id": qid, "assigned_engine": engine, "assignment_reason": "test"}
            for qid in qids
        ]
    return result


class TestEngineFitScore:
    """Test the per-query engine fit scoring."""

    def test_basic_crud_score_for_capable_engine(self):
        score = _engine_fit_score(
            "dynamodb",
            {"query_id": "q1"},
            {},
            {"q1": {"tables_accessed": ["db.users"]}},
            {},
        )
        assert score == BASIC_CRUD_SCORE

    def test_zero_score_for_incapable_engine(self):
        score = _engine_fit_score(
            "elasticache",
            {"query_id": "q1"},
            {},
            {"q1": {"tables_accessed": ["db.users"]}},
            {},
        )
        assert score == 0

    def test_signal_match_bonus(self):
        score = _engine_fit_score(
            "opensearch",
            {"query_id": "q1"},
            {"q1": ["text_search"]},
            {"q1": {"tables_accessed": ["db.posts"]}},
            {},
        )
        assert score == BASIC_CRUD_SCORE + SIGNAL_MATCH_BONUS

    def test_signal_mismatch_penalty(self):
        score = _engine_fit_score(
            "dynamodb",
            {"query_id": "q1"},
            {"q1": ["text_search"]},
            {"q1": {"tables_accessed": ["db.posts"]}},
            {},
        )
        assert score == BASIC_CRUD_SCORE - SIGNAL_MATCH_BONUS

    def test_analysis_confidence_used(self):
        score = _engine_fit_score(
            "dynamodb",
            {"query_id": "q1"},
            {},
            {"q1": {"tables_accessed": ["db.users"]}},
            {
                "dynamodb": {
                    "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
                }
            },
        )
        assert score == 90


class TestCanEngineServeQuery:
    """Test the capability check."""

    def test_signal_match_returns_true(self):
        assert _can_engine_serve_query(
            "opensearch",
            {"query_id": "q1"},
            {"q1": ["text_search"]},
            {"q1": {"tables_accessed": []}},
            {},
        )

    def test_signal_mismatch_returns_false(self):
        assert not _can_engine_serve_query(
            "dynamodb",
            {"query_id": "q1"},
            {"q1": ["text_search"]},
            {"q1": {"tables_accessed": []}},
            {},
        )

    def test_basic_crud_returns_true_without_signals(self):
        assert _can_engine_serve_query(
            "dynamodb",
            {"query_id": "q1"},
            {},
            {"q1": {"tables_accessed": []}},
            {},
        )


class TestRunRealityCheck:
    """Test the full reality check flow."""

    def test_no_consolidation_when_engines_have_unique_value(self):
        """Two engines with different signal specializations should both survive."""
        assignment = _make_assignment(
            [("q1", "dynamodb"), ("q2", "dynamodb"), ("q3", "opensearch")]
        )
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q3"],
                    "evidence": "LIKE query",
                }
            ]
        )
        collector = _make_collector(["q1", "q2", "q3"], extra={"q3": _REAL_SEARCH_DEPTH_QUERY})
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        assert len(result["consolidations"]) == 0

    def test_consolidation_when_engine_is_redundant(self):
        """An engine with no unique queries should be consolidated."""
        assignment = _make_assignment(
            [
                ("q1", "dynamodb"),
                ("q2", "dynamodb"),
                ("q3", "documentdb"),
                ("q4", "documentdb"),
            ]
        )
        triage = _make_triage()
        collector = _make_collector(["q1", "q2", "q3", "q4"])
        # Both engines score equally on all tables — documentdb is redundant
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        # documentdb should be consolidated (it's more expensive and redundant)
        assert len(result["consolidations"]) > 0
        consolidated_from = {c["from_engine"] for c in result["consolidations"]}
        assert "documentdb" in consolidated_from

    def test_revised_assignments_returned(self):
        """Consolidated queries appear in revised_assignments with new engine."""
        assignment = _make_assignment([("q1", "dynamodb"), ("q2", "documentdb")])
        triage = _make_triage()
        collector = _make_collector(["q1", "q2"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engines_after = {qa["assigned_engine"] for qa in result["revised_assignments"]}
        # After consolidation, all queries should be in dynamodb (cheaper)
        if result["consolidations"]:
            assert "dynamodb" in engines_after

    def test_unique_value_assessment_populated(self):
        """Every engine should have a unique value assessment."""
        assignment = _make_assignment([("q1", "dynamodb"), ("q2", "opensearch")])
        triage = _make_triage()
        collector = _make_collector(["q1", "q2"])

        result = run_reality_check(assignment, triage, {}, collector)
        assert "unique_value_assessment" in result
        # At least one engine should be assessed
        assert len(result["unique_value_assessment"]) > 0

    def test_architectural_patterns_detected(self):
        """Multi-engine setup should detect patterns like CQRS."""
        assignment = _make_assignment(
            [
                ("q1", "dynamodb"),
                ("q2", "dynamodb"),
                ("q3", "opensearch"),
            ]
        )
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q3"],
                    "evidence": "text search",
                }
            ]
        )
        collector = _make_collector(["q1", "q2", "q3"], extra={"q3": _REAL_SEARCH_DEPTH_QUERY})
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 85}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        pattern_names = [p["name"] for p in result["architectural_patterns"]]
        # Should detect CQRS or Materialized View with DynamoDB + OpenSearch
        assert len(pattern_names) > 0

    def test_recommendations_always_present(self):
        assignment = _make_assignment([("q1", "dynamodb")])
        triage = _make_triage()
        collector = _make_collector(["q1"])

        result = run_reality_check(assignment, triage, {}, collector)
        assert len(result["recommendations"]) > 0

    def test_text_search_not_consolidated_to_dynamodb(self):
        """Text search queries must stay in OpenSearch even with high table confidence."""
        assignment = _make_assignment(
            [("q1", "dynamodb"), ("q2", "dynamodb"), ("q3", "opensearch")]
        )
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q3"],
                    "evidence": "LIKE '%search%'",
                }
            ]
        )
        collector = _make_collector(["q1", "q2", "q3"], extra={"q3": _REAL_SEARCH_DEPTH_QUERY})
        # DynamoDB has high confidence on the same table — but can't do text search
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        # q3 must stay in opensearch — DynamoDB lacks text_search capability
        q3_engine = next(
            qa["assigned_engine"] for qa in result["revised_assignments"] if qa["query_id"] == "q3"
        )
        assert q3_engine == "opensearch"

    def test_mandatory_signal_override_protected(self):
        """Queries with signal override should not be consolidated."""
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
                {
                    "query_id": "q2",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "signal override: text_search",
                },
            ],
        }
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q2"],
                    "evidence": "LIKE query",
                }
            ]
        )
        collector = _make_collector(["q1", "q2"], extra={"q2": _REAL_SEARCH_DEPTH_QUERY})

        result = run_reality_check(assignment, triage, {}, collector)
        # q2 should still be in opensearch (mandatory)
        q2_engine = next(
            qa["assigned_engine"] for qa in result["revised_assignments"] if qa["query_id"] == "q2"
        )
        assert q2_engine == "opensearch"

    def test_mandatory_detected_from_structured_field(self):
        """Protection reads signal_override, so rewording the reason can't disable it (#151)."""
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
                {
                    "query_id": "q2",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "routed for full-text search",
                    "signal_override": "text_search",
                },
            ],
        }
        collector = _make_collector(["q1", "q2"], extra={"q2": _REAL_SEARCH_DEPTH_QUERY})

        result = run_reality_check(assignment, _make_triage(), {}, collector)

        assert result["unique_value_assessment"]["opensearch"]["is_mandatory"] is True
        q2 = next(qa for qa in result["revised_assignments"] if qa["query_id"] == "q2")
        assert q2["assigned_engine"] == "opensearch"

    def test_pass0_evaluates_most_expensive_engine_first(self):
        """Pass 0 removes the most expensive redundant engine before cheaper ones (#164)."""
        assignment = _make_assignment(
            [
                ("q1", "dynamodb"),
                ("q2", "elasticache"),
                ("q3", "opensearch"),
                ("q4", "documentdb"),
                ("q5", "aurora_mysql"),
            ]
        )
        collector = _make_collector(["q1", "q2", "q3", "q4", "q5"])

        result = run_reality_check(assignment, _make_triage(), {}, collector)

        # dynamodb is the primary and is evaluated last; the rest go by cost, highest first
        assert list(result["unique_value_assessment"]) == [
            "aurora_mysql",
            "documentdb",
            "opensearch",
            "elasticache",
            "dynamodb",
        ]


class TestAuroraAbsorptionPass:
    """Test the Aurora absorption pass (Pass 1)."""

    def test_full_absorption_documentdb_into_aurora(self):
        """DocumentDB with 5 queries, all scoring well on Aurora, gets fully absorbed."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "documentdb": ["dq1", "dq2", "dq3", "dq4", "dq5"],
            }
        )
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.docs", "confidence_score": 60}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.docs", "confidence_score": 55}]
            },
        }
        query_map = {f"dq{i}": {"tables_accessed": ["db.docs"]} for i in range(1, 6)}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.docs"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "documentdb"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        assert result.aurora_engine == "aurora_postgresql"
        assert "documentdb" in result.engines_eliminated
        assert len(result.absorbed_queries) == 5

    def test_low_aurora_fit_blocks_absorption(self):
        """DocumentDB survives when Aurora scores below min fit on its queries."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "documentdb": ["dq1", "dq2", "dq3", "dq4", "dq5"],
            }
        )
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.docs", "confidence_score": 30}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.docs", "confidence_score": 70}]
            },
        }
        query_map = {f"dq{i}": {"tables_accessed": ["db.docs"]} for i in range(1, 6)}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.docs"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "documentdb"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        assert result.engines_eliminated == []
        assert result.absorbed_queries == []

    def test_specialist_delta_protects_high_value_queries(self):
        """DynamoDB with 3 PK lookups scoring much higher than Aurora stays alive."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "dynamodb": ["ddb1", "ddb2", "ddb3"],
            }
        )
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.sessions", "confidence_score": 45}]
            },
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.sessions", "confidence_score": 95}]
            },
        }
        query_map = {
            "ddb1": {"tables_accessed": ["db.sessions"]},
            "ddb2": {"tables_accessed": ["db.sessions"]},
            "ddb3": {"tables_accessed": ["db.sessions"]},
        }
        query_map.update({f"aq{i}": {"tables_accessed": ["db.sessions"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "dynamodb"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        assert result.engines_eliminated == []
        assert result.absorbed_queries == []

    def test_above_threshold_skipped(self):
        """Engine with 12 queries is not a candidate (above threshold of 10)."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "documentdb": [f"dq{i}" for i in range(12)],
            }
        )
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.docs", "confidence_score": 80}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.docs", "confidence_score": 50}]
            },
        }
        query_map = {f"dq{i}": {"tables_accessed": ["db.docs"]} for i in range(12)}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.docs"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "documentdb"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        assert result.engines_eliminated == []
        assert result.absorbed_queries == []

    def test_no_aurora_in_stack_is_noop(self):
        """Pass does nothing when no Aurora engine is committed."""
        engine_queries = _make_engine_queries(
            {
                "dynamodb": [f"q{i}" for i in range(20)],
                "documentdb": ["dq1", "dq2", "dq3"],
            }
        )

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"dynamodb", "documentdb"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map={},
            analysis_outputs={},
            query_capabilities={},
        )

        assert result.aurora_engine == ""
        assert result.engines_eliminated == []
        assert result.absorbed_queries == []

    def test_mandatory_engine_above_tiny_threshold_never_absorbed(self):
        """OpenSearch with mandatory signal overrides is protected once it carries real volume."""
        os_ids = [f"os{i}" for i in range(1, TINY_MANDATORY_QUERY_THRESHOLD + 2)]
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "opensearch": os_ids,
            }
        )
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.posts", "confidence_score": 70}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.posts", "confidence_score": 60}]
            },
        }
        query_map = {qid: {"tables_accessed": ["db.posts"]} for qid in os_ids}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.posts"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "opensearch"},
            mandatory_committed_engines={"opensearch"},
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        assert result.engines_eliminated == []
        assert result.absorbed_queries == []

    def test_partial_absorption(self):
        """Engine with mix of absorbable and protected queries gets partially absorbed."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "elasticache": ["ec1", "ec2", "ec3", "ec4", "ec5", "ec6", "ec7", "ec8"],
            }
        )
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.cache", "confidence_score": 60},
                    {"table_id": "db.sessions", "confidence_score": 20},
                ]
            },
            "elasticache": {
                "table_recommendations": [
                    {"table_id": "db.cache", "confidence_score": 55},
                    {"table_id": "db.sessions", "confidence_score": 85},
                ]
            },
        }
        query_map = {}
        for i in range(1, 6):
            query_map[f"ec{i}"] = {"tables_accessed": ["db.cache"]}
        for i in range(6, 9):
            query_map[f"ec{i}"] = {"tables_accessed": ["db.sessions"]}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.cache"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "elasticache"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        assert len(result.absorbed_queries) == 5
        assert "elasticache" in result.engines_reduced
        assert "elasticache" not in result.engines_eliminated

    def test_mixed_specialist_value_engine_survives_for_protected(self):
        """ElastiCache survives for 2 protected queries even when 4 are absorbable."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "elasticache": ["ec1", "ec2", "ec3", "ec4", "ec5", "ec6"],
            }
        )
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.generic", "confidence_score": 55},
                    {"table_id": "db.hotcache", "confidence_score": 20},
                ]
            },
            "elasticache": {
                "table_recommendations": [
                    {"table_id": "db.generic", "confidence_score": 50},
                    {"table_id": "db.hotcache", "confidence_score": 85},
                ]
            },
        }
        query_map = {}
        for i in range(1, 5):
            query_map[f"ec{i}"] = {"tables_accessed": ["db.generic"]}
        for i in range(5, 7):
            query_map[f"ec{i}"] = {"tables_accessed": ["db.hotcache"]}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.generic"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "elasticache"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        assert len(result.absorbed_queries) == 4
        assert "elasticache" in result.engines_reduced
        assert "elasticache" not in result.engines_eliminated

    def test_majority_protected_engine_fully_survives(self):
        """Engine where most queries are specialist-protected is skipped entirely."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "elasticache": ["ec1", "ec2", "ec3", "ec4", "ec5", "ec6"],
            }
        )
        # ec1-ec2: Aurora scores 60, ElastiCache scores 55 => delta=-5 => absorbable
        # ec3-ec6: Aurora scores 20, ElastiCache scores 85 => delta=65 => protected
        # Protected count (4) > len(qas)//2 (3) => engine survives entirely
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.generic", "confidence_score": 60},
                    {"table_id": "db.hotpath", "confidence_score": 20},
                ]
            },
            "elasticache": {
                "table_recommendations": [
                    {"table_id": "db.generic", "confidence_score": 55},
                    {"table_id": "db.hotpath", "confidence_score": 85},
                ]
            },
        }
        query_map = {}
        for i in range(1, 3):
            query_map[f"ec{i}"] = {"tables_accessed": ["db.generic"]}
        for i in range(3, 7):
            query_map[f"ec{i}"] = {"tables_accessed": ["db.hotpath"]}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.generic"]} for i in range(30)})

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_postgresql", "elasticache"},
            mandatory_committed_engines=set(),
            query_signals={},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={},
        )

        # Majority protected: engine is skipped entirely, nothing absorbed
        assert result.absorbed_queries == []
        assert result.engines_eliminated == []
        assert result.engines_reduced == []


class TestAuroraAbsorptionIntegration:
    """Test Aurora absorption through the full run_reality_check() flow."""

    def test_documentdb_absorbed_into_aurora_full_flow(self):
        """DocumentDB with 5 low-delta queries gets absorbed into Aurora via full pipeline.

        Setup ensures both engines survive Pass 0 before Pass 1 can run:
        - Aurora queries use db.main (Aurora=80, DocDB=50 → delta=30 for Aurora, unique)
        - DocDB queries use db.docs (Aurora=65, DocDB=82 → delta=17 for DocDB, unique in Pass 0)
        - In Pass 1: DocDB has 5 queries < threshold(10), aurora_fit=65 >= 50, delta=17 < 30 → absorbed
        """
        aurora_query_ids = [f"aq{i}" for i in range(30)]
        doc_query_ids = [f"dq{i}" for i in range(1, 6)]
        assignment = _make_assignment(
            [(f"aq{i}", "aurora_postgresql") for i in range(30)]
            + [(f"dq{i}", "documentdb") for i in range(1, 6)]
        )
        triage = {
            "selected_agents": [
                {"agent_type": "aurora_postgresql"},
                {"agent_type": "documentdb"},
            ],
            "signals": [],
        }
        # Aurora queries on db.main, DocDB queries on db.docs — different tables
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.main"], "query_type": "SELECT"}
                    for qid in aurora_query_ids
                ]
                + [
                    {"query_id": qid, "tables_accessed": ["db.docs"], "query_type": "SELECT"}
                    for qid in doc_query_ids
                ]
            }
        }
        # db.main: Aurora=80, DocDB=50 → Aurora queries have delta=30 (unique in Pass 0)
        # db.docs: Aurora=65, DocDB=82 → DocDB queries have delta=17 (unique in Pass 0,
        #          but aurora_fit=65 >= 50 and delta=17 < 30, so absorbable in Pass 1)
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 80},
                    {"table_id": "db.docs", "confidence_score": 65},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 50},
                    {"table_id": "db.docs", "confidence_score": 82},
                ]
            },
        }

        result = run_reality_check(
            assignment=assignment,
            triage=triage,
            analysis_outputs=analysis_outputs,
            collector_output=collector,
        )

        # DocumentDB should be absorbed via Pass 1
        aurora_info = result.get("aurora_absorption", {})
        assert "documentdb" in aurora_info.get("engines_eliminated", [])

        # Check at least one absorption consolidation record exists with action "full"
        absorption_consolidations = [
            c
            for c in result["consolidations"]
            if c["to_engine"] == "aurora_postgresql" and c["from_engine"] == "documentdb"
        ]
        assert len(absorption_consolidations) >= 1
        assert any(c["action"] == "full" for c in absorption_consolidations)

    def test_dynamodb_protected_by_high_delta(self):
        """DynamoDB with high-value PK lookups survives absorption despite low count."""
        query_ids = [f"aq{i}" for i in range(30)] + ["ddb1", "ddb2", "ddb3"]
        assignment = _make_assignment(
            [(f"aq{i}", "aurora_postgresql") for i in range(30)]
            + [("ddb1", "dynamodb"), ("ddb2", "dynamodb"), ("ddb3", "dynamodb")]
        )
        triage = {
            "selected_agents": [
                {"agent_type": "aurora_postgresql"},
                {"agent_type": "dynamodb"},
            ],
            "signals": [],
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.sessions"], "query_type": "SELECT"}
                    for qid in query_ids
                ]
            }
        }
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.sessions", "confidence_score": 40}]
            },
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.sessions", "confidence_score": 95}]
            },
        }

        result = run_reality_check(
            assignment=assignment,
            triage=triage,
            analysis_outputs=analysis_outputs,
            collector_output=collector,
        )

        # DynamoDB should NOT be absorbed (delta = 95-40 = 55, well above threshold of 30)
        aurora_info = result.get("aurora_absorption", {})
        assert "dynamodb" not in aurora_info.get("engines_eliminated", [])


class TestAbsorptionAppliedToAssignments:
    """Consolidations and revised_assignments must agree (#168)."""

    @staticmethod
    def _run_partial_scenario() -> dict:
        """Aurora owns 30 join queries; ElastiCache has 5 absorbable and 3 protected queries."""
        aurora_ids = [f"aq{i}" for i in range(30)]
        assignment = _make_assignment(
            [(qid, "aurora_postgresql") for qid in aurora_ids]
            + [(f"ec{i}", "elasticache") for i in range(1, 9)]
        )
        triage = {
            "signals": [
                {
                    "signal": "complex_joins",
                    "targets": ["aurora_postgresql"],
                    "query_ids": aurora_ids,
                }
            ]
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.cache"]} for qid in aurora_ids
                ]
                + [
                    {
                        "query_id": f"ec{i}",
                        "tables_accessed": ["db.cache" if i < 6 else "db.sessions"],
                    }
                    for i in range(1, 9)
                ]
            }
        }
        analysis = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.cache", "confidence_score": 60},
                    {"table_id": "db.sessions", "confidence_score": 20},
                ]
            },
            "elasticache": {
                "table_recommendations": [
                    {"table_id": "db.cache", "confidence_score": 55},
                    {"table_id": "db.sessions", "confidence_score": 85},
                ]
            },
        }
        return run_reality_check(assignment, triage, analysis, collector)

    def test_partial_absorption_moves_absorbed_queries(self):
        result = self._run_partial_scenario()
        assert result["aurora_absorption"]["engines_reduced"] == ["elasticache"]

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert [engine_of[f"ec{i}"] for i in range(1, 6)] == ["aurora_postgresql"] * 5
        assert [engine_of[f"ec{i}"] for i in range(6, 9)] == ["elasticache"] * 3

    def test_partial_absorption_records_reason_on_moved_queries(self):
        result = self._run_partial_scenario()
        moved = next(qa for qa in result["revised_assignments"] if qa["query_id"] == "ec1")
        assert moved["assignment_reason"].startswith("reality check: absorbed from elasticache")

    def test_full_absorption_counts_each_query_once(self):
        """A fully absorbed engine yields one consolidation record set, not two."""
        aurora_ids = [f"aq{i}" for i in range(30)]
        doc_ids = [f"dq{i}" for i in range(1, 6)]
        assignment = _make_assignment(
            [(qid, "aurora_postgresql") for qid in aurora_ids]
            + [(qid, "documentdb") for qid in doc_ids]
        )
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.main"]} for qid in aurora_ids
                ]
                + [{"query_id": qid, "tables_accessed": ["db.docs"]} for qid in doc_ids]
            }
        }
        analysis = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 80},
                    {"table_id": "db.docs", "confidence_score": 65},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 50},
                    {"table_id": "db.docs", "confidence_score": 82},
                ]
            },
        }

        result = run_reality_check(assignment, {"signals": []}, analysis, collector)

        from_docdb = [c for c in result["consolidations"] if c["from_engine"] == "documentdb"]
        assert sum(c["query_count"] for c in from_docdb) == 5
        assert {c["to_engine"] for c in from_docdb} == {"aurora_postgresql"}
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert {engine_of[qid] for qid in doc_ids} == {"aurora_postgresql"}


class TestTinyMandatoryAbsorption:
    """A tiny mandatory engine can be absorbed when Aurora covers its signal at a basic level (#165)."""

    @staticmethod
    def _absorb(os_count: int, aurora_text_confidence: int = 70, required_caps=None):
        os_ids = [f"os{i}" for i in range(1, os_count + 1)]
        engine_queries = _make_engine_queries(
            {"aurora_mysql": [f"aq{i}" for i in range(10)], "opensearch": os_ids}
        )
        query_map = {qid: {"tables_accessed": ["db.users"]} for qid in os_ids}
        query_map.update({f"aq{i}": {"tables_accessed": ["db.posts"]} for i in range(10)})
        analysis_outputs = {
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": aurora_text_confidence}
                ]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 60}]
            },
        }
        return _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_mysql", "opensearch"},
            mandatory_committed_engines={"opensearch"},
            query_signals={qid: ["text_search"] for qid in os_ids},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={qid: required_caps or ["inverted_index"] for qid in os_ids},
        )

    def test_tiny_text_search_engine_absorbed_into_aurora_fulltext(self):
        result = self._absorb(os_count=3)

        assert result.engines_eliminated == ["opensearch"]
        assert {aq["to_engine"] for aq in result.absorbed_queries} == {"aurora_mysql"}
        assert len(result.absorbed_queries) == 3

    def test_absorption_reason_names_the_basic_capability(self):
        result = self._absorb(os_count=3)
        assert "text_search_basic" in result.absorbed_queries[0]["reason"]

    def test_tiny_mandatory_engine_kept_when_aurora_fit_is_low(self):
        result = self._absorb(os_count=3, aurora_text_confidence=20)

        assert result.engines_eliminated == []
        assert result.absorbed_queries == []

    def test_tiny_mandatory_engine_kept_when_hard_capability_missing(self):
        result = self._absorb(os_count=3, required_caps=["vector_index"])

        assert result.engines_eliminated == []
        assert result.absorbed_queries == []

    def test_tiny_mandatory_engine_never_partially_absorbed(self):
        """Moving only some mandatory queries keeps the engine and its cost, so move none."""
        os_ids = ["os1", "os2", "os3"]
        engine_queries = _make_engine_queries(
            {"aurora_mysql": [f"aq{i}" for i in range(10)], "opensearch": os_ids}
        )
        query_map = {
            "os1": {"tables_accessed": ["db.users"]},
            "os2": {"tables_accessed": ["db.users"]},
            "os3": {"tables_accessed": ["db.logs"]},
        }
        analysis_outputs = {
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 70},
                    {"table_id": "db.logs", "confidence_score": 10},
                ]
            },
        }

        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines={"aurora_mysql", "opensearch"},
            mandatory_committed_engines={"opensearch"},
            query_signals={qid: ["text_search"] for qid in os_ids},
            query_map=query_map,
            analysis_outputs=analysis_outputs,
            query_capabilities={qid: ["inverted_index"] for qid in os_ids},
        )

        assert result.absorbed_queries == []
        assert result.engines_reduced == []


def test_aurora_mysql_serves_inverted_index():
    """InnoDB FULLTEXT indexes give Aurora MySQL an inverted index (#165)."""
    from src.agents.referee.capability_registry import can_engine_serve_capability

    assert can_engine_serve_capability("aurora_mysql", ["inverted_index"])


def test_tiny_opensearch_absorbed_end_to_end():
    """WordPress shape: 3 text-search queries leave OpenSearch for Aurora MySQL (#165, #168)."""
    join_ids = [f"aq{i}" for i in range(7)]
    os_ids = ["os1", "os2", "os3"]
    assignment = {
        "version": 1,
        "query_assignments": [
            {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
            for i in range(20)
        ]
        + [
            {"query_id": qid, "assigned_engine": "aurora_mysql", "assignment_reason": "t"}
            for qid in join_ids
        ]
        + [
            {
                "query_id": qid,
                "assigned_engine": "opensearch",
                "assignment_reason": "signal override: text_search → opensearch",
                "signal_override": "text_search",
            }
            for qid in os_ids
        ],
    }
    triage = {
        "signals": [
            {"signal": "complex_joins", "targets": ["aurora_mysql"], "query_ids": join_ids},
            {"signal": "text_search", "targets": ["opensearch"], "query_ids": os_ids},
        ],
        "query_capabilities": {qid: ["inverted_index"] for qid in os_ids},
    }
    collector = {
        "queries": {
            "query_patterns": [
                {"query_id": f"dq{i}", "tables_accessed": ["db.options"]} for i in range(20)
            ]
            + [{"query_id": qid, "tables_accessed": ["db.posts"]} for qid in join_ids]
            + [{"query_id": qid, "tables_accessed": ["db.users"]} for qid in os_ids]
        }
    }
    analysis = {
        "dynamodb": {
            "table_recommendations": [
                {"table_id": "db.options", "confidence_score": 90},
                {"table_id": "db.posts", "confidence_score": 40},
                {"table_id": "db.users", "confidence_score": 85},
            ]
        },
        "aurora_mysql": {
            "table_recommendations": [
                {"table_id": "db.options", "confidence_score": 60},
                {"table_id": "db.posts", "confidence_score": 80},
                {"table_id": "db.users", "confidence_score": 80},
            ]
        },
        "opensearch": {"table_recommendations": [{"table_id": "db.users", "confidence_score": 60}]},
    }

    result = run_reality_check(assignment, triage, analysis, collector)

    engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
    assert {engine_of[qid] for qid in os_ids} == {"aurora_mysql"}
    from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
    assert len(from_os) == 1
    assert from_os[0]["to_engine"] == "aurora_mysql"
    assert from_os[0]["action"] == "full"
    assert from_os[0]["query_count"] == 3
    assert "text_search_basic" in from_os[0]["reason"]


class TestOpenSearchJustificationFloor:
    """OpenSearch is only kept as a standing engine when it earns its place (#326)."""

    def _run(self, os_query_text: str, os_cps: float, source_engine: str | None = None):
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(20)
            ]
            + [
                {
                    "query_id": "search",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "signal override: text_search → opensearch",
                    "signal_override": "text_search",
                }
            ],
        }
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "opensearch"}],
            "signals": [
                {"signal": "text_search", "targets": ["opensearch"], "query_ids": ["search"]}
            ],
        }
        collector: dict = {
            "queries": {
                "query_patterns": [
                    {"query_id": f"dq{i}", "tables_accessed": ["db.posts"], "query_type": "SELECT"}
                    for i in range(20)
                ]
                + [
                    {
                        "query_id": "search",
                        "tables_accessed": ["db.users"],
                        "query_type": "SELECT",
                        "query_text": os_query_text,
                        "calls_per_second": os_cps,
                    }
                ]
            }
        }
        if source_engine:
            collector["metadata"] = {"source_database": {"engine": source_engine}}
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.posts", "confidence_score": 80}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 60}]
            },
        }
        return run_reality_check(assignment, triage, analysis, collector)

    def test_like_prefix_query_at_low_traffic_is_dropped(self):
        """The wordpress shape: LIKE admin search, 4 queries at well under 1 call/s."""
        result = self._run("SELECT * FROM wp_users WHERE wp_usermeta.meta_value LIKE ?", 0.07)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "dynamodb"
        from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
        assert len(from_os) == 1
        assert "justification floor" in from_os[0]["reason"] or "326" in from_os[0]["reason"]

    def test_plain_join_with_no_search_predicate_is_dropped(self):
        """The Action Scheduler shape: a join with no text predicate at all."""
        result = self._run(
            "SELECT a.action_id FROM actions a LEFT JOIN groups g ON g.id = a.group_id", 0.45
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "dynamodb"

    def test_real_depth_at_sufficient_traffic_is_kept(self):
        """MATCH...AGAINST at traffic above the floor, with no native source engine, is justified."""
        result = self._run("SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)", 5.0)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "opensearch"
        assert not any(c["from_engine"] == "opensearch" for c in result["consolidations"])

    def test_native_postgres_tsvector_at_low_traffic_prefers_aurora(self):
        """discourse shape: to_tsvector construction, native to Postgres, at tiny traffic."""
        result = self._run("SELECT to_tsvector($1, $2)", 0.03, source_engine="postgresql")
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "dynamodb"

    def test_real_depth_insufficient_traffic_for_native_source_is_dropped(self):
        """A tsvector/MATCH query from a native source needs much higher traffic to justify
        OpenSearch over the source engine's own text_search_basic."""
        result = self._run(
            "SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)", 5.0, source_engine="mysql"
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "dynamodb"


class TestOpenSearchJustificationFunction:
    """Direct tests of opensearch_justification() (#326). It now returns a
    per-query {query_id: (justified, reason)} dict (review finding B1)."""

    def test_no_depth_fails(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE name LIKE ?", "calls_per_second": 10.0}
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_depth_but_low_traffic_fails(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 0.01,
            }
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "calls/s" in reason

    def test_depth_and_traffic_passes(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True


class TestOpenSearchPerQueryDepth:
    """Search depth is decided per query, not for the whole domain by one
    deep query (review finding 4, and finding B1: a shallow query must not
    sink a deep one, and the floor is judged only across the deep ones)."""

    def test_one_deep_query_does_not_unlock_a_shallow_one(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "deep"}, {"query_id": "shallow"}]
        query_map = {
            "deep": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 5.0,
            },
            "shallow": {
                "query_text": "SELECT * FROM t WHERE name LIKE ?",
                "calls_per_second": 5.0,
            },
        }
        verdicts = opensearch_justification(qas, query_map, None)
        assert verdicts["shallow"][0] is False
        assert verdicts["deep"][0] is True

    def test_shallow_key_lookup_does_not_sink_a_real_search_at_high_traffic(self):
        """Review finding B1: a key lookup sitting next to a real `@@ plainto_tsquery`
        search at 50 calls/s must not drop the search too -- only the lookup moves."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "search"}, {"query_id": "lookup"}]
        query_map = {
            "search": {
                "query_text": "SELECT * FROM posts WHERE body_tsv @@ plainto_tsquery($1)",
                "calls_per_second": 50.0,
            },
            "lookup": {
                "query_text": "SELECT * FROM posts WHERE id = $1",
                "calls_per_second": 0.5,
            },
        }
        verdicts = opensearch_justification(qas, query_map, None)
        assert verdicts["lookup"][0] is False
        assert verdicts["search"][0] is True

    def test_bare_group_by_is_not_a_facet(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT status, COUNT(*) FROM orders GROUP BY status",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_search_predicate_with_group_by_in_same_query_is_a_facet(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": (
                    "SELECT category, COUNT(*) FROM products WHERE name LIKE ? "
                    "AND to_tsvector(name) @@ to_tsquery(?) GROUP BY category"
                ),
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_create_extension_is_not_search(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "CREATE EXTENSION IF NOT EXISTS pg_trgm",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_tsvector_document_construction_is_not_search(self):
        """A plain to_tsvector() call with no @@ match is building a document, not searching."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {"q1": {"query_text": "SELECT to_tsvector($1, $2)", "calls_per_second": 20.0}}
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_large_corpus_justifies_below_the_traffic_floor(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 0.1,
                "rows_returned_avg": 5000,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_small_corpus_and_low_traffic_fails(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 0.1,
                "rows_returned_avg": 10,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False


class TestOpenSearchFuzzyMatchRegex:
    """Review finding B1: _FUZZY_MATCH_RE was inverted -- it matched a LIKE
    pattern's own wildcard and missed the real pg_trgm "%" operator."""

    def test_like_prefix_wildcard_is_not_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE name LIKE 'abc%'", "calls_per_second": 50.0}
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_like_both_sided_wildcard_is_not_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE name LIKE '%abc%'",
                "calls_per_second": 50.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_modulo_on_a_parameter_is_not_fuzzy_matching_inside_a_literal(self):
        """id % $2 used to match the old regex purely because of the literal check
        order; confirm a literal containing a percent sign is not mistaken for it."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE tag = 'sale%'", "calls_per_second": 50.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_real_trigram_operator_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE title % $1", "calls_per_second": 5.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_real_trigram_operator_against_a_literal_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE title % 'search term'",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_similarity_function_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE similarity(title, $1) > 0.3",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_word_similarity_function_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE word_similarity(title, $1) > 0.3",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_modulo_against_a_parameter_followed_by_a_comparison_is_not_fuzzy_matching(self):
        """A second review of #375: `id % $2 = 0` is modulo arithmetic, not the
        pg_trgm similarity operator -- the real operator is a boolean predicate
        on its own, never immediately followed by a comparison operator."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE id % $2 = 0", "calls_per_second": 50.0}
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_modulo_against_a_parameter_followed_by_arithmetic_is_not_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE id % $2 + 1 = 0", "calls_per_second": 50.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_pg_trgm_word_similarity_right_threshold_operator_is_fuzzy_matching(self):
        """The pg_trgm `%>` ("right word is similar enough") operator."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE title %> $1", "calls_per_second": 5.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_pg_trgm_word_similarity_left_threshold_operator_is_fuzzy_matching(self):
        """The pg_trgm `<%` ("left word is similar enough") operator."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE $1 <% title", "calls_per_second": 5.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True


class TestUtilityStatementsSurviveRealityCheck:
    """The #327 utility pin must survive the reality check, not just the
    assignment resolver (review finding 1): without "sql_admin", Aurora looked
    redundant by fit score and every utility statement moved to DynamoDB in v2."""

    def test_utility_statements_stay_on_aurora_even_when_aurora_looks_redundant(self):
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(20)
            ]
            + [
                {"query_id": f"aq{i}", "assigned_engine": "aurora_mysql", "assignment_reason": "t"}
                for i in range(3)
            ]
            + [
                {
                    "query_id": "show1",
                    "assigned_engine": "aurora_mysql",
                    "assignment_reason": "utility/metadata statement",
                },
                {
                    "query_id": "set1",
                    "assigned_engine": "aurora_mysql",
                    "assignment_reason": "utility/metadata statement",
                },
            ],
        }
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "aurora_mysql"}],
            "signals": [],
            "query_capabilities": {"show1": ["sql_admin"], "set1": ["sql_admin"]},
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": f"dq{i}", "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for i in range(20)
                ]
                + [
                    {"query_id": f"aq{i}", "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for i in range(3)
                ]
                + [
                    {
                        "query_id": "show1",
                        "tables_accessed": ["db.options"],
                        "query_type": "OTHER",
                        "query_text": "SHOW FULL FIELDS FROM wp_options",
                    },
                    {
                        "query_id": "set1",
                        "tables_accessed": ["db.options"],
                        "query_type": "OTHER",
                        "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
                    },
                ]
            }
        }
        # aurora_mysql scores identically to dynamodb on db.users (redundant by
        # fit score, no hard capability involved) but much worse on db.options
        # -- the utility queries' table -- so without the sql_admin gate the
        # reality check would judge aurora_mysql fully redundant.
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 80},
                    {"table_id": "db.options", "confidence_score": 80},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 80},
                    {"table_id": "db.options", "confidence_score": 10},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["show1"] == "aurora_mysql"
        assert engine_of["set1"] == "aurora_mysql"
