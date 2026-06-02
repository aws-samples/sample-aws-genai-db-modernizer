"""
Property-based tests for the Assignment Validator.

Tests correctness properties from the design document:
- Property 15: Validation Purity — same inputs always produce same output
  (deterministic, no side effects)

**Validates: Requirement 3.4**
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.assignment_validator import AssignmentValidator
from src.contracts.assignment_models import Assignment

# ---------------------------------------------------------------------------
# Strategies (reuse patterns from test_assignment_resolver_properties)
# ---------------------------------------------------------------------------

_engine = st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb"])
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,8}", fullmatch=True)
_query_id = st.from_regex(r"q-[0-9]{6}", fullmatch=True)


@st.composite
def analysis_table_rec(draw: st.DrawFn, table_id: str) -> dict:
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
    recs = [draw(analysis_table_rec(tid)) for tid in table_ids]
    return {
        "table_recommendations": recs,
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


@st.composite
def query_strategy(draw: st.DrawFn, table_ids: list[str]) -> dict:
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
def validator_inputs_strategy(
    draw: st.DrawFn,
) -> tuple[Assignment, dict, dict[str, dict]]:
    """Generate (assignment, collector_output, analysis_outputs) for the validator.

    Uses the AssignmentResolver to produce a valid assignment from generated
    collector and analysis data, ensuring realistic inputs.
    """
    n_tables = draw(st.integers(min_value=1, max_value=5))
    table_ids = draw(st.lists(_table_id, min_size=n_tables, max_size=n_tables, unique=True))

    n_queries = draw(st.integers(min_value=1, max_value=8))
    queries: list[dict] = []
    used_qids: set[str] = set()
    for _ in range(n_queries):
        q = draw(query_strategy(table_ids))
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

    n_engines = draw(st.integers(min_value=1, max_value=3))
    engines = draw(st.lists(_engine, min_size=n_engines, max_size=n_engines, unique=True))
    analysis_outputs: dict[str, dict] = {}
    for engine in engines:
        analysis_outputs[engine] = draw(analysis_output_strategy(table_ids))

    triage = {"selected": {e: ["signal"] for e in engines}}

    # Use the resolver to produce a realistic assignment
    resolver = AssignmentResolver()
    assignment = resolver.resolve(triage, analysis_outputs, collector_output)

    return assignment, collector_output, analysis_outputs


# ---------------------------------------------------------------------------
# Property 15: Validation Purity
# ---------------------------------------------------------------------------


class TestValidationPurity:
    """**Validates: Requirement 3.4**

    Property 15: Validation Purity — same inputs always produce same output
    (deterministic, no side effects).
    """

    @given(data=validator_inputs_strategy())
    @settings(deadline=None)
    def test_same_inputs_produce_same_output(
        self,
        data: tuple[Assignment, dict, dict[str, dict]],
    ) -> None:
        """Calling validate twice with identical inputs must return
        identical results — the function is pure with no side effects."""
        assignment, collector_output, analysis_outputs = data

        validator = AssignmentValidator()

        # Deep-copy inputs to ensure the validator doesn't mutate them
        assignment_copy = copy.deepcopy(assignment)
        collector_copy = copy.deepcopy(collector_output)
        analysis_copy = copy.deepcopy(analysis_outputs)

        result1 = validator.validate(assignment, collector_output, analysis_outputs)
        result2 = validator.validate(assignment_copy, collector_copy, analysis_copy)

        assert (
            result1.valid == result2.valid
        ), f"Purity violation: valid differs ({result1.valid} vs {result2.valid})"
        assert result1.warnings == result2.warnings, (
            f"Purity violation: warnings differ " f"({result1.warnings} vs {result2.warnings})"
        )
        assert result1.errors == result2.errors, (
            f"Purity violation: errors differ " f"({result1.errors} vs {result2.errors})"
        )

    @given(data=validator_inputs_strategy())
    @settings(deadline=None)
    def test_inputs_not_mutated(
        self,
        data: tuple[Assignment, dict, dict[str, dict]],
    ) -> None:
        """The validator must not mutate any of its inputs."""
        assignment, collector_output, analysis_outputs = data

        # Snapshot inputs before validation
        assignment_before = copy.deepcopy(assignment)
        collector_before = copy.deepcopy(collector_output)
        analysis_before = copy.deepcopy(analysis_outputs)

        validator = AssignmentValidator()
        validator.validate(assignment, collector_output, analysis_outputs)

        # Verify inputs unchanged
        assert (
            assignment.model_dump() == assignment_before.model_dump()
        ), "Purity violation: assignment was mutated"
        assert (
            collector_output == collector_before
        ), "Purity violation: collector_output was mutated"
        assert analysis_outputs == analysis_before, "Purity violation: analysis_outputs was mutated"
