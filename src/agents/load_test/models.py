# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Engine-agnostic models for load test orchestration."""

from pydantic import BaseModel

# Written to disk (seed-manifest.json), so it is a versioned contract (#972).
from src.contracts.load_test_models import SeedManifest

__all__ = ["RunResult", "SeedManifest"]


class RunResult(BaseModel):
    """Unified output from any runner."""

    returncode: int
    stdout: str
    stderr: str
    summary: dict | None = None
