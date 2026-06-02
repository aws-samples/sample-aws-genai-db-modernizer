"""Unit tests for Aurora anti-pattern detection logic.

Covers:
- Lowered thresholds for anti-01 (30 CPS) and anti-02 (20 CPS)
- aurora-anti-05: no-relational-need
- aurora-anti-06: single-access-pattern-table
- aurora-anti-07: high-volume-text-search
"""

from src.tools.analysis.aurora_common_analysis_tools import analyze_aurora_common_use_cases


def _make_collector(tables, queries):
    return {
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


# ==========================================================================
# TestLoweredThresholds
# ==========================================================================


class TestLoweredThresholds:
    def test_pk_lookup_fires_at_35_cps(self):
        """anti-01 fires: single-row PK SELECT at 35 CPS, 0 FKs, 0 joins."""
        tables = [{"table_id": "t1", "foreign_keys": [], "columns": []}]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 1.0,
                "calls_per_second": 35.0,
                "query_text": "SELECT * FROM t1 WHERE id = ?",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-01" in anti_ids

    def test_pk_lookup_does_not_fire_at_25_cps(self):
        """anti-01 does NOT fire: same pattern at 25 CPS (below 30 CPS threshold)."""
        tables = [{"table_id": "t1", "foreign_keys": [], "columns": []}]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 1.0,
                "calls_per_second": 25.0,
                "query_text": "SELECT * FROM t1 WHERE id = ?",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-01" not in anti_ids

    def test_cache_read_fires_at_25_cps(self):
        """anti-02 fires: SELECT on single table, no joins, rows_returned_avg <= 10, at 25 CPS."""
        tables = [{"table_id": "t1", "foreign_keys": [], "columns": []}]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 5.0,
                "calls_per_second": 25.0,
                "query_text": "SELECT value FROM config WHERE key = ?",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-02" in anti_ids


# ==========================================================================
# TestNoRelationalNeed (aurora-anti-05)
# ==========================================================================


class TestNoRelationalNeed:
    def test_fires_for_isolated_crud_table(self):
        """anti-05 fires: table with 0 FKs, all single-table INSERT/SELECT, no joins anywhere."""
        tables = [{"table_id": "t1", "foreign_keys": [], "columns": []}]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 1.0,
                "calls_per_second": 5.0,
                "query_text": "SELECT * FROM t1 WHERE id = ?",
            },
            {
                "query_id": "q2",
                "query_type": "INSERT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 0.0,
                "calls_per_second": 3.0,
                "query_text": "INSERT INTO t1 (id, val) VALUES (?, ?)",
            },
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-05" in anti_ids

    def test_does_not_fire_when_table_has_fks(self):
        """anti-05 does NOT fire: table with 1+ FK."""
        tables = [
            {
                "table_id": "t1",
                "foreign_keys": [{"column": "ref_id", "references": "t2"}],
                "columns": [],
            }
        ]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 1.0,
                "calls_per_second": 5.0,
                "query_text": "SELECT * FROM t1 WHERE id = ?",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-05" not in anti_ids

    def test_does_not_fire_when_table_participates_in_joins(self):
        """anti-05 does NOT fire: 0 FKs but a query accessing t1 has joins."""
        tables = [{"table_id": "t1", "foreign_keys": [], "columns": []}]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1", "t2"],
                "has_joins": True,
                "join_count": 1,
                "rows_returned_avg": 5.0,
                "calls_per_second": 5.0,
                "query_text": "SELECT t1.id FROM t1 JOIN t2 ON t1.id = t2.ref",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-05" not in anti_ids


# ==========================================================================
# TestSingleAccessPatternTable (aurora-anti-06)
# ==========================================================================


