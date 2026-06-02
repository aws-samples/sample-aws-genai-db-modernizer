"""
Property-based tests for the Orchestrator abstraction.

Tests correctness property from the design document:
- Property 4: Phase Ordering — a phase only enters IN_PROGRESS when all
  prerequisites are COMPLETED.

**Validates: Requirement 1.1**
"""

from __future__ import annotations

import tempfile
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.contracts.phase_models import PHASE_PREREQUISITES, Phase, PhaseStatus
from src.orchestrator.base import PhasePrerequisiteError, PhaseScope
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(tmpdir: str) -> LocalOrchestrator:
    """Create a LocalOrchestrator backed by a temporary directory."""
    store = LocalArtifactStore(base_dir=tmpdir)
    return LocalOrchestrator(store=store)


# Strategy: phases that have at least one prerequisite (i.e., can fail ordering)
_phases_with_prereqs = st.sampled_from([p for p in Phase if PHASE_PREREQUISITES.get(p)])

# Strategy: any valid Phase
_any_phase = st.sampled_from(list(Phase))

# Strategy: job IDs — simple alphanumeric strings
_job_ids = st.from_regex(r"[a-z][a-z0-9]{3,12}", fullmatch=True)

# Strategy: PhaseScope with 0-3 engine names
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


# ---------------------------------------------------------------------------
# Property 4: Phase Ordering
# ---------------------------------------------------------------------------


class TestPhaseOrdering:
    """**Validates: Requirement 1.1**

    Property 4: A phase only enters IN_PROGRESS when all prerequisites
    are COMPLETED.
    """

    @given(phase=_phases_with_prereqs, job_id=_job_ids)
    def test_resume_raises_when_prerequisites_not_met(self, phase: Phase, job_id: str) -> None:
        """Resuming a phase with unmet prerequisites raises PhasePrerequisiteError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            # Create a fresh progression where everything is NOT_STARTED
            progression = orch._new_progression(job_id)
            orch._save_progression(progression)

            with pytest.raises(PhasePrerequisiteError):
                orch.resume(job_id, phase)

    @given(job_id=_job_ids)
    def test_start_job_creates_progression_with_initial_phases_done(self, job_id: str) -> None:
        """start_job completes COLLECT_TRIAGE and sets ANALYSIS to AWAITING_REVIEW."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            with patch.object(orch, "_run_phase"):
                orch.start_job(job_id, config={})

            progression = orch.get_progression(job_id)

            # COLLECT_TRIAGE must be COMPLETED (it ran first, then analysis overwrote current_phase)
            ct_status = progression.phases[Phase.COLLECT_TRIAGE].status
            assert (
                ct_status == PhaseStatus.COMPLETED
            ), f"Expected COLLECT_TRIAGE=COMPLETED, got {ct_status}"

            # ANALYSIS should be AWAITING_REVIEW (completed then set to awaiting)
            an_status = progression.phases[Phase.ANALYSIS].status
            assert (
                an_status == PhaseStatus.AWAITING_REVIEW
            ), f"Expected ANALYSIS=AWAITING_REVIEW, got {an_status}"

    @given(job_id=_job_ids)
    def test_resume_assignment_succeeds_after_start_job(self, job_id: str) -> None:
        """After start_job, resuming ASSIGNMENT should not raise because
        ANALYSIS prerequisite was completed (even though it's now AWAITING_REVIEW,
        we need to mark it COMPLETED first for the prerequisite check).

        This test verifies the happy path: manually complete ANALYSIS, then
        resume ASSIGNMENT.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            with patch.object(orch, "_run_phase"):
                orch.start_job(job_id, config={})

            # Mark ANALYSIS as COMPLETED so prerequisite is satisfied
            progression = orch.get_progression(job_id)
            progression.phases[Phase.ANALYSIS].status = PhaseStatus.COMPLETED
            orch._save_progression(progression)

            # Should not raise
            with patch.object(orch, "_run_phase"):
                orch.resume(job_id, Phase.ASSIGNMENT)

            updated = orch.get_progression(job_id)
            assert updated.phases[Phase.ASSIGNMENT].status == PhaseStatus.COMPLETED

    @given(phase=_phases_with_prereqs, job_id=_job_ids, scope=_scopes)
    def test_phase_only_in_progress_when_prereqs_completed(
        self, phase: Phase, job_id: str, scope: PhaseScope | None
    ) -> None:
        """Property 4: a phase only enters IN_PROGRESS when all prerequisites
        are COMPLETED. We set up a progression where prerequisites are NOT
        completed and verify the phase cannot start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            progression = orch._new_progression(job_id)

            # Intentionally leave at least one prerequisite as NOT_STARTED
            prereqs = PHASE_PREREQUISITES[phase]
            # Complete all but the last prerequisite
            for prereq in prereqs[:-1]:
                progression.phases[prereq].status = PhaseStatus.COMPLETED
            # Last prereq stays NOT_STARTED

            orch._save_progression(progression)

            with pytest.raises(PhasePrerequisiteError):
                orch.resume(job_id, phase, scope=scope)

            # Verify the phase never reached IN_PROGRESS
            after = orch.get_progression(job_id)
            assert after.phases[phase].status == PhaseStatus.NOT_STARTED

    @given(job_id=_job_ids)
    def test_collect_triage_has_no_prerequisites(self, job_id: str) -> None:
        """COLLECT_TRIAGE has no prerequisites and can always start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            progression = orch._new_progression(job_id)
            orch._save_progression(progression)

            # COLLECT_TRIAGE has no prereqs, so resume should succeed
            with patch.object(orch, "_run_phase"):
                orch.resume(job_id, Phase.COLLECT_TRIAGE)

            updated = orch.get_progression(job_id)
            assert updated.phases[Phase.COLLECT_TRIAGE].status == PhaseStatus.COMPLETED
