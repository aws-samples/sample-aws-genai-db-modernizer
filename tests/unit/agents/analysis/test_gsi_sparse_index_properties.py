"""
Property 7: Sparse index detection

For any column that appears in query filter_columns and has an estimated
population rate below 0.30, detect_gsi_candidates shall flag it with
is_sparse=True and record the estimated_population_rate.

Feature: enhanced-dynamodb-analysis, Property 7: Sparse index detection
Validates: Requirements 3.5
"""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from src.tools.analysis.dynamodb_analysis_tools import classify_primary_keys, detect_gsi_candidates

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,10}", fullmatch=True)


@st.composite
def _collector_with_sparse_columns(draw: st.DrawFn) -> dict:
    """Generate collector output with columns that have low cardinality
    relative to row_count (population rate < 30%), triggering sparse detection.
    """
    tid = draw(_table_id)
    pk_col = draw(_column_name)
    filter_col = draw(_column_name.filter(lambda c: c != pk_col))

    row_count = draw(st.integers(min_value=1000, max_value=100_000))
    # Cardinality < 30% of row_count → sparse
    max_cardinality = int(row_count * 0.29)
    cardinality = draw(st.integers(min_value=1, max_value=max(1, max_cardinality)))

    table = {
        "table_id": tid,
        "table_name": tid.split(".")[-1],
        "row_count": row_count,
        "size_mb": 1.0,
        "columns": [
            {
                "column_name": pk_col,
                "ordinal_position": 1,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": filter_col,
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": True,
                "cardinality": cardinality,
            },
        ],
        "primary_key": [pk_col],
        "indexes": [],
    }

    # High-frequency query on the sparse column
    freq = draw(st.floats(min_value=101.0, max_value=5000.0))
    queries = [
        {
            "query_id": "q-sparse",
            "query_type": "SELECT",
            "frequency_per_hour": freq,
            "tables_accessed": [tid],
            "filter_columns": [filter_col],
        }
    ]

    return {
        "database_schema": {"tables": [table]},
        "queries": {"query_patterns": queries},
        "_expected_sparse": True,
        "_expected_rate": cardinality / row_count,
    }


@st.composite
def _collector_with_dense_columns(draw: st.DrawFn) -> dict:
    """Generate collector output with columns that have high cardinality
    relative to row_count (population rate >= 30%), so NOT sparse.
    """
    tid = draw(_table_id)
    pk_col = draw(_column_name)
    filter_col = draw(_column_name.filter(lambda c: c != pk_col))

    row_count = draw(st.integers(min_value=1000, max_value=100_000))
    # Cardinality >= 30% of row_count → not sparse
    # Use ceil to avoid truncation producing a value just below 0.30
    min_cardinality = math.ceil(row_count * 0.30)
    cardinality = draw(st.integers(min_value=max(1, min_cardinality), max_value=row_count))

    table = {
        "table_id": tid,
        "table_name": tid.split(".")[-1],
        "row_count": row_count,
        "size_mb": 1.0,
        "columns": [
            {
                "column_name": pk_col,
                "ordinal_position": 1,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": filter_col,
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
                "cardinality": cardinality,
            },
        ],
        "primary_key": [pk_col],
        "indexes": [],
    }

    freq = draw(st.floats(min_value=101.0, max_value=5000.0))
    queries = [
        {
            "query_id": "q-dense",
            "query_type": "SELECT",
            "frequency_per_hour": freq,
            "tables_accessed": [tid],
            "filter_columns": [filter_col],
        }
    ]

    return {
        "database_schema": {"tables": [table]},
        "queries": {"query_patterns": queries},
    }


