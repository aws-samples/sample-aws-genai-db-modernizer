"""A report.json written before utility pins left the mean fit (#1389).

Its stored figures count the pins and its traces show their scores; the
renderers format what the file holds and cannot recount (ADR-032). Every view
therefore says once that the report predates the rule, from one helper, and a
report written after it says nothing.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from src.report import executive_report
from src.report.renderers import render_engineering_report_md
from src.shared.ranking import PIN_RULE_PREDATES_NOTE, report_predates_pin_rule

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    report: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return report


def _flat(text: str) -> str:
    return " ".join(text.split())


def _pdf_text(report: dict[str, Any]) -> str:
    pdf = executive_report.render_executive_report_pdf(report)
    return _flat(" ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages))


@pytest.fixture
def legacy() -> dict[str, Any]:
    """Written at contract 1.41: no ``routed_pinned_queries``, no trace ``pinned``."""
    report = _load("wordpress_1_41_flagged_report.json")
    assert all("routed_pinned_queries" not in e for e in report["ranking"])
    return report


@pytest.fixture
def current() -> dict[str, Any]:
    """Written at contract 1.60: every routed ranking entry counts its pins."""
    report = _load("wordpress_model_run_1_60_report.json")
    assert any(e.get("routed_pinned_queries") for e in report["ranking"])
    return report


def test_an_old_report_is_recognised_and_a_current_one_is_not(legacy, current) -> None:
    assert report_predates_pin_rule(legacy)
    assert not report_predates_pin_rule(current)


def test_the_edge_cases_say_nothing() -> None:
    assert not report_predates_pin_rule({})
    assert not report_predates_pin_rule({"ranking": []})
    # No routed figure to be wrong: nothing to flag.
    assert not report_predates_pin_rule({"ranking": [{"target": "dynamodb"}]})
    # A pin count of 0 is a current report with no pins.
    assert not report_predates_pin_rule(
        {"ranking": [{"routed_queries": 4, "routed_pinned_queries": 0}]}
    )


def test_one_entry_without_the_field_is_enough() -> None:
    report = {
        "ranking": [
            {"routed_queries": 4, "routed_pinned_queries": 0},
            {"routed_queries": 3},
        ]
    }
    assert report_predates_pin_rule(report)


def test_the_engineering_report_notes_an_old_report_once(legacy) -> None:
    md = _flat(render_engineering_report_md(legacy))
    assert md.count(PIN_RULE_PREDATES_NOTE) == 1


def test_the_engineering_report_of_a_current_report_has_no_note(current) -> None:
    assert PIN_RULE_PREDATES_NOTE not in _flat(render_engineering_report_md(current))


def test_the_executive_report_notes_an_old_report_and_not_a_current_one(legacy, current) -> None:
    assert PIN_RULE_PREDATES_NOTE in _pdf_text(legacy)
    assert PIN_RULE_PREDATES_NOTE not in _pdf_text(current)


def test_the_helper_does_not_change_the_report(legacy) -> None:
    before = copy.deepcopy(legacy)
    report_predates_pin_rule(legacy)
    assert legacy == before
