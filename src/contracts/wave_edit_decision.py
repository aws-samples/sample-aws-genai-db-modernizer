# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Wave Edit Decision — Contract Model

The customer's decision on one wave's design review warnings (#2067, ``/wave-edit``):
which warnings they accept and which they do not, recorded at
``<db>/<job>/decisions/wave-edit/wave-<N>/v<k>.json`` by ``scripts/run_wave_edit.py``.
Each decision is a new version, never overwritten. Synthesis copies the assignment
version's decisions into ``report.json`` (``wave_warning_decisions``), from which each
wave's readiness line states which warnings the customer accepted.

Version History:
- 1.0 (2026-11-09, #2067): Initial version: the wave, the decision and assignment
  versions, the accepted and rejected warnings, the blockers sent back with them,
  whether an unattended run recorded it, and when.
"""

from pydantic import Field

from src.contracts.synthesis_output import WaveWarningDecision


class WaveEditDecision(WaveWarningDecision):
    """``decisions/wave-edit/wave-<N>/v<k>.json``: one wave edit decision."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