@st.composite
def _collector_without_cardinality(draw: st.DrawFn) -> dict:
    """Generate collector output where columns have no cardinality stats."""
    tid = draw(_table_id)
    pk_col = draw(_column_name)
    filter_col = draw(_column_name.filter(lambda c: c != pk_col))

    table = {
        "table_id": tid,
        "table_name": tid.split(".")[-1],
        "row_count": draw(st.integers(min_value=100, max_value=100_000)),
        "size_mb": 1.0,
        "columns": [
            {
                "column_name": pk_col,
                "ordinal_position": 1,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": filter_col,
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": True,
                # No cardinality field
            },
        ],
        "primary_key": [pk_col],
        "indexes": [],
    }

    freq = draw(st.floats(min_value=101.0, max_value=5000.0))
    queries = [
        {
            "query_id": "q-no-stats",
            "query_type": "SELECT",
            "frequency_per_hour": freq,
            "tables_accessed": [tid],
            "filter_columns": [filter_col],
        }
    ]

    return {
        "database_schema": {"tables": [table]},
        "queries": {"query_patterns": queries},
    }


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestSparseIndexDetection:
    """Property 7: Sparse index detection."""

    @given(data=_collector_with_sparse_columns())
    @settings(max_examples=100)
    def test_low_population_flagged_sparse(self, data: dict):
        """Columns with population rate < 30% are flagged is_sparse=True."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        assert len(candidates) >= 1, "Expected at least one GSI candidate"
        for c in candidates:
            assert c.is_sparse is True, (  # nosemgrep: is-function-without-parentheses
                f"Candidate for {c.table_id} with low population rate " f"should be flagged sparse"
            )

    @given(data=_collector_with_sparse_columns())
    @settings(max_examples=100)
    def test_sparse_has_population_rate(self, data: dict):
        """Sparse candidates have estimated_population_rate recorded."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            if c.is_sparse:  # nosemgrep: is-function-without-parentheses
                assert c.estimated_population_rate is not None, (
                    f"Sparse candidate for {c.table_id} should have "
                    f"estimated_population_rate set"
                )
                assert 0.0 <= c.estimated_population_rate < 0.30, (
                    f"Sparse candidate population rate "
                    f"{c.estimated_population_rate} should be < 0.30"
                )

    @given(data=_collector_with_dense_columns())
    @settings(max_examples=100)
    def test_high_population_not_flagged_sparse(self, data: dict):
        """Columns with population rate >= 30% are NOT flagged sparse."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert c.is_sparse is False, (  # nosemgrep: is-function-without-parentheses
                f"Candidate for {c.table_id} with high population rate "
                f"({c.estimated_population_rate}) should not be flagged sparse"
            )

    @given(data=_collector_without_cardinality())
    @settings(max_examples=100)
    def test_missing_stats_not_flagged_sparse(self, data: dict):
        """Without cardinality stats, candidates are not flagged sparse
        and estimated_population_rate is None.
        """
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert c.is_sparse is False, (  # nosemgrep: is-function-without-parentheses
                f"Candidate for {c.table_id} without cardinality stats "
                f"should not be flagged sparse"
            )
            assert c.estimated_population_rate is None, (
                f"Candidate for {c.table_id} without cardinality stats "
                f"should have estimated_population_rate=None"
            )

    def test_exact_boundary_30_percent_not_sparse(self):
        """A column with exactly 30% population rate is NOT sparse (< 30% required)."""
        data = {
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.items",
                        "table_name": "items",
                        "row_count": 1000,
                        "size_mb": 1.0,
                        "columns": [
                            {
                                "column_name": "id",
                                "ordinal_position": 1,
                                "data_type": "int",
                                "nullable": False,
                            },
                            {
                                "column_name": "status",
                                "ordinal_position": 2,
                                "data_type": "varchar",
                                "nullable": True,
                                "cardinality": 300,
                            },
                        ],
                        "primary_key": ["id"],
                        "indexes": [],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q-boundary",
                        "query_type": "SELECT",
                        "frequency_per_hour": 200,
                        "tables_accessed": ["app.items"],
                        "filter_columns": ["status"],
                    }
                ]
            },
        }
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        assert len(candidates) == 1
        assert candidates[0].is_sparse is False  # nosemgrep: is-function-without-parentheses
        assert candidates[0].estimated_population_rate == 0.3
