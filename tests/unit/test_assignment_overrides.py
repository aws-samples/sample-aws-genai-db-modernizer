"""Shared customer-override write path (ADR-028).

Locks the one helper both the web route and the ATX gate call: it must bump the
version, stamp status=customer_modified + source=customer_gate + previous_version,
mark reassigned queries customer_override, honor table-scope narrowing, compute
skipped engines, and fail loudly (without writing) on unknown queries, a missing
assignment, or a hard validation error.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.agents.referee.assignment_overrides import (
    AssignmentValidationFailed,
    NoAssignmentFound,
    QueryOverrideInput,
    UnknownQuery,
    apply_assignment_overrides,
)
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)


class _MemStore:
    """In-memory ArtifactStore-compatible store (read_json/write_json/list_prefix/exists)."""

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}

    def read_json(self, path: str) -> dict:
        return self.objects[path]

    def write_json(self, path: str, data: dict) -> None:
        self.objects[path] = data

    def exists(self, path: str) -> bool:
        return path in self.objects

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


DB = "discourse"
JOB = "job-1"


def _qa(query_id: str, engine: str, tables: list[str], in_scope: bool = True) -> QueryAssignment:
    return QueryAssignment(
        query_id=query_id,
        assigned_engine=engine,
        confidence=80,
        source_tables=tables,
        assignment_reason="test",
        in_scope=in_scope,
    )


def _seed(store: _MemStore, query_assignments: list[QueryAssignment], engines: list[str]) -> None:
    """Seed v1 assignment + the collector/analysis the validator reads."""
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=query_assignments,
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    # Collector: one query_pattern per assignment (single-table so no co-dep groups form).
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {"query_id": qa.query_id, "tables_accessed": qa.source_tables}
                    for qa in query_assignments
                ]
            },
            "database_schema": {"tables": []},
        },
    )
    for engine in engines:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})


def _store_with_two_ddb_queries() -> _MemStore:
    store = _MemStore()
    _seed(
        store,
        [_qa("q1", "dynamodb", ["t.users"]), _qa("q2", "dynamodb", ["t.posts"])],
        engines=["dynamodb", "opensearch"],
    )
    return store


class TestApplyAssignmentOverrides:
    def test_engine_reassignment_writes_v2_with_provenance(self) -> None:
        store = _store_with_two_ddb_queries()
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="opensearch")]
        )

        assert result.assignment.version == 2
        assert result.assignment.status is AssignmentStatus.CUSTOMER_MODIFIED
        assert result.assignment.source is AssignmentSource.CUSTOMER_GATE
        assert result.assignment.previous_version == 1
        assert result.written_path == f"{DB}/{JOB}/assignment/v2/assignment.json"
        assert store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
        # v1 is untouched.
        assert store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")["version"] == 1

        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q2"].assigned_engine == "opensearch"
        assert by_id["q2"].customer_override is True
        assert by_id["q1"].customer_override is False  # untouched

    def test_in_scope_toggle_is_not_an_engine_override(self) -> None:
        store = _store_with_two_ddb_queries()
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", in_scope=False)]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q1"].in_scope is False
        assert by_id["q1"].customer_override is False

    def test_skipped_engines_reports_zero_in_scope(self) -> None:
        # Drop the only opensearch query out of scope -> opensearch is skipped.
        store = _MemStore()
        _seed(
            store,
            [_qa("q1", "dynamodb", ["t.users"]), _qa("q2", "opensearch", ["t.docs"])],
            engines=["dynamodb", "opensearch"],
        )
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", in_scope=False)]
        )
        assert result.skipped_engines == ["opensearch"]

    def test_exclude_tables_drops_query_out_of_scope(self) -> None:
        store = _store_with_two_ddb_queries()
        result = apply_assignment_overrides(store, DB, JOB, [], exclude_tables=["t.posts"])
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q2"].in_scope is False  # only accesses the excluded table
        assert by_id["q1"].in_scope is True

    def test_unknown_query_raises_and_writes_nothing(self) -> None:
        store = _store_with_two_ddb_queries()
        with pytest.raises(UnknownQuery):
            apply_assignment_overrides(store, DB, JOB, [QueryOverrideInput("nope", in_scope=False)])
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_no_assignment_raises(self) -> None:
        with pytest.raises(NoAssignmentFound):
            apply_assignment_overrides(_MemStore(), DB, JOB, [])

    def test_hard_validation_error_raises_and_writes_nothing(self) -> None:
        # Reassign to an engine that never analyzed the query -> hard error.
        store = _store_with_two_ddb_queries()
        with pytest.raises(AssignmentValidationFailed) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="neptune")]
            )
        assert exc.value.errors
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


class TestMarkCustomerApproved:
    """mark_assignment_customer_approved stamps the artifact for approve-as-is."""

    def test_stamps_customer_approved_in_place_without_new_version(self) -> None:
        from src.agents.referee.assignment_overrides import mark_assignment_customer_approved

        store = _store_with_two_ddb_queries()
        updated = mark_assignment_customer_approved(store, DB, JOB)

        assert updated is not None
        assert updated.status is AssignmentStatus.CUSTOMER_APPROVED
        # Same version, rewritten in place — no v2 is created (staleness-safe).
        assert updated.version == 1
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
        persisted = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        assert persisted["status"] == AssignmentStatus.CUSTOMER_APPROVED.value

    def test_leaves_customer_modified_untouched(self) -> None:
        from src.agents.referee.assignment_overrides import mark_assignment_customer_approved

        store = _store_with_two_ddb_queries()
        # Apply an edit first -> v2 customer_modified becomes effective.
        apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="opensearch")]
        )
        updated = mark_assignment_customer_approved(store, DB, JOB)

        assert updated is not None
        assert updated.status is AssignmentStatus.CUSTOMER_MODIFIED  # not downgraded to approved

    def test_none_when_no_assignment(self) -> None:
        from src.agents.referee.assignment_overrides import mark_assignment_customer_approved

        assert mark_assignment_customer_approved(_MemStore(), DB, JOB) is None
