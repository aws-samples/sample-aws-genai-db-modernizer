"""
Property-based tests for the Assignment Resolver.

Tests correctness properties from the design document:
- Property 1: Assignment Completeness — every query_id from collector appears in exactly one assignment
- Property 3: Co-Dependency Integrity — all queries in a co-dependency group assigned to same engine,
              or warning exists
- Property 17: Co-Dependency Significance Filter — non-significant JOINs do not contribute to
               co-dependency grouping

**Validates: Requirements 2.1, 2.3, 2.5**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee.assignment_resolver import (
    AssignmentResolver,
    build_co_dependency_groups,
    is_significant_join,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_engine = st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb"])
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,8}", fullmatch=True)
_query_id = st.from_regex(r"q-[0-9]{6}", fullmatch=True)


@st.composite
def analysis_table_rec(draw: st.DrawFn, table_id: str) -> dict:
    """Generate a table recommendation for an analysis output."""
    return {
        "table_id": table_id,
        "confidence_score": draw(st.integers(min_value=0, max_value=100)),
        "rationale": "test recommendation",
        "score_breakdown": {
            "pattern_match_score": draw(st.integers(min_value=0, max_value=100)),
            "complexity_score": draw(st.integers(min_value=0, max_value=100)),
            "performance_score": draw(st.integers(min_value=0, max_value=100)),
            "cost_score": draw(st.integers(min_value=0, max_value=100)),
        },
    }


@st.composite
def analysis_output_strategy(draw: st.DrawFn, table_ids: list[str]) -> dict:
    """Generate a minimal analysis output dict with table recommendations."""
    recs = []
    for tid in table_ids:
        recs.append(draw(analysis_table_rec(tid)))
    return {
        "table_recommendations": recs,
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


@st.composite
def query_strategy(
    draw: st.DrawFn,
    table_ids: list[str],
    *,
    force_significant_join: bool = False,
    force_non_significant: bool = False,
) -> dict:
    """Generate a query dict accessing some of the given tables."""
    n_tables = draw(st.integers(min_value=1, max_value=max(1, min(3, len(table_ids)))))
    accessed = draw(
        st.lists(
            st.sampled_from(table_ids),
            min_size=n_tables,
            max_size=n_tables,
            unique=True,
        )
    )
    qid = draw(_query_id)

    if force_significant_join:
        # Ensure at least one significance criterion is met
        join_count = draw(st.integers(min_value=2, max_value=5))
        has_aggregation = draw(st.booleans())
        filter_tables = accessed[:1] if draw(st.booleans()) else []
        has_joins = True
    elif force_non_significant:
        # Ensure NO significance criterion is met
        join_count = draw(st.sampled_from([0, 1]))
        has_aggregation = False
        filter_tables = []
        has_joins = join_count > 0
    else:
        join_count = draw(st.integers(min_value=0, max_value=5))
        has_aggregation = draw(st.booleans())
        filter_tables = draw(
            st.lists(st.sampled_from(accessed), min_size=0, max_size=len(accessed), unique=True)
        )
        has_joins = join_count > 0 or len(accessed) > 1

    return {
        "query_id": qid,
        "query_text": "SELECT ... FROM ...",
        "query_type": "SELECT",
        "tables_accessed": accessed,
        "join_count": join_count,
        "has_joins": has_joins,
        "has_aggregation": has_aggregation,
        "filter_tables": filter_tables,
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }


@st.composite
def resolver_inputs_strategy(draw: st.DrawFn) -> tuple[dict, dict[str, dict], dict]:
    """Generate (triage, analysis_outputs, collector_output) for the resolver."""
    n_tables = draw(st.integers(min_value=1, max_value=5))
    table_ids = draw(st.lists(_table_id, min_size=n_tables, max_size=n_tables, unique=True))

    n_queries = draw(st.integers(min_value=1, max_value=8))
    queries = []
    used_qids: set[str] = set()
    for _ in range(n_queries):
        q = draw(query_strategy(table_ids))
        # Ensure unique query_ids
        while q["query_id"] in used_qids:
            q = draw(query_strategy(table_ids))
        used_qids.add(q["query_id"])
        queries.append(q)

    collector_output = {
        "job_id": "test-job",
        "database_schema": {
            "tables": [{"table_id": tid, "table_name": tid.split(".")[-1]} for tid in table_ids]
        },
        "queries": {"query_patterns": queries},
    }

    # Generate 1-3 engine analysis outputs
    n_engines = draw(st.integers(min_value=1, max_value=3))
    engines = draw(st.lists(_engine, min_size=n_engines, max_size=n_engines, unique=True))
    analysis_outputs = {}
    for engine in engines:
        analysis_outputs[engine] = draw(analysis_output_strategy(table_ids))

    triage = {"selected": {e: ["signal"] for e in engines}}

    return triage, analysis_outputs, collector_output


# ---------------------------------------------------------------------------
# Property 1: Assignment Completeness
# ---------------------------------------------------------------------------


class TestAssignmentCompleteness:
    """**Validates: Requirements 2.1, 2.5**

    Property 1: every query_id from collector appears in exactly one assignment.
    """

    @given(data=resolver_inputs_strategy())
    @settings(deadline=None)
    def test_every_query_assigned_exactly_once(
        self, data: tuple[dict, dict[str, dict], dict]
    ) -> None:
        """Every query_id from the collector output must appear in exactly
        one query assignment in the resolver output."""
        triage, analysis_outputs, collector_output = data
        resolver = AssignmentResolver()
        assignment = resolver.resolve(triage, analysis_outputs, collector_output)

        collector_qids = {q["query_id"] for q in collector_output["queries"]["query_patterns"]}
        assigned_qids = [qa.query_id for qa in assignment.query_assignments]

        # Every collector query appears in assignment
        assert (
            set(assigned_qids) == collector_qids
        ), f"Mismatch: collector has {collector_qids}, assignment has {set(assigned_qids)}"

        # Each query_id appears exactly once (no duplicates)
        assert len(assigned_qids) == len(set(assigned_qids)), (
            f"Duplicate query_ids in assignment: "
            f"{[qid for qid in assigned_qids if assigned_qids.count(qid) > 1]}"
        )

    @given(data=resolver_inputs_strategy())
    @settings(deadline=None)
    def test_confidence_scores_in_valid_range(
        self, data: tuple[dict, dict[str, dict], dict]
    ) -> None:
        """All confidence scores in the assignment must be integers in [0, 100]."""
        triage, analysis_outputs, collector_output = data
        resolver = AssignmentResolver()
        assignment = resolver.resolve(triage, analysis_outputs, collector_output)

        for qa in assignment.query_assignments:
            assert isinstance(qa.confidence, int)
            assert (
                0 <= qa.confidence <= 100
            ), f"Confidence {qa.confidence} out of range for {qa.query_id}"


# ---------------------------------------------------------------------------
# Property 3: Co-Dependency Integrity
# ---------------------------------------------------------------------------


class TestCoDependencyIntegrity:
    """**Validates: Requirements 2.3, 2.5**

    Property 3: all queries in a co-dependency group assigned to same engine,
    or warning exists.
    """

    @given(data=resolver_inputs_strategy())
    @settings(deadline=None)
    def test_co_dependency_groups_assigned_to_same_engine(
        self, data: tuple[dict, dict[str, dict], dict]
    ) -> None:
        """For each co-dependency group in the assignment, all queries in the
        group must be assigned to the same engine (since the resolver assigns
        them atomically)."""
        triage, analysis_outputs, collector_output = data
        resolver = AssignmentResolver()
        assignment = resolver.resolve(triage, analysis_outputs, collector_output)

        # Build lookup: query_id → assigned_engine
        qid_to_engine = {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}

        for group in assignment.co_dependency_groups:
            engines_in_group = {qid_to_engine[qid] for qid in group if qid in qid_to_engine}
            # The resolver assigns co-dep groups atomically, so all should
            # be on the same engine. If not, a validation warning must exist.
            if len(engines_in_group) > 1:
                # Check that a warning exists for this split
                group_set = set(group)
                has_warning = any(
                    any(qid in w for qid in group_set) for w in assignment.validation_warnings
                )
                assert has_warning, (
                    f"Co-dependency group {group} split across engines "
                    f"{engines_in_group} without a validation warning"
                )


# ---------------------------------------------------------------------------
# Property 17: Co-Dependency Significance Filter
# ---------------------------------------------------------------------------


class TestCoDependencySignificanceFilter:
    """**Validates: Requirements 2.3**

    Property 17: non-significant JOINs do not contribute to co-dependency grouping.
    """

    @given(
        table_ids=st.lists(_table_id, min_size=2, max_size=4, unique=True),
        data=st.data(),
    )
    @settings(deadline=None)
    def test_non_significant_joins_do_not_group(
        self, table_ids: list[str], data: st.DataObject
    ) -> None:
        """Queries with only non-significant JOINs on shared tables must NOT
        be placed in the same co-dependency group."""
        # Generate 2+ queries that all have non-significant joins
        n_queries = data.draw(st.integers(min_value=2, max_value=5))
        queries = []
        used_qids: set[str] = set()
        for _ in range(n_queries):
            q = data.draw(query_strategy(table_ids, force_non_significant=True))
            while q["query_id"] in used_qids:
                q = data.draw(query_strategy(table_ids, force_non_significant=True))
            used_qids.add(q["query_id"])
            queries.append(q)

        tables = [{"table_id": tid, "table_name": tid.split(".")[-1]} for tid in table_ids]
        groups = build_co_dependency_groups(queries, tables)

        # With only non-significant joins, no co-dependency groups should form
        assert groups == [], f"Non-significant JOINs produced co-dependency groups: {groups}"

    @given(
        table_ids=st.lists(_table_id, min_size=1, max_size=3, unique=True),
        data=st.data(),
    )
    @settings(deadline=None)
    def test_is_significant_join_criteria(self, table_ids: list[str], data: st.DataObject) -> None:
        """is_significant_join returns False when join_count < 2,
        has_aggregation is False, and table not in filter_tables."""
        table = data.draw(st.sampled_from(table_ids))
        query = {
            "join_count": data.draw(st.sampled_from([0, 1])),
            "has_aggregation": False,
            "filter_tables": [],
        }
        assert not is_significant_join(
            query, table
        ), f"Expected non-significant join for query={query}, table={table}"

    @given(
        table_ids=st.lists(_table_id, min_size=1, max_size=3, unique=True),
        data=st.data(),
    )
    @settings(deadline=None)
    def test_significant_join_detected(self, table_ids: list[str], data: st.DataObject) -> None:
        """is_significant_join returns True when at least one criterion is met."""
        table = data.draw(st.sampled_from(table_ids))
        # Pick at least one significance criterion
        criterion = data.draw(st.sampled_from(["join_count", "aggregation", "filter_table"]))

        if criterion == "join_count":
            query = {
                "join_count": data.draw(st.integers(min_value=2, max_value=10)),
                "has_aggregation": False,
                "filter_tables": [],
            }
        elif criterion == "aggregation":
            query = {
                "join_count": 0,
                "has_aggregation": True,
                "filter_tables": [],
            }
        else:  # filter_table
            query = {
                "join_count": 0,
                "has_aggregation": False,
                "filter_tables": [table],
            }

        assert is_significant_join(
            query, table
        ), f"Expected significant join for criterion={criterion}, query={query}, table={table}"
