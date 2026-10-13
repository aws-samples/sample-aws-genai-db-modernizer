"""The plain-language workload-fit definition appears once in every deliverable
that shows the routing fit score (bug: Deliverables present a fit score as a
confidence percentage).

Each deliverable states the shared ``WORKLOAD_FIT_DEFINITION``
(``src/shared/workload_fit_labels.py``) verbatim, at the figure's first
appearance:

- the decision report (HTML): right after the recommendation table, where the
  per-engine "Why" cell first states a routed fit (``renderers.
  render_decision_report_html``);
- the engineering report (Markdown): right after the "Target engines" table's
  "Why" column, where a routed fit first appears (``renderers.
  render_engineering_report_md``) -- deliberately not under "Migration map"
  (which only renders once schema design has produced ``table_mappings``):
  review round 2's own re-render of the deterministic wordpress sample (no
  schema design, as every ``--llm-mode none`` run is) found the definition
  missing from the engineering report entirely, because it had been placed
  under a section that run never renders;
- the deck: the "Workload Coverage and Engine Assignment" slide's
  ``footer_note``, where the per-engine fit percentage first appears
  (``pptx_report.slide_coverage``) -- exactly once, whether or not the report
  carries ``routed_confidence``. Review round 1 found the deck restating the
  full definition a second time, verbatim, on the "Engine Fit and Open
  Decisions" slide whenever ``routed_confidence`` was present, and the
  original version of this test used a fixture with no ``routed_confidence``
  set, so it never exercised that path and never caught the duplicate; this
  version checks both a report without it (``e2e09_report.json``) and one
  with it (``test_deck_routed_confidence._report``, the discourse shape after
  #296/#304).

The UI analysis export, the React results page and the approval gate are
covered by the JS parity suite (``src/ui/src/utils/__tests__/
workloadFitDefinition.test.js``) and ``test_workload_fit_labels_js_sync.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.report import pptx_report, renderers
from src.shared.workload_fit_labels import WORKLOAD_FIT_DEFINITION
from tests.unit.report.test_deck_routed_confidence import _report as _routed_report

FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def test_decision_report_states_the_definition_once(report: dict) -> None:
    html = renderers.render_decision_report_html(report)
    assert html.count(WORKLOAD_FIT_DEFINITION) == 1


def test_engineering_report_states_the_definition_once(report: dict) -> None:
    md = renderers.render_engineering_report_md(report)
    assert md.count(WORKLOAD_FIT_DEFINITION) == 1


def test_engineering_report_states_the_definition_without_schema_design() -> None:
    """Review round 2: a report with no ``table_mappings`` (schema design never
    ran, e.g. every ``--llm-mode none`` run) must still state the definition --
    "Target engines" is unconditional, "Migration map" is not."""
    report_without_schema_design = {
        "database_name": "wordpress",
        "ranking": [
            {"target": "aurora_mysql", "confidence_score": 30, "routed_confidence": 30},
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "aurora_mysql", "rationale": "30% mean fit across 96 queries."}
            ]
        },
    }
    md = renderers.render_engineering_report_md(report_without_schema_design)
    assert "table_mappings" not in report_without_schema_design
    assert "## Target engines" in md
    assert "## Migration map" not in md
    assert md.count(WORKLOAD_FIT_DEFINITION) == 1


def _full_deck_text(rep: dict[str, Any]) -> str:
    f = pptx_report.derive(rep, {})
    prs = pptx_report.open_deck(keep=1)
    for build in pptx_report.SLIDES:
        build(prs, f)
    texts = [
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame
    ]
    return " ".join(" ".join(texts).split())


def test_deck_states_the_definition_exactly_once_without_routed_confidence(
    report: dict,
) -> None:
    assert "routed_confidence" not in report.get("ranking", [{}])[0]
    full_text = _full_deck_text(report)
    assert full_text.count(WORKLOAD_FIT_DEFINITION) == 1


def test_deck_states_the_definition_exactly_once_with_routed_confidence() -> None:
    rep = _routed_report()
    assert rep["ranking"][0].get("routed_confidence") is not None
    full_text = _full_deck_text(rep)
    assert full_text.count(WORKLOAD_FIT_DEFINITION) == 1
