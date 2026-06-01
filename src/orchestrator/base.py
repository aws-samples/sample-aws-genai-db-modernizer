"""Orchestrator abstraction — base class and shared types.

Defines the Orchestrator ABC, PhaseScope dataclass, and PhasePrerequisiteError
exception used by both LocalOrchestrator and StepFunctionsOrchestrator.

Requirements: 1.1
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.contracts.phase_models import Phase, PhaseProgression


@dataclass
class PhaseScope:
    """Scope metadata passed when resuming a phase.

    Attributes:
        engines: List of engine identifiers in scope for this phase execution.
    """

    engines: list[str] = field(default_factory=list)


class PhasePrerequisiteError(Exception):
    """Raised when a phase is requested but its prerequisites are not COMPLETED."""


class TaskTokenNotFoundError(Exception):
    """Raised when no task token is stored for a job — execution hasn't reached the gate yet."""


class Orchestrator(ABC):
    """Abstract orchestrator for job lifecycle and phase dispatch.

    Concrete implementations:
    - StepFunctionsOrchestrator (cloud): delegates to SFN + DynamoDB
    - LocalOrchestrator (local dev): direct function calls with same ordering rules
    """

    @abstractmethod
    def start_job(self, job_id: str, config: dict) -> None:
        """Start a new job, running initial phases.

        Args:
            job_id: Unique job identifier.
            config: Job configuration dict.
        """
        ...

    @abstractmethod
    def resume(self, job_id: str, phase: Phase, scope: PhaseScope | None = None) -> None:
        """Resume execution at the given phase.

        Args:
            job_id: Unique job identifier.
            phase: The phase to resume.
            scope: Optional scope metadata (e.g., which engines to run).

        Raises:
            PhasePrerequisiteError: If prerequisites for *phase* are not met.
        """
        ...

    @abstractmethod
    def get_progression(self, job_id: str) -> PhaseProgression:
        """Return the current phase progression for a job.

        Args:
            job_id: Unique job identifier.

        Returns:
            PhaseProgression with per-phase status records.
        """
        ...
