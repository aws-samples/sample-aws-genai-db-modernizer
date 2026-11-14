# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The judge's facts carry the adoption paths the deliverables show (#1136).

The executive report's "Ways to adopt" page and the engineering report's "Adoption
paths" section print ``report.json``'s ``facts.adoption``. The judge is told its facts
are the ground truth, so they carry what both print, computed here from the report
and never by a model: the paths shown, each path's engines and queries off the
relational engine, its cost where a deliverable may print one, the cost basis, the
paths not offered and the one-step verdict. No model is called here."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ci.llm import judge, judge_facts, replay
from src.shared.cutover_message import CUTOVER_MESSAGE
from tests.unit.report.test_adoption_paths_rendering import _renderable

ADOPTION_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "adoption_paths"
RUBRIC = judge.DEFAULT_RUBRIC_PATH.read_text()
PATH_KEYS = {
    "path_id",
    "engines",
    "step_count",
    "stage_count",
    "gate_count",
    "queries_off_relational",
    "queries_by_engine",
    "monthly_cost_usd",
    "cost_delta_vs_relational_usd",
    "critical_high_risks",
    "replicated_table_count",
    "temporary_stream_count",
}


def _sample(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ADOPTION_FIXTURES / f"{name}.json").read_text())
    return data


def _section(name: str) -> str:
    parts = re.split(r"^## \d+\. (\w+)\s*$", RUBRIC, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))[name]


def _flat(text: str) -> str:
    return " ".join(text.split())


# -- the block, from the wordpress and discourse samples -----------------------------


@pytest.mark.parametrize(
    ("sample", "relational"),
    [("wordpress", "aurora_mysql"), ("discourse", "aurora_postgresql")],
)
def test_the_facts_carry_the_adoption_paths_the_reports_show(sample: str, relational: str) -> None:
    report = _sample(sample)
    adoption = report["facts"]["adoption"]
    block = judge_facts.build_facts(report)["adoption"]

    assert block["default_path_id"] == "full_target"
    assert block["path_count"] == adoption["path_count"]
    assert block["paths_shown"] == adoption["summary_path_ids"]
    assert block["relational_engine"] == relational
    assert block["cost_basis"] == "full_target_estimates"
    assert [p["path_id"] for p in block["paths"]] == adoption["summary_path_ids"]
    for path, stated in zip(block["paths"], adoption["paths"], strict=True):
        assert set(path) == PATH_KEYS
        assert path["engines"] == stated["engines"]
        assert path["queries_off_relational"] == stated["queries_moved"]
        assert path["queries_by_engine"] == stated["queries_by_engine"]
        assert path["gate_count"] == stated["gate_count"]
    one = adoption["one_step"]
    assert block["one_step"]["suits_one_step"] is one["suits_one_step"] is False
    assert block["one_step"]["checks"] == one["one_step_checks"]
    assert block["one_step"]["staged_step_count"] == one["staged_step_count"]


@pytest.mark.parametrize("sample", ["wordpress", "discourse"])
def test_an_earlier_path_not_priced_for_itself_states_no_cost(sample: str) -> None:
    """Under ``full_target_estimates`` an earlier path's figure is the full target's
    estimates summed, which no deliverable prints (ADR-033 Amendment 2): the judge
    reads null there, so a printed figure is unsupported. The full target's cost, the
    headline, is always stated; with no relational cost there is no difference."""
    report = _sample(sample)
    stated = {p["path_id"]: p for p in report["facts"]["adoption"]["paths"]}
    paths = {p["path_id"]: p for p in judge_facts.build_facts(report)["adoption"]["paths"]}
    assert paths["full_target"]["monthly_cost_usd"] == stated["full_target"]["monthly_cost_usd"]
    for path_id, path in paths.items():
        assert path["cost_delta_vs_relational_usd"] is None, path_id
        if path_id != "full_target":
            assert path["monthly_cost_usd"] is None, path_id
            assert stated[path_id]["monthly_cost_usd"] > 0  # the figure exists, unprinted


def test_a_report_priced_path_by_path_states_every_cost_and_difference() -> None:
    report = _renderable(facts=True)
    adoption = report["facts"]["adoption"]
    assert adoption["cost_basis"] == "per_path_sizing"
    block = judge_facts.build_facts(report)["adoption"]
    assert block["cost_basis"] == "per_path_sizing"
    assert block["relational_monthly_cost_usd"] == adoption["relational_monthly_cost_usd"]
    for path, stated in zip(block["paths"], adoption["paths"], strict=True):
        assert path["monthly_cost_usd"] == stated["monthly_cost_usd"]
        # the relational move only is what the difference is measured from
        expected = (
            None if path["path_id"] == "relational_only" else stated["cost_delta_vs_relational_usd"]
        )
        assert path["cost_delta_vs_relational_usd"] == expected


