"""Orchestrator package — factory for the local orchestrator.

Requirements: 9.5
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestrator.base import Orchestrator


def create_orchestrator(**kwargs) -> Orchestrator:
    """Create a ``LocalOrchestrator``.

    Keyword arguments are forwarded to the constructor: ``store`` (ArtifactStore).
    """
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    return LocalOrchestrator(**kwargs)
