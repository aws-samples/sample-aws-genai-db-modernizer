"""Unit tests for assignment resolver signal overrides and anti-pattern penalties."""

from src.agents.referee.assignment_resolver import AssignmentResolver, derive_table_assignments
from src.contracts.assignment_models import QueryAssignment


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
                    "has_aggregations": False,
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
        # leaderboard_pattern is a cache-overlay hint, never ownership (#296): the
        # cache owns nothing, so q2 goes to its highest-confidence owner
        assert q2.assigned_engine == "dynamodb"
        assert q2.signal_override is None
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

    def test_signal_override_recorded_as_structured_field(self):
        """Mandatory status travels as data, not as reason text (#151)."""
        resolver = AssignmentResolver()
        triage = _make_triage(
            ["dynamodb", "opensearch"],
            signals=[{"signal": "text_search", "targets": ["opensearch"], "query_ids": ["q1"]}],
        )
        collector = _make_collector(["q1", "q2"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=90),
            "opensearch": _make_analysis("opensearch", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        by_id = {qa.query_id: qa for qa in result.query_assignments}
        assert by_id["q1"].signal_override == "text_search"
        assert by_id["q2"].signal_override is None

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


def _qa(query_id: str, engine: str, tables: list[str], confidence: int = 50) -> QueryAssignment:
    return QueryAssignment(
        query_id=query_id,
        assigned_engine=engine,
        confidence=confidence,
        source_tables=tables,
        assignment_reason="test",
    )


class TestTableAssignmentPrimaryEngine:
    """#317: a table's primary_engine must never be a read-model/cache engine."""

    def test_primary_engine_skips_opensearch_for_a_durable_owner(self):
        # opensearch has the most queries, but aurora_mysql also owns one —
        # aurora_mysql must win, not the engine with the raw highest count.
        query_assignments = [
            _qa("q1", "opensearch", ["db.users"]),
            _qa("q2", "opensearch", ["db.users"]),
            _qa("q3", "opensearch", ["db.users"]),
            _qa("q4", "aurora_mysql", ["db.users"]),
        ]
        tables = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.users")
        assert table.primary_engine == "aurora_mysql"
        assert table.engines == ["aurora_mysql", "opensearch"]

    def test_primary_engine_skips_elasticache_for_a_durable_owner(self):
        query_assignments = [
            _qa("q1", "elasticache", ["db.sessions"]),
            _qa("q2", "elasticache", ["db.sessions"]),
            _qa("q3", "dynamodb", ["db.sessions"]),
        ]
        tables = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.sessions")
        assert table.primary_engine == "dynamodb"

    def test_primary_engine_falls_back_to_retained_engine_when_no_owner(self):
        # Every query on this table went to opensearch: no durable owner among
        # the table's engines, so primary_engine falls back to the retained
        # source-compatible engine rather than staying on opensearch.
        query_assignments = [
            _qa("q1", "opensearch", ["db.search_log"]),
            _qa("q2", "opensearch", ["db.search_log"]),
        ]
        tables = derive_table_assignments(query_assignments, retained_engine="aurora_postgresql")
        table = next(t for t in tables if t.table_id == "db.search_log")
        assert table.primary_engine == "aurora_postgresql"
        assert table.engines == ["opensearch"]

    def test_primary_engine_without_retained_engine_never_falls_back_to_non_owner(self):
        # #317 finding 3: no retained_engine (e.g. enforce_exclusions_on_overrides,
        # or an unrecognized source engine) and no durable owner anywhere in the
        # whole assignment either: the generic "aurora" placeholder wins, never
        # opensearch — OpenSearch is a read model only.
        query_assignments = [
            _qa("q1", "opensearch", ["db.search_log"]),
        ]
        tables = derive_table_assignments(query_assignments)
        table = next(t for t in tables if t.table_id == "db.search_log")
        assert table.primary_engine == "aurora"

    def test_primary_engine_without_retained_engine_falls_back_to_global_busiest_owner(self):
        # No retained_engine, but another table in the same assignment is
        # owned by dynamodb: that busiest owner engine wins over the generic
        # "aurora" placeholder, and still never opensearch.
        query_assignments = [
            _qa("q1", "opensearch", ["db.search_log"]),
            _qa("q2", "dynamodb", ["db.orders"]),
            _qa("q3", "dynamodb", ["db.orders"]),
        ]
        tables = derive_table_assignments(query_assignments)
        table = next(t for t in tables if t.table_id == "db.search_log")
        assert table.primary_engine == "dynamodb"

    def test_primary_engine_without_retained_engine_prefers_global_aurora_engine(self):
        # No retained_engine, but an Aurora engine was used elsewhere in the
        # assignment: it wins over a busier non-Aurora owner, since Aurora is
        # the retained relational core.
        query_assignments = [
            _qa("q1", "opensearch", ["db.search_log"]),
            _qa("q2", "dynamodb", ["db.orders"]),
            _qa("q3", "dynamodb", ["db.orders"]),
            _qa("q4", "aurora_mysql", ["db.accounts"]),
        ]
        tables = derive_table_assignments(query_assignments)
        table = next(t for t in tables if t.table_id == "db.search_log")
        assert table.primary_engine == "aurora_mysql"

    def test_primary_engine_unaffected_when_already_a_durable_owner(self):
        # A table already correctly owned by a durable engine keeps the same
        # primary_engine the old highest-count logic would have picked.
        query_assignments = [
            _qa("q1", "dynamodb", ["db.orders"]),
            _qa("q2", "dynamodb", ["db.orders"]),
            _qa("q3", "aurora_mysql", ["db.orders"]),
        ]
        tables = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "dynamodb"


class TestPrimaryEngineTieBreak:
    """#317 finding 2: an exact tie between owner engines must not depend on
    dict/insertion order — it goes to the retained engine, then engine name."""

    def test_tie_goes_to_retained_engine_regardless_of_insertion_order(self):
        # dynamodb and aurora_mysql are tied at one query each; aurora_mysql
        # is the retained engine, so it must win even though dynamodb's query
        # was inserted first.
        query_assignments = [
            _qa("q1", "dynamodb", ["db.orders"]),
            _qa("q2", "aurora_mysql", ["db.orders"]),
        ]
        tables = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "aurora_mysql"

    def test_tie_goes_to_retained_engine_with_reversed_insertion_order(self):
        # Same tie, opposite insertion order: the result must not flip.
        query_assignments = [
            _qa("q1", "aurora_mysql", ["db.orders"]),
            _qa("q2", "dynamodb", ["db.orders"]),
        ]
        tables = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "aurora_mysql"

    def test_tie_with_no_retained_engine_breaks_by_name(self):
        # No retained_engine: the tie-break falls through to alphabetical
        # order, so the result is deterministic either way.
        query_assignments = [
            _qa("q1", "dynamodb", ["db.orders"]),
            _qa("q2", "documentdb", ["db.orders"]),
        ]
        tables = derive_table_assignments(query_assignments)
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "documentdb"  # "documentdb" < "dynamodb"

    def test_tie_with_no_retained_engine_breaks_by_name_reversed_insertion(self):
        query_assignments = [
            _qa("q1", "documentdb", ["db.orders"]),
            _qa("q2", "dynamodb", ["db.orders"]),
        ]
        tables = derive_table_assignments(query_assignments)
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "documentdb"

    def test_three_way_tie_prefers_retained_then_name(self):
        query_assignments = [
            _qa("q1", "dynamodb", ["db.orders"]),
            _qa("q2", "documentdb", ["db.orders"]),
            _qa("q3", "aurora_mysql", ["db.orders"]),
        ]
        tables = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "aurora_mysql"
