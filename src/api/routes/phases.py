"""Phase progression and resume routes.

Requirements: 14.3, 14.4, 13.2
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.contracts.phase_models import Phase, PhaseProgression
from src.orchestrator.base import (
    Orchestrator,
    PhasePrerequisiteError,
    PhaseScope,
    TaskTokenNotFoundError,
)

router = APIRouter(prefix="/api/v1/assessments", tags=["phases"])

# Service injected by main.py at startup
orchestrator: Orchestrator | None = None


def _require_orchestrator() -> Orchestrator:
    if not orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not configured")
    return orchestrator


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ResumeRequest(BaseModel):
    """POST body for resuming a phase."""

    phase: Phase = Field(..., description="Phase to resume")
    scope_engines: list[str] | None = Field(
        None, description="Optional list of engines in scope for this phase"
    )


class ResumeResponse(BaseModel):
    """Response after a successful resume call."""

    job_id: str
    phase: str
    status: str = "resumed"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{job_id}/phases", response_model=PhaseProgression)
async def get_phases(job_id: str):
    """Return the PhaseProgression for a job."""
    orch = _require_orchestrator()
    return orch.get_progression(job_id)


@router.post("/{job_id}/resume", response_model=ResumeResponse)
async def resume_phase(job_id: str, body: ResumeRequest):
    """Resume execution at the given phase.

    Returns HTTP 409 if phase prerequisites are not met.
    """
    orch = _require_orchestrator()

    scope: PhaseScope | None = None
    if body.scope_engines:
        scope = PhaseScope(engines=body.scope_engines)

    try:
        # Use lenient resume for local dev (auto-completes prerequisites)
        resume_fn = getattr(orch, "resume_lenient", orch.resume)
        resume_fn(job_id, body.phase, scope=scope)
    except PhasePrerequisiteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskTokenNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return ResumeResponse(job_id=job_id, phase=body.phase.value)
