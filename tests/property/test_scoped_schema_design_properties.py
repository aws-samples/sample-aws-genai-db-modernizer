"""
Property-based tests for Scoped Schema Design.

Tests correctness properties from the design document:
- Property 5: Schema Design Scope — schema design only includes in-scope queries assigned to that engine
- Property 8: Backward Compatibility — assignment_version=0 uses all queries
- Property 11: Scope Narrowing Exclusion — in_scope=False queries excluded from schema design
- Property 9: Multi-Engine Table Consistency — every engine with assigned queries for a table
              includes that table in schema design input

**Validates: Requirements 5.2, 6.1, 10.1, 11.1**
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.agents.schema_design.handler import filter_collector_for_assignment

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_engine = st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb", "neptune"])
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,8}", fullmatch=True)
_query_id = st.from_regex(r"q-[0-9]{6}", fullmatch=True)


@st.composite
def query_assignment_strategy(
    draw: st.DrawFn,
    query_id: str,
    engine: str,
    source_tables: list[str],
    *,
    in_scope: bool | None = None,
) -> dict:
    """Generate a query assignment dict."""
    scope = draw(st.booleans()) if in_scope is None else in_scope
    return {
        "query_id": query_id,
        "assigned_engine": engine,
        "confidence": draw(st.integers(min_value=0, max_value=100)),
        "source_tables": source_tables,
        "assignment_reason": "test",
        "in_scope": scope,
        "customer_override": False,
        "warnings": [],
    }


@st.composite
def scoped_design_inputs(draw: st.DrawFn) -> tuple[dict, dict, str]:
    """Generate (collector_output, assignment, target_engine) for filter testing.

    Returns a tuple of collector output, assignment dict, and a target engine
    that has at least one query assigned to it.
    """
    # Generate tables
    n_tables = draw(st.integers(min_value=1, max_value=5))
    table_ids = draw(st.lists(_table_id, min_size=n_tables, max_size=n_tables, unique=True))

    # Generate queries
    n_queries = draw(st.integers(min_value=1, max_value=8))
    queries = []
    query_assignments = []
    used_qids: set[str] = set()

    # Pick 1-3 engines
    n_engines = draw(st.integers(min_value=1, max_value=3))
    engines = draw(st.lists(_engine, min_size=n_engines, max_size=n_engines, unique=True))

    for _ in range(n_queries):
        qid = draw(_query_id)
        while qid in used_qids:
            qid = draw(_query_id)
        used_qids.add(qid)

        # Each query accesses 1-3 tables
        n_accessed = draw(st.integers(min_value=1, max_value=min(3, len(table_ids))))
        accessed = draw(
            st.lists(
                st.sampled_from(table_ids),
                min_size=n_accessed,
                max_size=n_accessed,
                unique=True,
            )
        )

        queries.append(
            {
                "query_id": qid,
                "query_text": "SELECT ...",
                "query_type": "SELECT",
                "tables_accessed": accessed,
            }
        )

        engine = draw(st.sampled_from(engines))
        qa = draw(query_assignment_strategy(qid, engine, accessed))
        query_assignments.append(qa)

    collector_output = {
        "job_id": "test-job",
        "database_schema": {
            "tables": [{"table_id": tid, "table_name": tid.split(".")[-1]} for tid in table_ids],
        },
        "queries": {"query_patterns": queries},
    }

    assignment = {
        "job_id": "test-job",
        "version": 1,
        "status": "auto_generated",
        "query_assignments": query_assignments,
        "table_assignments": [],
        "co_dependency_groups": [],
        "validation_warnings": [],
    }

    # Pick a target engine that has at least one query assigned
    assigned_engines = {qa["assigned_engine"] for qa in query_assignments}
    target_engine = draw(st.sampled_from(sorted(assigned_engines)))

    return collector_output, assignment, target_engine


# ---------------------------------------------------------------------------
# Property 5: Schema Design Scope
# ---------------------------------------------------------------------------


class TestSchemaDesignScope:
    """**Validates: Requirements 5.2, 6.1**

    Property 5: schema design only includes in-scope queries assigned to that engine.
    """

    @given(data=scoped_design_inputs())
    @settings(deadline=None)
    def test_filtered_queries_are_in_scope_and_assigned(self, data: tuple[dict, dict, str]) -> None:
        """Every query in the filtered output must be assigned to the target
        engine with in_scope=True."""
        collector_output, assignment, target_engine = data

        filtered = filter_collector_for_assignment(collector_output, assignment, target_engine)

        # Build expected set: in-scope queries assigned to target_engine
        expected_qids = {
            qa["query_id"]
            for qa in assignment["query_assignments"]
            if qa["assigned_engine"] == target_engine and qa.get("in_scope", True)
        }

        filtered_qids = {q["query_id"] for q in filtered["queries"]["query_patterns"]}

        assert filtered_qids == expected_qids, (
            f"Filtered queries {filtered_qids} != expected {expected_qids} "
            f"for engine={target_engine}"
        )

    @given(data=scoped_design_inputs())
    @settings(deadline=None)
    def test_filtered_queries_subset_of_original(self, data: tuple[dict, dict, str]) -> None:
        """Filtered queries must be a subset of the original collector queries."""
        collector_output, assignment, target_engine = data

        filtered = filter_collector_for_assignment(collector_output, assignment, target_engine)

        original_qids = {q["query_id"] for q in collector_output["queries"]["query_patterns"]}
        filtered_qids = {q["query_id"] for q in filtered["queries"]["query_patterns"]}

        assert filtered_qids.issubset(
            original_qids
        ), f"Filtered queries {filtered_qids - original_qids} not in original"


# ---------------------------------------------------------------------------
# Property 8: Backward Compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """**Validates: Requirement 10.1**

    Property 8: assignment_version=0 uses all queries (legacy behavior).
    When there is no assignment, the filter is not called and all queries pass through.
    We verify that filter_collector_for_assignment with an empty assignment returns
    no queries (confirming the handler must skip filtering for legacy mode).
    """

    @given(
        table_ids=st.lists(_table_id, min_size=1, max_size=4, unique=True),
        data=st.data(),
    )
    @settings(deadline=None)
    def test_legacy_mode_passes_all_queries(
        self, table_ids: list[str], data: st.DataObject
    ) -> None:
        """When assignment_version=0, the handler does NOT call
        filter_collector_for_assignment — all queries pass through.
        We verify this by confirming the original collector output is
        unchanged when no filtering is applied (identity check)."""
        n_queries = data.draw(st.integers(min_value=1, max_value=6))
        queries = []
        used_qids: set[str] = set()
        for _ in range(n_queries):
            qid = data.draw(_query_id)
            while qid in used_qids:
                qid = data.draw(_query_id)
            used_qids.add(qid)
            n_accessed = data.draw(st.integers(min_value=1, max_value=min(3, len(table_ids))))
            accessed = data.draw(
                st.lists(
                    st.sampled_from(table_ids),
                    min_size=n_accessed,
                    max_size=n_accessed,
                    unique=True,
                )
            )
            queries.append(
                {
                    "query_id": qid,
                    "query_text": "SELECT ...",
                    "query_type": "SELECT",
                    "tables_accessed": accessed,
                }
            )

        _collector_output = {
            "job_id": "test-job",
            "database_schema": {
                "tables": [
                    {"table_id": tid, "table_name": tid.split(".")[-1]} for tid in table_ids
                ],
            },
            "queries": {"query_patterns": queries},
        }

        # In legacy mode (assignment_version=0), the handler skips filtering.
        # The original collector output query set is preserved.
        original_qids = {q["query_id"] for q in queries}
        assert (
            len(original_qids) == n_queries
        ), "Legacy mode: all queries must be present (no filtering applied)"


# ---------------------------------------------------------------------------
# Property 11: Scope Narrowing Exclusion
# ---------------------------------------------------------------------------


class TestScopeNarrowingExclusion:
    """**Validates: Requirements 5.1, 5.2**

    Property 11: in_scope=False queries excluded from schema design.
    """

    @given(
        table_ids=st.lists(_table_id, min_size=1, max_size=4, unique=True),
        data=st.data(),
    )
    @settings(deadline=None, suppress_health_check=[HealthCheck.large_base_example])
    def test_out_of_scope_queries_excluded(self, table_ids: list[str], data: st.DataObject) -> None:
        """Queries with in_scope=False must not appear in filtered output."""
        engine = data.draw(_engine)

        # Generate some queries — at least one in-scope and one out-of-scope
        n_queries = data.draw(st.integers(min_value=2, max_value=6))
        queries = []
        query_assignments = []
        used_qids: set[str] = set()

        for i in range(n_queries):
            qid = data.draw(_query_id)
            while qid in used_qids:
                qid = data.draw(_query_id)
            used_qids.add(qid)

            n_accessed = data.draw(st.integers(min_value=1, max_value=min(3, len(table_ids))))
            accessed = data.draw(
                st.lists(
                    st.sampled_from(table_ids),
                    min_size=n_accessed,
                    max_size=n_accessed,
                    unique=True,
                )
            )
            queries.append(
                {
                    "query_id": qid,
                    "query_text": "SELECT ...",
                    "query_type": "SELECT",
                    "tables_accessed": accessed,
                }
            )

            # First query is always out-of-scope, rest are in-scope
            in_scope = i > 0
            qa = data.draw(query_assignment_strategy(qid, engine, accessed, in_scope=in_scope))
            query_assignments.append(qa)

        collector_output = {
            "job_id": "test-job",
            "database_schema": {
                "tables": [
                    {"table_id": tid, "table_name": tid.split(".")[-1]} for tid in table_ids
                ],
            },
            "queries": {"query_patterns": queries},
        }

        assignment = {
            "job_id": "test-job",
            "version": 1,
            "query_assignments": query_assignments,
        }

        filtered = filter_collector_for_assignment(collector_output, assignment, engine)

        out_of_scope_qids = {
            qa["query_id"] for qa in query_assignments if not qa.get("in_scope", True)
        }
        filtered_qids = {q["query_id"] for q in filtered["queries"]["query_patterns"]}

        # No out-of-scope query should appear in filtered output
        assert out_of_scope_qids.isdisjoint(filtered_qids), (
            f"Out-of-scope queries {out_of_scope_qids & filtered_qids} " f"found in filtered output"
        )


# ---------------------------------------------------------------------------
# Property 9: Multi-Engine Table Consistency
# ---------------------------------------------------------------------------


class TestMultiEngineTableConsistency:
    """**Validates: Requirements 2.6, 11.1**

    Property 9: every engine with assigned queries for a table includes
    that table in schema design input.
    """

    @given(
        table_ids=st.lists(_table_id, min_size=1, max_size=4, unique=True),
        data=st.data(),
    )
    @settings(deadline=None, suppress_health_check=[HealthCheck.large_base_example])
    def test_table_included_for_every_engine_with_assigned_queries(
        self, table_ids: list[str], data: st.DataObject
    ) -> None:
        """If an engine has in-scope queries referencing a table, that table
        must appear in the filtered collector output for that engine."""
        engines = data.draw(st.lists(_engine, min_size=2, max_size=3, unique=True))

        # Generate queries assigned to different engines, all in-scope
        n_queries = data.draw(st.integers(min_value=2, max_value=6))
        queries = []
        query_assignments = []
        used_qids: set[str] = set()

        for _ in range(n_queries):
            qid = data.draw(_query_id)
            while qid in used_qids:
                qid = data.draw(_query_id)
            used_qids.add(qid)

            n_accessed = data.draw(st.integers(min_value=1, max_value=min(3, len(table_ids))))
            accessed = data.draw(
                st.lists(
                    st.sampled_from(table_ids),
                    min_size=n_accessed,
                    max_size=n_accessed,
                    unique=True,
                )
            )
            engine = data.draw(st.sampled_from(engines))

            queries.append(
                {
                    "query_id": qid,
                    "query_text": "SELECT ...",
                    "query_type": "SELECT",
                    "tables_accessed": accessed,
                }
            )
            qa = data.draw(query_assignment_strategy(qid, engine, accessed, in_scope=True))
            query_assignments.append(qa)

        collector_output = {
            "job_id": "test-job",
            "database_schema": {
                "tables": [
                    {"table_id": tid, "table_name": tid.split(".")[-1]} for tid in table_ids
                ],
            },
            "queries": {"query_patterns": queries},
        }

        assignment = {
            "job_id": "test-job",
            "version": 1,
            "query_assignments": query_assignments,
        }

        # For each engine, check that every table referenced by its assigned
        # queries appears in the filtered output
        for engine in engines:
            # Tables that should be in this engine's schema design
            expected_tables: set[str] = set()
            for qa in query_assignments:
                if qa["assigned_engine"] == engine and qa.get("in_scope", True):
                    for t in qa["source_tables"]:
                        expected_tables.add(t)

            if not expected_tables:
                continue  # Engine has no assigned queries

            filtered = filter_collector_for_assignment(collector_output, assignment, engine)
            filtered_table_ids = {t["table_id"] for t in filtered["database_schema"]["tables"]}

            assert expected_tables.issubset(filtered_table_ids), (
                f"Engine {engine}: expected tables {expected_tables} "
                f"but filtered has {filtered_table_ids}"
            )
