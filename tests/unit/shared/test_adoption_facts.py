# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``facts.slice_plan`` and ``facts.adoption`` (#1128): one entry point in the facts
layer (ADR-032). The slice plan is computed from the assignment synthesis passes (#1129,
#1698); a report filled at read time has no assignment, so it states the empty plan. The
adoption paths stay empty until #1130."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import (
    AdoptionFacts,
    ReportFacts,
    SlicePlanFacts,
    SynthesisOutputContract,
)
from src.report.engineering_report import render_engineering_report_md
from src.report.facts import report_facts
from src.shared.adoption_facts import adoption_facts, slice_plan_facts
from src.shared.report_facts import compute_report_facts

REPO = Path(__file__).resolve().parents[3]
SAVED_REPORTS = (
    # A job an older release wrote (v0.4.0-beta.2, contract 1.69).
    REPO / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json",
    REPO / "docs/examples/wordpress/sample-run/synthesis/v2/report.json",
    REPO / "tests/fixtures/real_runs/wordpress-both/report.json",
)


def _load(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(path.read_text())
    return report


@pytest.mark.passes_on_main  # #2156's read-time fill: no assignment, so the empty plan
@pytest.mark.parametrize("path", SAVED_REPORTS, ids=lambda p: p.parts[-5])
def test_a_report_written_before_the_slice_plan_loads_and_is_filled_at_read_time(
    path: Path,
) -> None:
    # ADR-032 Decision 4: one reader, one answer. A stored block written before 1.96 has
    # neither block; it loads, and the reader fills both from the same functions
    # synthesis calls. With no assignment to read, the slice plan is the empty one.
    report = _load(path)
    assert "slice_plan" not in report["facts"] and "adoption" not in report["facts"]
    contract = SynthesisOutputContract.model_validate(report)
    assert contract.facts is not None
    assert contract.facts.slice_plan is None and contract.facts.adoption is None
    facts = report_facts(report)
    plan = slice_plan_facts(report)
    assert plan["plan_steps"] == []
    assert facts["slice_plan"] == plan
    assert facts["adoption"] == adoption_facts(report, plan)
    ReportFacts.model_validate(facts)


def test_a_stored_block_is_read_as_stored() -> None:
    report = _load(SAVED_REPORTS[1])
    stored = {"step_basis": "engine_waves", "plan_steps": [], "marker": True}
    report["facts"] = {**report["facts"], "slice_plan": stored}
    facts = report_facts(report)
    assert facts["slice_plan"] is stored
    assert facts["adoption"] == adoption_facts(report, stored)


@pytest.mark.passes_on_main  # #2156's read-time fill: no assignment, so the empty plan
def test_without_the_assignment_both_blocks_are_empty_and_valid() -> None:
    report = _load(SAVED_REPORTS[1])
    plan = slice_plan_facts(report)
    adoption = adoption_facts(report, plan)
    assert SlicePlanFacts.model_validate(plan).model_dump(mode="json") == plan
    assert AdoptionFacts.model_validate(adoption).model_dump(mode="json") == adoption
    assert plan["step_basis"] == "engine_waves" and plan["slice_basis"] is None
    assert plan["plan_steps"] == [] and plan["move_units"] == [] and plan["step_count"] == 0
    assert adoption["paths"] == [] and adoption["default_path_id"] is None
    assert adoption["one_step"] is None and adoption["not_offered"] == []


def test_the_facts_layer_carries_both_blocks() -> None:
    report = {k: v for k, v in _load(SAVED_REPORTS[1]).items() if k != "facts"}
    facts = compute_report_facts(report)
    assert facts["slice_plan"] == slice_plan_facts(report)
    assert facts["adoption"] == adoption_facts(report, facts["slice_plan"])
    validated = ReportFacts.model_validate(facts)
    assert validated.slice_plan is not None and validated.slice_plan.plan_steps == []
    assert validated.adoption is not None and validated.adoption.paths == []


def test_the_deliverables_ignore_the_empty_blocks() -> None:
    # Nothing renders the two blocks yet (#1131 to #1133 will): a report whose facts
    # carry them renders byte for byte as one whose facts do not.
    report = _load(SAVED_REPORTS[1])
    bare = {k: v for k, v in report["facts"].items() if k not in ("slice_plan", "adoption")}
    plan = slice_plan_facts(report)
    carried = {**bare, "slice_plan": plan, "adoption": adoption_facts(report, plan)}
    before = render_engineering_report_md({**report, "facts": bare})
    after = render_engineering_report_md({**report, "facts": carried})
    assert after == before
