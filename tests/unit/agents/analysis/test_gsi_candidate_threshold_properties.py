"""
Property 5: GSI candidate threshold and ordering

For any collector output, detect_gsi_candidates shall return only composite
column groups whose combined query frequency exceeds 100 calls per hour and
whose columns are not part of the table's primary key. Each candidate's
partition_key_columns shall contain at most 4 columns and sort_key_columns
at most 4 columns (per DynamoDB multi-key GSI limits). The returned list
shall be sorted by total_frequency_per_hour in descending order.

Feature: enhanced-dynamodb-analysis, Property 5: GSI candidate threshold and ordering
Validates: Requirements 3.1, 3.2
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.tools.analysis.dynamodb_analysis_tools import classify_primary_keys, detect_gsi_candidates

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,10}", fullmatch=True)


@st.composite
def _collector_with_queries(draw: st.DrawFn) -> dict:
    """Generate a collector output with 1-2 tables and queries that have
    filter_columns and sort_columns with varying frequencies.

    Some queries will be above the 100 calls/hour threshold, some below.
    """
    tid = draw(_table_id)
    pk_col = draw(_column_name)

    # Generate 2-6 non-PK columns for filter/sort usage
    non_pk_cols = draw(
        st.lists(_column_name, min_size=2, max_size=6, unique=True).filter(
            lambda cols: pk_col not in cols
        )
    )

    table = {
        "table_id": tid,
        "table_name": tid.split(".")[-1],
        "row_count": draw(st.integers(min_value=100, max_value=1_000_000)),
        "size_mb": 1.0,
        "columns": [
            {"column_name": pk_col, "ordinal_position": 1, "data_type": "int", "nullable": False},
        ]
        + [
            {
                "column_name": c,
                "ordinal_position": i + 2,
                "data_type": "varchar",
                "nullable": draw(st.booleans()),
            }
            for i, c in enumerate(non_pk_cols)
        ],
        "primary_key": [pk_col],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": [pk_col],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
    }

    # Generate queries with varying frequencies
    num_queries = draw(st.integers(min_value=1, max_value=6))
    queries = []
    for qi in range(num_queries):
        # Pick 1-5 filter columns from non-PK columns
        num_filters = draw(st.integers(min_value=1, max_value=min(5, len(non_pk_cols))))
        filter_cols = draw(
            st.lists(
                st.sampled_from(non_pk_cols),
                min_size=num_filters,
                max_size=num_filters,
                unique=True,
            )
        )
        # Optionally pick sort columns
        remaining = [c for c in non_pk_cols if c not in filter_cols]
        num_sorts = draw(st.integers(min_value=0, max_value=min(5, len(remaining))))
        sort_cols = (
            draw(
                st.lists(
                    st.sampled_from(remaining) if remaining else st.nothing(),
                    min_size=num_sorts,
                    max_size=num_sorts,
                    unique=True,
                )
            )
            if remaining and num_sorts > 0
            else []
        )

        # Frequency: mix of above and below threshold
        freq = draw(st.floats(min_value=1.0, max_value=500.0))

        queries.append(
            {
                "query_id": f"q-{qi}",
                "query_text": "SELECT * FROM t WHERE ...",
                "query_type": "SELECT",
                "frequency_per_hour": freq,
                "calls_per_second": freq / 3600,
                "tables_accessed": [tid],
                "rows_returned_avg": 10.0,
                "filter_columns": filter_cols,
                "sort_columns": sort_cols,
            }
        )

    return {
        "database_schema": {"tables": [table]},
        "queries": {"query_patterns": queries},
    }


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestGsiCandidateThresholdAndOrdering:
    """Property 5: GSI candidate threshold and ordering."""

    @given(data=_collector_with_queries())
    @settings(max_examples=100)
    def test_all_candidates_exceed_threshold(self, data: dict):
        """Every returned GSI candidate has total_frequency_per_hour > 100."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert c.total_frequency_per_hour > 100, (
                f"Candidate for {c.table_id} with columns {c.partition_key_columns} "
                f"has frequency {c.total_frequency_per_hour} which is <= 100"
            )

    @given(data=_collector_with_queries())
    @settings(max_examples=100)
    def test_candidates_sorted_by_frequency_descending(self, data: dict):
        """The returned list is sorted by total_frequency_per_hour descending."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for i in range(len(candidates) - 1):
            assert (
                candidates[i].total_frequency_per_hour >= candidates[i + 1].total_frequency_per_hour
            ), (
                f"Candidate at index {i} ({candidates[i].total_frequency_per_hour}) "
                f"should be >= candidate at index {i + 1} ({candidates[i + 1].total_frequency_per_hour})"
            )

    @given(data=_collector_with_queries())
    @settings(max_examples=100)
    def test_partition_key_columns_capped_at_4(self, data: dict):
        """Each candidate's partition_key_columns has at most 4 columns (DynamoDB GSI limit)."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert len(c.partition_key_columns) <= 4, (
                f"Candidate for {c.table_id} has {len(c.partition_key_columns)} "
                f"partition key columns, expected 1-4"
            )

    @given(data=_collector_with_queries())
    @settings(max_examples=100)
    def test_sort_key_columns_capped_at_4(self, data: dict):
        """Each candidate's sort_key_columns has at most 4 columns (DynamoDB GSI limit)."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert len(c.sort_key_columns) <= 4, (
                f"Candidate for {c.table_id} has {len(c.sort_key_columns)} "
                f"sort key columns, expected 0-4"
            )

    @given(data=_collector_with_queries())
    @settings(max_examples=100)
    def test_no_pk_columns_in_candidates(self, data: dict):
        """No candidate's partition_key_columns or sort_key_columns contain PK columns."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            pk = pk_cls.get(c.table_id)
            if pk:
                pk_set = {col.lower() for col in pk.pk_columns}
                for col in c.partition_key_columns:
                    assert (
                        col.lower() not in pk_set
                    ), f"PK column '{col}' found in partition_key_columns for {c.table_id}"
                for col in c.sort_key_columns:
                    assert (
                        col.lower() not in pk_set
                    ), f"PK column '{col}' found in sort_key_columns for {c.table_id}"

    def test_below_threshold_returns_empty(self):
        """Queries with frequency <= 100 calls/hour produce no candidates."""
        data = {
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.orders",
                        "table_name": "orders",
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
                                "nullable": False,
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
                        "query_id": "q-1",
                        "query_type": "SELECT",
                        "frequency_per_hour": 50,
                        "tables_accessed": ["app.orders"],
                        "filter_columns": ["status"],
                    }
                ]
            },
        }
        pk_cls = classify_primary_keys(data)
        assert detect_gsi_candidates(data, pk_cls) == []

    def test_empty_input_returns_empty(self):
        """Empty collector output returns empty list."""
        empty = {"database_schema": {"tables": []}, "queries": {"query_patterns": []}}
        assert detect_gsi_candidates(empty, {}) == []
        assert detect_gsi_candidates({}, {}) == []
