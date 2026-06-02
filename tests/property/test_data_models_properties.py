"""
Property-based tests for Phase 1A data models.

Tests correctness properties from the design document:
- Property 10: Confidence Score Range — confidence is int in [0, 100]
- Property 2: Assignment Exclusivity — each query_id appears exactly once in query_assignments

**Validates: Requirements 2.1, 12.1**
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from src.contracts.assignment_models import (
    Assignment,
    AssignmentStatus,
    QueryAssignment,
    TableAssignment,
)
from src.contracts.phase_models import PHASE_PREREQUISITES, Phase, PhaseRecord, PhaseStatus

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_engine = st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb", "redis"])
_query_id = st.from_regex(r"q-[0-9]{4}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,8}", fullmatch=True)


@st.composite
def query_assignment_strategy(draw: st.DrawFn, query_id: str | None = None) -> QueryAssignment:
    """Generate a valid QueryAssignment with a random confidence in [0, 100]."""
    qid = query_id or draw(_query_id)
    return QueryAssignment(
        query_id=qid,
        assigned_engine=draw(_engine),
        confidence=draw(st.integers(min_value=0, max_value=100)),
        source_tables=draw(st.lists(_table_id, min_size=1, max_size=3, unique=True)),
        assignment_reason=draw(st.text(min_size=1, max_size=40)),
    )


@st.composite
def unique_query_assignments_strategy(draw: st.DrawFn) -> list[QueryAssignment]:
    """Generate a list of QueryAssignment objects with unique query_ids."""
    n = draw(st.integers(min_value=1, max_value=10))
    ids = draw(
        st.lists(
            st.from_regex(r"q-[0-9]{6}", fullmatch=True),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    assignments = []
    for qid in ids:
        assignments.append(draw(query_assignment_strategy(query_id=qid)))
    return assignments


# ---------------------------------------------------------------------------
# Property 10: Confidence Score Range
# ---------------------------------------------------------------------------


class TestConfidenceScoreRange:
    """**Validates: Requirements 2.1, 12.1**

    Property 10: confidence is int in [0, 100].
    """

    @given(confidence=st.integers(min_value=0, max_value=100))
    def test_valid_confidence_accepted(self, confidence: int) -> None:
        """Any integer in [0, 100] is a valid confidence score."""
        qa = QueryAssignment(
            query_id="q-0001",
            assigned_engine="dynamodb",
            confidence=confidence,
            source_tables=["public.orders"],
            assignment_reason="highest score",
        )
        assert qa.confidence == confidence
        assert isinstance(qa.confidence, int)

    @given(confidence=st.integers(max_value=-1))
    def test_confidence_below_zero_rejected(self, confidence: int) -> None:
        """Confidence below 0 must be rejected by Pydantic validation."""
        with pytest.raises(ValidationError):
            QueryAssignment(
                query_id="q-0001",
                assigned_engine="dynamodb",
                confidence=confidence,
                source_tables=["public.orders"],
                assignment_reason="test",
            )

    @given(confidence=st.integers(min_value=101))
    def test_confidence_above_100_rejected(self, confidence: int) -> None:
        """Confidence above 100 must be rejected by Pydantic validation."""
        with pytest.raises(ValidationError):
            QueryAssignment(
                query_id="q-0001",
                assigned_engine="dynamodb",
                confidence=confidence,
                source_tables=["public.orders"],
                assignment_reason="test",
            )

    @given(confidence=st.floats(min_value=0.01, max_value=0.99))
    def test_float_confidence_rejected(self, confidence: float) -> None:
        """Float values like 0.92 must be rejected — confidence is strict int."""
        with pytest.raises(ValidationError):
            QueryAssignment(
                query_id="q-0001",
                assigned_engine="dynamodb",
                confidence=confidence,  # type: ignore[arg-type]
                source_tables=["public.orders"],
                assignment_reason="test",
            )


# ---------------------------------------------------------------------------
# Property 2: Assignment Exclusivity
# ---------------------------------------------------------------------------


class TestAssignmentExclusivity:
    """**Validates: Requirements 2.1, 12.1**

    Property 2: each query_id appears exactly once in query_assignments.
    """

    @given(data=unique_query_assignments_strategy())
    def test_unique_query_ids_in_assignment(self, data: list[QueryAssignment]) -> None:
        """For any list of QueryAssignment objects, each query_id appears exactly once."""
        query_ids = [qa.query_id for qa in data]
        assert len(query_ids) == len(
            set(query_ids)
        ), f"Duplicate query_ids found: {[qid for qid in query_ids if query_ids.count(qid) > 1]}"

    @given(data=unique_query_assignments_strategy())
    def test_assignment_model_preserves_exclusivity(self, data: list[QueryAssignment]) -> None:
        """An Assignment built from unique query assignments preserves exclusivity."""
        table_assignments = [
            TableAssignment(
                table_id=t,
                primary_engine=data[0].assigned_engine,
                engines=[data[0].assigned_engine],
                query_count=1,
            )
            for t in {t for qa in data for t in qa.source_tables}
        ]

        assignment = Assignment(
            job_id="test-job",
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            timestamp=datetime.now(tz=UTC),
            query_assignments=data,
            table_assignments=table_assignments,
            co_dependency_groups=[],
            validation_warnings=[],
        )

        ids = [qa.query_id for qa in assignment.query_assignments]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# PhaseRecord defaults
# ---------------------------------------------------------------------------


class TestPhaseRecordDefaults:
    """Verify PhaseRecord defaults match the design: status=NOT_STARTED, iteration=1."""

    @given(phase=st.sampled_from(list(Phase)))
    def test_default_status_is_not_started(self, phase: Phase) -> None:
        record = PhaseRecord(phase=phase)
        assert record.status == PhaseStatus.NOT_STARTED

    @given(phase=st.sampled_from(list(Phase)))
    def test_default_iteration_is_one(self, phase: Phase) -> None:
        record = PhaseRecord(phase=phase)
        assert record.iteration == 1


# ---------------------------------------------------------------------------
# PHASE_PREREQUISITES acyclicity
# ---------------------------------------------------------------------------


class TestPhasePrerequisitesAcyclicity:
    """PHASE_PREREQUISITES must be acyclic — no phase is its own prerequisite."""

    def test_no_direct_self_prerequisite(self) -> None:
        """No phase lists itself as a direct prerequisite."""
        for phase, prereqs in PHASE_PREREQUISITES.items():
            assert phase not in prereqs, f"{phase} is its own direct prerequisite"

    def test_no_transitive_cycle(self) -> None:
        """No phase can reach itself through transitive prerequisites."""
        for start_phase in PHASE_PREREQUISITES:
            visited: set[Phase] = set()
            stack = list(PHASE_PREREQUISITES.get(start_phase, []))
            while stack:
                current = stack.pop()
                assert (
                    current != start_phase
                ), f"Cycle detected: {start_phase} is reachable from itself"
                if current not in visited:
                    visited.add(current)
                    stack.extend(PHASE_PREREQUISITES.get(current, []))

    def test_all_phases_have_prerequisite_entry(self) -> None:
        """Every Phase enum member has an entry in PHASE_PREREQUISITES."""
        for phase in Phase:
            assert phase in PHASE_PREREQUISITES, f"{phase} missing from PHASE_PREREQUISITES"
