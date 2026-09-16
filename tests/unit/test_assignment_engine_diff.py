"""assignment_engine_diff classifies a new assignment's engines (ADR-029 A)."""

from __future__ import annotations

from src.storage.assignment_versioning import assignment_engine_diff

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


def test_moved_query_marks_both_engines_affected() -> None:
    store = _MemStore()
    _write(
        store, 1, [("q1", "dynamodb", True), ("q2", "dynamodb", True), ("q3", "opensearch", True)]
    )
    _write(
        store, 2, [("q1", "dynamodb", True), ("q2", "opensearch", True), ("q3", "opensearch", True)]
    )
    diff = assignment_engine_diff(store, DB, JOB, 1, 2)
    assert diff == {"affected": ["dynamodb", "opensearch"], "unaffected": []}


def test_unchanged_engine_is_unaffected() -> None:
    store = _MemStore()
    _write(store, 1, [("q1", "dynamodb", True), ("q3", "opensearch", True)])
    _write(
        store, 2, [("q1", "dynamodb", True), ("q3", "opensearch", True), ("q4", "opensearch", True)]
    )
    diff = assignment_engine_diff(store, DB, JOB, 1, 2)
    assert diff == {"affected": ["opensearch"], "unaffected": ["dynamodb"]}


def test_eliminated_engine_appears_in_neither_list() -> None:
    store = _MemStore()
    _write(store, 1, [("q1", "dynamodb", True), ("q2", "documentdb", True)])
    _write(store, 2, [("q1", "dynamodb", True), ("q2", "dynamodb", True)])
    diff = assignment_engine_diff(store, DB, JOB, 1, 2)
    assert diff == {"affected": ["dynamodb"], "unaffected": []}
    assert "documentdb" not in diff["affected"] + diff["unaffected"]


def test_scope_flip_marks_engine_affected() -> None:
    store = _MemStore()
    _write(store, 1, [("q1", "dynamodb", True), ("q2", "dynamodb", True)])
    _write(store, 2, [("q1", "dynamodb", True), ("q2", "dynamodb", False)])
    diff = assignment_engine_diff(store, DB, JOB, 1, 2)
    assert diff == {"affected": ["dynamodb"], "unaffected": []}


def test_newly_added_engine_is_affected() -> None:
    store = _MemStore()
    _write(store, 1, [("q1", "dynamodb", True)])
    _write(store, 2, [("q1", "dynamodb", True), ("q2", "aurora_postgresql", True)])
    diff = assignment_engine_diff(store, DB, JOB, 1, 2)
    assert diff == {"affected": ["aurora_postgresql"], "unaffected": ["dynamodb"]}
