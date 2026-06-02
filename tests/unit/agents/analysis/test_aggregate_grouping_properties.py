"""
Property 3: Aggregate grouping with co-access confidence

For any set of tables connected by foreign keys, identify_aggregates shall
place them in the same aggregate. Furthermore, if at least one query
co-accesses tables in the aggregate, the co_access_confidence shall be
strictly greater than the confidence assigned to an aggregate whose tables
are connected by FK only (no co-access queries).

Feature: enhanced-dynamodb-analysis, Property 3: Aggregate grouping with co-access confidence
Validates: Requirements 2.2, 2.3
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.contracts.analysis_output import WorkloadAnalysis
from src.tools.analysis.scoring import Aggregate, identify_aggregates

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)
_schema_name = st.from_regex(r"[a-z]{2,6}", fullmatch=True)
_table_name = st.from_regex(r"[a-z][a-z0-9_]{1,9}", fullmatch=True)


def _unique_table_ids(n: int) -> st.SearchStrategy[list[str]]:
    """Generate a list of n unique table_ids like 'schema.table_name'."""
    return st.lists(
        st.tuples(_schema_name, _table_name).map(lambda t: f"{t[0]}.{t[1]}"),
        min_size=n,
        max_size=n,
        unique=True,
    )


def _fk_chain_collector_output(
    num_tables: st.SearchStrategy[int] = st.integers(min_value=2, max_value=4),
) -> st.SearchStrategy[dict]:
    """Generate a collector output with tables connected in an FK chain.

    A→B→C means A has FK to B, B has FK to C.
    No co-access queries are included.
    """

    @st.composite
    def _build(draw: st.DrawFn) -> dict:
        n = draw(num_tables)
        tids = draw(_unique_table_ids(n))

        tables = []
        for i, tid in enumerate(tids):
            tname = tid.split(".")[-1]
            fks = []
            if i > 0:
                # Each table references the previous one in the chain
                fks.append({"referenced_table": tids[i - 1]})
            tables.append(
                {
                    "table_id": tid,
                    "table_name": tname,
                    "row_count": draw(st.integers(min_value=1, max_value=100_000)),
                    "size_mb": 1.0,
                    "columns": [
                        {
                            "column_name": "id",
                            "ordinal_position": 1,
                            "data_type": "int",
                            "nullable": False,
                        }
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": fks,
                }
            )

        return {
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": []},
        }

    return _build()


def _fk_chain_with_co_access_collector_output(
    num_tables: st.SearchStrategy[int] = st.integers(min_value=2, max_value=4),
) -> st.SearchStrategy[dict]:
    """Generate a collector output with FK-chained tables AND co-access queries.

    At least one query accesses multiple tables in the chain.
    """

    @st.composite
    def _build(draw: st.DrawFn) -> dict:
        n = draw(num_tables)
        tids = draw(_unique_table_ids(n))

        tables = []
        for i, tid in enumerate(tids):
            tname = tid.split(".")[-1]
            fks = []
            if i > 0:
                fks.append({"referenced_table": tids[i - 1]})
            tables.append(
                {
                    "table_id": tid,
                    "table_name": tname,
                    "row_count": draw(st.integers(min_value=1, max_value=100_000)),
                    "size_mb": 1.0,
                    "columns": [
                        {
                            "column_name": "id",
                            "ordinal_position": 1,
                            "data_type": "int",
                            "nullable": False,
                        }
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": fks,
                }
            )

        # Build at least one co-access query touching 2+ tables
        num_co_access = draw(st.integers(min_value=1, max_value=3))
        queries = []
        for qi in range(num_co_access):
            # Pick at least 2 tables to co-access
            co_count = draw(st.integers(min_value=2, max_value=min(n, 4)))
            co_tables = draw(
                st.lists(st.sampled_from(tids), min_size=co_count, max_size=co_count, unique=True)
            )
            queries.append(
                {
                    "query_id": f"q-co-{qi}",
                    "tables_accessed": co_tables,
                    "frequency_per_hour": draw(st.floats(min_value=1.0, max_value=500.0)),
                    "query_type": "SELECT",
                }
            )

        return {
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": queries},
        }

    return _build()


def _disconnected_tables_collector_output(
    num_tables: st.SearchStrategy[int] = st.integers(min_value=2, max_value=5),
) -> st.SearchStrategy[dict]:
    """Generate tables with no FK relationships and no co-access queries."""

    @st.composite
    def _build(draw: st.DrawFn) -> dict:
        n = draw(num_tables)
        tids = draw(_unique_table_ids(n))

        tables = []
        for tid in tids:
            tname = tid.split(".")[-1]
            tables.append(
                {
                    "table_id": tid,
                    "table_name": tname,
                    "row_count": draw(st.integers(min_value=1, max_value=100_000)),
                    "size_mb": 1.0,
                    "columns": [
                        {
                            "column_name": "id",
                            "ordinal_position": 1,
                            "data_type": "int",
                            "nullable": False,
                        }
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [],
                }
            )

        # Only single-table queries (no co-access)
        queries = []
        for i, tid in enumerate(tids):
            queries.append(
                {
                    "query_id": f"q-single-{i}",
                    "tables_accessed": [tid],
                    "frequency_per_hour": draw(st.floats(min_value=1.0, max_value=100.0)),
                    "query_type": "SELECT",
                }
            )

        return {
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": queries},
        }

    return _build()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_WORKLOAD = WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=[])


def _find_aggregate_for_table(aggregates: list[Aggregate], table_id: str) -> Aggregate | None:
    """Find the aggregate containing a given table_id."""
    for agg in aggregates:
        if table_id in agg.member_tables:
            return agg
    return None


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestAggregateGroupingWithCoAccessConfidence:
    """Property 3: Aggregate grouping with co-access confidence."""

    @given(data=_fk_chain_collector_output())
    def test_fk_connected_tables_in_same_aggregate(self, data: dict):
        """FK-connected tables (A→B→C chain) all end up in the same aggregate.

        Validates: Requirements 2.2, 2.3
        """
        tables = data["database_schema"]["tables"]
        table_ids = [t["table_id"] for t in tables]

        aggregates = identify_aggregates(data, _MINIMAL_WORKLOAD)

        # All FK-connected tables must be in the same aggregate
        first_agg = _find_aggregate_for_table(aggregates, table_ids[0])
        assert first_agg is not None, "First table should be in an aggregate"

        for tid in table_ids:
            agg = _find_aggregate_for_table(aggregates, tid)
            assert agg is not None, f"Table {tid} should be in an aggregate"
            assert (
                agg.aggregate_id == first_agg.aggregate_id
            ), f"Table {tid} should be in the same aggregate as {table_ids[0]}"

    @given(data=_fk_chain_collector_output())
    def test_co_access_confidence_higher_than_fk_only(self, data: dict):
        """Co-access evidence yields strictly higher confidence than FK-only.

        Generate the same FK-connected tables: once without co-access queries
        (FK-only) and once with co-access queries. The co-access version must
        have strictly higher co_access_confidence.

        Validates: Requirements 2.2, 2.3
        """
        tables = data["database_schema"]["tables"]
        table_ids = [t["table_id"] for t in tables]

        # FK-only: no co-access queries
        fk_only_aggregates = identify_aggregates(data, _MINIMAL_WORKLOAD)
        fk_only_agg = _find_aggregate_for_table(fk_only_aggregates, table_ids[0])
        assert fk_only_agg is not None

        # Now add co-access queries
        co_access_data = {
            "database_schema": data["database_schema"],
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q-co-injected",
                        "tables_accessed": table_ids[:2],
                        "frequency_per_hour": 100.0,
                        "query_type": "SELECT",
                    }
                ]
            },
        }
        co_access_aggregates = identify_aggregates(co_access_data, _MINIMAL_WORKLOAD)
        co_access_agg = _find_aggregate_for_table(co_access_aggregates, table_ids[0])
        assert co_access_agg is not None

        assert co_access_agg.co_access_confidence > fk_only_agg.co_access_confidence, (
            f"Co-access confidence ({co_access_agg.co_access_confidence}) should be "
            f"strictly greater than FK-only confidence ({fk_only_agg.co_access_confidence})"
        )

    @given(data=_fk_chain_with_co_access_collector_output())
    def test_co_access_confidence_range_with_evidence(self, data: dict):
        """When co-access evidence exists, co_access_confidence is in [80, 100].

        Validates: Requirements 2.2, 2.3
        """
        aggregates = identify_aggregates(data, _MINIMAL_WORKLOAD)

        for agg in aggregates:
            if agg.co_access_evidence:
                assert 80 <= agg.co_access_confidence <= 100, (
                    f"Aggregate {agg.aggregate_id} with co-access evidence should have "
                    f"confidence in [80, 100], got {agg.co_access_confidence}"
                )

    @given(data=_fk_chain_collector_output())
    def test_co_access_confidence_range_fk_only(self, data: dict):
        """When no co-access evidence exists (FK-only), co_access_confidence is in [40, 60].

        Validates: Requirements 2.2, 2.3
        """
        aggregates = identify_aggregates(data, _MINIMAL_WORKLOAD)

        for agg in aggregates:
            if not agg.co_access_evidence:
                assert 40 <= agg.co_access_confidence <= 60, (
                    f"Aggregate {agg.aggregate_id} without co-access evidence should have "
                    f"confidence in [40, 60], got {agg.co_access_confidence}"
                )

    @given(data=_disconnected_tables_collector_output())
    def test_disconnected_tables_in_separate_aggregates(self, data: dict):
        """Tables with no FK relationships and no co-access queries are each in
        their own separate aggregate.

        Validates: Requirements 2.2, 2.3
        """
        tables = data["database_schema"]["tables"]
        table_ids = [t["table_id"] for t in tables]

        aggregates = identify_aggregates(data, _MINIMAL_WORKLOAD)

        # Each table should be in its own aggregate (singleton)
        assert len(aggregates) == len(
            table_ids
        ), f"Expected {len(table_ids)} aggregates for disconnected tables, got {len(aggregates)}"

        for agg in aggregates:
            assert len(agg.member_tables) == 1, (
                f"Aggregate {agg.aggregate_id} should have exactly 1 member, "
                f"got {len(agg.member_tables)}: {agg.member_tables}"
            )

    def test_empty_input_returns_empty(self):
        """Edge case: empty collector output returns empty list."""
        empty_data: dict = {"database_schema": {"tables": []}, "queries": {"query_patterns": []}}
        result = identify_aggregates(empty_data, _MINIMAL_WORKLOAD)
        assert result == []

        # Also test completely missing keys
        assert identify_aggregates({}, _MINIMAL_WORKLOAD) == []
        assert identify_aggregates({"database_schema": {}}, _MINIMAL_WORKLOAD) == []
