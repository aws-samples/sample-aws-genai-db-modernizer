"""LocalOrchestrator.resume clears a stale scripts/run_*.py failure marker
for the phase it is about to (re)run (#517 round-1 review, I3): an
API-driven resume succeeding (or even just starting, here) is itself
evidence the job is moving again, the same way a script clears its own
marker when it starts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.contracts.phase_models import Phase, PhaseStatus
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-517"


def _store(tmp_path: Path) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"metadata": {}})
    return store


def _progression_with_analysis_done(orch: LocalOrchestrator) -> None:
    progression = orch.get_progression(JOB)
    orch._set_phase_status(progression, Phase.ANALYSIS, PhaseStatus.COMPLETED)
    orch._save_progression(progression)


def _progression_with_collect_triage_done(orch: LocalOrchestrator) -> None:
    progression = orch.get_progression(JOB)
    orch._set_phase_status(progression, Phase.COLLECT_TRIAGE, PhaseStatus.COMPLETED)
    orch._save_progression(progression)


class TestResumeLenientClearsPrerequisiteMarkers:
    """#517 round-3 review, item 1: resume_lenient's _auto_complete_prerequisites
    force-runs every unmet prerequisite phase (recursively, down the whole
    chain) -- each one is "the job moving again" for that phase just as
    much as resume() is for its own top-level phase, so each one must
    clear its own stale script-driven marker too."""

    def test_a_reality_check_marker_is_cleared_by_resume_lenient_schema_design(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {"status": "FAILED", "phase": "reality_check", "error_type": "RuntimeError"},
        )
        orch = LocalOrchestrator(store)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume_lenient(JOB, Phase.SCHEMA_DESIGN)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"

        from src.api.services.local_execution import LocalExecutionService

        execution = LocalExecutionService(store).describe_execution(JOB)
        assert execution is not None
        assert execution["status"] != "FAILED"


class TestResumeClearsScriptFailureMarker:
    def test_resuming_the_matching_phase_clears_it(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {"status": "FAILED", "phase": "assignment", "error_type": "RuntimeError"},
        )
        orch = LocalOrchestrator(store)
        _progression_with_analysis_done(orch)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume(JOB, Phase.ASSIGNMENT)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"

    def test_resuming_a_different_phase_leaves_it_alone(self, tmp_path: Path) -> None:
        """I4: resuming ASSIGNMENT must not clear a FAILED marker that
        actually names a different, still-unresolved phase."""
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {"status": "FAILED", "phase": "synthesis", "error_type": "RuntimeError"},
        )
        orch = LocalOrchestrator(store)
        _progression_with_analysis_done(orch)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume(JOB, Phase.ASSIGNMENT)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "FAILED"
        assert marker["phase"] == "synthesis"

    def test_a_cancelled_marker_is_left_alone(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.write_json(f"{DB}/{JOB}/_status.json", {"status": "CANCELLED", "stopped_at": "t"})
        orch = LocalOrchestrator(store)
        _progression_with_analysis_done(orch)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume(JOB, Phase.ASSIGNMENT)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "CANCELLED"


class TestResumeClearsWholeFanOutPhase:
    """#517 round-2 review, I3: resuming a fan-out phase (ANALYSIS,
    SCHEMA_DESIGN) reruns every selected engine at once, not one specific
    engine -- clearing must match a FAILED marker for *any* engine of that
    phase, not just an exact engine=None."""

    def test_resuming_analysis_clears_a_specific_engines_marker(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {
                "status": "FAILED",
                "phase": "analysis",
                "engine": "dynamodb",
                "error_type": "RuntimeError",
            },
        )
        orch = LocalOrchestrator(store)
        _progression_with_collect_triage_done(orch)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume(JOB, Phase.ANALYSIS)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"


class TestResumeCollectTriageClearsBothScriptPhases:
    """#517 round-2 review, I3: scripts/run_collect.py and
    scripts/run_triage.py are two separate scripts (two separate possible
    markers), but one single Phase.COLLECT_TRIAGE here -- resuming it must
    clear whichever of the two actually has a stale FAILED marker."""

    def test_clears_a_stale_collect_marker(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {"status": "FAILED", "phase": "collect", "error_type": "RuntimeError"},
        )
        orch = LocalOrchestrator(store)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume(JOB, Phase.COLLECT_TRIAGE)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"

    def test_clears_a_stale_triage_marker(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {"status": "FAILED", "phase": "triage", "error_type": "RuntimeError"},
        )
        orch = LocalOrchestrator(store)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume(JOB, Phase.COLLECT_TRIAGE)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"

    def test_leaves_an_unrelated_phases_marker_alone(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {"status": "FAILED", "phase": "synthesis", "error_type": "RuntimeError"},
        )
        orch = LocalOrchestrator(store)

        with patch.object(orch, "_run_phase", return_value=None):
            orch.resume(JOB, Phase.COLLECT_TRIAGE)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "FAILED"
        assert marker["phase"] == "synthesis"
