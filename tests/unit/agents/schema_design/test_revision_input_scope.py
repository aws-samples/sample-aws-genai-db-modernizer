"""A schema revision's designer inputs are narrowed to the engine's scope (#1524)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.schema_design import handler, revision_handler
from src.contracts.schema_revision_models import NewPattern, SchemaRevisionRequest
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-1524"


def _store(tmp_path: Path, assigned: dict[str, str] | None) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "database_schema": {
                "tables": [
                    {"table_id": "shop.orders", "table_name": "orders"},
                    {"table_id": "shop.users", "table_name": "users"},
                ]
            },
            "queries": {
                "query_patterns": [
                    {"query_id": "q1", "tables_accessed": ["shop.orders"]},
                    {"query_id": "q2", "tables_accessed": ["shop.users"]},
                ]
            },
        },
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json",
        {
            "table_recommendations": [
                {"table_id": "shop.orders", "confidence_score": 50},
                {"table_id": "shop.users", "confidence_score": 50},
            ]
        },
    )
    if assigned is not None:
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json",
            {
                "query_assignments": [
                    {
                        "query_id": q,
                        "assigned_engine": engine,
                        "in_scope": True,
                        "source_tables": [t],
                    }
                    for (q, t), engine in zip(
                        (("q1", "shop.orders"), ("q2", "shop.users")),
                        (assigned["q1"], assigned["q2"]),
                        strict=True,
                    )
                ]
            },
        )
    return store


def _inputs(store: LocalArtifactStore, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    seen: dict = {}

    def fake_dispatch(target_type, collector_path, analysis_path, **_kw):
        seen["collector"] = json.loads(Path(collector_path).read_text())
        seen["analysis"] = json.loads(Path(analysis_path).read_text())
        return json.dumps({}), None

    monkeypatch.setattr(handler, "_dispatch_schema_agent", fake_dispatch)
    request = SchemaRevisionRequest(
        base_version=1,
        pattern_modifications=[],
        table_modifications=[],
        new_patterns=[NewPattern(description="d", target_engine="dynamodb", source_tables=[])],
    )
    revision_handler._dispatch_llm_redesign(JOB, DB, "dynamodb", {}, request, store)
    return seen["collector"], seen["analysis"]


def _ids(collector: dict, analysis: dict) -> tuple[list, list, list]:
    return (
        [t["table_id"] for t in collector["database_schema"]["tables"]],
        [q["query_id"] for q in collector["queries"]["query_patterns"]],
        [r["table_id"] for r in analysis["table_recommendations"]],
    )


def test_a_revision_holds_only_the_engines_tables_and_queries(tmp_path, monkeypatch):
    store = _store(tmp_path, {"q1": "dynamodb", "q2": "aurora_mysql"})
    assert _ids(*_inputs(store, monkeypatch)) == (["shop.orders"], ["q1"], ["shop.orders"])


def test_an_engine_with_nothing_assigned_gets_empty_inputs_not_the_whole_job(tmp_path, monkeypatch):
    store = _store(tmp_path, {"q1": "aurora_mysql", "q2": "aurora_mysql"})
    assert _ids(*_inputs(store, monkeypatch)) == ([], [], [])


@pytest.mark.passes_on_main
def test_a_job_without_an_assignment_is_unfiltered(tmp_path, monkeypatch):
    store = _store(tmp_path, None)
    assert _ids(*_inputs(store, monkeypatch)) == (
        ["shop.orders", "shop.users"],
        ["q1", "q2"],
        ["shop.orders", "shop.users"],
    )


def test_a_revision_uses_the_latest_assignment_version(tmp_path, monkeypatch):
    """After a reroute v2 moves q1 off the engine: the revision follows v2, not v1."""
    store = _store(tmp_path, {"q1": "dynamodb", "q2": "dynamodb"})
    v1 = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
    v2 = json.loads(json.dumps(v1))
    v2["query_assignments"][0]["assigned_engine"] = "aurora_mysql"
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", v2)
    assert _ids(*_inputs(store, monkeypatch)) == (["shop.users"], ["q2"], ["shop.users"])
