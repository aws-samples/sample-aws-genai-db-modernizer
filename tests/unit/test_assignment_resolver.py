"""Unit tests for assignment resolver signal overrides and anti-pattern penalties."""

from src.agents.referee.assignment_resolver import AssignmentResolver


def _make_collector(query_ids: list[str], tables: list[str] | None = None) -> dict:
    """Build minimal collector output."""
    if tables is None:
        tables = ["db.users"]
    return {
        "job_id": "test",
        "database_schema": {
            "tables": [{"table_id": t, "table_name": t.split(".")[-1]} for t in tables]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": qid,
                    "query_text": "SELECT ...",
                    "query_type": "SELECT",
                    "tables_accessed": tables,
                    "join_count": 0,
                    "has_joins": False,
                    "has_aggregation": False,
                    "filter_tables": [],
                    "calls_per_second": 1.0,
                    "rows_returned_avg": 10,
                }
                for qid in query_ids
            ]
        },
    }


def _make_triage(engines: list[str], signals: list[dict] | None = None) -> dict:
    return {
        "selected_agents": [{"agent_type": e} for e in engines],
        "signals": signals or [],
    }


def _make_analysis(engine: str, table_ids: list[str], confidence: int = 70) -> dict:
    return {
        "table_recommendations": [
            {"table_id": t, "confidence_score": confidence} for t in table_ids
        ],
        "workload_analysis": {
            "patterns_detected": [],
            "anti_patterns_detected": [],
        },
    }


class TestSignalOverrides:
    """Test that triage signals force queries to specific engines."""

    def test_text_search_overrides_to_opensearch(self):
        """Queries with text_search signal should go to opensearch."""
        resolver = AssignmentResolver()
        triage = _make_triage(
            ["dynamodb", "opensearch"],
            signals=[
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q1"],
                    "evidence": "LIKE query",
                }
            ],
        )
        collector = _make_collector(["q1", "q2"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=90),
            "opensearch": _make_analysis("opensearch", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "opensearch"
        assert "signal override" in q1.assignment_reason

    def test_signal_override_ignored_when_engine_not_selected(self):
        """Signal override should not apply if the target engine wasn't selected."""
        resolver = AssignmentResolver()
        triage = _make_triage(
            ["dynamodb"],  # opensearch NOT selected
            signals=[
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q1"],
                    "evidence": "LIKE query",
                }
            ],
        )
        collector = _make_collector(["q1"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=80),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "dynamodb"

    def test_multiple_signal_overrides(self):
        """Different signals can override different queries to different engines."""
        resolver = AssignmentResolver()
        triage = _make_triage(
            ["dynamodb", "opensearch", "elasticache"],
            signals=[
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q1"],
                    "evidence": "LIKE query",
                },
                {
                    "signal": "leaderboard_pattern",
                    "targets": ["elasticache"],
                    "query_ids": ["q2"],
                    "evidence": "ORDER BY score LIMIT 10",
                },
            ],
        )
        collector = _make_collector(["q1", "q2", "q3"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=90),
            "opensearch": _make_analysis("opensearch", ["db.users"], confidence=50),
            "elasticache": _make_analysis("elasticache", ["db.users"], confidence=40),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        q2 = next(qa for qa in result.query_assignments if qa.query_id == "q2")
        q3 = next(qa for qa in result.query_assignments if qa.query_id == "q3")
        assert q1.assigned_engine == "opensearch"
        assert q2.assigned_engine == "elasticache"
        assert q3.assigned_engine == "dynamodb"  # no override, highest confidence wins


class TestAntiPatternPenalties:
    """Test that anti-pattern penalties demote engines for specific queries."""

    def test_anti_pattern_reduces_score(self):
        """A text_search anti-pattern should penalize DynamoDB enough to lose."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "opensearch"])
        collector = _make_collector(["q1"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}],
                "workload_analysis": {
                    "patterns_detected": [],
                    "anti_patterns_detected": [
                        {
                            "anti_pattern_type": "text_search",
                            "query_ids": ["q1"],
                            "description": "Full text search not supported",
                        }
                    ],
                },
            },
            "opensearch": _make_analysis("opensearch", ["db.users"], confidence=60),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        # DynamoDB 80 - 40 penalty = 40, OpenSearch 60 wins
        assert q1.assigned_engine == "opensearch"

    def test_cross_engine_pattern_penalty(self):
        """A pattern detected in one engine penalizes other engines."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "opensearch"])
        collector = _make_collector(["q1"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=75),
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}],
                "workload_analysis": {
                    "patterns_detected": [
                        {
                            "pattern_type": "text_search",
                            "query_ids": ["q1"],
                            "table_ids": ["db.users"],
                        }
                    ],
                    "anti_patterns_detected": [],
                },
            },
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        # DynamoDB 75 - 40 (cross-engine penalty) = 35, OpenSearch 70 wins
        assert q1.assigned_engine == "opensearch"

    def test_no_penalty_for_unknown_anti_pattern(self):
        """Unknown anti-pattern types should not penalize."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb"])
        collector = _make_collector(["q1"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}],
                "workload_analysis": {
                    "patterns_detected": [],
                    "anti_patterns_detected": [
                        {
                            "anti_pattern_type": "unknown_type",
                            "query_ids": ["q1"],
                        }
                    ],
                },
            },
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.confidence == 80  # no penalty applied


class TestComputeQueryConfidence:
    """Test the 3-tier confidence lookup."""

    def test_pattern_match_takes_priority(self):
        """Queries found in patterns use pattern table confidence."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb"])
        collector = _make_collector(["q1"], tables=["db.users", "db.posts"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 90},
                    {"table_id": "db.posts", "confidence_score": 30},
                ],
                "workload_analysis": {
                    "patterns_detected": [
                        {
                            "pattern_type": "key_value",
                            "query_ids": ["q1"],
                            "table_ids": ["db.users"],  # only users, not posts
                        }
                    ],
                    "anti_patterns_detected": [],
                },
            },
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        # Should use pattern-matched table (db.users=90), not average of both
        assert q1.confidence == 90

    def test_tables_accessed_fallback(self):
        """Queries not in patterns fall back to tables_accessed lookup."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb"])
        collector = _make_collector(["q1"], tables=["db.users"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 75},
                    {"table_id": "db.posts", "confidence_score": 30},
                ],
                "workload_analysis": {
                    "patterns_detected": [],  # no patterns
                    "anti_patterns_detected": [],
                },
            },
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        # Should use tables_accessed (db.users=75), not global average
        assert q1.confidence == 75


class TestAssignmentReasons:
    """Test that assignment reasons are descriptive."""

    def test_signal_override_reason(self):
        resolver = AssignmentResolver()
        triage = _make_triage(
            ["dynamodb", "opensearch"],
            signals=[{"signal": "text_search", "targets": ["opensearch"], "query_ids": ["q1"]}],
        )
        collector = _make_collector(["q1"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=90),
            "opensearch": _make_analysis("opensearch", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert "signal override: text_search" in q1.assignment_reason

    def test_highest_confidence_reason(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "opensearch"])
        collector = _make_collector(["q1"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=90),
            "opensearch": _make_analysis("opensearch", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert "highest confidence" in q1.assignment_reason
