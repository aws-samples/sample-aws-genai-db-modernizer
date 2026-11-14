# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each step's why sentence and the order metrics' plain names in the reports (#1699, PR B).

``facts.slice_plan`` states each step's ``why`` and the plan's ``metric_labels`` (PR A).
The engineering report's roadmap gives each wave its steps' sentences and a drill-down of
their figures, headed by the labels; the executive report's wave panels state the
sentences. Renderers only format (ADR-032): the sentence is printed as stored, escaped,
and every metric name is read from ``metric_labels``, never from a copy here. The default
view does not change: the roadmap stays wave by wave (#1699's decisions).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.report import adoption, escaping, executive_report
from src.report.adoption_paths_md import wave_stages_md
from src.report.formatting import _SaidOnce
from src.report.renderers import render_engineering_report_md
from src.shared.step_explanations import METRIC_LABELS
from tests.executive_pdf import normalize, section_texts
from tests.unit.report.test_adoption_paths_rendering import _facts
from tests.unit.report.test_ways_to_adopt_page import _render, _renderable


def _stages(facts: dict[str, Any], wave: int) -> str:
    return "\n".join(wave_stages_md(facts, wave, _SaidOnce()))


def _steps(facts: dict[str, Any], wave: int) -> list[dict[str, Any]]:
    return [s for s in facts["slice_plan"]["plan_steps"] if s["phase_wave"] == wave]


def _with_labels(facts: dict[str, Any], prefix: str) -> dict[str, Any]:
    """``facts`` with every metric label replaced by a sentinel no copy could hold."""
    out = copy.deepcopy(facts)
    labels = out["slice_plan"]["metric_labels"]
    out["slice_plan"]["metric_labels"] = {k: f"{prefix} {k}" for k in labels}
    return out


def _header(section: str, first: str) -> list[str]:
    line = next(line for line in section.splitlines() if line.startswith(f"| {first} |"))
    return [c.strip() for c in line.strip().strip("|").split("|")]


# --- The engineering report -------------------------------------------------------------


@pytest.mark.parametrize("wave", [1, 2, 3])
def test_each_wave_states_its_steps_why_sentences_as_stored(wave: int) -> None:
    facts = _facts()
    section = _stages(facts, wave)
    assert adoption.WHY_HEADING in section
    for step in _steps(facts, wave):
        assert step["why"]
        assert f"  - {escaping.md_text(step['why'])}" in section.splitlines(), step["step_number"]


def test_a_why_sentence_is_escaped_as_markdown() -> None:
    facts = _facts()
    facts["slice_plan"]["plan_steps"][3]["why"] = "Step 4 moves *4* | `x` <b>y</b>."
    section = _stages(facts, 3)
    assert f"  - {escaping.md_text('Step 4 moves *4* | `x` <b>y</b>.')}" in section.splitlines()
    assert "  - Step 4 moves *4* |" not in section


def test_the_step_figures_are_headed_by_the_plan_s_metric_labels() -> None:
    facts = _with_labels(_facts(), "Sentinel")
    section = _stages(facts, 3)
    # The job's slice steps state no split transactions and no moved cost (None), so
    # those two columns are left out; the rest are named by the facts' own labels.
    assert _header(section, adoption.STEP_COLUMN) == [
        adoption.STEP_COLUMN,
        "Sentinel gravity",
        "Sentinel hub_score",
        "Sentinel replica_table_count",
        "Sentinel write_site_count",
        "Sentinel value_call_share_percent",
    ]
    # No default wording survives in a heading: every name is the facts'. (The sentences
    # themselves use the default words, as the facts state them.)
    headings = "\n".join(line for line in section.splitlines() if line.startswith("| "))
    for label in METRIC_LABELS.values():
        assert label not in headings.lower(), label


def test_the_step_figures_state_each_slice_step_s_stored_metrics() -> None:
    facts = _facts()
    section = _stages(facts, 3)
    rows = [line for line in section.splitlines() if line.startswith("| Step 3 |")]
    assert len(rows) == 1
    metrics = facts["slice_plan"]["plan_steps"][2]["metrics"]
    cells = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    assert cells == [
        "Step 3",
        adoption.metric_value("gravity", metrics["gravity"]),
        adoption.metric_value("hub_score", metrics["hub_score"]),
        adoption.metric_value("replica_table_count", metrics["cut_cost"]["replica_table_count"]),
        adoption.metric_value("write_site_count", metrics["cut_cost"]["write_site_count"]),
        adoption.metric_value("value_call_share_percent", metrics["value_call_share_percent"]),
    ]
    assert adoption.metric_value("value_call_share_percent", 4.1) == "4.1%"


def test_a_wave_with_no_slice_step_states_no_figures_table() -> None:
    section = _stages(_facts(), 1)
    assert not [
        line for line in section.splitlines() if line.startswith(f"| {adoption.STEP_COLUMN} |")
    ]


def test_the_unit_table_names_the_unit_metrics_from_the_facts() -> None:
    facts = _with_labels(_facts(), "Sentinel")
    section = _stages(facts, 3)
    header = _header(section, "Stage | Pilot")
    units = [h for h in header if h.startswith("Sentinel")]
    labels = facts["slice_plan"]["metric_labels"]
    assert units == [adoption.capitalized(labels[f]) for f in adoption.UNIT_METRIC_FIELDS]
    # The stage table, the first "| Stage |" table, has none; the unit table has them all.
    tables = [line for line in section.splitlines() if line.startswith("| Stage |")]
    assert len(tables) == 2 and "Sentinel" not in tables[0] and "Sentinel" in tables[1]


def test_a_plan_with_no_labels_drops_the_columns_it_cannot_name() -> None:
    """A renderer never falls back to its own words: no label, no column."""
    facts = _facts()
    del facts["slice_plan"]["metric_labels"]
    section = _stages(facts, 3)
    assert adoption.WHY_HEADING in section
    assert not [
        line for line in section.splitlines() if line.startswith(f"| {adoption.STEP_COLUMN} |")
    ]
    header = _header(section, "Stage | Pilot")
    assert header[-1] == "CDC copy back to"


def test_the_full_report_states_every_step_s_sentence_once() -> None:
    report = _renderable(facts=True)
    md = render_engineering_report_md(report)
    for step in report["facts"]["slice_plan"]["plan_steps"]:
        assert md.count(escaping.md_text(step["why"])) == 1, step["step_number"]


# --- The executive report -----------------------------------------------------------------


def test_each_wave_panel_states_its_steps_sentences() -> None:
    report = _renderable(facts=True)
    waves = normalize(section_texts(_render(report), "app")[executive_report.H_WAVES])
    for step in report["facts"]["slice_plan"]["plan_steps"]:
        assert f"{executive_report.L_WHY}: {normalize(step['why'])}" in waves, step["step_number"]


def test_an_executive_why_sentence_is_printed_literally() -> None:
    report = _renderable(facts=True)
    report["facts"]["slice_plan"]["plan_steps"][3]["why"] = "Step 4 <b>bold</b> & more."
    waves = normalize(section_texts(_render(report), "app")[executive_report.H_WAVES])
    assert "Step 4 <b>bold</b> & more." in waves
