"""``load_synthesis_data`` loads the pre-Reality-Check assignment (#335).

A risk check needs to tell "Reality Check moved this query to engine E" apart
from "this query was simply assigned to E by the initial resolver" -- the two
look identical in the effective assignment alone. ``SynthesisData.
pre_reality_check_assignment`` is the version Reality Check started from,
resolved via ``resolve_reality_check_input_version`` (``src/storage/
assignment_versioning.py``), loaded here once so every downstream risk check
can diff against it without re-reading the store.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import load_synthesis_data
from src.storage.local_store import LocalArtifactStore

DB = "wordpress"
JOB = "job-pre-rc"


def _seed_triage_and_collector(store: LocalArtifactStore) -> None:
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": "dynamodb"}]},
    )
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"queries": {"query_patterns": []}})
    store.write_json(f"{DB}/{JOB}/analysis-dynamodb/analysis.json", {"workload_analysis": {}})


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(base_dir=str(tmp_path))


def test_reality_check_output_marks_v2_as_reality_check_produced(store) -> None:
    """v1 (the assignment resolver's output) -> v2 (Reality Check's output,
    ``source: "reality_check"``, ADR-028) is the real-world shape every job
    writes."""
    _seed_triage_and_collector(store)
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "opensearch", "in_scope": True},
                {"query_id": "q2", "assigned_engine": "dynamodb", "in_scope": True},
            ],
        },
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v2/assignment.json",
        {
            "version": 2,
            "previous_version": 1,
            "source": "reality_check",
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True},
                {"query_id": "q2", "assigned_engine": "dynamodb", "in_scope": True},
            ],
        },
    )
    data = load_synthesis_data(store, JOB, DB, assignment_version=2)
    assert data.pre_reality_check_assignment is not None
    before = {
        qa["query_id"]: qa["assigned_engine"]
        for qa in data.pre_reality_check_assignment["query_assignments"]
    }
    assert before == {"q1": "opensearch", "q2": "dynamodb"}


def test_no_prior_version_leaves_it_none(store) -> None:
    """Only v1 exists (a fresh job, Reality Check never ran) -- nothing to diff
    against, so this is ``None``, not a copy of the same version."""
    _seed_triage_and_collector(store)
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True}
            ],
        },
    )
    data = load_synthesis_data(store, JOB, DB, assignment_version=1)
    assert data.pre_reality_check_assignment is None


def test_unversioned_load_leaves_it_none(store) -> None:
    """``assignment_version=0`` (unversioned/legacy load) never resolves a
    Reality Check lineage -- fail open to None, not an error."""
    _seed_triage_and_collector(store)
    data = load_synthesis_data(store, JOB, DB, assignment_version=0)
    assert data.pre_reality_check_assignment is None
