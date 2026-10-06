"""Unit tests for assignment resolver signal overrides and anti-pattern penalties."""

from src.agents.referee.assignment_resolver import AssignmentResolver, derive_table_assignments
from src.agents.referee.table_resolution import TableNameResolver
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


class TestUtilityStatementsExcludedFromRouting:
    """Utility/metadata statements never route to a target engine (#327)."""

    def _collector_with_metadata(self, query_texts: dict[str, str], source_engine: str) -> dict:
        collector = _make_collector(list(query_texts))
        collector["metadata"] = {"source_database": {"engine": source_engine}}
        for q in collector["queries"]["query_patterns"]:
            q["query_text"] = query_texts[q["query_id"]]
        return collector

    def test_show_statement_pinned_to_aurora_mysql(self):
        """SHOW FULL FIELDS would otherwise score highest for dynamodb (#327)."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        collector = self._collector_with_metadata(
            {"q1": "SHOW FULL FIELDS FROM `wp_options`"}, "mysql"
        )
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=10),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_mysql"
        assert "utility/metadata statement" in q1.assignment_reason

    def test_set_session_pinned_to_aurora_mysql(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        collector = self._collector_with_metadata(
            {"q1": "SET SESSION `SQL_BIG_SELECTS` = ?"}, "mysql"
        )
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=10),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_mysql"

    def test_create_extension_pinned_to_aurora_postgresql_even_with_signal_override(self):
        """CREATE EXTENSION pg_trgm must not win text_search's signal override to opensearch."""
        resolver = AssignmentResolver()
        triage = _make_triage(
            ["dynamodb", "opensearch", "aurora_postgresql"],
            signals=[
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q1"],
                    "evidence": "CREATE EXTENSION pg_trgm",
                }
            ],
        )
        collector = self._collector_with_metadata(
            {"q1": "CREATE EXTENSION IF NOT EXISTS pg_trgm"}, "postgresql"
        )
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=50),
            "opensearch": _make_analysis("opensearch", ["db.users"], confidence=90),
            "aurora_postgresql": _make_analysis("aurora_postgresql", ["db.users"], confidence=10),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_postgresql"
        assert q1.signal_override is None

    def test_ordinary_query_unaffected(self):
        """A normal SELECT keeps the normal confidence-scoring outcome."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        collector = self._collector_with_metadata({"q1": "SELECT * FROM wp_posts"}, "mysql")
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=10),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "dynamodb"

    def test_tableless_catalog_call_pinned_to_aurora_postgresql(self):
        """discourse shape: obj_description($1::regclass::oid, $2), no real source table."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_postgresql"])
        collector = self._collector_with_metadata(
            {"q1": "SELECT obj_description($1::regclass::oid, $2)"}, "postgresql"
        )
        for q in collector["queries"]["query_patterns"]:
            q["tables_accessed"] = ["unknown"]
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_postgresql": _make_analysis("aurora_postgresql", ["db.users"], confidence=10),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_postgresql"
        assert "utility/metadata statement" in q1.assignment_reason

    def test_no_aurora_engine_leaves_scored_assignment(self):
        """With no Aurora candidate, the scored assignment is left in place."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb"])
        collector = self._collector_with_metadata({"q1": "SHOW TABLES"}, "mysql")
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "dynamodb"


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
        tables, _ = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.users")
        assert table.primary_engine == "aurora_mysql"
        assert table.engines == ["aurora_mysql", "opensearch"]

    def test_primary_engine_skips_elasticache_for_a_durable_owner(self):
        query_assignments = [
            _qa("q1", "elasticache", ["db.sessions"]),
            _qa("q2", "elasticache", ["db.sessions"]),
            _qa("q3", "dynamodb", ["db.sessions"]),
        ]
        tables, _ = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
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
        tables, _ = derive_table_assignments(query_assignments, retained_engine="aurora_postgresql")
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
        tables, _ = derive_table_assignments(query_assignments)
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
        tables, _ = derive_table_assignments(query_assignments)
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
        tables, _ = derive_table_assignments(query_assignments)
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
        tables, _ = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
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
        tables, _ = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "aurora_mysql"

    def test_tie_goes_to_retained_engine_with_reversed_insertion_order(self):
        # Same tie, opposite insertion order: the result must not flip.
        query_assignments = [
            _qa("q1", "aurora_mysql", ["db.orders"]),
            _qa("q2", "dynamodb", ["db.orders"]),
        ]
        tables, _ = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "aurora_mysql"

    def test_tie_with_no_retained_engine_breaks_by_name(self):
        # No retained_engine: the tie-break falls through to alphabetical
        # order, so the result is deterministic either way.
        query_assignments = [
            _qa("q1", "dynamodb", ["db.orders"]),
            _qa("q2", "documentdb", ["db.orders"]),
        ]
        tables, _ = derive_table_assignments(query_assignments)
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "documentdb"  # "documentdb" < "dynamodb"

    def test_tie_with_no_retained_engine_breaks_by_name_reversed_insertion(self):
        query_assignments = [
            _qa("q1", "documentdb", ["db.orders"]),
            _qa("q2", "dynamodb", ["db.orders"]),
        ]
        tables, _ = derive_table_assignments(query_assignments)
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "documentdb"

    def test_three_way_tie_prefers_retained_then_name(self):
        query_assignments = [
            _qa("q1", "dynamodb", ["db.orders"]),
            _qa("q2", "documentdb", ["db.orders"]),
            _qa("q3", "aurora_mysql", ["db.orders"]),
        ]
        tables, _ = derive_table_assignments(query_assignments, retained_engine="aurora_mysql")
        table = next(t for t in tables if t.table_id == "db.orders")
        assert table.primary_engine == "aurora_mysql"


class TestTableNameResolver:
    """#316 finding 1: canonical table_id resolution, not exact string match.

    Live and offline collectors spell the same table differently from how
    the SQL parser spells it in tables_accessed/source_tables: a bare name
    (live MySQL), a schema prefix the collector's own table_id does not use
    or vice-versa (live/offline Postgres), different quoting or case.
    """

    def test_exact_table_id_match(self):
        resolver = TableNameResolver.from_collector(
            {"database_schema": {"tables": [{"table_id": "wordpress.wp_posts"}]}}
        )
        assert resolver.resolve("wordpress.wp_posts") == "wordpress.wp_posts"

    def test_bare_name_matches_table_name_field(self):
        # Live MySQL: table_id is schema-qualified, tables_accessed is bare.
        resolver = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [{"table_id": "wordpress.wp_posts", "table_name": "wp_posts"}]
                }
            }
        )
        assert resolver.resolve("wp_posts") == "wordpress.wp_posts"

    def test_schema_qualified_name_normalizes_to_db_qualified_table_id(self):
        # Live Postgres: table_id is public.topics; or offline Postgres keeps
        # a public. prefix the collector's db-qualified table_id does not use.
        resolver = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [{"table_id": "discourse.topics", "table_name": "topics"}]
                }
            }
        )
        assert resolver.resolve("public.topics") == "discourse.topics"
        assert resolver.resolve("topics") == "discourse.topics"

    def test_quoted_and_mixed_case_name_resolves(self):
        resolver = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [{"table_id": "wordpress.wp_users", "table_name": "wp_users"}]
                }
            }
        )
        assert resolver.resolve("`WP_USERS`") == "wordpress.wp_users"
        assert resolver.resolve('"wp_users"') == "wordpress.wp_users"

    def test_view_reference_resolves(self):
        resolver = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [],
                    "views": [{"view_id": "badge_posts", "view_name": "badge_posts"}],
                }
            }
        )
        assert resolver.resolve("badge_posts") == "badge_posts"
        assert resolver.resolve("BADGE_POSTS") == "badge_posts"

    def test_ambiguous_normalized_name_is_left_unresolved(self):
        # Two different tables normalize to the same bare key: neither wins.
        resolver = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [
                        {"table_id": "shop.orders", "table_name": "orders"},
                        {"table_id": "legacy.orders", "table_name": "orders"},
                    ]
                }
            }
        )
        assert resolver.resolve("ORDERS") is None

    def test_unresolvable_name_returns_none(self):
        resolver = TableNameResolver.from_collector(
            {"database_schema": {"tables": [{"table_id": "db.users"}]}}
        )
        assert resolver.resolve("cte_alias") is None

    def test_missing_schema_returns_none(self):
        assert TableNameResolver.from_collector({}) is None

    def test_empty_schema_returns_none(self):
        # A schema present but with no table or view at all is indistinguishable
        # from a missing one: resolving against it would drop every table, so
        # it is treated the same as "no schema" (keep every name).
        assert TableNameResolver.from_collector({"database_schema": {"tables": []}}) is None

    def test_from_known_ids_resolves_by_normalization_only(self):
        # The migration-waves builder only has the flat known-id set, no bare
        # table_name/view_name field -- normalization is still enough to
        # bridge a schema-qualification mismatch.
        resolver = TableNameResolver.from_known_ids({"discourse.topics"})
        assert resolver.resolve("public.topics") == "discourse.topics"
        assert resolver.resolve("cte_alias") is None

    def test_from_known_ids_with_no_ids_returns_none(self):
        assert TableNameResolver.from_known_ids([]) is None


class TestDeriveTableAssignmentsNoiseFiltering:
    """#316: source_tables noise the SQL parser introduces is dropped, not
    treated as a table, and the dropped names are recorded."""

    def test_drops_names_not_in_known_tables(self):
        query_assignments = [
            _qa("q1", "aurora_mysql", ["db.users", "cte_alias", "pg_proc"]),
        ]
        known_tables = TableNameResolver.from_known_ids({"db.users"})
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=known_tables)
        assert [t.table_id for t in tables] == ["db.users"]
        assert unresolved.count == 2
        assert unresolved.names == ["cte_alias", "pg_proc"]

    def test_no_filtering_when_known_tables_is_none(self):
        # Missing collector schema: every name is kept, the pre-#316 behavior.
        query_assignments = [_qa("q1", "aurora_mysql", ["db.users", "cte_alias"])]
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=None)
        assert sorted(t.table_id for t in tables) == ["cte_alias", "db.users"]
        assert unresolved.count == 0
        assert unresolved.names == []

    def test_no_dropped_names_when_everything_resolves(self):
        query_assignments = [_qa("q1", "aurora_mysql", ["db.users"])]
        known_tables = TableNameResolver.from_known_ids({"db.users"})
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=known_tables)
        assert len(tables) == 1
        assert unresolved.count == 0
        assert unresolved.names == []

    def test_dropped_names_are_deduplicated_and_sorted(self):
        query_assignments = [
            _qa("q1", "aurora_mysql", ["zeta_noise", "db.users"]),
            _qa("q2", "dynamodb", ["zeta_noise", "alpha_noise"]),
        ]
        known_tables = TableNameResolver.from_known_ids({"db.users"})
        _, unresolved = derive_table_assignments(query_assignments, known_tables=known_tables)
        assert unresolved.names == ["alpha_noise", "zeta_noise"]
        assert unresolved.count == 2

    def test_pseudo_tables_are_dropped_silently_not_counted_as_unresolved(self):
        # #316 finding 4: DUAL/unknown are parser/dialect artifacts, never a
        # real table -- dropped like noise, but never reported as unresolved.
        query_assignments = [
            _qa("q1", "aurora_mysql", ["db.users", "DUAL", "unknown"]),
        ]
        known_tables = TableNameResolver.from_known_ids({"db.users"})
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=known_tables)
        assert [t.table_id for t in tables] == ["db.users"]
        assert unresolved.count == 0
        assert unresolved.names == []

    def test_pseudo_tables_dropped_even_with_no_known_tables(self):
        query_assignments = [_qa("q1", "aurora_mysql", ["DUAL"])]
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=None)
        assert tables == []
        assert unresolved.count == 0

    def test_view_only_table_is_kept_not_dropped(self):
        # #316 finding 2: a query touching only a view must still produce a
        # table_assignments row under the view's canonical id.
        query_assignments = [_qa("q1", "aurora_mysql", ["badge_posts"])]
        known_tables = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [],
                    "views": [{"view_id": "badge_posts", "view_name": "badge_posts"}],
                }
            }
        )
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=known_tables)
        assert [t.table_id for t in tables] == ["badge_posts"]
        assert unresolved.count == 0

    def test_differently_spelled_names_merge_into_one_canonical_row(self):
        # A bare name in one query and the qualified table_id in another must
        # count toward the SAME table, not two separate ones.
        query_assignments = [
            _qa("q1", "dynamodb", ["wp_posts"]),
            _qa("q2", "dynamodb", ["wordpress.wp_posts"]),
        ]
        known_tables = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [{"table_id": "wordpress.wp_posts", "table_name": "wp_posts"}]
                }
            }
        )
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=known_tables)
        assert [t.table_id for t in tables] == ["wordpress.wp_posts"]
        assert tables[0].query_count == 2
        assert unresolved.count == 0

    def test_live_mysql_naming_bare_name_against_qualified_table_id(self):
        # #316 finding 1 probe: live MySQL's parser emits a bare table name
        # while the collector's table_id is schema-qualified.
        query_assignments = [_qa("q1", "aurora_mysql", ["wp_posts"])]
        known_tables = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": [{"table_id": "wordpress.wp_posts", "table_name": "wp_posts"}]
                }
            }
        )
        tables, unresolved = derive_table_assignments(query_assignments, known_tables=known_tables)
        assert [t.table_id for t in tables] == ["wordpress.wp_posts"]
        assert unresolved.count == 0

    def test_live_postgres_naming_bare_and_schema_qualified_against_db_qualified_table_id(self):
        # #316 finding 1 probe: live Postgres strips the public. prefix (bare
        # `topics`); offline Postgres parsing keeps it (`public.topics`).
        # Both must resolve to the collector's discourse.topics table_id.
        for name in ("topics", "public.topics"):
            query_assignments = [_qa("q1", "aurora_postgresql", [name])]
            known_tables = TableNameResolver.from_collector(
                {
                    "database_schema": {
                        "tables": [{"table_id": "discourse.topics", "table_name": "topics"}]
                    }
                }
            )
            tables, unresolved = derive_table_assignments(
                query_assignments, known_tables=known_tables
            )
            assert [t.table_id for t in tables] == ["discourse.topics"], name
            assert unresolved.count == 0, name


class TestResolveNoiseFiltering:
    """#316 end-to-end through AssignmentResolver.resolve()."""

    def test_resolve_drops_noise_and_records_unresolved_table_names(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["aurora_mysql"])
        collector = _make_collector(["q1"], tables=["db.users"])
        # The query's tables_accessed in _make_collector is set to the same
        # `tables` list it was given, so inject noise directly into the
        # collector's query pattern to simulate a parser mistake.
        collector["queries"]["query_patterns"][0]["tables_accessed"] = ["db.users", "cte_alias"]
        analysis = {"aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=80)}

        result = resolver.resolve(triage, analysis, collector)

        assert [t.table_id for t in result.table_assignments] == ["db.users"]
        assert result.unresolved_table_names.count == 1
        assert result.unresolved_table_names.names == ["cte_alias"]
        # The noise is dropped only from table_assignments; the query's own
        # source_tables (query-level routing/scope) is untouched.
        qa = result.query_assignments[0]
        assert qa.source_tables == ["db.users", "cte_alias"]

    def test_resolve_with_no_noise_has_empty_unresolved_table_names(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["aurora_mysql"])
        collector = _make_collector(["q1"], tables=["db.users"])
        analysis = {"aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=80)}

        result = resolver.resolve(triage, analysis, collector)

        assert result.unresolved_table_names.count == 0
        assert result.unresolved_table_names.names == []

    def test_resolve_handles_live_mysql_bare_table_name(self):
        # End-to-end #316 finding 1 probe: a bare name parsed from a live
        # MySQL query must resolve against the schema-qualified table_id the
        # collector assigned, not get dropped as unresolved noise.
        resolver = AssignmentResolver()
        triage = _make_triage(["aurora_mysql"])
        collector = _make_collector(["q1"], tables=["wordpress.wp_posts"])
        collector["queries"]["query_patterns"][0]["tables_accessed"] = ["wp_posts"]
        analysis = {
            "aurora_mysql": _make_analysis("aurora_mysql", ["wordpress.wp_posts"], confidence=80)
        }

        result = resolver.resolve(triage, analysis, collector)

        assert [t.table_id for t in result.table_assignments] == ["wordpress.wp_posts"]
        assert result.unresolved_table_names.count == 0


class TestHardCapabilityGateAtInitialAssignment:
    """The hard-capability gate runs at v1 too, not just inside the reality
    check (review finding 8): a bare COUNT(*)/FOUND_ROWS()/SQL_CALC_FOUND_ROWS
    query must never win the v1 confidence-scoring path onto DynamoDB."""

    def test_bare_count_star_never_lands_on_dynamodb_at_v1(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        collector = _make_collector(["q1"])
        collector["queries"]["query_patterns"][0][
            "query_text"
        ] = "SELECT COUNT(*) FROM wp_postmeta WHERE meta_key = ?"
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_mysql"

    def test_found_rows_never_lands_on_dynamodb_at_v1(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        collector = _make_collector(["q1"])
        collector["queries"]["query_patterns"][0]["query_text"] = "SELECT FOUND_ROWS()"
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_mysql"

    def test_sql_calc_found_rows_never_lands_on_dynamodb_at_v1(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        collector = _make_collector(["q1"])
        collector["queries"]["query_patterns"][0][
            "query_text"
        ] = "SELECT SQL_CALC_FOUND_ROWS id FROM wp_posts"
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_mysql"

    def test_ordinary_query_is_unaffected(self):
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        collector = _make_collector(["q1"])
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "dynamodb"

    def test_precomputed_query_capabilities_from_triage_are_used(self):
        """When triage already computed query_capabilities, the resolver reuses them
        instead of recomputing from scratch."""
        resolver = AssignmentResolver()
        triage = _make_triage(["dynamodb", "aurora_mysql"])
        triage["query_capabilities"] = {"q1": ["aggregation"]}
        collector = _make_collector(["q1"])
        # query_text looks ordinary -- only the precomputed capability should matter.
        analysis = {
            "dynamodb": _make_analysis("dynamodb", ["db.users"], confidence=95),
            "aurora_mysql": _make_analysis("aurora_mysql", ["db.users"], confidence=50),
        }

        result = resolver.resolve(triage, analysis, collector)
        q1 = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        assert q1.assigned_engine == "aurora_mysql"
