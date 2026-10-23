"""Every deliverable shows the same executive summary (#1403).

A report whose facts carry ``executive_brief`` shows the deterministic summary worded
from those facts (with or without a schema design, so a ``--llm-mode none`` run shows its
recommendation, risk and decision), a generated one only when a schema design backs it,
and the results page the same paragraphs. An older report keeps its stored summary.
"""

from __future__ import annotations

import copy

from src.report.engineering_report import WITHHELD_UNTRUSTED_NOTE, brief_summary, summary_paragraphs
from tests.unit.shared.test_executive_brief import FACTS


def _report(source: str = "deterministic", designed: bool = False, summary: str = "") -> dict:
    facts = copy.deepcopy(FACTS)
    report = {
        "database_name": "wordpress",
        "facts": facts,
        "ranking": [{"target": "dynamodb", "schema_design_available": designed}],
        "summary_deterministic": "Analyzed 50 source tables and 107 query patterns.",
        "summary_source": source,
    }
    report["summary"] = summary or brief_summary(report)
    return report


def test_a_deterministic_summary_is_shown_without_a_schema_design() -> None:
    report = _report()
    paragraphs, note = summary_paragraphs(report)
    assert [p.split(":", 1)[0] for p in paragraphs] == ["Recommendation", "Risk", "Decision"]
    assert note is None


def test_it_is_worded_from_the_facts_the_report_holds_now() -> None:
    report = _report()
    report["facts"]["executive_brief"]["overall_risk_level"] = "HIGH"
    paragraphs, _ = summary_paragraphs(report)
    assert paragraphs[1].startswith("Risk: overall risk is high.")


def test_a_generated_summary_with_a_schema_design_is_shown() -> None:
    report = _report("llm", designed=True, summary="Recommendation: generated.\nRisk: x.")
    assert summary_paragraphs(report) == (["Recommendation: generated.", "Risk: x."], None)


def test_a_generated_summary_with_no_schema_design_is_withheld_for_the_brief() -> None:
    report = _report("llm", designed=False, summary="Recommendation: generated.")
    paragraphs, note = summary_paragraphs(report)
    assert paragraphs[0].startswith("Recommendation: move the wordpress MySQL database")
    assert note == WITHHELD_UNTRUSTED_NOTE


def test_a_fallback_names_its_reason() -> None:
    report = _report("deterministic_fallback")
    report["summary_validation_warnings"] = [
        "[high confidence] executive structure: the summary claims a saving, but no source "
        "cost was collected: The plan saves 30%."
    ]
    _, note = summary_paragraphs(report)
    assert note and "claimed a saving with no source cost to compare against" in note


def test_an_older_report_keeps_its_stored_summary() -> None:
    report = _report()
    del report["facts"]["executive_brief"]
    report["summary"] = report["summary_deterministic"]
    paragraphs, _ = summary_paragraphs(report)
    assert paragraphs[0].startswith("Analyzed 50 source tables")
