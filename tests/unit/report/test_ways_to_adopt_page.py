# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive report's Ways to adopt page (#1133, ADR-033 Amendment 2).

The page shows ``facts.adoption`` in the words of the engineering report's "Adoption
paths" (#1131): both read every label, fixed sentence and printing rule from
``src.report.adoption``, and only the layout differs ("Deliverables agree", AGENTS.md).
These tests render the same facts through both reports and check that each fact both
show reads the same in both, then cover the page's own cases:

- a capped plan (only ``summary_path_ids``' paths);
- a plan with no relational step (no difference from a relational move it never makes);
- a single-path plan (no one-step form);
- a report priced before per-path sizing (only the full target's cost);
- the withheld moving-table count (#2237), planted in every place it lives;
- a report saved before the adoption facts existed: the PDF renders, without the page.

``tests/e2e/test_report_pdf.py`` runs the same checks on the samples' real runs.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import AdoptionFacts, PhaseFacts
from src.report import adoption, escaping, executive_report
from src.report.adoption_paths_md import adoption_md
from src.report.formatting import _SaidOnce
from src.report.renderers import render_engineering_report_md
from src.tools.validation.customer_text_guard import scan_text
from tests.executive_pdf import normalize, outline, outside_the_page, page_texts, section_texts
from tests.unit.report.test_adoption_paths_rendering import (
    POINTER,
    SENTINEL,
    _facts,
    _renderable,
    _with_sentinel_moving_tables,
)

FIXTURES = Path(__file__).parent / "fixtures"
PRE_2170 = FIXTURES / "wordpress_pre_2170_report.json"
TITLE = executive_report.H_ADOPTION


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _render(report: dict[str, Any]) -> bytes:
    return executive_report.render_executive_report_pdf(report)


def _page(report: dict[str, Any]) -> str:
    """The Ways to adopt page's text, continued pages included, or "" with no page."""
    return section_texts(_render(report), str(report.get("database_name") or "?")).get(TITLE, "")


def _md_section(report: dict[str, Any]) -> str:
    return normalize("\n".join(adoption_md(report["facts"], POINTER, _SaidOnce())))


@pytest.fixture
def report() -> dict[str, Any]:
    """A small report whose facts hold the job's slice plan and its priced paths."""
    return _renderable(facts=True)


def _with_adoption(report: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(report)
    out["facts"]["adoption"] = AdoptionFacts.model_validate(block).model_dump(mode="json")
    return out


# -- the page and its place in the document -----------------------------------------


def test_the_page_comes_after_the_migration_waves(report: dict[str, Any]) -> None:
    titles = outline(_render(report))
    assert titles.index(TITLE) == titles.index(executive_report.H_WAVES) + 1
    assert executive_report.section_headings(report) == titles


def test_a_report_saved_before_the_adoption_facts_renders_without_the_page() -> None:
    """An old saved report states no path: the PDF renders, and the page is left out,
    not left blank."""
    old = _fixture("wordpress_no_schema_design_report.json")
    data = _render(old)
    assert data.startswith(b"%PDF")
    assert TITLE not in outline(data)
    assert executive_report.section_headings(old) == [
        h for h in executive_report.SECTION_HEADINGS if h != TITLE
    ]
    assert [p for p in page_texts(data) if p.startswith(TITLE)] == []
    assert outside_the_page(data) == []


# -- the two reports say the same thing in the same words ----------------------------


def test_both_reports_state_the_same_path_figures_in_the_same_words(
    report: dict[str, Any],
) -> None:
    """Each shown path's label, queries, cost and difference read the same on the page as
    in the engineering report's table, under the same headers, after the same intro and
    cost note."""
    facts = report["facts"]
    page, md = _page(report), _md_section(report)
    for shared in (
        adoption.intro_text(facts),
        adoption.cost_note(facts),
        adoption.queries_moved_header(facts),
        adoption.DIFFERENCE_HEADER,
        adoption.RISKS_HEADER,
    ):
        assert shared and normalize(shared) in page and normalize(shared) in md, shared
    default = facts["adoption"]["default_path_id"]
    for path in adoption.summary_paths(facts):
        label = adoption.path_label(path["path_id"], default)
        cost = adoption.cost_text(path, adoption.per_path_priced(facts))
        difference = adoption.cost_difference(path) or adoption.NO_FIGURE
        md_row = f"| {label} | {path['queries_moved']} | {cost} | {difference} |"
        assert md_row in md, md_row
        page_row = (
            f"{label} {path['step_count']} {path['gate_count']} {path['queries_moved']} "
            f"{cost} {difference} {path['critical_high_risks']}"
        )
        assert page_row in page, page_row
    assert page.count(adoption.DEFAULT_SUFFIX) == 1
    # The page's own words are only its layout labels: no "recommended", no "No difference".
    for gone in ("recommended", "No difference", "Way to adopt"):
        assert gone not in page, gone


def test_both_reports_state_the_same_one_step_form_and_verdict(report: dict[str, Any]) -> None:
    """The one-step lines, the fixed "suits one step" sentence and every check row, word
    for word in both reports, for a workload that suits one step and one that does not."""
    one = report["facts"]["adoption"]["one_step"]
    passing = {
        **one,
        "suits_one_step": True,
        "one_step_checks": [{**c, "observed": 0, "passed": True} for c in one["one_step_checks"]],
    }
    for block, verdict, other in (
        (one, adoption.ONE_STEP_DOES_NOT_SUIT, adoption.ONE_STEP_SUITS),
        (passing, adoption.ONE_STEP_SUITS, adoption.ONE_STEP_DOES_NOT_SUIT),
    ):
        r = _with_adoption(report, {**report["facts"]["adoption"], "one_step": block})
        page, md = _page(r), _md_section(r)
        for line in adoption.one_step_lines(block):
            assert normalize(line) in page and normalize(line) in md, line
        assert verdict in page and verdict in md
        assert other not in page and other not in md
        assert " ".join(adoption.ONE_STEP_CHECK_HEADER) in page
        for row in adoption.one_step_check_rows(block):
            assert " ".join(row) in page, row
            assert "| " + " | ".join(row) + " |" in md, row


def test_both_reports_state_the_same_not_offered_lines() -> None:
    """A path the relational gates refuse: the same sentence, naming its closure, in both
    reports, and no failing query id on the page."""
    gated = _facts(gated={"aurora_mysql": ("q03", "q04")})
    entries = gated["adoption"]["not_offered"]
    assert entries and entries[0]["reason"] == "fails_relational_gates"
    report = _renderable(facts=True)
    report["facts"] = {**report["facts"], **gated}
    page, md = _page(report), _md_section(report)
    texts = adoption.not_offered_texts(report["facts"])
    assert texts
    for text in texts:
        # The engineering report escapes Markdown punctuation; the words are the same.
        assert normalize(text) in page and normalize(escaping.md_text(text)) in md, text
    for entry in entries:
        for qid in entry["failing_query_ids"]:
            assert qid not in page, qid


def test_the_capped_page_shows_only_the_paths_summary_path_ids_names(
    report: dict[str, Any],
) -> None:
    """Two of three paths shown: the same cap sentence's tail as the engineering report,
    and the dropped path named nowhere on the page."""
    block = copy.deepcopy(report["facts"]["adoption"])
    block["summary_path_ids"] = ["relational_only", "full_target"]
    capped = copy.deepcopy(report)
    capped["facts"]["adoption"] = block  # summary ids are the contract's pick: not re-validated
    page, md = _page(capped), _md_section(capped)
    tail = "show 2 of the 3 paths: the first, the last, and evenly spaced steps between them."
    assert f"The rows below {tail}" in page
    assert f"The table and the blocks below {tail}" in md
    assert "Through step 3" not in page


# -- what may be printed ------------------------------------------------------------


def test_a_report_priced_before_per_path_sizing_states_only_the_full_target_s_cost() -> None:
    """ADR-033 Amendment 2 and the 2026-11-12 decision on #2170: with
    ``full_target_estimates`` an earlier path's cost is the full target's estimates summed,
    so the page prints only the full target's cost and no difference, as the engineering
    report does."""
    report = json.loads(PRE_2170.read_text(encoding="utf-8"))
    block = report["facts"]["adoption"]
    assert block["cost_basis"] == "full_target_estimates"
    costs = {p["path_id"]: p["monthly_cost_usd"] for p in block["paths"]}
    assert costs == {"relational_only": 681.84, "full_target": 757.62}
    data = _render(report)
    page = section_texts(data, "wordpress")[TITLE]
    assert "$681.84" not in normalize(" ".join(page_texts(data)))
    assert "Relational move only 2 2 0 - 1" in page
    assert "Full target, the default 3 4 39 $757.62 1" in page
    assert adoption.DIFFERENCE_HEADER not in page
    assert adoption.FULL_TARGET_COST_ONLY in page
    assert block["cost_basis_note"] not in page


def test_a_plan_with_no_relational_step_states_no_difference(report: dict[str, Any]) -> None:
    """With no relational step there is no relational move only to measure against:
    ``relational_monthly_cost_usd`` is None and the difference column is left out, though
    the contract keeps a number in every path (#2214)."""
    block = copy.deepcopy(report["facts"]["adoption"])
    block["relational_monthly_cost_usd"] = None
    block["paths"] = [p for p in block["paths"] if p["path_id"] != "relational_only"]
    block["path_count"] = len(block["paths"])
    block["summary_path_ids"] = [p["path_id"] for p in block["paths"]]
    block["not_offered"] = [
        {
            "path_id": "relational_only",
            "reason": "no_relational_engine",
            "offered_instead": block["paths"][0]["path_id"],
        }
    ]
    r = _with_adoption(report, block)
    assert any(p["cost_delta_vs_relational_usd"] != 0 for p in r["facts"]["adoption"]["paths"])
    page = _page(r)
    assert adoption.DIFFERENCE_HEADER not in page
    assert re.search(r"\$[\d,.]+ (?:more|less)\b", page) is None, page
    assert "the same" not in page
    assert "Relational move only is not offered" in page


def test_a_single_path_plan_states_that_path_and_no_one_step_form(
    report: dict[str, Any],
) -> None:
    """A plan whose only path is the relational move has no full target, so no one-step
    form: the page states that one path as the default."""
    block = copy.deepcopy(report["facts"]["adoption"])
    block["paths"] = [p for p in block["paths"] if p["path_id"] == "relational_only"]
    block.update(
        path_count=1,
        summary_path_ids=["relational_only"],
        default_path_id="relational_only",
        one_step=None,
    )
    page = _page(_with_adoption(report, block))
    assert "Relational move only, the default" in page
    assert "show 1 of" not in page
    assert executive_report.L_ONE_STEP.upper() not in page
    assert adoption.ONE_STEP_SUITS not in page and adoption.ONE_STEP_DOES_NOT_SUIT not in page


def test_the_page_names_no_table_and_no_withheld_moving_table_count(
    report: dict[str, Any],
) -> None:
    """Review of #2241: the moving-table count (``paths[].tables_migrate``, and wave 1's
    ``leaves_count``) disagrees with the headline "tables migrate" until #2237 lands, so
    no part of the PDF prints it. A path's replicated tables and its gate stay in the
    engineering report; the page passes the customer-text guard."""
    planted = _with_sentinel_moving_tables(report)
    assert planted["facts"]["tables_migrate"] != SENTINEL
    text = normalize(" ".join(page_texts(_render(planted))))
    assert TITLE in text
    assert str(SENTINEL) not in text and f"{SENTINEL:,}" not in text
    assert "owned by a later step" not in text
    page = _page(report)
    for path in report["facts"]["adoption"]["paths"]:
        for table in path["replicated_tables"]:
            assert table not in page, table
        assert path["path_id"] not in page
    assert re.search(r"\b[a-z]+_[a-z_]+\b", page) is None, page
    assert scan_text(page, "executive-report.pdf") == []


def test_the_engineering_report_still_withholds_the_planted_count(
    report: dict[str, Any],
) -> None:
    """The same planted report through the engineering renderer, after the module split."""
    md = render_engineering_report_md(_with_sentinel_moving_tables(report))
    assert "## Adoption paths" in md
    assert str(SENTINEL) not in md and f"{SENTINEL:,}" not in md


# -- the migration waves page's stages ------------------------------------------------


def test_every_wave_states_its_stages_from_the_slice_plan(report: dict[str, Any]) -> None:
    """#1133: each wave's stage count and stage kinds, as its engine phase rolls the
    plan's steps up, in the words the engineering report's stage table uses."""
    phases = [PhaseFacts.model_validate(p) for p in report["facts"]["slice_plan"]["wave_phases"]]
    assert phases
    waves = section_texts(_render(report), "app")[executive_report.H_WAVES]
    md = render_engineering_report_md(report)
    for phase in phases:
        p = phase.model_dump(mode="json")
        kinds = adoption.stage_kinds_text(p)
        n = p["stage_count"]
        expected = f"{n} {'stage' if n == 1 else 'stages'} ({kinds})"
        assert f"{executive_report.L_STAGES}: {expected}" in waves, phase.phase_wave
        for kind in p["stage_kinds"]:
            assert adoption.STAGE_KIND_LABELS[kind] in md, kind


def test_every_stage_kind_and_one_step_rule_has_a_label() -> None:
    """A kind or a rule with no label would be dropped from both reports in silence."""
    from typing import get_args

    from src.contracts.synthesis_output import OneStepRule, SliceStageKind

    assert set(adoption.STAGE_KIND_LABELS) == set(get_args(SliceStageKind))
    assert set(adoption.ONE_STEP_RULE_LABELS) == set(get_args(OneStepRule))
