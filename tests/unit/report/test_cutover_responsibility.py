# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The deliverables give no cutover advice: the cutover is the customer's (#2107).

Maintainer decision of 2026-11-10 (#2062, #2107): keep the generic migration guidance,
make the timing the customer's, and say so in one sentence. The wave plan of every
deliverable carries "The cutover is your responsibility.", one fact computed in
``src/shared/report_facts.py`` (ADR-032) that the engineering report, the executive
report and the UI only print. Wave 1's gate and the "before cutover" mitigations state
no timing criterion, and nothing says "caught up", "in sync", "proven" or "ready" as a
readiness claim.

The sentence has one source, ``src/shared/cutover_message.py`` (#2106), which step 1 quotes
too. The guard against cutover advice, step 1's and the deliverables', with one list of
banned words, is ``tests/unit/docs/test_no_cutover_advice.py``; it renders ``REPORTS`` below.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.synthesis_report import _build_mitigation_strategies
from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md, resolve_migration_waves
from src.shared.cutover_message import CUTOVER_MESSAGE, DECOMMISSION_MESSAGE
from src.shared.report_facts import compute_report_facts
from tests.executive_pdf import executive_text, normalize
from tests.unit.agents.referee import test_migration_waves as built
from tests.unit.agents.referee.test_synthesis_mitigation_strategies import (
    EFFECTIVE,
    WORDPRESS_LIKE,
    _risk,
)
from tests.unit.report.test_migration_waves_rendering import _legacy_report, _report_with_waves
from tests.unit.report.test_price_list_date import _report

REPO = Path(__file__).resolve().parents[3]
SENTENCE = "The cutover is your responsibility."
# #2265: decommissioning the source is the customer's too, in the same plain form.
DECOMMISSION = "Decommissioning the source database is your responsibility."
UI_FACTS = REPO / "src/ui/src/utils/reportFacts.js"


def _built_report(**overrides: Any) -> dict[str, Any]:
    """A report whose waves and mitigations the current code built."""
    report = _report_with_waves()
    report["migration_waves"] = built._build(**overrides)
    severe = _risk("RISK-009", "PERFORMANCE_DEGRADATION", "HIGH", "dynamodb", query_ids=["q"])
    risks = [*WORDPRESS_LIKE, severe]
    report["risk_assessment"] = {
        "risks": risks,
        "mitigation_strategies": _build_mitigation_strategies(risks, EFFECTIVE),
    }
    return report


REPORTS = {
    "homogeneous": lambda: _built_report(),
    "cross-engine": lambda: _built_report(source_engine="sqlserver"),
    "legacy": _legacy_report,
}


# ---------------------------------------------------------------------------
# The fact
# ---------------------------------------------------------------------------


@pytest.mark.passes_on_main
def test_the_words() -> None:
    """One source: ``src/shared/cutover_message.py`` (#2106), the deliverables' too."""
    assert CUTOVER_MESSAGE == SENTENCE
    assert DECOMMISSION_MESSAGE == DECOMMISSION


def test_the_facts_hold_the_sentence() -> None:
    facts = compute_report_facts(_report(None, stored=False), waves=[])
    assert facts["cutover_responsibility"] == SENTENCE
    assert ReportFacts.model_validate(facts).cutover_responsibility == SENTENCE


def test_a_report_written_before_validates_with_none() -> None:
    facts = compute_report_facts(_report(None, stored=False), waves=[])
    del facts["cutover_responsibility"]
    assert ReportFacts.model_validate(facts).cutover_responsibility is None


@pytest.mark.parametrize("stored", [{}, {"cutover_responsibility": None}])
def test_the_facts_reader_fills_an_older_block(stored: dict[str, None]) -> None:
    old = copy.deepcopy(_report(None))
    old["facts"].pop("cutover_responsibility")
    old["facts"].update(stored)
    assert report_facts(old)["cutover_responsibility"] == SENTENCE


def test_the_ui_fallback_words_equal_the_fact() -> None:
    assert f"CUTOVER_RESPONSIBILITY = '{SENTENCE}'" in UI_FACTS.read_text()


# ---------------------------------------------------------------------------
# Each deliverable prints it next to the wave plan
# ---------------------------------------------------------------------------


def _roadmap(md: str) -> str:
    return md.split("## Migration roadmap", 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_the_engineering_reports_wave_plan_states_it_under_its_opening_and_in_wave_1(
    name: str,
) -> None:
    """Every occurrence counted: the line under the opening sentence and wave 1's gate."""
    md = render_engineering_report_md(REPORTS[name]())
    roadmap = _roadmap(md)
    lines = roadmap.splitlines()
    assert roadmap.count(SENTENCE) == 2
    assert lines.count(f"*{SENTENCE}*") == 1
    wave_1 = roadmap.split("### Wave 1", 1)[1].split("### Wave 2", 1)[0]
    gate = next(line for line in wave_1.splitlines() if line.startswith("- Gate before"))
    assert gate.endswith(f"{SENTENCE} {DECOMMISSION}") and wave_1.count(SENTENCE) == 1
    # #2265: the decommissioning sentence once, in wave 1's gate only.
    assert wave_1.count(DECOMMISSION) == 1 and roadmap.count(DECOMMISSION) == 1
    assert "decommission" not in roadmap.replace(DECOMMISSION, "").lower()
    # Right under the suggested-path note, before the first wave.
    note = next(i for i, line in enumerate(lines) if "one suggested adoption path" in line)
    assert lines[note + 2] == f"*{SENTENCE}*"
    assert lines.index(f"*{SENTENCE}*") < next(
        i for i, line in enumerate(lines) if line.startswith("### Wave 1")
    )


def test_the_engineering_report_prints_the_stored_words() -> None:
    report = _built_report()
    report["facts"] = report_facts(report)
    report["facts"]["cutover_responsibility"] = "Yours to run."
    assert "*Yours to run.*" in _roadmap(render_engineering_report_md(report))


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_the_executive_reports_wave_plan_states_it_under_its_opening_and_in_wave_1(
    name: str,
) -> None:
    """Every occurrence counted: once under the opening sentence, once in wave 1's card,
    none in a later wave's card."""
    text = normalize(executive_text(REPORTS[name]()))
    intro, cards = text.split("one suggested adoption path", 1)[1].split("WAVE 1", 1)
    wave_1, later = cards.split("WAVE 2", 1)
    assert intro.count(SENTENCE) == 1
    assert wave_1.count(SENTENCE) == 1
    assert later.split("WAVE 4", 1)[0].count(SENTENCE) == 0
    # #2265: the decommissioning sentence once, in wave 1's card only.
    assert wave_1.count(DECOMMISSION) == 1 and text.count(DECOMMISSION) == 1
    assert "decommission" not in text.replace(DECOMMISSION, "").lower()


@pytest.mark.passes_on_main
def test_no_wave_plan_no_sentence() -> None:
    report = _report_with_waves()
    del report["migration_waves"], report["cache_overlay"]
    report["ranking"] = []
    assert resolve_migration_waves(report) == []
    assert SENTENCE not in render_engineering_report_md(report)


# ---------------------------------------------------------------------------
# The texts the issue names
# ---------------------------------------------------------------------------


def test_the_legacy_wave_1_gate_gives_no_cutover_or_decommission_timing() -> None:
    wave1 = resolve_migration_waves(_legacy_report())[0]
    assert wave1["gate"] == (
        "Schema and data parity validated against the source database. Later waves move "
        "from Aurora MySQL, not from the source database. The cutover is your "
        "responsibility. Decommissioning the source database is your responsibility."
    )


def test_the_unmitigated_and_load_test_strategies_give_no_cutover_timing() -> None:
    strategies = _built_report()["risk_assessment"]["mitigation_strategies"]
    assert (
        "RISK-009 (HIGH, DynamoDB; 1 query): no mitigation recorded; define one. " f"{SENTENCE}"
    ) in strategies
    load = [s for s in strategies if s.startswith("Load-test the queries behind")]
    assert load and all(s.endswith(f"production-scale data. {SENTENCE}") for s in load)


def test_the_transformation_contract_description_is_neutral() -> None:
    from src.contracts.transformation_contract import Rule

    text = Rule.model_fields["moves_rows"].description or ""
    assert "after cutover" not in text
    assert text.endswith("a not_moved warning finding says why and what to rebuild")


def test_the_help_page_quotes_the_wave_1_gate() -> None:
    page = (REPO / "docs/use-claude-code.md").read_text()
    gate = built._build()[0]["gate"].split(". ", 1)[1]
    assert f'wave 1\'s gate reads "{gate}"' in page
    assert "caught up" not in page.split("## Move step 1", 1)[0]


@pytest.mark.parametrize("name", ["docs/use-claude-code.md", "docs/understand-the-results.md"])
def test_the_help_pages_say_nothing_about_decommissioning_but_the_sentence(name: str) -> None:
    """#2265: no "decommission the source once you have completed your cutover"."""
    page = re.sub(r"<!--.*?-->", "", (REPO / name).read_text(), flags=re.S)  # claim markers
    assert DECOMMISSION in page
    assert "decommission" not in page.replace(DECOMMISSION, "").lower()
