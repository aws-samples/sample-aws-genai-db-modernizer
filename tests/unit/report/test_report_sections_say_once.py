# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A sentence one section states is not said again in the next (#2171).

The discourse judge scored concision 2 once it read the whole executive PDF (#2146).
Inside one deliverable it found the cost label and caveat on both the recommendation
page and The numbers; the owner rule's "kept current from its owner via CDC" right
after the Target engines note had said a table another engine owns is kept current by
CDC from it (both reports); and the engineering report's Cache layer paragraph saying
"cache-aside, the owner serves misses and writes" twice: in its lead and in the role
sentence (with the Valkey durability clause) that follows it. Each test here guards
one of those on a saved model run's report.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report import cache_layer
from src.report.architecture import KEPT_IN_SYNC_NOTE
from src.report.escaping import md_text
from src.report.executive_report import render_executive_report_pdf
from src.report.renderers import render_engineering_report_md
from src.report.table_owners import OWNER_RULE, OWNER_RULE_CDC, OWNER_RULE_OWNER, owner_rule
from src.shared.report_facts import COST_CAVEAT, COST_SCOPE_LABEL
from tests.executive_pdf import normalize, repeated_across_sections, section_texts
from tests.report_concision import section

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_model_run_1_60_report.json"


@pytest.fixture
def report() -> dict[str, Any]:
    saved: dict[str, Any] = json.loads(FIXTURE.read_text())
    return copy.deepcopy(saved)


def _sections(report: dict[str, Any]) -> dict[str, str]:
    return section_texts(render_executive_report_pdf(report), str(report["database_name"]))


def test_the_cost_label_and_caveat_are_said_once_with_the_first_costs(report) -> None:
    """The recommendation's engine table prints the costs first, with the label and the
    caveat; The numbers prints the same costs and, on main, both sentences again."""
    sections = _sections(report)
    label, caveat = normalize(COST_SCOPE_LABEL), normalize(COST_CAVEAT)
    assert label in sections["The recommendation"] and caveat in sections["The recommendation"]
    assert label not in sections["The numbers"] and caveat not in sections["The numbers"]
    assert repeated_across_sections(sections) == []


@pytest.mark.passes_on_main  # the negative case: no engine table, so no first statement
def test_without_an_engine_table_the_numbers_page_states_them(report) -> None:
    report["facts"]["engines"] = []
    numbers = _sections(report)["The numbers"]
    assert normalize(COST_SCOPE_LABEL) in numbers and normalize(COST_CAVEAT) in numbers


def test_the_cache_layer_paragraph_says_cache_aside_once(report) -> None:
    body = section(render_engineering_report_md(report), "Cache layer")
    paragraph = next(line for line in body.splitlines() if " fronts " in line)
    assert paragraph.count("cache-aside") == 1
    assert "which serves every miss and every write" not in paragraph
    assert "Valkey 9.0" in paragraph


@pytest.mark.passes_on_main  # the negative case: with no role sentence the lead says it
def test_without_the_role_sentence_the_lead_says_cache_aside(report, monkeypatch) -> None:
    monkeypatch.setattr(cache_layer, "cache_role_notes", lambda _report: [])
    body = section(render_engineering_report_md(report), "Cache layer")
    paragraph = next(line for line in body.splitlines() if " fronts " in line)
    assert "cache-aside: each cached read stays with its owner engine" in paragraph


def test_the_owner_rule_leaves_out_the_cdc_sentence_the_scope_note_said(report) -> None:
    """Both reports print the Target engines note (``KEPT_IN_SYNC_NOTE``) and then the
    owner rule; the rule's CDC sentence is said by the note already."""
    recommendation = _sections(report)["The recommendation"]
    assert normalize(KEPT_IN_SYNC_NOTE) in recommendation
    assert normalize(OWNER_RULE_OWNER) in recommendation
    assert normalize(OWNER_RULE_CDC) not in recommendation
    md = render_engineering_report_md(report)
    assert md_text(KEPT_IN_SYNC_NOTE) in section(md, "Target engines")
    mapping = section(md, "Migration map")
    assert md_text(OWNER_RULE_OWNER) in mapping and md_text(OWNER_RULE_CDC) not in mapping
    assert md.count(md_text(OWNER_RULE_CDC)) == 0


@pytest.mark.passes_on_main  # the negative case: no scope note, so the rule says it all
def test_without_the_scope_note_the_owner_rule_says_the_cdc_sentence(report) -> None:
    for e in report["facts"]["engines"]:
        e["kept_in_sync_tables"], e["moves_to_tables"] = 0, {}
    assert normalize(KEPT_IN_SYNC_NOTE) not in _sections(report)["The recommendation"]
    assert normalize(OWNER_RULE_CDC) in _sections(report)["The recommendation"]
    md = render_engineering_report_md(report)
    assert md_text(OWNER_RULE) in section(md, "Migration map")


@pytest.mark.passes_on_main  # the UI keeps the whole rule (its words are pinned to it)
def test_the_full_rule_is_the_three_parts() -> None:
    assert owner_rule(columns=True, cdc_defined_above=False) == OWNER_RULE
