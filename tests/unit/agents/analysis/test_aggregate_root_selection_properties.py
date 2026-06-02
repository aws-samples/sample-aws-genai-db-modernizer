"""
Property 4: Aggregate root selection

For any aggregate with 2+ member tables, the designated root_table shall be
the table with the most incoming foreign key references among the aggregate
members. If two or more tables tie on incoming FK count, the root shall be
the table with the highest total query frequency (frequency_per_hour).

Feature: enhanced-dynamodb-analysis, Property 4: Aggregate root selection
Validates: Requirements 2.4
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.contracts.analysis_output import WorkloadAnalysis
from src.tools.analysis.scoring import Aggregate, identify_aggregates

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_WORKLOAD = WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=[])

_schema_name = st.from_regex(r"[a-z]{2,6}", fullmatch=True)
_table_name = st.from_regex(r"[a-z][a-z0-9_]{1,9}", fullmatch=True)


def _unique_table_ids(n: int) -> st.SearchStrategy[list[str]]:
    """Generate a list of *n* unique table_ids like 'schema.table_name'."""
    return st.lists(
        st.tuples(_schema_name, _table_name).map(lambda t: f"{t[0]}.{t[1]}"),
        min_size=n,
        max_size=n,
        unique=True,
    )


def _make_table(tid: str, fk_targets: list[str] | None = None) -> dict:
    """Build a minimal table dict with optional FK references."""
    fks = [{"referenced_table": ref} for ref in (fk_targets or [])]
    return {
        "table_id": tid,
        "table_name": tid.split(".")[-1],
        "row_count": 100,
        "size_mb": 1.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False}
        ],
        "primary_key": ["id"],
        "foreign_keys": fks,
    }


def _find_aggregate_for_table(aggregates: list[Aggregate], table_id: str) -> Aggregate | None:
    for agg in aggregates:
        if table_id in agg.member_tables:
            return agg
    return None


# ---------------------------------------------------------------------------
# Composite strategies
# ---------------------------------------------------------------------------


@st.composite
def _star_topology(draw: st.DrawFn) -> tuple[dict, str]:
    """Generate a star topology: one central table referenced by 2-3 children.

    Returns (collector_output, expected_root_table_id).
    The central table has the most incoming FK references and should be root.
    """
    num_children = draw(st.integers(min_value=2, max_value=3))
    total = 1 + num_children
    tids = draw(_unique_table_ids(total))

    center = tids[0]
    children = tids[1:]

    tables = [_make_table(center)]  # center has no outgoing FKs
    for child_tid in children:
        tables.append(_make_table(child_tid, fk_targets=[center]))

    # Optional single-table queries (don't affect root selection by FK count)
    queries = []
    for i, tid in enumerate(tids):
        queries.append(
            {
                "query_id": f"q-{i}",
                "tables_accessed": [tid],
                "frequency_per_hour": draw(st.floats(min_value=1.0, max_value=100.0)),
                "query_type": "SELECT",
            }
        )

    collector_output = {
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }
    return collector_output, center


@st.composite
def _tiebreak_by_frequency(draw: st.DrawFn) -> tuple[dict, str]:
    """Generate tables where two tables tie on incoming FK count.

    Layout: 4 tables — A, B, C, D
      - A has FK to B  (B gets 1 incoming)
      - A has FK to D  (D gets 1 incoming)
      - C has FK to B  (B gets 2 incoming)
      - C has FK to D  (D gets 2 incoming)

    B and D both have 2 incoming FKs — tied.
    The one with higher query frequency should be root.
    """
    tids = draw(_unique_table_ids(4))
    a, b, c, d = tids

    tables = [
        _make_table(a, fk_targets=[b, d]),
        _make_table(b),
        _make_table(c, fk_targets=[b, d]),
        _make_table(d),
    ]

    # Give one of the tied tables (B or D) strictly higher frequency
    high_freq = draw(st.floats(min_value=200.0, max_value=500.0))
    low_freq = draw(st.floats(min_value=1.0, max_value=50.0))

    # Randomly decide which tied table gets the higher frequency
    b_is_winner = draw(st.booleans())
    if b_is_winner:
        b_freq, d_freq = high_freq, low_freq
        expected_root = b
    else:
        b_freq, d_freq = low_freq, high_freq
        expected_root = d

    queries = [
        {
            "query_id": "q-b",
            "tables_accessed": [b],
            "frequency_per_hour": b_freq,
            "query_type": "SELECT",
        },
        {
            "query_id": "q-d",
            "tables_accessed": [d],
            "frequency_per_hour": d_freq,
            "query_type": "SELECT",
        },
        # A and C get some low frequency — doesn't matter, they have 0 incoming FKs
        {
            "query_id": "q-a",
            "tables_accessed": [a],
            "frequency_per_hour": 1.0,
            "query_type": "SELECT",
        },
        {
            "query_id": "q-c",
            "tables_accessed": [c],
            "frequency_per_hour": 1.0,
            "query_type": "SELECT",
        },
    ]

    collector_output = {
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }
    return collector_output, expected_root


@st.composite
def _single_table_aggregate(draw: st.DrawFn) -> dict:
    """Generate a single disconnected table (no FKs, no co-access)."""
    tids = draw(_unique_table_ids(1))
    tid = tids[0]

    tables = [_make_table(tid)]
    queries = [
        {
            "query_id": "q-solo",
            "tables_accessed": [tid],
            "frequency_per_hour": draw(st.floats(min_value=1.0, max_value=100.0)),
            "query_type": "SELECT",
        }
    ]

    return {
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


@st.composite
def _multi_table_aggregate(draw: st.DrawFn) -> dict:
    """Generate 2-5 FK-connected tables forming a single aggregate."""
    n = draw(st.integers(min_value=2, max_value=5))
    tids = draw(_unique_table_ids(n))

    # Chain topology: each table references the previous one
    tables = []
    for i, tid in enumerate(tids):
        fk_targets = [tids[i - 1]] if i > 0 else []
        tables.append(_make_table(tid, fk_targets=fk_targets))

    queries = []
    for i, tid in enumerate(tids):
        queries.append(
            {
                "query_id": f"q-{i}",
                "tables_accessed": [tid],
                "frequency_per_hour": draw(st.floats(min_value=1.0, max_value=200.0)),
                "query_type": "SELECT",
            }
        )

    return {
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestAggregateRootSelection:
    """Property 4: Aggregate root selection."""

    @given(data=_star_topology())
    def test_root_is_table_with_most_incoming_fks(self, data: tuple[dict, str]):
        """The table with the most incoming FK references is selected as root.

        Validates: Requirements 2.4
        """
        collector_output, expected_root = data

        aggregates = identify_aggregates(collector_output, _MINIMAL_WORKLOAD)

        # All tables should be in one aggregate
        assert len(aggregates) == 1
        agg = aggregates[0]
        assert (
            agg.root_table == expected_root
        ), f"Expected root {expected_root}, got {agg.root_table}"

    @given(data=_tiebreak_by_frequency())
    def test_root_tiebreak_by_query_frequency(self, data: tuple[dict, str]):
        """When two tables tie on incoming FK count, the one with higher
        total query frequency (frequency_per_hour) is selected as root.

        Validates: Requirements 2.4
        """
        collector_output, expected_root = data

        aggregates = identify_aggregates(collector_output, _MINIMAL_WORKLOAD)

        assert len(aggregates) == 1
        agg = aggregates[0]
        assert (
            agg.root_table == expected_root
        ), f"Expected root {expected_root} (higher frequency), got {agg.root_table}"

    @given(data=_single_table_aggregate())
    def test_single_member_aggregate_root_is_itself(self, data: dict):
        """A single-table aggregate should have that table as its root.

        Validates: Requirements 2.4
        """
        aggregates = identify_aggregates(data, _MINIMAL_WORKLOAD)

        tables = data["database_schema"]["tables"]
        assert len(tables) == 1
        tid = tables[0]["table_id"]

        agg = _find_aggregate_for_table(aggregates, tid)
        assert agg is not None
        assert agg.root_table == tid

    @given(data=_multi_table_aggregate())
    def test_root_is_always_a_member(self, data: dict):
        """For any generated aggregate, root_table must be in member_tables.

        Validates: Requirements 2.4
        """
        aggregates = identify_aggregates(data, _MINIMAL_WORKLOAD)

        for agg in aggregates:
            assert (
                agg.root_table in agg.member_tables
            ), f"Root {agg.root_table} not in members {agg.member_tables}"
