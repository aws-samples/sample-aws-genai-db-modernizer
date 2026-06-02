"""
Property 9: Secondary index dominance detection

For any table where a secondary index accounts for more than 50% of total
query frequency, `detect_secondary_index_dominance` shall return a
`SecondaryIndexDominance` with `frequency_share > 0.5` and the correct
index columns. For any table where no secondary index exceeds 50%, the
function shall not return a result for that table.

Feature: enhanced-dynamodb-analysis, Property 9: Secondary index dominance detection
Validates: Requirements 5.1, 5.2
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from src.tools.analysis.dynamodb_analysis_tools import (
    classify_primary_keys,
    detect_secondary_index_dominance,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,8}", fullmatch=True)
_index_name = st.from_regex(r"idx_[a-z]{2,8}", fullmatch=True)


def _secondary_index_strategy() -> st.SearchStrategy[dict]:
    """Generate a non-primary secondary index with 1-3 columns."""
    return st.builds(
        lambda name, cols: {
            "index_name": name,
            "columns": cols,
            "is_primary": False,
            "is_unique": False,
        },
        name=_index_name,
        cols=st.lists(_column_name, min_size=1, max_size=3, unique=True),
    )


def _table_with_indexes_strategy() -> st.SearchStrategy[dict]:
    """Generate a table with a PK and 1-3 secondary indexes."""
    return st.builds(
        lambda tid, pk_col, sec_indexes, row_count: {
            "table_id": tid,
            "table_name": tid.split(".")[-1],
            "row_count": row_count,
            "size_mb": 1.0,
            "primary_key": [pk_col],
            "columns": [
                {
                    "column_name": pk_col,
                    "ordinal_position": 1,
                    "data_type": "int",
                    "nullable": False,
                },
            ]
            + [
                {
                    "column_name": c,
                    "ordinal_position": i + 2,
                    "data_type": "varchar",
                    "nullable": False,
                }
                for idx in sec_indexes
                for i, c in enumerate(idx["columns"])
            ],
            "indexes": [
                {
                    "index_name": "pk_idx",
                    "columns": [pk_col],
                    "is_primary": True,
                    "is_unique": True,
                },
            ]
            + sec_indexes,
            "foreign_keys": [],
        },
        tid=_table_id,
        pk_col=_column_name,
        sec_indexes=st.lists(
            _secondary_index_strategy(), min_size=1, max_size=3, unique_by=lambda x: x["index_name"]
        ),
        row_count=st.integers(min_value=100, max_value=1_000_000),
    )


def _query_targeting_index(
    table_id: str,
    index_columns: list[str],
    frequency: float,
) -> dict:
    """Build a query dict that filters on the given index columns."""
    return {
        "query_id": f"q-{table_id}-{'_'.join(index_columns)}-{frequency}",
        "query_text": f"SELECT * FROM {table_id} WHERE "  # nosec B608 — test fixture, not executed
        + " AND ".join(f"{c} = ?" for c in index_columns),
        "query_type": "SELECT",
        "tables_accessed": [table_id],
        "filter_columns": list(index_columns),
        "sort_columns": [],
        "frequency_per_hour": frequency,
        "calls_per_second": frequency / 3600,
        "rows_returned_avg": 10,
    }


def _query_targeting_pk(
    table_id: str,
    pk_col: str,
    frequency: float,
) -> dict:
    """Build a query dict that filters on the PK column."""
    return {
        "query_id": f"q-{table_id}-pk-{frequency}",
        "query_text": f"SELECT * FROM {table_id} WHERE {pk_col} = ?",  # nosec B608 — test fixture, not executed
        "query_type": "SELECT",
        "tables_accessed": [table_id],
        "filter_columns": [pk_col],
        "sort_columns": [],
        "frequency_per_hour": frequency,
        "calls_per_second": frequency / 3600,
        "rows_returned_avg": 1,
    }


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestSecondaryIndexDominanceDetection:
    """Property 9: Secondary index dominance detection."""

    @given(
        table=_table_with_indexes_strategy(),
        dominant_share=st.floats(min_value=0.51, max_value=0.99),
        total_freq=st.floats(min_value=10.0, max_value=10000.0),
    )
    @settings(max_examples=100)
    def test_dominant_index_detected_when_share_exceeds_50_percent(
        self,
        table: dict,
        dominant_share: float,
        total_freq: float,
    ):
        """**Validates: Requirements 5.1**

        When a secondary index accounts for >50% of query frequency,
        the function shall flag the table as secondary-index-dominant.
        """
        sec_indexes = [idx for idx in table["indexes"] if not idx.get("is_primary")]
        assume(len(sec_indexes) >= 1)

        dominant_idx = sec_indexes[0]
        pk_col = table["primary_key"][0]

        # Ensure PK col is different from index cols
        assume(pk_col not in {c.lower() for c in dominant_idx["columns"]})

        dominant_freq = total_freq * dominant_share
        pk_freq = total_freq * (1.0 - dominant_share)

        queries = [
            _query_targeting_index(table["table_id"], dominant_idx["columns"], dominant_freq),
            _query_targeting_pk(table["table_id"], pk_col, pk_freq),
        ]

        collector_output = {
            "database_schema": {"tables": [table]},
            "queries": {"query_patterns": queries},
        }

        pk_classifications = classify_primary_keys(collector_output)
        results = detect_secondary_index_dominance(collector_output, pk_classifications)

        # Should detect at least one dominant index
        table_results = [r for r in results if r.table_id == table["table_id"]]
        assert (
            len(table_results) >= 1
        ), f"Expected at least 1 dominance result for table {table['table_id']}, got {len(table_results)}"

        # The targeted index should be among the results
        matching = [r for r in table_results if r.dominant_index_name == dominant_idx["index_name"]]
        assert (
            len(matching) >= 1
        ), f"Expected dominant index {dominant_idx['index_name']} in results"
        result = matching[0]
        assert result.frequency_share > 0.5
        assert result.dominant_index_columns == [c.lower() for c in dominant_idx["columns"]]

    @given(
        table=_table_with_indexes_strategy(),
        dominant_share=st.floats(min_value=0.51, max_value=0.99),
        total_freq=st.floats(min_value=10.0, max_value=10000.0),
    )
    @settings(max_examples=100)
    def test_dominant_index_records_alternative_pk_sk_candidates(
        self,
        table: dict,
        dominant_share: float,
        total_freq: float,
    ):
        """**Validates: Requirements 5.2**

        When flagged as secondary-index-dominant, the function shall record
        the dominant index columns as alternative PK/SK candidates.
        """
        sec_indexes = [idx for idx in table["indexes"] if not idx.get("is_primary")]
        assume(len(sec_indexes) >= 1)

        dominant_idx = sec_indexes[0]
        pk_col = table["primary_key"][0]
        assume(pk_col not in {c.lower() for c in dominant_idx["columns"]})

        dominant_freq = total_freq * dominant_share
        pk_freq = total_freq * (1.0 - dominant_share)

        queries = [
            _query_targeting_index(table["table_id"], dominant_idx["columns"], dominant_freq),
            _query_targeting_pk(table["table_id"], pk_col, pk_freq),
        ]

        collector_output = {
            "database_schema": {"tables": [table]},
            "queries": {"query_patterns": queries},
        }

        pk_classifications = classify_primary_keys(collector_output)
        results = detect_secondary_index_dominance(collector_output, pk_classifications)

        table_results = [r for r in results if r.table_id == table["table_id"]]
        assert len(table_results) >= 1

        # Find the result for the targeted dominant index
        matching = [r for r in table_results if r.dominant_index_name == dominant_idx["index_name"]]
        assert len(matching) >= 1
        result = matching[0]
        idx_cols = [c.lower() for c in dominant_idx["columns"]]

        # First column should be alternative PK candidate
        assert result.alternative_pk_candidate == idx_cols[0]

        # Second column (if exists) should be alternative SK candidate
        if len(idx_cols) >= 2:
            assert result.alternative_sk_candidate == idx_cols[1]
        else:
            assert result.alternative_sk_candidate is None

    @given(
        table=_table_with_indexes_strategy(),
        index_share=st.floats(min_value=0.01, max_value=0.49),
        total_freq=st.floats(min_value=10.0, max_value=10000.0),
    )
    @settings(max_examples=100)
    def test_no_result_when_no_index_exceeds_50_percent(
        self,
        table: dict,
        index_share: float,
        total_freq: float,
    ):
        """**Validates: Requirements 5.1**

        When no secondary index exceeds 50% of query frequency,
        the function shall not return a result for that table.
        """
        sec_indexes = [idx for idx in table["indexes"] if not idx.get("is_primary")]
        assume(len(sec_indexes) >= 1)

        dominant_idx = sec_indexes[0]
        pk_col = table["primary_key"][0]

        # Ensure PK col doesn't appear in ANY secondary index columns,
        # otherwise the PK query would also match those indexes
        all_sec_cols = {c.lower() for idx in sec_indexes for c in idx["columns"]}
        assume(pk_col.lower() not in all_sec_cols)

        index_freq = total_freq * index_share
        pk_freq = total_freq * (1.0 - index_share)

        queries = [
            _query_targeting_index(table["table_id"], dominant_idx["columns"], index_freq),
            _query_targeting_pk(table["table_id"], pk_col, pk_freq),
        ]

        collector_output = {
            "database_schema": {"tables": [table]},
            "queries": {"query_patterns": queries},
        }

        pk_classifications = classify_primary_keys(collector_output)
        results = detect_secondary_index_dominance(collector_output, pk_classifications)

        table_results = [r for r in results if r.table_id == table["table_id"]]
        assert len(table_results) == 0, (
            f"Expected no dominance result for table {table['table_id']} "
            f"with index share {index_share:.2f}, but got {len(table_results)}"
        )

    @given(
        table=_table_with_indexes_strategy(),
    )
    @settings(max_examples=100)
    def test_frequency_share_is_between_0_and_1(self, table: dict):
        """**Validates: Requirements 5.1**

        Any returned SecondaryIndexDominance shall have frequency_share in (0.5, 1.0].
        """
        sec_indexes = [idx for idx in table["indexes"] if not idx.get("is_primary")]
        assume(len(sec_indexes) >= 1)

        dominant_idx = sec_indexes[0]
        pk_col = table["primary_key"][0]
        assume(pk_col not in {c.lower() for c in dominant_idx["columns"]})

        # 80% on secondary index
        queries = [
            _query_targeting_index(table["table_id"], dominant_idx["columns"], 800.0),
            _query_targeting_pk(table["table_id"], pk_col, 200.0),
        ]

        collector_output = {
            "database_schema": {"tables": [table]},
            "queries": {"query_patterns": queries},
        }

        pk_classifications = classify_primary_keys(collector_output)
        results = detect_secondary_index_dominance(collector_output, pk_classifications)

        for r in results:
            assert 0.0 < r.frequency_share <= 1.0

    def test_empty_tables_returns_empty_list(self):
        """An input with no tables produces an empty list."""
        data = {"database_schema": {"tables": []}, "queries": {"query_patterns": []}}
        assert detect_secondary_index_dominance(data, {}) == []

    def test_empty_queries_returns_empty_list(self):
        """An input with no queries produces an empty list."""
        table = {
            "table_id": "app.products",
            "table_name": "products",
            "row_count": 1000,
            "size_mb": 10.0,
            "primary_key": ["id"],
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False}
            ],
            "indexes": [
                {"index_name": "pk_idx", "columns": ["id"], "is_primary": True, "is_unique": True},
                {"index_name": "idx_cat", "columns": ["category_id"], "is_primary": False},
            ],
            "foreign_keys": [],
        }
        data = {"database_schema": {"tables": [table]}, "queries": {"query_patterns": []}}
        pk_cls = classify_primary_keys(data)
        assert detect_secondary_index_dominance(data, pk_cls) == []

    def test_exact_50_percent_not_flagged(self):
        """Exactly 50% share should NOT be flagged (must be strictly >50%)."""
        table = {
            "table_id": "app.products",
            "table_name": "products",
            "row_count": 1000,
            "size_mb": 10.0,
            "primary_key": ["id"],
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
                {
                    "column_name": "category_id",
                    "ordinal_position": 2,
                    "data_type": "int",
                    "nullable": False,
                },
            ],
            "indexes": [
                {"index_name": "pk_idx", "columns": ["id"], "is_primary": True, "is_unique": True},
                {"index_name": "idx_cat", "columns": ["category_id"], "is_primary": False},
            ],
            "foreign_keys": [],
        }
        queries = [
            _query_targeting_index("app.products", ["category_id"], 500.0),
            _query_targeting_pk("app.products", "id", 500.0),
        ]
        data = {"database_schema": {"tables": [table]}, "queries": {"query_patterns": queries}}
        pk_cls = classify_primary_keys(data)
        results = detect_secondary_index_dominance(data, pk_cls)
        assert len(results) == 0
