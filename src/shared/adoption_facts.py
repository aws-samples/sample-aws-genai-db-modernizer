# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The slice plan and the adoption paths, computed once for ``facts`` (#1128, ADR-032).

``facts.slice_plan`` holds the steps, their ordered stages and move units, and the
dependency graph behind their order (#1129, #1698); ``facts.adoption`` holds the
adoption paths and the one-step form of the full target (#1130, #473). Both are
computed deterministically from the report synthesis writes; a model may explain them,
never decide them. Neither records a Move or re-scores a query: a path is a projection
of the effective assignment.

Today both functions return the empty, validated structures the contract defines
(``SlicePlanFacts``, ``AdoptionFacts``), so no deliverable changes; the computation
lands with #1129, #1130 and #1698 behind these same two entry points.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.contracts.synthesis_output import (
    AdoptionFacts,
    SlicePlanFacts,
    check_adoption_against_plan,
)

__all__ = ["adoption_facts", "slice_plan_facts"]


def slice_plan_facts(report: Mapping[str, Any]) -> dict[str, Any]:
    """``facts.slice_plan`` for ``report``: no steps yet (#1129 and #1698 fill it).

    The empty plan follows the migration waves (``step_basis="engine_waves"``), the
    basis ADR-033 Decision 1 falls back to until the slice plan exists.
    """
    del report  # read by the computation that follows (#1129, #1698)
    return SlicePlanFacts(step_basis="engine_waves").model_dump(mode="json")


def adoption_facts(
    report: Mapping[str, Any], slice_plan: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``facts.adoption`` for ``report`` and its ``slice_plan``: no paths yet (#1130)."""
    del report  # read by the computation that follows (#1130)
    adoption = AdoptionFacts()
    if slice_plan is not None:
        check_adoption_against_plan(adoption, SlicePlanFacts.model_validate(slice_plan))
    return adoption.model_dump(mode="json")
