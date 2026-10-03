"""Reality Check input/output assignment versions (issue #189).

Reality Check consolidates the newest assignment version it did not produce
itself and writes its revision to ``next_assignment_version()``, so it never
overwrites an existing version. These tests drive every entry point that runs it
(the handler, ``run_assessment``'s phase/finalize/resume path,
``run_reality_check.py``, and the ATX core) against a real local store, with the
consolidation logic stubbed to "move every documentdb query to dynamodb".
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from src.agents.referee.reality_check_handler import run_reality_check_handler
from src.storage.assignment_versioning import (
    assignment_artifact_path,
    resolve_downstream_assignment_version,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-1"


def _qa(query_id: str, engine: str) -> dict:
    return {
        "query_id": query_id,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": ["users"],
        "assignment_reason": "fixture",
    }


def _assignment(version: int, doc_engine: str = "documentdb", **extra: object) -> dict:
    return {
        "job_id": JOB,
        "version": version,
        "status": "auto_generated",
        "timestamp": "2026-10-01T00:00:00+00:00",
        "query_assignments": [_qa("DDB-AP-1", "dynamodb"), _qa("DOC-AP-1", doc_engine)],
        "table_assignments": [],
        "co_dependency_groups": [],
        "validation_warnings": [],
        **extra,
    }


def _fake_reality_check(assignment, triage, analysis, collector, caps) -> dict:
    """Consolidate documentdb into dynamodb; no-op when no documentdb query remains."""
    moved = [qa for qa in assignment["query_assignments"] if qa["assigned_engine"] == "documentdb"]
    revised = [
        {**qa, "assigned_engine": "dynamodb"} if qa["assigned_engine"] == "documentdb" else qa
        for qa in assignment["query_assignments"]
    ]
    consolidations = (
        [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": len(moved),
                "reason": "no unique value",
                "saved_cost_estimate": 100,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        ]
        if moved
        else []
    )
    return {
        "consolidations": consolidations,
        "recommendations": ["fixture"],
        "architectural_patterns": [],
        "unique_value_assessment": {},
        "revised_assignments": revised,
        "lightweight_recommendations": [],
    }


@pytest.fixture(autouse=True)
def _stub_consolidation():
    with patch(
        "src.agents.referee.reality_check_handler.run_reality_check",
        side_effect=_fake_reality_check,
    ):
        yield


@pytest.fixture()
def store(tmp_path) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    prefix = f"{DB}/{JOB}"
    s.write_json(
        f"{prefix}/referee-triage/triage.json",
        {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
            "signals": [],
            "query_capabilities": {},
        },
    )
    s.write_json(
        f"{prefix}/collector/output.json",
        {"database_schema": {"tables": []}, "queries": {"query_patterns": []}},
    )
    s.write_json(_path(1), _assignment(1, source="assignment_resolution"))
    return s


def _path(version: int) -> str:
    return assignment_artifact_path(DB, JOB, version)


def _rc_output(store: LocalArtifactStore) -> dict:
    return store.read_json(f"{DB}/{JOB}/reality-check/output.json")


def _reenter(store: LocalArtifactStore, version: int) -> None:
    """Simulate a customer-gate re-entry that routes DOC-AP-1 back to documentdb."""
    store.write_json(
        _path(version),
        _assignment(
            version,
            source="customer_gate",
            previous_version=version - 1,
            reality_check_applied=True,
        ),
    )


def _run(store: LocalArtifactStore, **kwargs) -> dict:
    return run_reality_check_handler(JOB, DB, store, llm_mode="none", **kwargs)


# =============================================================================
# Handler


class TestHandlerVersions:
    def test_fresh_job_reads_v1_writes_v2(self, store) -> None:
        summary = _run(store)

        assert summary["input_version"] == 1
        assert summary["output_version"] == 2
        v2 = store.read_json(_path(2))
        assert v2["source"] == "reality_check"
        assert v2["previous_version"] == 1
        assert _rc_output(store)["source_assignment_version"] == 1
        assert _rc_output(store)["output_assignment_version"] == 2

    def test_reentry_consolidates_new_version_without_touching_v2(self, store) -> None:
        _run(store)
        v2_before = copy.deepcopy(store.read_json(_path(2)))
        _reenter(store, 3)

        summary = _run(store)

        assert summary["input_version"] == 3
        assert summary["output_version"] == 4
        assert _rc_output(store)["before_distribution"] == {"dynamodb": 1, "documentdb": 1}
        v4 = store.read_json(_path(4))
        assert v4["previous_version"] == 3
        assert v4["source"] == "reality_check"
        assert store.read_json(_path(2)) == v2_before
        # Downstream (schema design, synthesis, report) reads the newest consolidation.
        assert resolve_downstream_assignment_version(store, DB, JOB) == 4

    def test_rerun_without_new_assignment_is_a_noop(self, store) -> None:
        _run(store)
        _reenter(store, 3)
        _run(store)
        output_before = copy.deepcopy(_rc_output(store))
        v4_before = copy.deepcopy(store.read_json(_path(4)))

        summary = _run(store)

        assert summary["status"] == "skipped"
        assert summary["input_version"] == 3
        assert summary["output_version"] == 4
        assert not store.exists(_path(5))
        assert store.read_json(_path(4)) == v4_before
        assert _rc_output(store) == output_before

    def test_explicit_input_version_forces_rerun_into_next_version(self, store) -> None:
        _run(store)

        summary = _run(store, assignment_version=1)

        assert summary["status"] == "complete"
        assert summary["output_version"] == 3
        assert store.read_json(_path(3))["previous_version"] == 1

    def test_no_consolidation_writes_no_version(self, store) -> None:
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))

        summary = _run(store)

        assert summary["output_version"] is None
        assert not store.exists(_path(2))
        assert _rc_output(store)["output_assignment_version"] is None

    def test_external_mode_defers_the_revision_to_finalize(self, store) -> None:
        _run_external = run_reality_check_handler(JOB, DB, store, llm_mode="external")

        assert _run_external["status"] == "awaiting_llm"
        assert not store.exists(_path(2))


# =============================================================================
# scripts/run_assessment.py (phase, finalize, --resume-reality-check)


class TestRunAssessment:
    def test_phase_reality_check_resolves_input(self, store) -> None:
        from scripts.run_assessment import phase_reality_check

        _run(store)
        _reenter(store, 3)

        phase_reality_check(store, JOB, DB, "none")

        assert store.read_json(_path(4))["previous_version"] == 3

    def test_resume_finalize_uses_same_resolution(self, store) -> None:
        from scripts.run_assessment import phase_reality_check, phase_reality_check_finalize

        _run(store)
        _reenter(store, 3)
        assert phase_reality_check(store, JOB, DB, "external") == "awaiting_llm"
        store.write_json(f"{DB}/{JOB}/llm_responses/reality_check.json", {})

        # Called exactly as the --resume-reality-check path calls it.
        phase_reality_check_finalize(store, JOB, DB)

        assert _rc_output(store)["source_assignment_version"] == 3
        assert store.read_json(_path(4))["previous_version"] == 3
        assert store.read_json(_path(2))["previous_version"] == 1

    def test_resume_finalize_twice_does_not_duplicate(self, store) -> None:
        from scripts.run_assessment import phase_reality_check_finalize

        store.write_json(f"{DB}/{JOB}/llm_responses/reality_check.json", {})
        phase_reality_check_finalize(store, JOB, DB)
        phase_reality_check_finalize(store, JOB, DB)

        assert store.exists(_path(2))
        assert not store.exists(_path(3))


# =============================================================================
# scripts/run_reality_check.py


class TestRunRealityCheckScript:
    def test_standard_and_finalize_resolve_input(self, store, capsys) -> None:
        from scripts.run_reality_check import run_finalize, run_standard

        run_standard(store, JOB, DB, None, "none")
        _reenter(store, 3)
        store.write_json(f"{DB}/{JOB}/llm_responses/reality_check.json", {})
        run_finalize(store, JOB, DB, None)

        assert store.read_json(_path(2))["previous_version"] == 1
        assert store.read_json(_path(4))["previous_version"] == 3


# =============================================================================
# AWS Transform core


class TestAtxRealityCheckCore:
    def test_core_resolves_input_and_reports_effective_version(self, store, monkeypatch) -> None:
        from src.atx_orchestrator.core import run_reality_check_core

        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "none")
        _run(store)
        _reenter(store, 3)

        summary = run_reality_check_core(JOB, DB, store=store)

        assert summary["source_assignment_version"] == 3
        assert summary["effective_assignment_version"] == 4
        assert store.read_json(_path(4))["previous_version"] == 3
