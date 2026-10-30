# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aurora MySQL and Aurora PostgreSQL credited for the SQL features both have
(catalog PR C, #920; review X4 and item 12).

For a heterogeneous source (SQL Server, Oracle, DB2) ADR-031 Decision 6 compares the two
Aurora engines' totals, so a capability credited to one Aurora engine only decides the
engine on a missing catalog row rather than on capability. MySQL 8.0, which Aurora MySQL
version 3 is compatible with, has window functions, recursive CTEs and LATERAL derived
tables; PostgreSQL has functions and procedures, ``UPDATE ... FROM`` / ``DELETE ...
USING`` and ``string_agg``. Each Aurora engine now reports each of those patterns and
rates it with its own sourced fit score. Where one engine's documentation states more
(PostgreSQL's SEARCH and CYCLE clauses and no default recursion cap; MySQL's
multi-table UPDATE changing several tables), that engine keeps the higher score: the
goal is balance, not equal numbers. The in-code bonus for a shared capability is one
table both engines apply, so it no longer favours whichever engine had the row.
"""

from __future__ import annotations

import pytest

from src.agents.referee.aurora_choice import (
    FEATURE_LABELS,
    PRIMARY_FEATURE_SIGNALS,
    choose_heterogeneous_engine,
    describe_features,
    detect_heterogeneous_features,
)
from src.contracts.analysis_output import ScoreBreakdown
from src.shared.catalog.access_classes import class_engine_scores
from src.shared.catalog.patterns import pattern
from src.tools.analysis.aurora_mysql_analysis_tools import (
    _apply_aurora_mysql_adjustments,
    analyze_aurora_mysql_use_cases,
)
from src.tools.analysis.aurora_pg_analysis_tools import (
    _apply_aurora_pg_adjustments,
    analyze_aurora_pg_use_cases,
)
from src.tools.analysis.scoring import TableProfile

SHARED = (
    "window-function",
    "cte-recursive",
    "lateral-join",
    "stored-routine",
    "multi-table-update",
    "group-concat-pivot",
)


def _collector(sql: str, source: str, query_type: str = "SELECT") -> dict:
    return {
        "metadata": {"source_database": {"engine": source}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "t",
                    "columns": [],
                    "primary_key": ["id"],
                    "foreign_keys": [],
                    "row_count": 1000,
                    "size_mb": 1.0,
                }
            ]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_type": query_type,
                    "query_text": sql,
                    "tables_accessed": ["t"],
                    "calls_per_second": 1.0,
                    "rows_returned_avg": 10.0,
                }
            ]
        },
    }


def _types(analyze, sql: str, source: str, query_type: str = "SELECT") -> set[str]:
    analysis = analyze(_collector(sql, source, query_type))
    return {p.pattern_type for p in analysis.patterns_detected}


BOTH = (analyze_aurora_mysql_use_cases, analyze_aurora_pg_use_cases)

# (pattern, SQL that shows it, source dialect, query type, SQL that does not)
CASES = [
    (
        "window-function",
        "SELECT [ID], ROW_NUMBER() OVER (PARTITION BY [K] ORDER BY [ID]) FROM [T]",
        "sqlserver",
        "SELECT",
        "SELECT [ID] FROM [T] ORDER BY [ID]",
    ),
    (
        "cte-recursive",
        "WITH RECURSIVE tree AS (SELECT id FROM t UNION ALL SELECT t.id FROM t "
        "JOIN tree ON t.parent_id = tree.id) SELECT * FROM tree",
        "postgresql",
        "SELECT",
        "WITH cte AS (SELECT id FROM t) SELECT * FROM cte",
    ),
    (
        "cte-recursive",
        "WITH tree AS (SELECT [ID] FROM [T] UNION ALL SELECT c.[ID] FROM [T] c "
        "JOIN tree p ON c.[ParentID] = p.[ID]) SELECT * FROM tree",
        "sqlserver",
        "SELECT",
        "WITH tree AS (SELECT [ID] FROM [T]) SELECT * FROM tree",
    ),
    (
        "lateral-join",
        "SELECT * FROM t JOIN LATERAL (SELECT * FROM u WHERE u.t_id = t.id LIMIT 3) x ON true",
        "postgresql",
        "SELECT",
        "SELECT * FROM t JOIN u ON u.t_id = t.id",
    ),
    (
        "stored-routine",
        "EXEC [dbo].[usp_PlaceOrder] @CustomerID = @CustomerID",
        "sqlserver",
        "OTHER",
        "SELECT [ExecutedAt] FROM [T]",
    ),
    (
        "multi-table-update",
        "UPDATE t SET x = u.y FROM u WHERE u.id = t.id",
        "postgresql",
        "UPDATE",
        "UPDATE t SET x = (SELECT MAX(y) FROM u WHERE u.id = t.id) WHERE id = 1",
    ),
    (
        "multi-table-update",
        "UPDATE t SET t.[X] = u.[Y] FROM [T] t JOIN [U] u ON u.[ID] = t.[ID]",
        "sqlserver",
        "UPDATE",
        "UPDATE [T] SET [X] = 1 WHERE [ID] = @id",
    ),
    (
        "multi-table-update",
        "DELETE FROM t USING u WHERE u.id = t.id",
        "postgresql",
        "DELETE",
        "DELETE FROM t WHERE id = $1",
    ),
    (
        "group-concat-pivot",
        "SELECT [K], STRING_AGG([Name], ',') FROM [T] GROUP BY [K]",
        "sqlserver",
        "SELECT",
        "SELECT [K], COUNT(*) FROM [T] GROUP BY [K]",
    ),
]


class TestBothAuroraEnginesReportEachSharedCapability:
    @pytest.mark.parametrize("analyze", BOTH, ids=["aurora_mysql", "aurora_postgresql"])
    @pytest.mark.parametrize(
        "pattern_id,sql,source,query_type,negative", CASES, ids=[f"{c[0]}-{c[2]}" for c in CASES]
    )
    def test_positive_and_negative(self, analyze, pattern_id, sql, source, query_type, negative):
        assert pattern_id in _types(analyze, sql, source, query_type), sql
        assert pattern_id not in _types(analyze, negative, source, query_type), negative

    def test_a_postgresql_or_mysql_execute_is_a_prepared_statement_not_a_routine(self):
        for analyze in BOTH:
            assert "stored-routine" not in _types(analyze, "EXECUTE fetch_user(1)", "postgresql")


class TestEachEnginesCreditIsItsOwnSourcedFit:
    @pytest.mark.parametrize("pattern_id", SHARED)
    def test_both_aurora_engines_have_a_fit_with_a_source(self, pattern_id):
        entry = pattern(pattern_id)
        for engine in ("aurora_mysql", "aurora_postgresql"):
            support = entry.engines[engine]
            assert support.kind == "fit" and support.score, (pattern_id, engine)
            assert (
                "MySQL Reference Manual" in support.why or "PostgreSQL documentation" in support.why
            ), (pattern_id, engine, support.why)

    def test_the_documented_strengths_keep_their_extra_credit(self):
        """Not levelled: each engine keeps the edge its documentation states."""
        score = {
            (p, e): pattern(p).engines[e].score
            for p in SHARED
            for e in ("aurora_mysql", "aurora_postgresql")
        }
        assert score["cte-recursive", "aurora_postgresql"] > score["cte-recursive", "aurora_mysql"]
        assert (
            score["window-function", "aurora_postgresql"] > score["window-function", "aurora_mysql"]
        )
        assert score["lateral-join", "aurora_postgresql"] > score["lateral-join", "aurora_mysql"]
        assert (
            score["multi-table-update", "aurora_mysql"]
            > score["multi-table-update", "aurora_postgresql"]
        )
        assert (
            score["stored-routine", "aurora_mysql"] == score["stored-routine", "aurora_postgresql"]
        )
        assert (
            score["group-concat-pivot", "aurora_mysql"]
            == score["group-concat-pivot", "aurora_postgresql"]
        )

    @pytest.mark.parametrize("pattern_id", SHARED)
    def test_the_in_code_bonus_for_a_shared_capability_is_the_same_on_both(self, pattern_id):
        profile = TableProfile(
            table_id="t",
            row_count=1000,
            size_mb=1.0,
            column_count=3,
            has_primary_key=True,
            foreign_key_count=0,
            pattern_types=[pattern_id],
        )
        base = ScoreBreakdown(
            pattern_match_score=50, complexity_score=50, performance_score=50, cost_score=50
        )
        assert _apply_aurora_mysql_adjustments(base, profile) == _apply_aurora_pg_adjustments(
            base, profile
        )


class TestTheAggregationClassCreditsAWindowOnlyWhenTheQueryHasOne:
    def test_no_window_no_window_credit(self):
        scores = class_engine_scores()["aggregation"]
        assert scores["aurora_mysql"] == scores["aurora_postgresql"]

    def test_a_window_takes_each_engines_window_fit(self):
        scores = class_engine_scores(frozenset({"window_function"}))["aggregation"]
        assert (
            scores["aurora_postgresql"]
            == pattern("window-function").engines["aurora_postgresql"].score
        )
        assert scores["aurora_mysql"] == pattern("window-function").engines["aurora_mysql"].score


class TestDecision6RecursiveCtesNoLongerTipTowardPostgresql:
    def test_recursive_ctes_are_not_a_tipping_feature(self):
        assert "recursive_ctes" not in PRIMARY_FEATURE_SIGNALS
        features = detect_heterogeneous_features(
            {
                "database_schema": {"tables": []},
                "queries": {
                    "query_patterns": [
                        {"query_id": "q", "query_text": "WITH RECURSIVE r AS (SELECT 1) SELECT 1"}
                    ]
                },
            },
            "oracle",
        )
        assert "recursive_ctes" not in features

    def test_a_close_call_with_only_a_recursive_cte_goes_to_the_higher_total(self):
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 1000.0, "aurora_postgresql": 980.0},
            features={"recursive_ctes": True},
        )
        assert winner == "aurora_mysql"
        assert trace["deciding_features"] == []

    def test_an_old_trace_naming_recursive_ctes_still_reads(self):
        """A saved assignment from before this change may list the feature."""
        assert "recursive_ctes" in FEATURE_LABELS
        assert describe_features(["recursive_ctes", "sequences"]) == "recursive CTEs, sequences"
