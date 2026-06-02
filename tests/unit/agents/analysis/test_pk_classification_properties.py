"""
Property 1: PK classification correctness

For any table in a collector output, classify_primary_keys shall produce a
PkClassification where:
- if the table has 0 PK columns → no_pk=True and partition_key_candidate is None
- if the table has 1 PK column → partition_key_candidate == pk_columns[0]
  and sort_key_candidate is None
- if the table has 2 PK columns → partition_key_candidate == pk_columns[0]
  and sort_key_candidate == pk_columns[1]
- if the table has 3+ PK columns → needs_redesign=True and all PK columns
  are recorded

Feature: enhanced-dynamodb-analysis, Property 1: PK classification correctness
Validates: Requirements 1.1, 1.2, 1.3, 1.4
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.tools.analysis.dynamodb_analysis_tools import classify_primary_keys

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,19}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,8}\.[a-z_]{2,12}", fullmatch=True)


def _table_strategy(
    pk_size: st.SearchStrategy[int] = st.integers(min_value=0, max_value=6),
) -> st.SearchStrategy[dict]:
    """Generate a table dict with a configurable number of PK columns."""
    return st.builds(
        lambda tid, pk_cols, row_count: {
            "table_id": tid,
            "table_name": tid.split(".")[-1],
            "row_count": row_count,
            "size_mb": 1.0,
            "columns": [
                {
                    "column_name": c,
                    "ordinal_position": i + 1,
                    "data_type": "varchar",
                    "nullable": False,
                }
                for i, c in enumerate(pk_cols)
            ],
            "primary_key": pk_cols,
        },
        tid=_table_id,
        pk_cols=pk_size.flatmap(
            lambda n: st.lists(_column_name, min_size=n, max_size=n, unique=True)
        ),
        row_count=st.integers(min_value=0, max_value=10_000_000),
    )


def _collector_output_strategy(
    pk_size: st.SearchStrategy[int] = st.integers(min_value=0, max_value=6),
) -> st.SearchStrategy[dict]:
    """Generate a minimal collector output with 1–5 tables with unique IDs."""
    return st.lists(
        _table_strategy(pk_size), min_size=1, max_size=5, unique_by=lambda t: t["table_id"]
    ).map(
        lambda tables: {
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": []},
        }
    )


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestPkClassificationCorrectness:
    """Property 1: PK classification correctness."""

    @given(data=_collector_output_strategy())
    def test_every_table_gets_a_classification(self, data: dict):
        """classify_primary_keys returns one entry per table in the input."""
        tables = data["database_schema"]["tables"]
        result = classify_primary_keys(data)

        assert len(result) == len(tables)
        for t in tables:
            assert t["table_id"] in result

    @given(data=_collector_output_strategy(pk_size=st.just(0)))
    def test_zero_pk_columns_yields_no_pk(self, data: dict):
        """0 PK columns → no_pk=True, partition_key_candidate is None."""
        result = classify_primary_keys(data)

        for classification in result.values():
            assert classification.no_pk is True
            assert classification.partition_key_candidate is None
            assert classification.sort_key_candidate is None
            assert classification.needs_redesign is False
            assert classification.pk_columns == []

    @given(data=_collector_output_strategy(pk_size=st.just(1)))
    def test_single_pk_column_yields_partition_key_only(self, data: dict):
        """1 PK column → partition_key_candidate=col, sort_key_candidate=None."""
        result = classify_primary_keys(data)

        for classification in result.values():
            assert classification.no_pk is False
            assert classification.needs_redesign is False
            assert len(classification.pk_columns) == 1
            assert classification.partition_key_candidate == classification.pk_columns[0]
            assert classification.sort_key_candidate is None

    @given(data=_collector_output_strategy(pk_size=st.just(2)))
    def test_two_pk_columns_yields_partition_and_sort_key(self, data: dict):
        """2 PK columns → partition_key_candidate=col[0], sort_key_candidate=col[1]."""
        result = classify_primary_keys(data)

        for classification in result.values():
            assert classification.no_pk is False
            assert classification.needs_redesign is False
            assert len(classification.pk_columns) == 2
            assert classification.partition_key_candidate == classification.pk_columns[0]
            assert classification.sort_key_candidate == classification.pk_columns[1]

    @given(data=_collector_output_strategy(pk_size=st.integers(min_value=3, max_value=6)))
    def test_three_plus_pk_columns_yields_needs_redesign(self, data: dict):
        """3+ PK columns → needs_redesign=True, all PK columns recorded."""
        result = classify_primary_keys(data)

        for tid, classification in result.items():
            table = next(t for t in data["database_schema"]["tables"] if t["table_id"] == tid)
            assert classification.no_pk is False
            assert classification.needs_redesign is True
            assert classification.partition_key_candidate is None
            assert classification.sort_key_candidate is None
            assert classification.pk_columns == table["primary_key"]

    @given(data=_collector_output_strategy())
    def test_pk_columns_are_preserved_exactly(self, data: dict):
        """The pk_columns field always matches the input primary_key list."""
        result = classify_primary_keys(data)

        for t in data["database_schema"]["tables"]:
            tid = t["table_id"]
            expected_pk = t.get("primary_key") or []
            assert result[tid].pk_columns == expected_pk

    @given(data=_collector_output_strategy())
    def test_classification_flags_are_mutually_exclusive(self, data: dict):
        """no_pk and needs_redesign are never both True simultaneously."""
        result = classify_primary_keys(data)

        for classification in result.values():
            assert not (classification.no_pk and classification.needs_redesign)

    def test_empty_tables_returns_empty_dict(self):
        """An input with no tables produces an empty classification dict."""
        data = {"database_schema": {"tables": []}, "queries": {"query_patterns": []}}
        assert classify_primary_keys(data) == {}

    def test_missing_schema_returns_empty_dict(self):
        """An input with no database_schema key produces an empty dict."""
        assert classify_primary_keys({}) == {}
        assert classify_primary_keys({"database_schema": {}}) == {}
