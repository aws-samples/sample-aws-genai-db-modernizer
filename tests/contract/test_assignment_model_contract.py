"""Contract coverage for the Assignment ``source`` provenance field (ADR-028).

The ``source`` field records which pipeline stage produced an assignment version.
It is optional and defaults to ``None`` so artifacts written before it existed
still load (backward compatibility), round-trips through JSON, and accepts the
declared provenance values. ``AssignmentSummary.source`` on the synthesis output
carries the same provenance downstream.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.contracts.assignment_models import Assignment, AssignmentSource, AssignmentStatus
from src.contracts.synthesis_output import AssignmentSummary


def _assignment(**overrides) -> dict:
    base = {
        "job_id": "job-1",
        "version": 1,
        "status": AssignmentStatus.AUTO_GENERATED.value,
        "timestamp": datetime.now(UTC).isoformat(),
        "query_assignments": [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "confidence": 80,
                "source_tables": ["t.users"],
                "assignment_reason": "key-value",
            }
        ],
        "table_assignments": [],
        "co_dependency_groups": [],
        "validation_warnings": [],
    }
    base.update(overrides)
    return base


class TestAssignmentSource:
    def test_source_defaults_to_none_when_absent(self) -> None:
        """A legacy artifact written before the field existed still loads."""
        a = Assignment.model_validate(_assignment())
        assert a.source is None

    def test_accepts_each_provenance_value(self) -> None:
        for src in AssignmentSource:
            a = Assignment.model_validate(_assignment(source=src.value))
            assert a.source is src

    def test_round_trips_through_json(self) -> None:
        a = Assignment.model_validate(_assignment(source=AssignmentSource.CUSTOMER_GATE.value))
        dumped = a.model_dump(mode="json")
        assert dumped["source"] == "customer_gate"
        assert Assignment.model_validate(dumped).source is AssignmentSource.CUSTOMER_GATE

    def test_customer_approved_status_is_valid(self) -> None:
        """First real use of CUSTOMER_APPROVED (approve-as-is at the review gate)."""
        a = Assignment.model_validate(_assignment(status=AssignmentStatus.CUSTOMER_APPROVED.value))
        assert a.status is AssignmentStatus.CUSTOMER_APPROVED


class TestAssignmentSummarySource:
    def test_summary_source_optional_and_round_trips(self) -> None:
        base = {"version": 3, "query_count": 10, "in_scope_count": 8}
        s = AssignmentSummary.model_validate({**base, "source": "customer_gate"})
        assert s.source == "customer_gate"
        assert AssignmentSummary.model_validate(base).source is None
