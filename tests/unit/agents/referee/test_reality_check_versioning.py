"""Reality Check input/output assignment versions (issue #189).

Reality Check runs once per assignment lineage: it consolidates the newest
assignment version it did not produce itself, writes its revision to
``next_assignment_version()`` (never overwriting a version), skips when it
already ran for the lineage (including a customer edit made after it), and never
moves a ``customer_override`` query. These tests drive every entry point that
runs it (the handler, ``run_assessment``'s phase/finalize/resume path,
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
    for engine in ("dynamodb", "documentdb"):
        s.write_json(f"{prefix}/analysis-{engine}/analysis.json", {"signals": []})
    s.write_json(_path(1), _assignment(1, source="assignment_resolution"))
    return s


def _path(version: int) -> str:
    return assignment_artifact_path(DB, JOB, version)


def _rc_output(store: LocalArtifactStore) -> dict:
    return store.read_json(f"{DB}/{JOB}/reality-check/output.json")


def _customer_edit(store: LocalArtifactStore, version: int) -> None:
    """A customer-gate edit of v{version-1} that routes DOC-AP-1 back to documentdb."""
    doc = _assignment(
        version, source="customer_gate", previous_version=version - 1, reality_check_applied=True
    )
    doc["status"] = "customer_modified"
    doc["query_assignments"][1]["customer_override"] = True
    store.write_json(_path(version), doc)


def _re_resolve(store: LocalArtifactStore, version: int) -> None:
    """The assignment phase re-runs and writes a fresh resolution (new lineage)."""
    store.write_json(_path(version), _assignment(version, source="assignment_resolution"))


def _run(store: LocalArtifactStore, **kwargs) -> dict:
    return run_reality_check_handler(JOB, DB, store, llm_mode="none", **kwargs)


def _engine_of(store: LocalArtifactStore, version: int, query_id: str) -> str:
    qas = store.read_json(_path(version))["query_assignments"]
    return str(next(qa["assigned_engine"] for qa in qas if qa["query_id"] == query_id))


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

    def test_customer_edit_after_consolidation_is_not_reconsolidated(self, store) -> None:
        _run(store)
        _customer_edit(store, 3)
        output_before = copy.deepcopy(_rc_output(store))

        summary = _run(store)

        assert summary == {"status": "skipped", "input_version": 1, "output_version": 2}
        assert not store.exists(_path(4))
        assert _rc_output(store) == output_before
        # Downstream reads the customer's edit, with DOC-AP-1 where they put it.
        assert resolve_downstream_assignment_version(store, DB, JOB) == 3
        assert _engine_of(store, 3, "DOC-AP-1") == "documentdb"

    def test_re_resolution_is_consolidated_without_touching_v2(self, store) -> None:
        _run(store)
        v2_before = copy.deepcopy(store.read_json(_path(2)))
        _re_resolve(store, 3)

        summary = _run(store)

        assert summary["input_version"] == 3
        assert summary["output_version"] == 4
        assert _rc_output(store)["before_distribution"] == {"dynamodb": 1, "documentdb": 1}
        v4 = store.read_json(_path(4))
        assert v4["previous_version"] == 3
        assert v4["source"] == "reality_check"
        assert store.read_json(_path(2)) == v2_before
        assert resolve_downstream_assignment_version(store, DB, JOB) == 4

    def test_rerun_without_new_assignment_is_a_noop(self, store) -> None:
        _run(store)
        _re_resolve(store, 3)
        _run(store)
        output_before = copy.deepcopy(_rc_output(store))
        v4_before = copy.deepcopy(store.read_json(_path(4)))

        summary = _run(store)

        assert summary == {"status": "skipped", "input_version": 3, "output_version": 4}
        assert not store.exists(_path(5))
        assert store.read_json(_path(4)) == v4_before
        assert _rc_output(store) == output_before

    def test_customer_edit_after_a_run_that_consolidated_nothing(self, store) -> None:
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        _run(store)  # nothing to consolidate: no version written
        _customer_edit(store, 2)

        summary = _run(store)

        assert summary == {"status": "skipped", "input_version": 1, "output_version": None}
        assert not store.exists(_path(3))

    def test_explicit_input_version_forces_rerun_into_next_version(self, store) -> None:
        _run(store)

        summary = _run(store, assignment_version=1)

        assert summary["status"] == "complete"
        assert summary["output_version"] == 3
        assert store.read_json(_path(3))["previous_version"] == 1

    def test_explicit_run_never_moves_customer_override(self, store) -> None:
        _run(store)
        doc = _assignment(3, source="customer_gate", previous_version=2)
        doc["query_assignments"][1]["customer_override"] = True
        doc["query_assignments"].append(_qa("DOC-AP-2", "documentdb"))
        store.write_json(_path(3), doc)

        summary = _run(store, assignment_version=3)

        assert summary["output_version"] == 4
        assert _engine_of(store, 4, "DOC-AP-1") == "documentdb"  # customer's choice kept
        assert _engine_of(store, 4, "DOC-AP-2") == "dynamodb"  # still consolidated

    def test_sweep_cannot_move_customer_override(self, store) -> None:
        _run(store)
        _customer_edit(store, 3)

        def _sweep_everything(revised, consolidations, caps):
            moved = [{**qa, "assigned_engine": "dynamodb"} for qa in revised]
            return moved, consolidations or [
                {
                    "from_engine": "documentdb",
                    "to_engine": "dynamodb",
                    "query_count": 1,
                    "reason": "orphan",
                    "saved_cost_estimate": 0,
                    "action": "full",
                    "queries_retained": [],
                    "retention_reason": None,
                }
            ]

        with patch(
            "src.agents.referee.reality_check_handler.sanity_sweep", side_effect=_sweep_everything
        ):
            summary = _run(store, assignment_version=3)

        # Nothing moved once the override is restored, so the sweep's record is
        # reconciled away and no revision is written (#218).
        version = summary["output_version"] or summary["input_version"]
        assert _engine_of(store, version, "DOC-AP-1") == "documentdb"
        assert _rc_output(store)["after_distribution"] == {"dynamodb": 1, "documentdb": 1}
        assert _rc_output(store)["consolidations"] == []

    def test_sweep_editing_in_place_cannot_move_customer_override(self, store) -> None:
        """The real sweep edits dicts in place; the override query must not alias the input."""
        _run(store)
        _customer_edit(store, 3)

        def _sweep_in_place(revised, consolidations, caps):
            for qa in revised:
                qa["assigned_engine"] = "dynamodb"
            return revised, consolidations

        with patch(
            "src.agents.referee.reality_check_handler.sanity_sweep", side_effect=_sweep_in_place
        ):
            summary = _run(store, assignment_version=3)

        version = summary["output_version"] or summary["input_version"]
        assert _engine_of(store, version, "DOC-AP-1") == "documentdb"
        assert _rc_output(store)["after_distribution"] == {"dynamodb": 1, "documentdb": 1}

    def test_no_consolidation_writes_no_version(self, store) -> None:
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))

        summary = _run(store)

        assert summary["output_version"] is None
        assert not store.exists(_path(2))
        assert _rc_output(store)["output_assignment_version"] is None

    def test_external_mode_defers_the_revision_to_finalize(self, store) -> None:
        summary = run_reality_check_handler(JOB, DB, store, llm_mode="external")

        assert summary["status"] == "awaiting_llm"
        assert not store.exists(_path(2))


# =============================================================================
# Real writers: customer gate on top of a real Reality Check output


class TestCustomerGateSurvivesRealityCheck:
    def test_override_survives_default_and_explicit_runs(self, store) -> None:
        from src.agents.referee.assignment_overrides import (
            QueryOverrideInput,
            apply_assignment_overrides,
        )

        _run(store)  # v2: DOC-AP-1 consolidated into dynamodb
        assert _engine_of(store, 2, "DOC-AP-1") == "dynamodb"
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput(query_id="DOC-AP-1", assigned_engine="documentdb")]
        )
        gate_version = result.assignment.version
        assert gate_version == 3

        assert _run(store)["status"] == "skipped"
        assert not store.exists(_path(4))
        assert resolve_downstream_assignment_version(store, DB, JOB) == 3
        assert _engine_of(store, 3, "DOC-AP-1") == "documentdb"

        # Even a forced run leaves the overridden query alone: it is not a
        # consolidation candidate, so here there is nothing left to consolidate.
        explicit = _run(store, assignment_version=3)
        assert explicit["status"] == "complete"
        assert explicit["output_version"] is None
        assert not store.exists(_path(4))
        assert _rc_output(store)["after_distribution"] == {"dynamodb": 1, "documentdb": 1}


# =============================================================================
# scripts/run_assessment.py (phase, finalize, --resume-reality-check)


class TestRunAssessment:
    def test_phase_reality_check_resolves_input(self, store) -> None:
        from scripts.run_assessment import phase_reality_check

        _run(store)
        _re_resolve(store, 3)

        phase_reality_check(store, JOB, DB, "none")

        assert store.read_json(_path(4))["previous_version"] == 3

    def test_phase_reality_check_skips_after_customer_edit(self, store) -> None:
        from scripts.run_assessment import phase_reality_check

        _run(store)
        _customer_edit(store, 3)

        assert phase_reality_check(store, JOB, DB, "none") == "complete"
        assert not store.exists(_path(4))

    def test_resume_finalize_uses_same_resolution(self, store) -> None:
        from scripts.run_assessment import phase_reality_check, phase_reality_check_finalize

        _run(store)
        _re_resolve(store, 3)
        assert phase_reality_check(store, JOB, DB, "external") == "awaiting_llm"
        store.write_json(f"{DB}/{JOB}/llm_responses/reality_check.json", {})

        # Called exactly as the --resume-reality-check path calls it.
        phase_reality_check_finalize(store, JOB, DB)

        assert _rc_output(store)["source_assignment_version"] == 3
        assert store.read_json(_path(4))["previous_version"] == 3
        assert store.read_json(_path(2))["previous_version"] == 1

    def test_resume_finalize_twice_does_not_duplicate(self, store) -> None:
        from scripts.run_assessment import phase_reality_check, phase_reality_check_finalize

        assert phase_reality_check(store, JOB, DB, "external") == "awaiting_llm"
        store.write_json(f"{DB}/{JOB}/llm_responses/reality_check.json", {})
        phase_reality_check_finalize(store, JOB, DB)
        phase_reality_check_finalize(store, JOB, DB)

        assert store.exists(_path(2))
        assert not store.exists(_path(3))

    def test_finalize_without_consolidation_then_rerun_is_skipped(self, store) -> None:
        from scripts.run_assessment import phase_reality_check, phase_reality_check_finalize

        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        assert phase_reality_check(store, JOB, DB, "external") == "awaiting_llm"
        store.write_json(f"{DB}/{JOB}/llm_responses/reality_check.json", {})
        phase_reality_check_finalize(store, JOB, DB)
        _customer_edit(store, 2)

        assert _run(store)["status"] == "skipped"


# =============================================================================
# scripts/run_reality_check.py


class TestRunRealityCheckScript:
    def test_standard_and_finalize_resolve_input(self, store, capsys) -> None:
        from scripts.run_reality_check import run_finalize, run_standard

        run_standard(store, JOB, DB, None, "none")
        _re_resolve(store, 3)
        store.write_json(f"{DB}/{JOB}/llm_responses/reality_check.json", {})
        run_finalize(store, JOB, DB, None)

        assert store.read_json(_path(2))["previous_version"] == 1
        assert store.read_json(_path(4))["previous_version"] == 3


# =============================================================================
# Container entrypoint and AWS Transform core


class TestEntrypoint:
    @pytest.mark.parametrize(("env_value", "expected"), [("", None), ("3", 3)])
    def test_container_path_resolves_unless_pinned(self, monkeypatch, env_value, expected) -> None:
        import src.agents.entrypoint as ep

        monkeypatch.setattr(ep, "AGENT_TYPE", "reality-check")
        monkeypatch.setattr(ep, "ASSIGNMENT_VERSION", env_value)
        with patch("src.agents.referee.reality_check_handler.run_reality_check_handler") as rc:
            ep._dispatch_agent()

        assert rc.call_args.kwargs["assignment_version"] == expected


class TestAtxRealityCheckCore:
    def test_core_resolves_input_and_reports_effective_version(self, store, monkeypatch) -> None:
        from src.atx_orchestrator.core import run_reality_check_core

        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "none")
        _run(store)
        _re_resolve(store, 3)

        summary = run_reality_check_core(JOB, DB, store=store)

        assert summary["source_assignment_version"] == 3
        assert summary["effective_assignment_version"] == 4
        assert store.read_json(_path(4))["previous_version"] == 3

    def test_core_skips_after_customer_edit(self, store, monkeypatch) -> None:
        from src.atx_orchestrator.core import run_reality_check_core

        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "none")
        _run(store)
        _customer_edit(store, 3)

        summary = run_reality_check_core(JOB, DB, store=store)

        assert summary["effective_assignment_version"] == 3
        assert not store.exists(_path(4))
