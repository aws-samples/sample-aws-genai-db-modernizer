"""Orchestrator package — factory for local vs. cloud orchestration.

``RUNTIME_MODE=local`` (or unset) → ``LocalOrchestrator``
``RUNTIME_MODE=cloud`` → ``StepFunctionsOrchestrator``

Requirements: 9.5
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestrator.base import Orchestrator


def create_orchestrator(**kwargs) -> Orchestrator:
    """Create the appropriate orchestrator based on ``RUNTIME_MODE`` env var.

    Keyword arguments are forwarded to the concrete constructor:
    - For ``LocalOrchestrator``: ``store`` (ArtifactStore)
    - For ``StepFunctionsOrchestrator``: ``sfn_client``, ``dynamodb_table_name``,
      ``state_machine_arn``, and optionally ``dynamodb_resource``
    """
    mode = os.environ.get("RUNTIME_MODE", "local").lower()

    if mode == "cloud":
        from src.orchestrator.sfn_orchestrator import StepFunctionsOrchestrator

        return StepFunctionsOrchestrator(**kwargs)

    from src.orchestrator.local_orchestrator import LocalOrchestrator

    return LocalOrchestrator(**kwargs)