def test_only_the_paths_a_capped_view_shows_reach_the_judge() -> None:
    report = _renderable(facts=True)
    report["facts"]["adoption"]["summary_path_ids"] = ["relational_only", "full_target"]
    block = judge_facts.build_facts(report)["adoption"]
    assert block["path_count"] == 3
    assert block["paths_shown"] == ["relational_only", "full_target"]
    assert [p["path_id"] for p in block["paths"]] == ["relational_only", "full_target"]


def test_the_paths_not_offered_and_the_closure_offered_instead() -> None:
    block = judge_facts.build_facts(_sample("discourse"))["adoption"]
    assert block["not_offered"] == [
        {
            "path_id": "relational_only",
            "reason": "fails_relational_gates",
            "failing_query_count": 2,
            "serving_engines": ["dynamodb"],
            "offered_instead": "through_step_2",
        }
    ]


def test_no_deliverable_withheld_figure_reaches_the_judge() -> None:
    """Neither report prints how many tables a path moves (``tables_migrate``, #2237)
    nor the query ids behind a path not offered; the judge must not expect them."""
    text = json.dumps(judge_facts.build_facts(_sample("discourse"))["adoption"])
    for withheld in ("tables_migrate", "failing_query_ids", 'replicated_tables"', "q176"):
        assert withheld not in text, withheld


def test_the_adoption_paths_are_never_cut_from_the_facts() -> None:
    """Dropping a path would make the facts say the reports show fewer paths."""
    report = copy.deepcopy(_sample("discourse"))
    report["risk_assessment"] = {
        "risks": [
            {"risk_id": f"R{i}", "severity": "LOW", "description": "x" * 200} for i in range(80)
        ]
    }
    facts = judge_facts.build_facts(report)
    body, info = judge.truncate_json(facts, len(judge.dump_facts(facts)) // 2)
    assert info["truncated"]
    cut = json.loads(body)["adoption"]
    assert [p["path_id"] for p in cut["paths"]] == ["through_step_2", "full_target"]
    assert all(isinstance(p, dict) for p in cut["paths"])


# -- the saved real runs -------------------------------------------------------------


def test_a_report_written_before_the_adoption_facts_reads_null() -> None:
    """The saved wordpress run predates ``facts.adoption``: the judge reads null (the
    report does not carry it), and no other facts field changes."""
    fixture = replay.DEFAULT_FIXTURE
    report = json.loads(replay.fixture_text(fixture, "report.json"))
    assert "adoption" not in report["facts"]
    llm_input = json.loads(replay.fixture_text(fixture, "llm_input.json"))
    facts = judge_facts.build_facts(report, llm_input)
    assert facts["adoption"] is None
    assert facts["engines"] and facts["totals"]["open_risks"] is not None


def test_a_report_that_offers_no_path_says_so() -> None:
    """The saved discourse run's report offers no path: the block says none is shown,
    so the judge expects no Ways to adopt page and no Adoption paths section."""
    fixture = replay.FIXTURES[1]
    report = json.loads(replay.fixture_text(fixture, "report.json"))
    assert report["facts"]["adoption"]["paths"] == []
    block = judge_facts.build_facts(report)["adoption"]
    assert block["path_count"] == 0
    assert block["paths_shown"] == [] and block["paths"] == []
    assert block["one_step"] is None


# -- the rubric ----------------------------------------------------------------------


def test_the_rubric_lists_the_adoption_facts_as_an_input() -> None:
    inputs = _flat(RUBRIC[RUBRIC.index("## Inputs") : RUBRIC.index("## Rules for every")])
    assert "`adoption`" in inputs
    assert "`facts.adoption`" in RUBRIC


def test_the_rubric_scores_the_adoption_paths_in_the_right_criteria() -> None:
    grounded = _flat(_section("grounded"))
    assert "`facts.adoption.paths`" in grounded
    assert "`per_path_sizing`" in grounded  # an earlier path's cost only when priced for itself
    roadmap = _flat(_section("roadmap"))
    assert "`default_path_id`" in roadmap  # the staged full target is the default
    assert "ranks one path above another" in roadmap
    assert f'"{CUTOVER_MESSAGE}"' in roadmap  # the one cutover sentence allowed
    contradictions = _flat(_section("contradictions"))
    assert "Ways to adopt" in contradictions and "Adoption paths" in contradictions
    concision = _flat(_section("concision"))
    assert "summary first" in concision  # #1699's readability criterion
