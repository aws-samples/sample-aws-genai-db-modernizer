"""Shared downstream helpers in assignment_versioning (ADR-028).

Schema design and synthesis must read the effective assignment (v2 when
Reality Check consolidated) on every path: ATX, LocalOrchestrator, and the
local scripts the Claude Code skills drive. These pin the two helpers all of
them share.
"""

from __future__ import annotations

from src.storage.assignment_versioning import (
    engines_with_in_scope_queries,
    resolve_downstream_assignment_version,
    synthesis_report_candidates,
)

DB, JOB = "db", "job"


class _MemStore:
    def __init__(self) -> None:
        self.o: dict[str, dict] = {}

    def write_json(self, path: str, data: dict) -> None:
        self.o[path] = data

    def read_json(self, path: str) -> dict:
        return self.o[path]

    def exists(self, path: str) -> bool:
        return path in self.o

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.o if k.startswith(prefix)]


def _write(store: _MemStore, version: int, qas: list[tuple[str, str, bool]]) -> None:
    store.write_json(
        f"{DB}/{JOB}/assignment/v{version}/assignment.json",
        {
            "query_assignments": [
                {"query_id": q, "assigned_engine": e, "in_scope": s} for q, e, s in qas
            ]
        },
    )


class TestResolveDownstreamAssignmentVersion:
    def test_returns_latest_version(self) -> None:
        store = _MemStore()
        _write(store, 1, [("q1", "dynamodb", True)])
        _write(store, 2, [("q1", "dynamodb", True)])
        assert resolve_downstream_assignment_version(store, DB, JOB) == 2

    def test_coerces_missing_assignment_to_v1(self) -> None:
        assert resolve_downstream_assignment_version(_MemStore(), DB, JOB) == 1


class TestEnginesWithInScopeQueries:
    def test_consolidated_engine_is_dropped(self) -> None:
        store = _MemStore()
        _write(store, 1, [("q1", "dynamodb", True), ("q2", "documentdb", True)])
        _write(store, 2, [("q1", "dynamodb", True), ("q2", "dynamodb", True)])
        assert engines_with_in_scope_queries(store, DB, JOB, 1) == {"dynamodb", "documentdb"}
        assert engines_with_in_scope_queries(store, DB, JOB, 2) == {"dynamodb"}

    def test_out_of_scope_queries_do_not_count(self) -> None:
        store = _MemStore()
        _write(store, 1, [("q1", "dynamodb", True), ("q2", "opensearch", False)])
        assert engines_with_in_scope_queries(store, DB, JOB, 1) == {"dynamodb"}

    def test_missing_version_is_empty(self) -> None:
        assert engines_with_in_scope_queries(_MemStore(), DB, JOB, 3) == set()


class TestSynthesisReportCandidates:
    def test_versioned_report_comes_first(self) -> None:
        store = _MemStore()
        _write(store, 1, [("q1", "dynamodb", True)])
        _write(store, 2, [("q1", "dynamodb", True)])
        assert synthesis_report_candidates(store, DB, JOB) == [
            f"{DB}/{JOB}/synthesis/v2/report.json",
            f"{DB}/{JOB}/referee-synthesis/report.json",
            f"{DB}/{JOB}/synthesis/report.json",
        ]

    def test_no_assignment_falls_back_to_legacy(self) -> None:
        assert synthesis_report_candidates(_MemStore(), DB, JOB) == [
            f"{DB}/{JOB}/referee-synthesis/report.json",
            f"{DB}/{JOB}/synthesis/report.json",
        ]
