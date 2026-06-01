"""Engine-agnostic models for load test orchestration."""
from typing import Any

from pydantic import BaseModel


class SeedManifest(BaseModel):
    """What was seeded. Engine-specific details live in resources dict."""

    resources: dict[str, Any]
    total_items: int
    duration_seconds: float


class RunResult(BaseModel):
    """Unified output from any runner."""

    returncode: int
    stdout: str
    stderr: str
    summary: dict | None = None
