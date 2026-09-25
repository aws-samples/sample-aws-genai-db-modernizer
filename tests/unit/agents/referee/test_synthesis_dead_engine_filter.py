"""Synthesis drops consolidation-eliminated engines (ADR-029 Layer E).

An engine triage selected but that ends up with no in-scope query in the
effective assignment (consolidated away, or a customer re-route) must not reach
the ranking / table mappings / schema summaries. load_synthesis_data is the
single choke point, so filtering there keeps the eliminated engine out of every
downstream builder while the reality-check summary still explains the
consolidation.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import load_synthesis_data
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-synth-filter"


def _seed(store, *, selected: list[str], assignment: dict | None) -> None:
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in selected]},
    )
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"queries": {"query_patterns": []}})
    for engine in selected:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    if assignment is not None:
        store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", assignment)


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(base_dir=str(tmp_path))


def test_eliminated_engine_is_dropped_from_engines(store) -> None:
    # documentdb was selected+analyzed but consolidation moved its only query to
    # dynamodb, so the effective assignment routes nothing in-scope to it.
    assignment = {
        "version": 2,
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True},
            {"query_id": "q2", "assigned_engine": "dynamodb", "in_scope": True},
        ],
    }
    _seed(store, selected=["dynamodb", "documentdb"], assignment=assignment)
    data = load_synthesis_data(store, JOB, DB, assignment_version=2)
    assert set(data.engines) == {"dynamodb"}


def test_no_assignment_keeps_all_selected_engines(store) -> None:
    # Fail-open: with no assignment (version 0) nothing is filtered.
    _seed(store, selected=["dynamodb", "documentdb"], assignment=None)
    data = load_synthesis_data(store, JOB, DB, assignment_version=0)
    assert set(data.engines) == {"dynamodb", "documentdb"}


def test_out_of_scope_only_engine_is_dropped(store) -> None:
    # An engine whose every query is out of scope has no in-scope routing.
    assignment = {
        "version": 2,
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True},
            {"query_id": "q2", "assigned_engine": "opensearch", "in_scope": False},
        ],
    }
    _seed(store, selected=["dynamodb", "opensearch"], assignment=assignment)
    data = load_synthesis_data(store, JOB, DB, assignment_version=2)
    assert set(data.engines) == {"dynamodb"}
