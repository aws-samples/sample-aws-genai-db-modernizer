"""Unit tests for the reality check agent core logic."""


from src.agents.referee.reality_check import (
    BASIC_CRUD_SCORE,
    SIGNAL_MATCH_BONUS,
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


def _make_collector(query_ids: list[str]) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": qid,
                    "tables_accessed": ["db.users"],
                    "query_type": "SELECT",
                }
                for qid in query_ids
            ]
        }
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
        collector = _make_collector(["q1", "q2", "q3"])
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
        collector = _make_collector(["q1", "q2", "q3"])
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
        collector = _make_collector(["q1", "q2", "q3"])
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
        collector = _make_collector(["q1", "q2"])

        result = run_reality_check(assignment, triage, {}, collector)
        # q2 should still be in opensearch (mandatory)
        q2_engine = next(
            qa["assigned_engine"] for qa in result["revised_assignments"] if qa["query_id"] == "q2"
        )
        assert q2_engine == "opensearch"


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

    def test_mandatory_engine_never_absorbed(self):
        """OpenSearch with mandatory signal override is protected regardless of count."""
        engine_queries = _make_engine_queries(
            {
                "aurora_postgresql": [f"aq{i}" for i in range(30)],
                "opensearch": ["os1", "os2", "os3"],
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
        query_map = {
            "os1": {"tables_accessed": ["db.posts"]},
            "os2": {"tables_accessed": ["db.posts"]},
            "os3": {"tables_accessed": ["db.posts"]},
        }
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