class TestSingleAccessPatternTable:
    def test_fires_for_single_query_pk_table(self):
        """anti-06 fires: table with 0 FKs, only 1 query, single-table, no joins."""
        tables = [{"table_id": "t1", "foreign_keys": [], "columns": []}]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 1.0,
                "calls_per_second": 5.0,
                "query_text": "SELECT * FROM t1 WHERE id = ?",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-06" in anti_ids

    def test_does_not_fire_when_three_plus_queries(self):
        """anti-06 does NOT fire: table accessed by 3+ distinct queries."""
        tables = [{"table_id": "t1", "foreign_keys": [], "columns": []}]
        queries = [
            {
                "query_id": f"q{i}",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 1.0,
                "calls_per_second": 5.0,
                "query_text": f"SELECT * FROM t1 WHERE col{i} = ?",  # nosec B608
            }
            for i in range(1, 4)
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-06" not in anti_ids


# ==========================================================================
# TestHighVolumeTextSearch (aurora-anti-07)
# ==========================================================================


class TestHighVolumeTextSearch:
    def test_fires_for_text_heavy_table_with_search(self):
        """anti-07 fires: 4/5 text columns (80%), LIKE '%term%' at 15 CPS."""
        tables = [
            {
                "table_id": "t1",
                "foreign_keys": [],
                "columns": [
                    {"name": "id", "data_type": "integer"},
                    {"name": "title", "data_type": "varchar"},
                    {"name": "body", "data_type": "text"},
                    {"name": "summary", "data_type": "varchar"},
                    {"name": "tags", "data_type": "text"},
                ],
            }
        ]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 20.0,
                "calls_per_second": 15.0,
                "query_text": "SELECT * FROM t1 WHERE body LIKE '%search_term%'",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-07" in anti_ids

    def test_does_not_fire_below_10_cps(self):
        """anti-07 does NOT fire: same text-heavy table but search at 8 CPS."""
        tables = [
            {
                "table_id": "t1",
                "foreign_keys": [],
                "columns": [
                    {"name": "id", "data_type": "integer"},
                    {"name": "title", "data_type": "varchar"},
                    {"name": "body", "data_type": "text"},
                    {"name": "summary", "data_type": "varchar"},
                    {"name": "tags", "data_type": "text"},
                ],
            }
        ]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 20.0,
                "calls_per_second": 8.0,
                "query_text": "SELECT * FROM t1 WHERE body LIKE '%search_term%'",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-07" not in anti_ids

    def test_does_not_fire_when_text_ratio_low(self):
        """anti-07 does NOT fire: only 1/5 text columns (20%), search at 15 CPS."""
        tables = [
            {
                "table_id": "t1",
                "foreign_keys": [],
                "columns": [
                    {"name": "id", "data_type": "integer"},
                    {"name": "amount", "data_type": "numeric"},
                    {"name": "status", "data_type": "integer"},
                    {"name": "created_at", "data_type": "timestamp"},
                    {"name": "notes", "data_type": "text"},
                ],
            }
        ]
        queries = [
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "tables_accessed": ["t1"],
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 20.0,
                "calls_per_second": 15.0,
                "query_text": "SELECT * FROM t1 WHERE notes LIKE '%keyword%'",
            }
        ]
        _, anti_patterns = analyze_aurora_common_use_cases(_make_collector(tables, queries))
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-07" not in anti_ids


# ==========================================================================
# TestAntiPatternExclusionLogic
# ==========================================================================


class TestAntiPatternExclusionLogic:
    """anti-01 and anti-02 should not overlap — clean separation at thresholds."""

    def test_high_cps_pk_lookup_only_triggers_anti_01(self):
        """At 35 CPS with PK lookup: anti-01 fires, anti-02 should NOT."""
        collector = _make_collector(
            tables=[
                {
                    "table_id": "users",
                    "columns": [{"column_name": "id", "data_type": "integer"}],
                    "primary_key": {"columns": ["id"]},
                    "foreign_keys": [],
                    "row_count": 10000,
                    "size_mb": 5.0,
                }
            ],
            queries=[
                {
                    "query_id": "q1",
                    "query_type": "SELECT",
                    "query_text": "SELECT * FROM users WHERE id = $1",
                    "tables_accessed": ["users"],
                    "calls_per_second": 35.0,
                    "has_joins": False,
                    "join_count": 0,
                    "rows_returned_avg": 1.0,
                }
            ],
        )
        _, anti_patterns = analyze_aurora_common_use_cases(collector)
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-01" in anti_ids
        assert "aurora-anti-02" not in anti_ids

    def test_moderate_cps_non_pk_triggers_anti_02(self):
        """At 25 CPS without being a single-row PK lookup: anti-02 fires."""
        collector = _make_collector(
            tables=[
                {
                    "table_id": "config",
                    "columns": [
                        {"column_name": "id", "data_type": "integer"},
                        {"column_name": "key", "data_type": "varchar"},
                    ],
                    "primary_key": {"columns": ["id"]},
                    "foreign_keys": [],
                    "row_count": 50,
                    "size_mb": 0.01,
                }
            ],
            queries=[
                {
                    "query_id": "q1",
                    "query_type": "SELECT",
                    "query_text": "SELECT * FROM config WHERE key = $1",
                    "tables_accessed": ["config"],
                    "calls_per_second": 25.0,
                    "has_joins": False,
                    "join_count": 0,
                    "rows_returned_avg": 3.0,  # not single-row, so not PK lookup
                }
            ],
        )
        _, anti_patterns = analyze_aurora_common_use_cases(collector)
        anti_ids = [ap.anti_pattern_id for ap in anti_patterns]
        assert "aurora-anti-02" in anti_ids
        assert "aurora-anti-01" not in anti_ids
