"""Every cost figure is the full-modernization estimate, with one caveat (#1474).

The label and the caveat are two facts computed once in ``src/shared/report_facts.py``
(ADR-032); the engineering report, the PDF executive report, the chat summary and the UI
only print them. A report written before the facts existed prints the same words.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.report.formatting import cost_note_text
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import COST_CAVEAT, COST_SCOPE_LABEL, compute_report_facts
from tests.executive_pdf import executive_text, normalize
from tests.unit.report.test_price_list_date import _report

LABEL = "Full-modernization estimate: all waves done, every kept engine running."
CAVEAT = (
    "Estimates from public on-demand list prices for the stated Region. Check them with "
    "your own pricing in the AWS Pricing Calculator (https://calculator.aws/)."
)
UI_FACTS = Path(__file__).resolve().parents[3] / "src/ui/src/utils/reportFacts.js"


def _old(report: dict[str, Any], **keys: Any) -> dict[str, Any]:
    """The report as saved before the facts: no keys, or None after a contract round trip."""
    old = copy.deepcopy(report)
    for key in ("cost_scope_label", "cost_caveat"):
        old["facts"].pop(key)
        if key in keys:
            old["facts"][key] = keys[key]
    return old


def test_the_words() -> None:
    assert (COST_SCOPE_LABEL, COST_CAVEAT) == (LABEL, CAVEAT)


def test_the_facts_hold_the_label_and_the_caveat() -> None:
    facts = compute_report_facts(_report(None, stored=False), waves=[])
    assert (facts["cost_scope_label"], facts["cost_caveat"]) == (LABEL, CAVEAT)
    model = ReportFacts.model_validate(facts)
    assert (model.cost_scope_label, model.cost_caveat) == (LABEL, CAVEAT)


def test_a_report_written_before_validates_with_none() -> None:
    facts = compute_report_facts(_report(None, stored=False), waves=[])
    del facts["cost_scope_label"], facts["cost_caveat"]
    model = ReportFacts.model_validate(facts)
    assert (model.cost_scope_label, model.cost_caveat) == (None, None)


@pytest.mark.parametrize("keys", [{}, {"cost_scope_label": None, "cost_caveat": None}])
def test_the_facts_reader_fills_an_older_block(keys: dict[str, None]) -> None:
    facts = report_facts(_old(_report(None), **keys))
    assert (facts["cost_scope_label"], facts["cost_caveat"]) == (LABEL, CAVEAT)


def test_the_stored_words_are_the_ones_printed() -> None:
    report = _report(None)
    report["facts"]["cost_scope_label"] = "Whole plan."
    report["facts"]["cost_caveat"] = "Check prices."
    md = render_engineering_report_md(report)
    assert "Whole plan." in md and "Check prices." in md
    assert LABEL not in md and CAVEAT not in md


class TestTheEngineeringReport:
    def test_the_cost_section_leads_with_the_label_and_ends_with_the_caveat(self) -> None:
        md = render_engineering_report_md(_report(None))
        cost = md.split("## Cost", 1)[1].split("\n## ", 1)[0]
        assert cost.strip().startswith(LABEL.rstrip("."))
        assert cost.index(LABEL.rstrip(".")) < cost.index("Projected monthly")
        assert cost.rstrip().endswith("https://calculator.aws/\\).")
        assert "Estimates from public on-demand list prices for the stated Region." in cost

    def test_an_older_report_prints_the_same_words(self) -> None:
        for keys in ({}, {"cost_scope_label": None, "cost_caveat": None}):
            old = render_engineering_report_md(_old(_report(None), **keys))
            assert old == render_engineering_report_md(_report(None))


class TestThePdfReport:
    def test_the_label_and_caveat_are_printed(self) -> None:
        text = executive_text(_report(None))
        assert normalize(LABEL) in text
        assert normalize(CAVEAT) in text

    def test_an_older_report_prints_the_same_words(self) -> None:
        text = executive_text(_old(_report(None)))
        assert normalize(LABEL) in text and normalize(CAVEAT) in text

    def test_the_cost_tile_keeps_its_title_next_to_the_figure(self) -> None:
        assert re.search(r"\$121\.06 Projected monthly cost", executive_text(_report(None)))


class TestTheChatSummary:
    def _final(self, report: dict[str, Any]) -> str:
        return render_final_summary(
            report,
            database_name="shop",
            job_id="job1",
            deliverables_dir="artifacts/shop/job1/deliverables/",
            deliverables=[],
        )

    def test_the_projected_cost_is_followed_by_the_note(self) -> None:
        lines = self._final(_report(None)).splitlines()
        i = next(i for i, line in enumerate(lines) if line.startswith("- Projected monthly cost"))
        assert lines[i + 1] == f"- {LABEL} {CAVEAT}"

    def test_an_older_report_prints_the_same_words(self) -> None:
        assert f"- {LABEL} {CAVEAT}" in self._final(_old(_report(None)))


def test_the_note_is_the_label_then_the_caveat() -> None:
    assert cost_note_text({}) == f"{LABEL} {CAVEAT}"


def test_the_ui_fallback_words_equal_the_facts() -> None:
    """The results page falls back to these for a report with no facts."""
    source = UI_FACTS.read_text()
    assert f"COST_SCOPE_LABEL = '{LABEL}'" in source
    assert f"COST_CAVEAT = '{CAVEAT}'" in source
