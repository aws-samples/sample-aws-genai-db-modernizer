"""The plain-language workload-fit definition appears once in every deliverable
that shows the routing fit score (bug: Deliverables present a fit score as a
confidence percentage).

Each deliverable states the shared ``WORKLOAD_FIT_DEFINITION``
(``src/shared/workload_fit_labels.py``) verbatim, at the figure's first
appearance:

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
  (the PDF report's engines table) -- exactly once, whether or not the report
  carries ``routed_confidence``. Review round 1 found the deck restating the
  full definition a second time, verbatim, on the "Engine Fit and Open
  Decisions" slide whenever ``routed_confidence`` was present, and the
  original version of this test used a fixture with no ``routed_confidence``
  set, so it never exercised that path and never caught the duplicate; this
  version checks both a report without it (``e2e09_report.json``) and one
  with it (``test_deck_routed_confidence._report``, the discourse shape after
  #296/#304).

The React results page and the approval gate are
covered by the JS parity suite (``src/ui/src/utils/__tests__/
workloadFitDefinition.test.js``) and ``test_workload_fit_labels_js_sync.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.report import renderers
from src.shared.workload_fit_labels import WORKLOAD_FIT_DEFINITION
from tests.executive_pdf import engineering_text

FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def _routed_report() -> dict[str, Any]:
    """The discourse shape after #296/#304, with routed confidence."""
    return {
        "database_name": "discourse",
        "job_id": "job-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "ranking": [
            {
                "target": "aurora_postgresql",
                "confidence_score": 67,
                "analysis_confidence": 67,
                "routed_confidence": 93,
                "workload_percent": 76.5,
                "assigned_queries": 1266,
            },
            {
                "target": "dynamodb",
                "confidence_score": 62,
                "analysis_confidence": 62,
                "routed_confidence": 91,
                "workload_percent": 23.3,
                "assigned_queries": 385,
            },
            {
                "target": "opensearch",
                "confidence_score": 2,
                "analysis_confidence": 2,
                "routed_confidence": 60,
                "workload_percent": 0.2,
                "assigned_queries": 3,
            },
            {
                "target": "elasticache",
                "role": "cache_layer",
                "confidence_score": 56,
                "analysis_confidence": 56,
                "routed_confidence": 87,
                "routed_confidence_basis": "cached_reads",
                "workload_percent": 0.0,
                "assigned_queries": 0,
                "cache_overlay_queries": 3,
                "cache_call_share_percent": 25.1,
            },
        ],
        "cache_overlay": {"engine": "elasticache", "query_count": 3, "call_share_percent": 25.1},
        "recommended_architecture": {
            "databases": [
                {"service": "aurora_postgresql", "table_count": 0},
                {"service": "dynamodb", "table_count": 0},
                {"service": "opensearch", "table_count": 0},
                {"service": "elasticache", "table_count": 0},
            ]
        },
        "schema_designs": {},
    }


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


def test_md_states_the_definition_exactly_once_without_routed_confidence(
    report: dict,
) -> None:
    assert "routed_confidence" not in report.get("ranking", [{}])[0]
    full_text = engineering_text(report)
    assert full_text.count(WORKLOAD_FIT_DEFINITION) == 1


def test_md_states_the_definition_exactly_once_with_routed_confidence() -> None:
    rep = _routed_report()
    assert rep["ranking"][0].get("routed_confidence") is not None
    full_text = engineering_text(rep)
    assert full_text.count(WORKLOAD_FIT_DEFINITION) == 1
