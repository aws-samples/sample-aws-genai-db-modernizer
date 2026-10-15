"""Synthesis records, per consolidation, which queries it moved and which a
later edit put back (#861), so the facts layer can tell an undone
consolidation from one that still applies."""

from __future__ import annotations

from src.agents.referee.synthesis_handler import consolidation_query_moves
from src.storage.assignment_versioning import assignment_artifact_path
from src.storage.local_store import LocalArtifactStore

DB, JOB = "db", "job"


def _version(store: LocalArtifactStore, version: int, engines: dict[str, str]) -> dict:
    doc = {
        "version": version,
        "query_assignments": [{"query_id": q, "assigned_engine": e} for q, e in engines.items()],
    }
    store.write_json(assignment_artifact_path(DB, JOB, version), doc)
    return doc


def test_moved_and_moved_back_per_consolidation(tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _version(store, 1, {"p": "documentdb", "a": "documentdb", "b": "documentdb", "c": "documentdb"})
    _version(store, 2, {"p": "documentdb", "a": "aurora_mysql", "b": "dynamodb", "c": "dynamodb"})
    effective = _version(
        store, 3, {"p": "documentdb", "a": "aurora_mysql", "b": "documentdb", "c": "dynamodb"}
    )
    rc_output: dict = {
        "source_assignment_version": 1,
        "output_assignment_version": 2,
        "consolidations": [
            {"from_engine": "documentdb", "to_engine": "aurora_mysql", "action": "partial"},
            {"from_engine": "documentdb", "to_engine": "dynamodb", "action": "partial"},
        ],
    }

    records = consolidation_query_moves(store, DB, JOB, rc_output, effective)

    assert [(r["moved_query_ids"], r["moved_back_query_ids"]) for r in records] == [
        (["a"], []),
        (["b", "c"], ["b"]),
    ]
    # The reality check's own output is not edited.
    assert "moved_query_ids" not in rc_output["consolidations"][0]


def test_without_a_revision_nothing_is_recorded(tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    effective = _version(store, 1, {"a": "dynamodb"})
    rc_output: dict = {
        "source_assignment_version": 1,
        "output_assignment_version": None,
        "consolidations": [{"from_engine": "documentdb", "to_engine": "dynamodb"}],
    }
    records = consolidation_query_moves(store, DB, JOB, rc_output, effective)
    assert records == [{"from_engine": "documentdb", "to_engine": "dynamodb"}]
