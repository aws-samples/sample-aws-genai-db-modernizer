"""#517 round-2 review, N1 (critical): mark_assignment_reviewed must never
write a partial .meta/<job_id>.json when none exists yet.

A hand-written progression with only ``assignment_review`` set still
validates as JSON, but LocalOrchestrator would then load *that* as the
job's entire progression instead of inferring one properly from real
artifacts -- and a later resume/get_progression call either KeyErrors on a
missing phase or 500s over the API. mark_assignment_reviewed now records
the approval in decisions/assignment_review.json instead when .meta is
absent, so the orchestrator's first real touch of the job still goes
through its own, battle-tested _infer_progression path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.contracts.phase_models import Phase
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.shared.job_status import mark_assignment_reviewed
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-517"


def _store(tmp_path: Path) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"metadata": {}})
    return store


def test_decisions_file_records_approval_without_touching_meta(tmp_path: Path) -> None:
    store = _store(tmp_path)

    mark_assignment_reviewed(store, DB, JOB, "synthesis")

    assert not store.exists(f".meta/{JOB}.json")
    decision = store.read_json(f"{DB}/{JOB}/decisions/assignment_review.json")
    assert decision["status"] == "completed"


def test_resume_lenient_succeeds_after_a_decisions_only_review(tmp_path: Path) -> None:
    """The regression this guards against: before the fix, resume_lenient
    (or a bare get_progression) on a job whose only .meta content was a
    hand-written assignment_review would KeyError/500 on any other phase."""
    store = _store(tmp_path)
    mark_assignment_reviewed(store, DB, JOB, "synthesis")

    orch = LocalOrchestrator(store)
    with patch.object(orch, "_run_phase", return_value=None):
        orch.resume_lenient(JOB, Phase.SYNTHESIS)  # must not raise


def test_get_phases_has_every_phase_after_resume_lenient(tmp_path: Path) -> None:
    """GET /phases (LocalOrchestrator.get_progression) must come back with
    a complete progression -- every Phase present -- not just whatever
    mark_assignment_reviewed happened to touch."""
    store = _store(tmp_path)
    mark_assignment_reviewed(store, DB, JOB, "synthesis")

    orch = LocalOrchestrator(store)
    with patch.object(orch, "_run_phase", return_value=None):
        orch.resume_lenient(JOB, Phase.SYNTHESIS)

    progression = orch.get_progression(JOB)
    for phase in Phase:
        assert phase in progression.phases, f"{phase} missing from progression"
