"""
Property-based tests for error handling and recovery.

Tests correctness property from the design document:
- Property 4: Phase Ordering — verify that resume rejects phases with
  incomplete prerequisites

**Validates: Requirement 1.1, 13.1**
"""

from __future__ import annotations

import tempfile
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.contracts.phase_models import PHASE_PREREQUISITES, Phase, PhaseStatus
from src.orchestrator.base import PhasePrerequisiteError, PhaseScope
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_job_ids = st.from_regex(r"[a-z][a-z0-9]{3,12}", fullmatch=True)

_phases_with_prereqs = st.sampled_from([p for p in Phase if PHASE_PREREQUISITES.get(p)])

_scopes = st.one_of(
    st.none(),
    st.builds(
        PhaseScope,
        engines=st.lists(
            st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb"]),
            min_size=0,
            max_size=3,
        ),
    ),
)

# All non-terminal statuses that are NOT COMPLETED
_non_completed_statuses = st.sampled_from(
    [
        PhaseStatus.NOT_STARTED,
        PhaseStatus.IN_PROGRESS,
        PhaseStatus.FAILED,
        PhaseStatus.AWAITING_REVIEW,
        PhaseStatus.AWAITING_INPUT,
        PhaseStatus.SKIPPED,
    ]
)


def _make_orchestrator(tmpdir: str) -> LocalOrchestrator:
    store = LocalArtifactStore(base_dir=tmpdir)
    return LocalOrchestrator(store=store)


# ---------------------------------------------------------------------------
# Property 4: Phase Ordering (error handling focus)
# ---------------------------------------------------------------------------


class TestPhaseOrderingErrorHandling:
    """**Validates: Requirement 1.1, 13.1**

    Property 4: Phase Ordering — verify that resume rejects phases with
    incomplete prerequisites. This class focuses on error handling aspects:
    - Failed phases are retryable
    - Non-COMPLETED prerequisite statuses all block progression
    - Error messages are preserved in phase records
    """

    @given(
        phase=_phases_with_prereqs,
        job_id=_job_ids,
        prereq_status=_non_completed_statuses,
    )
    def test_resume_rejects_any_non_completed_prerequisite(
        self, phase: Phase, job_id: str, prereq_status: PhaseStatus
    ) -> None:
        """Any prerequisite status other than COMPLETED blocks the phase."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            progression = orch._new_progression(job_id)

            prereqs = PHASE_PREREQUISITES[phase]
            # Set all prereqs to COMPLETED except the last one
            for prereq in prereqs[:-1]:
                progression.phases[prereq].status = PhaseStatus.COMPLETED
            # Set last prereq to the non-completed status
            progression.phases[prereqs[-1]].status = prereq_status

            orch._save_progression(progression)

            with pytest.raises(PhasePrerequisiteError):
                orch.resume(job_id, phase)

    @given(job_id=_job_ids)
    @settings(deadline=None)
    def test_failed_phase_is_retryable(self, job_id: str) -> None:
        """A phase that failed can be retried via resume after fixing
        prerequisites (the FAILED status of the phase itself doesn't
        block re-execution — only prerequisite statuses matter)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)

            # Set up: COLLECT_TRIAGE completed, ANALYSIS failed
            with patch.object(orch, "_run_phase"):
                orch.start_job(job_id, config={})

            progression = orch.get_progression(job_id)
            progression.phases[Phase.ANALYSIS].status = PhaseStatus.COMPLETED
            progression.phases[Phase.ASSIGNMENT].status = PhaseStatus.FAILED
            progression.phases[Phase.ASSIGNMENT].error_message = "Test failure"
            orch._save_progression(progression)

            # Retry: ASSIGNMENT's prereq is ANALYSIS which is COMPLETED
            # So resume should succeed
            with patch.object(orch, "_run_phase"):
                orch.resume(job_id, Phase.ASSIGNMENT)

            updated = orch.get_progression(job_id)
            assert updated.phases[Phase.ASSIGNMENT].status == PhaseStatus.COMPLETED

    @given(job_id=_job_ids)
    @settings(deadline=None)
    def test_phase_failure_sets_failed_status_with_error_message(self, job_id: str) -> None:
        """When a phase raises an exception, the orchestrator sets FAILED
        status with the error message preserved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)

            with patch.object(orch, "_run_phase"):
                orch.start_job(job_id, config={})

            progression = orch.get_progression(job_id)
            progression.phases[Phase.ANALYSIS].status = PhaseStatus.COMPLETED
            orch._save_progression(progression)

            error_msg = "Something went wrong in assignment"

            def failing_run_phase(jid, phase, config=None, scope=None):
                raise RuntimeError(error_msg)

            with patch.object(orch, "_run_phase", side_effect=failing_run_phase):
                with pytest.raises(RuntimeError, match=error_msg):
                    orch.resume(job_id, Phase.ASSIGNMENT)

            updated = orch.get_progression(job_id)
            assert updated.phases[Phase.ASSIGNMENT].status == PhaseStatus.FAILED
            assert updated.phases[Phase.ASSIGNMENT].error_message == error_msg

    @given(
        phase=_phases_with_prereqs,
        job_id=_job_ids,
        scope=_scopes,
    )
    def test_prerequisite_check_happens_before_phase_starts(
        self, phase: Phase, job_id: str, scope: PhaseScope | None
    ) -> None:
        """The prerequisite check must happen before the phase transitions
        to IN_PROGRESS. If prerequisites are not met, the phase status
        must remain unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            progression = orch._new_progression(job_id)
            orch._save_progression(progression)

            with pytest.raises(PhasePrerequisiteError):
                orch.resume(job_id, phase, scope=scope)

            after = orch.get_progression(job_id)
            assert after.phases[phase].status == PhaseStatus.NOT_STARTED, (
                f"Phase {phase.value} should remain NOT_STARTED when " f"prerequisites are not met"
            )

    @given(job_id=_job_ids)
    @settings(deadline=None)
    def test_start_job_failure_sets_failed_status(self, job_id: str) -> None:
        """When start_job fails during COLLECT_TRIAGE, the phase is set
        to FAILED with the error message."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            error_msg = "Collector failed"

            def failing_run_phase(jid, phase, config=None, scope=None):
                raise RuntimeError(error_msg)

            with patch.object(orch, "_run_phase", side_effect=failing_run_phase):
                with pytest.raises(RuntimeError, match=error_msg):
                    orch.start_job(job_id, config={})

            updated = orch.get_progression(job_id)
            assert updated.phases[Phase.COLLECT_TRIAGE].status == PhaseStatus.FAILED
            assert updated.phases[Phase.COLLECT_TRIAGE].error_message == error_msg
