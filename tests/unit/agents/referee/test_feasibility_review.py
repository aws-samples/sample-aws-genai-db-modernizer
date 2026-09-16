"""Post-gate feasibility reviewer (ADR-029 Layer C)."""

from __future__ import annotations

from datetime import UTC, datetime

from src.agents.referee.feasibility_review import review_assignment_feasibility
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.contracts.feasibility_models import FindingKind, FindingSeverity


def _qa(qid: str, engine: str, tables: list[str], in_scope: bool = True) -> QueryAssignment:
    return QueryAssignment(
        query_id=qid,
        assigned_engine=engine,
        confidence=80,
        source_tables=tables,
        assignment_reason="test",
        in_scope=in_scope,
    )


def _assignment(qas: list[QueryAssignment], codep: list[list[str]] | None = None) -> Assignment:
    return Assignment(
        job_id="j",
        version=2,
        status=AssignmentStatus.CUSTOMER_MODIFIED,
        source=AssignmentSource.CUSTOMER_GATE,
        timestamp=datetime.now(UTC),
        query_assignments=qas,
        table_assignments=[],
        co_dependency_groups=codep or [],
        validation_warnings=[],
    )


def _collector(patterns: list[dict]) -> dict:
    return {"queries": {"query_patterns": patterns}}


class TestReadWriteSplit:
    def test_reads_on_engine_without_writes_is_blocking(self) -> None:
        assignment = _assignment(
            [_qa("qw", "dynamodb", ["t.orders"]), _qa("qr", "opensearch", ["t.orders"])]
        )
        collector = _collector(
            [
                {"query_id": "qw", "query_type": "INSERT", "tables_accessed": ["t.orders"]},
                {"query_id": "qr", "query_type": "SELECT", "tables_accessed": ["t.orders"]},
            ]
        )
        findings = review_assignment_feasibility(assignment, collector)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind is FindingKind.READ_WRITE_SPLIT
        assert f.severity is FindingSeverity.BLOCKING
        assert f.table == "t.orders"
        assert f.engines == ["dynamodb", "opensearch"]

    def test_reads_colocated_with_writes_is_clean(self) -> None:
        assignment = _assignment(
            [_qa("qw", "dynamodb", ["t.orders"]), _qa("qr", "dynamodb", ["t.orders"])]
        )
        collector = _collector(
            [
                {"query_id": "qw", "query_type": "UPDATE", "tables_accessed": ["t.orders"]},
                {"query_id": "qr", "query_type": "SELECT", "tables_accessed": ["t.orders"]},
            ]
        )
        assert review_assignment_feasibility(assignment, collector) == []

    def test_read_only_table_has_no_finding(self) -> None:
        assignment = _assignment([_qa("qr", "opensearch", ["t.docs"])])
        collector = _collector(
            [{"query_id": "qr", "query_type": "SELECT", "tables_accessed": ["t.docs"]}]
        )
        assert review_assignment_feasibility(assignment, collector) == []

    def test_unclassifiable_query_type_is_not_a_write(self) -> None:
        # An OTHER-typed query on another engine must not count as a write, so the
        # table stays read-only and produces no split finding.
        assignment = _assignment([_qa("qo", "dynamodb", ["t.x"]), _qa("qr", "opensearch", ["t.x"])])
        collector = _collector(
            [
                {"query_id": "qo", "query_type": "OTHER", "tables_accessed": ["t.x"]},
                {"query_id": "qr", "query_type": "SELECT", "tables_accessed": ["t.x"]},
            ]
        )
        assert review_assignment_feasibility(assignment, collector) == []

    def test_out_of_scope_write_does_not_anchor_the_table(self) -> None:
        assignment = _assignment(
            [
                _qa("qw", "dynamodb", ["t.orders"], in_scope=False),
                _qa("qr", "opensearch", ["t.orders"]),
            ]
        )
        collector = _collector(
            [
                {"query_id": "qw", "query_type": "INSERT", "tables_accessed": ["t.orders"]},
                {"query_id": "qr", "query_type": "SELECT", "tables_accessed": ["t.orders"]},
            ]
        )
        assert review_assignment_feasibility(assignment, collector) == []


class TestCoDependencySplit:
    def test_split_onto_non_join_engine_is_blocking(self) -> None:
        assignment = _assignment(
            [_qa("q1", "aurora_postgresql", ["t.a"]), _qa("q2", "dynamodb", ["t.b"])],
            codep=[["q1", "q2"]],
        )
        findings = review_assignment_feasibility(assignment, _collector([]))
        assert len(findings) == 1
        assert findings[0].kind is FindingKind.CO_DEPENDENCY_SPLIT
        assert findings[0].severity is FindingSeverity.BLOCKING
        assert findings[0].query_ids == ["q1", "q2"]

    def test_split_across_join_capable_engines_is_advisory(self) -> None:
        assignment = _assignment(
            [_qa("q1", "aurora_postgresql", ["t.a"]), _qa("q2", "aurora_mysql", ["t.b"])],
            codep=[["q1", "q2"]],
        )
        findings = review_assignment_feasibility(assignment, _collector([]))
        assert len(findings) == 1
        assert findings[0].severity is FindingSeverity.ADVISORY

    def test_colocated_group_is_clean(self) -> None:
        assignment = _assignment(
            [_qa("q1", "dynamodb", ["t.a"]), _qa("q2", "dynamodb", ["t.b"])],
            codep=[["q1", "q2"]],
        )
        assert review_assignment_feasibility(assignment, _collector([])) == []


def test_blocking_findings_sort_before_advisory() -> None:
    assignment = _assignment(
        [
            _qa("qw", "dynamodb", ["t.orders"]),
            _qa("qr", "opensearch", ["t.orders"]),
            _qa("q1", "aurora_postgresql", ["t.a"]),
            _qa("q2", "aurora_mysql", ["t.b"]),
        ],
        codep=[["q1", "q2"]],  # advisory (both join-capable)
    )
    collector = _collector(
        [
            {"query_id": "qw", "query_type": "INSERT", "tables_accessed": ["t.orders"]},
            {"query_id": "qr", "query_type": "SELECT", "tables_accessed": ["t.orders"]},
        ]
    )
    findings = review_assignment_feasibility(assignment, collector)
    assert [f.severity for f in findings] == [FindingSeverity.BLOCKING, FindingSeverity.ADVISORY]
