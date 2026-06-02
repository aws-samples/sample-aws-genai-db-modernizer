"""
Property 6: GSI candidate field completeness

For any GSI candidate in the output, the record shall contain a non-empty
partition_key_columns list (1-4 columns), a valid table_id, a positive
total_frequency_per_hour, and a non-empty query_ids list. If the column
group is covered by an existing secondary index on the source database,
existing_index_name shall be non-null.

Feature: enhanced-dynamodb-analysis, Property 6: GSI candidate field completeness
Validates: Requirements 3.3, 3.4
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
def _collector_with_high_freq_queries(draw: st.DrawFn) -> dict:
    """Generate collector output where queries always exceed the 100/hr threshold
    so we get candidates to validate field completeness on.
    """
    tid = draw(_table_id)
    pk_col = draw(_column_name)

    non_pk_cols = draw(
        st.lists(_column_name, min_size=2, max_size=5, unique=True).filter(
            lambda cols: pk_col not in cols
        )
    )

    # Optionally add a secondary index covering some non-PK columns
    add_secondary_idx = draw(st.booleans())
    indexes = [
        {
            "index_name": "PRIMARY",
            "columns": [pk_col],
            "is_unique": True,
            "is_primary": True,
            "index_type": "btree",
        }
    ]
    covered_cols: set[str] = set()
    if add_secondary_idx and len(non_pk_cols) >= 1:
        idx_col_count = draw(st.integers(min_value=1, max_value=min(3, len(non_pk_cols))))
        idx_cols = non_pk_cols[:idx_col_count]
        covered_cols = {c.lower() for c in idx_cols}
        indexes.append(
            {
                "index_name": "idx_secondary",
                "columns": idx_cols,
                "is_unique": False,
                "is_primary": False,
                "index_type": "btree",
            }
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
                "nullable": False,
            }
            for i, c in enumerate(non_pk_cols)
        ],
        "primary_key": [pk_col],
        "indexes": indexes,
    }

    # Generate high-frequency queries (> 100/hr each) to guarantee candidates
    num_queries = draw(st.integers(min_value=1, max_value=4))
    queries = []
    for qi in range(num_queries):
        num_filters = draw(st.integers(min_value=1, max_value=min(4, len(non_pk_cols))))
        filter_cols = draw(
            st.lists(
                st.sampled_from(non_pk_cols),
                min_size=num_filters,
                max_size=num_filters,
                unique=True,
            )
        )
        remaining = [c for c in non_pk_cols if c not in filter_cols]
        num_sorts = draw(st.integers(min_value=0, max_value=min(3, len(remaining))))
        sort_cols = (
            draw(
                st.lists(
                    st.sampled_from(remaining),
                    min_size=num_sorts,
                    max_size=num_sorts,
                    unique=True,
                )
            )
            if remaining and num_sorts > 0
            else []
        )

        # Always above threshold
        freq = draw(st.floats(min_value=101.0, max_value=5000.0))
        queries.append(
            {
                "query_id": f"q-{qi}",
                "query_type": "SELECT",
                "frequency_per_hour": freq,
                "calls_per_second": freq / 3600,
                "tables_accessed": [tid],
                "filter_columns": filter_cols,
                "sort_columns": sort_cols,
            }
        )

    return {
        "database_schema": {"tables": [table]},
        "queries": {"query_patterns": queries},
        "_covered_cols": covered_cols,  # test metadata
        "_has_secondary_idx": add_secondary_idx and len(covered_cols) > 0,
    }


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestGsiCandidateFieldCompleteness:
    """Property 6: GSI candidate field completeness."""

    @given(data=_collector_with_high_freq_queries())
    @settings(max_examples=100)
    def test_partition_key_columns_non_empty(self, data: dict):
        """Every candidate has a non-empty partition_key_columns list (1-4, DynamoDB GSI limit)."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert (
                len(c.partition_key_columns) >= 1
            ), f"Candidate for {c.table_id} has empty partition_key_columns"
            assert len(c.partition_key_columns) <= 4

    @given(data=_collector_with_high_freq_queries())
    @settings(max_examples=100)
    def test_table_id_is_valid(self, data: dict):
        """Every candidate has a valid table_id that exists in the input."""
        table_ids = {t["table_id"] for t in data["database_schema"]["tables"]}
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert (
                c.table_id in table_ids
            ), f"Candidate table_id '{c.table_id}' not found in input tables"

    @given(data=_collector_with_high_freq_queries())
    @settings(max_examples=100)
    def test_frequency_is_positive(self, data: dict):
        """Every candidate has a positive total_frequency_per_hour."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert c.total_frequency_per_hour > 0, (
                f"Candidate for {c.table_id} has non-positive frequency "
                f"{c.total_frequency_per_hour}"
            )

    @given(data=_collector_with_high_freq_queries())
    @settings(max_examples=100)
    def test_query_ids_non_empty(self, data: dict):
        """Every candidate has a non-empty query_ids list."""
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        for c in candidates:
            assert len(c.query_ids) >= 1, f"Candidate for {c.table_id} has empty query_ids"

    def test_existing_index_detected(self):
        """When a secondary index covers the candidate columns, existing_index_name is set."""
        data = {
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.orders",
                        "table_name": "orders",
                        "row_count": 10000,
                        "size_mb": 5.0,
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
                            {
                                "column_name": "order_date",
                                "ordinal_position": 3,
                                "data_type": "datetime",
                                "nullable": False,
                            },
                        ],
                        "primary_key": ["id"],
                        "indexes": [
                            {
                                "index_name": "PRIMARY",
                                "columns": ["id"],
                                "is_unique": True,
                                "is_primary": True,
                                "index_type": "btree",
                            },
                            {
                                "index_name": "idx_status",
                                "columns": ["status"],
                                "is_unique": False,
                                "is_primary": False,
                                "index_type": "btree",
                            },
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q-1",
                        "query_type": "SELECT",
                        "frequency_per_hour": 500,
                        "tables_accessed": ["app.orders"],
                        "filter_columns": ["status"],
                    }
                ]
            },
        }
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        assert len(candidates) == 1
        assert candidates[0].existing_index_name == "idx_status"

    def test_no_existing_index_returns_none(self):
        """When no secondary index covers the columns, existing_index_name is None."""
        data = {
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.orders",
                        "table_name": "orders",
                        "row_count": 10000,
                        "size_mb": 5.0,
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
                        "indexes": [
                            {
                                "index_name": "PRIMARY",
                                "columns": ["id"],
                                "is_unique": True,
                                "is_primary": True,
                                "index_type": "btree",
                            },
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q-1",
                        "query_type": "SELECT",
                        "frequency_per_hour": 500,
                        "tables_accessed": ["app.orders"],
                        "filter_columns": ["status"],
                    }
                ]
            },
        }
        pk_cls = classify_primary_keys(data)
        candidates = detect_gsi_candidates(data, pk_cls)

        assert len(candidates) == 1
        assert candidates[0].existing_index_name is None
