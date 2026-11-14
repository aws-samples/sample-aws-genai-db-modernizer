# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The results page states the adoption paths in the reports' words (#1132).

``src/ui/src/locales/en.json`` carries the results page's copy of the fixed sentences,
labels and lines that ``src.report.adoption`` keeps for both reports (#2240): the
definitions, the one-step verdicts and lines, the full-target-cost-only note, the path,
stage-kind and one-step-rule labels and the column headers, so the page and the reports
cannot drift apart silently (as ``test_table_owners_js_sync.py`` pins the owner rule).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contracts.synthesis_output import OneStepRule, SliceStageKind
from src.report import adoption

REPO = Path(__file__).resolve().parents[3]
EN = json.loads((REPO / "src/ui/src/locales/en.json").read_text())
KEY = "analysis-results.adoption."


@pytest.mark.parametrize(
    ("key", "sentence"),
    [
        ("definitions", adoption.DEFINITIONS),
        ("one-step.suits", adoption.ONE_STEP_SUITS),
        ("one-step.does-not-suit", adoption.ONE_STEP_DOES_NOT_SUIT),
        ("full-target-cost-only", adoption.FULL_TARGET_COST_ONLY),
    ],
)
def test_the_ui_states_each_fixed_sentence_word_for_word(key: str, sentence: str) -> None:
    assert EN[KEY + key] == sentence


def test_the_ui_names_the_paths_as_the_reports_do() -> None:
    labels = adoption.PATH_LABELS
    assert EN[KEY + "path.relational_only"] == labels["relational_only"]
    assert EN[KEY + "path.full_target"] == labels["full_target"]
    # "Through step 5" and "Full target, the default", filled the way the report fills them.
    label = adoption.path_label
    assert EN[KEY + "path.through-step"].replace("{{step}}", "5") == label("through_step_5")
    assert EN[KEY + "path.default"].replace("{{label}}", labels["full_target"]) == label(
        "full_target", "full_target"
    )


def test_the_ui_states_every_stage_kind_in_the_reports_words() -> None:
    labels = adoption.STAGE_KIND_LABELS
    for kind in SliceStageKind.__args__:  # type: ignore[attr-defined]
        assert EN[f"{KEY}stage-kind.{kind}"] == labels[kind], kind


def test_the_ui_states_every_one_step_rule_in_the_reports_words() -> None:
    labels = adoption.ONE_STEP_RULE_LABELS
    for rule in OneStepRule.__args__:  # type: ignore[attr-defined]
        assert EN[f"{KEY}one-step.rule.{rule}"] == labels[rule], rule


def test_the_ui_s_tile_and_check_headers_are_the_report_s_columns() -> None:
    assert EN[KEY + "tile.risks"] == adoption.RISKS_HEADER
    columns = [EN[f"{KEY}one-step.column.{c}"] for c in ("check", "observed", "limit", "holds")]
    assert tuple(columns) == adoption.ONE_STEP_CHECK_HEADER


@pytest.mark.parametrize("count", [1, 3])
def test_the_ui_s_one_step_lines_are_the_report_s(count: int) -> None:
    """The one-step option's lines, filled as the page fills them, are the lines the
    reports print (``one_step_lines``), singular and plural."""
    one = {
        "staged_step_count": count,
        "staged_stage_count": count,
        "gate_count": count,
        "staged_temporary_stream_count": count,
    }
    form = "one" if count == 1 else "other"

    def fill(key: str, **values: str) -> str:
        text = str(EN[KEY + key])
        for name, value in values.items():
            text = text.replace("{{" + name + "}}", value)
        return text

    steps = fill(f"step-count_{form}", count=str(count))
    stages = fill(f"stage-count_{form}", count=str(count))
    assert [
        fill("one-step.in-place", steps=steps, stages=stages),
        fill(f"one-step.gate_{form}", count=str(count)),
        fill(f"one-step.streams_{form}", count=str(count)),
    ] == adoption.one_step_lines(one)


def test_the_ui_s_one_step_pointer_is_the_engineering_report_s() -> None:
    """Review of #2249: the one-step option names what the engineering report names,
    from the same fact (a completed schema design, ``one_step_pointer``)."""
    from src.report.roadmap import one_step_pointer

    skipped = {"schema_designs": {"dynamodb": {"status": "skipped"}}}
    assert EN[KEY + "one-step.link-architecture"] == one_step_pointer(skipped)
    assert EN[KEY + "one-step.link-architecture"] == one_step_pointer({})
    completed = one_step_pointer({"schema_designs": {"dynamodb": {"status": "completed"}}})
    assert completed.startswith(EN[KEY + "one-step.link-schemas"].removesuffix(" below"))
