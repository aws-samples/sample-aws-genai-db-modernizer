"""
Phase Execution Models

Data models for phased workflow execution: phase definitions, status tracking,
phase records, and progression state.

Version History:
- 1.0 (2026-04-01): Initial version — Phase 1A phased execution models
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Phase(str, Enum):
    """Execution phases in the modernization pipeline.

    Phases execute in order with prerequisite enforcement.
    LOAD_TEST is a placeholder for Phase 1B.
    """

    COLLECT_TRIAGE = "collect_triage"
    ANALYSIS = "analysis"
    ASSIGNMENT = "assignment"
    REALITY_CHECK = "reality_check"
    ASSIGNMENT_REVIEW = "assignment_review"
    SCHEMA_DESIGN = "schema_design"
    LOAD_TEST = "load_test"
    SYNTHESIS = "synthesis"


class PhaseStatus(str, Enum):
    """Status of a single phase within a job's progression."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_INPUT = "awaiting_input"
    SKIPPED = "skipped"


class PhaseRecord(BaseModel):
    """Status record for a single phase execution."""

    phase: Phase = Field(..., description="Which phase this record tracks")
    status: PhaseStatus = Field(
        default=PhaseStatus.NOT_STARTED, description="Current status of the phase"
    )
    started_at: datetime | None = Field(None, description="ISO 8601 timestamp when phase started")
    completed_at: datetime | None = Field(
        None, description="ISO 8601 timestamp when phase completed"
    )
    execution_id: str | None = Field(
        None, description="ECS task or subprocess execution identifier"
    )
    scope_engines: list[str] | None = Field(
        None, description="Engines in scope for this phase execution"
    )
    assignment_version: int | None = Field(
        None, description="Assignment version used for this phase execution"
    )
    error_message: str | None = Field(None, description="Error message if phase status is FAILED")
    iteration: int = Field(
        default=1, ge=1, description="Execution iteration count (increments on retry)"
    )
    revision_in_progress: bool = Field(
        default=False,
        description="Concurrency lock: True while a schema revision is being processed",
    )


class PhaseProgression(BaseModel):
    """Full progression state for a job across all phases."""

    job_id: str = Field(..., description="Unique job identifier")
    current_phase: Phase = Field(..., description="Phase currently active or most recently active")
    phases: dict[Phase, PhaseRecord] = Field(
        ..., description="Per-phase status records keyed by Phase enum"
    )
    assignment_version: int = Field(
        default=0,
        ge=0,
        description="Current assignment version (0 = no assignment / legacy)",
    )
    total_iterations: int = Field(
        default=1, ge=1, description="Total iteration count across all phases"
    )


# ---------------------------------------------------------------------------
# Phase prerequisite mapping
# ---------------------------------------------------------------------------

PHASE_PREREQUISITES: dict[Phase, list[Phase]] = {
    Phase.COLLECT_TRIAGE: [],
    Phase.ANALYSIS: [Phase.COLLECT_TRIAGE],
    Phase.ASSIGNMENT: [Phase.ANALYSIS],
    Phase.REALITY_CHECK: [Phase.ASSIGNMENT],
    Phase.ASSIGNMENT_REVIEW: [Phase.REALITY_CHECK],
    Phase.SCHEMA_DESIGN: [Phase.ASSIGNMENT_REVIEW],
    Phase.LOAD_TEST: [Phase.SCHEMA_DESIGN],
    Phase.SYNTHESIS: [Phase.SCHEMA_DESIGN],
}
"""Prerequisite phases that must be COMPLETED before a phase can start.

REALITY_CHECK requires ASSIGNMENT (CTO-level engine consolidation).
ASSIGNMENT_REVIEW requires REALITY_CHECK (human gate to approve/override assignments).
SCHEMA_DESIGN requires ASSIGNMENT_REVIEW (designs use the approved assignment).
LOAD_TEST requires SCHEMA_DESIGN (placeholder for Phase 1B).
SYNTHESIS requires SCHEMA_DESIGN (not LOAD_TEST, so Phase 1A works without it).
"""
