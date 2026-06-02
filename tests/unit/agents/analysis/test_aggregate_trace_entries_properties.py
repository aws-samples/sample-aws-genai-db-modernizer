"""
Property 12: Aggregate entries in decision trace

For any collector output that produces N aggregates (N >= 1), the decision
trace shall contain exactly N aggregate entries, one per aggregate, each
referencing the correct root table and member tables.

Feature: enhanced-dynamodb-analysis, Property 12: Aggregate entries in decision trace
Validates: Requirements 2.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.tools.analysis.scoring import identify_aggregates

# ---------------------------------------------------------------------------
# Hypothesis strategies — schemas that guarantee at least one FK edge
# ---------------------------------------------------------------------------

_col_name = st.from_regex(r"[a-z][a-z0-9_]{1,9}", fullmatch=True)


def _fk_connected_collector_strategy() -> st.SearchStrategy[dict]:
    """Generate a collector output with 2-4 tables connected by FKs.

    Guarantees at least one FK relationship so identify_aggregates produces
    at least one multi-table aggregate.
    """
    return st.integers(min_value=2, max_value=4).flatmap(_build_fk_schema)


def _build_fk_schema(n_tables: int) -> st.SearchStrategy[dict]:
    """Build a schema with n_tables where table[i] has FK to table[i-1]."""
    # Fixed table names to avoid collisions and ensure FK resolution
    table_names = [f"tbl_{i}" for i in range(n_tables)]
    table_ids = [f"app.{name}" for name in table_names]

    tables = []
    for i, (tid, name) in enumerate(zip(table_ids, table_names, strict=False)):
        t: dict = {
            "table_id": tid,
            "table_name": name,
            "row_count": 1000 * (i + 1),
            "size_mb": 1.0,
            "columns": [
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "int",
                    "nullable": False,
                },
                {
                    "column_name": "data",
                    "ordinal_position": 2,
                    "data_type": "varchar",
                    "nullable": False,
                },
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                }
            ],
            "primary_key": ["id"],
        }
        # Add FK from table[i] -> table[i-1] for i > 0
        if i > 0:
            parent_name = table_names[i - 1]
            t["columns"].append(
                {
                    "column_name": f"{parent_name}_id",
                    "ordinal_position": 3,
                    "data_type": "int",
                    "nullable": False,
                }
            )
            t["foreign_keys"] = [
                {
                    "constraint_name": f"fk_{parent_name}",
                    "columns": [f"{parent_name}_id"],
                    "referenced_table": parent_name,
                    "referenced_columns": ["id"],
                }
            ]
        tables.append(t)

    # Generate optional co-access queries referencing pairs of connected tables
    def _build_queries(co_access_pairs):
        queries = []
        for idx, (a, b) in enumerate(co_access_pairs):
            queries.append(
                {
                    "query_id": f"q-{idx:03d}",
                    "query_text": f"SELECT * FROM {a} JOIN {b}",  # nosec B608 — test fixture, not executed
                    "query_type": "SELECT",
                    "frequency_per_hour": 200.0,
                    "calls_per_second": 200.0 / 3600.0,
                    "tables_accessed": [a, b],
                    "rows_returned_avg": 5.0,
                    "filter_columns": ["id"],
                    "has_joins": True,
                    "join_count": 1,
                }
            )
        return queries

    # Optionally add co-access queries between adjacent tables
    adjacent_pairs = [(table_ids[i], table_ids[i + 1]) for i in range(n_tables - 1)]

    return st.lists(
        st.sampled_from(adjacent_pairs) if adjacent_pairs else st.nothing(),
        min_size=0,
        max_size=len(adjacent_pairs),
        unique=True,
    ).map(
        lambda pairs: {
            "job_id": "agg-trace-test",
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": _build_queries(pairs)},
        }
    )


def _isolated_tables_collector_strategy() -> st.SearchStrategy[dict]:
    """Generate a collector output with 2-3 tables that have NO FK relationships.

    Each table becomes its own single-table aggregate.
    """
    return st.integers(min_value=2, max_value=3).map(
        lambda n: {
            "job_id": "isolated-test",
            "database_schema": {
                "tables": [
                    {
                        "table_id": f"app.standalone_{i}",
                        "table_name": f"standalone_{i}",
                        "row_count": 500,
                        "size_mb": 1.0,
                        "columns": [
                            {
                                "column_name": "id",
                                "ordinal_position": 1,
                                "data_type": "int",
                                "nullable": False,
                            }
                        ],
                        "indexes": [
                            {
                                "index_name": "PRIMARY",
                                "columns": ["id"],
                                "is_unique": True,
                                "is_primary": True,
                                "index_type": "btree",
                            }
                        ],
                        "primary_key": ["id"],
                    }
                    for i in range(n)
                ]
            },
            "queries": {"query_patterns": []},
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_agent(collector_output: dict) -> tuple[dict, list]:
    """Run the agent and return (trace, aggregates from scoring module)."""
    from unittest.mock import patch as _patch

    from src.tools.analysis.dynamodb_analysis_tools import analyze_dynamodb_use_cases

    with _patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
        inp = AnalysisInput(
            job_id=collector_output.get("job_id", "test"),
            collector_output=collector_output,
            target_database=TargetDatabase.dynamodb,
        )
        _, trace, _ = analyze_for_dynamodb(inp)

    # Also compute aggregates independently for cross-check
    workload = analyze_dynamodb_use_cases(collector_output)
    aggregates = identify_aggregates(collector_output, workload)
    return trace, aggregates


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestAggregateTraceEntries:
    """Property 12: Aggregate entries in decision trace."""

    @given(data=_fk_connected_collector_strategy())
    @settings(max_examples=100)
    def test_trace_has_exactly_n_aggregate_entries(self, data: dict):
        """Trace shall contain exactly N aggregate entries matching identify_aggregates."""
        trace, aggregates = _run_agent(data)
        trace_aggs = trace["aggregates"]
        assert len(trace_aggs) == len(aggregates)

    @given(data=_fk_connected_collector_strategy())
    @settings(max_examples=100)
    def test_each_trace_aggregate_has_correct_root(self, data: dict):
        """Each trace aggregate entry shall reference the correct root table."""
        trace, aggregates = _run_agent(data)
        trace_aggs = trace["aggregates"]

        expected_roots = {a.aggregate_id: a.root_table for a in aggregates}
        for entry in trace_aggs:
            agg_id = entry["aggregate_id"]
            assert agg_id in expected_roots
            assert entry["root_table"] == expected_roots[agg_id]

    @given(data=_fk_connected_collector_strategy())
    @settings(max_examples=100)
    def test_each_trace_aggregate_has_correct_members(self, data: dict):
        """Each trace aggregate entry shall list the correct member tables."""
        trace, aggregates = _run_agent(data)
        trace_aggs = trace["aggregates"]

        expected_members = {a.aggregate_id: sorted(a.member_tables) for a in aggregates}
        for entry in trace_aggs:
            agg_id = entry["aggregate_id"]
            assert sorted(entry["member_tables"]) == expected_members[agg_id]

    @given(data=_fk_connected_collector_strategy())
    @settings(max_examples=100)
    def test_fk_connected_tables_produce_at_least_one_multi_table_aggregate(self, data: dict):
        """FK-connected tables shall produce at least one aggregate with 2+ members."""
        trace, _ = _run_agent(data)
        trace_aggs = trace["aggregates"]
        multi_table = [a for a in trace_aggs if len(a["member_tables"]) > 1]
        assert len(multi_table) >= 1

    @given(data=_isolated_tables_collector_strategy())
    @settings(max_examples=50)
    def test_isolated_tables_produce_single_member_aggregates(self, data: dict):
        """Tables with no FK relationships each become their own aggregate."""
        trace, aggregates = _run_agent(data)
        trace_aggs = trace["aggregates"]
        n_tables = len(data["database_schema"]["tables"])

        # Each isolated table is its own aggregate
        assert len(trace_aggs) == n_tables
        for entry in trace_aggs:
            assert len(entry["member_tables"]) == 1

    @given(data=_fk_connected_collector_strategy())
    @settings(max_examples=100)
    def test_all_tables_covered_by_aggregates(self, data: dict):
        """Every table in the input shall appear in exactly one aggregate."""
        trace, _ = _run_agent(data)
        trace_aggs = trace["aggregates"]

        all_table_ids = {t["table_id"] for t in data["database_schema"]["tables"]}
        covered = set()
        for entry in trace_aggs:
            for tid in entry["member_tables"]:
                assert tid not in covered, f"{tid} appears in multiple aggregates"
                covered.add(tid)
        assert covered == all_table_ids
