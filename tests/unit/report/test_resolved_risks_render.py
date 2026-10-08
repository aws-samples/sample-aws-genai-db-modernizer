"""Risks the assignment resolved are visible in the deliverables (#221).

``risk_assessment.resolved_risks`` records analysis risks whose queries the effective
assignment moved to an engine that serves them. The engineering report lists them in a
"Resolved by the assignment" section and the decision report's risk line counts them,
so a HIGH risk never vanishes from a deliverable without a trace.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.report import renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"

RESOLVED = [
    {
        "engine": "aurora_mysql",
        "severity": "HIGH",
        "description": "[aurora_mysql] Single-row SELECT by primary key at very high frequency.",
        "affected_tables": ["wordpress.wp_options"],
        "query_ids": ["q1"],
        "resolved_on": "dynamodb",
        "reason": "the Aurora MySQL analysis recommended DynamoDB for these queries and the "
        "assignment moved them there",
    },
    {
        "engine": "aurora_mysql",
        "severity": "MEDIUM",
        "description": "[aurora_mysql] Table accessed by at most 2 query patterns.",
        "affected_tables": [],
        "query_ids": ["q2"],
        "resolved_on": "dynamodb",
        "reason": "the queries moved to DynamoDB, whose schema design serves all of them",
    },
]


@pytest.fixture
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    data = copy.deepcopy(data)
    # deepcopy RESOLVED too -- a test that mutates its own report's resolved_risks
    # must not leak that mutation into every other test sharing this fixture.
    data["risk_assessment"]["resolved_risks"] = copy.deepcopy(RESOLVED)
    return data


def test_engineering_report_lists_resolved_risks(report: dict) -> None:
    md = renderers.render_engineering_report_md(report)
    assert "## Resolved by the assignment (2)" in md
    section = md.split("## Resolved by the assignment (2)", 1)[1].split("\n## ", 1)[0]
    # #221 follow-up: display names, not the raw engine ids ("aurora_mysql",
    # "dynamodb") the description and resolved_on fields carry.
    assert "HIGH · Aurora MySQL → DynamoDB — Single-row SELECT" in section
    assert "aurora_mysql" not in section
    assert "aurora\\_mysql" not in section
    assert (
        "Resolved because the Aurora MySQL analysis recommended DynamoDB for these queries "
        "and the assignment moved them there."
    ) in section


def test_engineering_report_resolved_risks_use_display_names_for_unknown_engine(
    report: dict,
) -> None:
    """An engine id with no display-name entry falls back to a title-cased id.

    ``src.shared.engine_names.display_engine`` prettifies rather than passing the raw
    id through, so even an engine the mapping has not caught up with still reads as
    prose instead of ``unmapped_engine``.
    """
    report["risk_assessment"]["resolved_risks"][0]["engine"] = "neptune"
    report["risk_assessment"]["resolved_risks"][0]["resolved_on"] = "unmapped_engine"
    md = renderers.render_engineering_report_md(report)
    section = md.split("## Resolved by the assignment (2)", 1)[1].split("\n## ", 1)[0]
    assert "Neptune → Unmapped Engine" in section


def test_decision_report_counts_resolved_risks(report: dict) -> None:
    html = renderers.render_decision_report_html(report)
    assert "2 more were resolved by the assignment." in html


def test_no_resolved_risks_no_section(report: dict) -> None:
    report["risk_assessment"].pop("resolved_risks")
    assert "Resolved by the assignment" not in renderers.render_engineering_report_md(report)
    assert "resolved by the assignment" not in renderers.render_decision_report_html(report)


def test_resolved_risk_label_kept_when_resolved_onto_its_own_engine() -> None:
    """#434 review: a risk resolved without moving anywhere used to render
    "Aurora MySQL → Aurora MySQL", naming the same engine twice as if the
    queries moved. It did not move; say so."""
    risk = {"engine": "aurora_mysql", "resolved_on": "aurora_mysql"}
    assert renderers.resolved_risk_label(risk) == "Aurora MySQL (kept)"


def test_resolved_risk_label_moved() -> None:
    risk = {"engine": "aurora_mysql", "resolved_on": "dynamodb"}
    assert renderers.resolved_risk_label(risk) == "Aurora MySQL → DynamoDB"


def test_resolved_risk_label_no_resolved_on() -> None:
    assert renderers.resolved_risk_label({"engine": "aurora_mysql"}) == "Aurora MySQL"


def test_engineering_report_says_kept_not_the_same_engine_twice(report: dict) -> None:
    report["risk_assessment"]["resolved_risks"][0]["resolved_on"] = "aurora_mysql"
    md = renderers.render_engineering_report_md(report)
    section = md.split("## Resolved by the assignment (2)", 1)[1].split("\n## ", 1)[0]
    # "(" and ")" are backslash-escaped in Markdown flowing text (escaping.md_text).
    assert "Aurora MySQL \\(kept\\)" in section
    assert "Aurora MySQL → Aurora MySQL" not in section


def test_decision_report_states_resolved_count_when_no_open_risks_remain(report: dict) -> None:
    """#434: with no open risks the "Risk posture" section used to disappear
    whenever there were also no mitigation strategies, saying nothing about the
    risks the assignment resolved -- "overall risk LOW" with no section at all
    reads as "no migration risk"."""
    report["risk_assessment"]["risks"] = []
    report["risk_assessment"]["mitigation_strategies"] = []
    html = renderers.render_decision_report_html(report)
    assert "No open migration risks; 2 were resolved by the assignment" in html
    assert "see the Engineering Report" in html
    assert renderers.NO_OPEN_RISKS_CAVEAT in html


def test_decision_report_no_section_when_truly_no_risks(report: dict) -> None:
    report["risk_assessment"]["risks"] = []
    report["risk_assessment"]["resolved_risks"] = []
    report["risk_assessment"]["mitigation_strategies"] = []
    html = renderers.render_decision_report_html(report)
    assert "Risk posture" not in html


def test_mitigation_repeating_the_description_is_not_rendered_twice(report: dict) -> None:
    """#222 item 3: a mitigation contained in the description is omitted."""
    report["risk_assessment"]["risks"] = [
        {
            "risk_id": "RISK-002",
            "risk_type": "MIGRATION_COMPLEXITY",
            "severity": "MEDIUM",
            "description": "[dynamodb] aggregation: COUNT and SUM have no server-side "
            "equivalent.  Compute them application-side.",
            "mitigation": "compute them   APPLICATION-side.",
        },
        {
            "risk_id": "RISK-003",
            "risk_type": "MIGRATION_COMPLEXITY",
            "severity": "MEDIUM",
            "description": "[dynamodb] aggregation: COUNT(*) is not served.",
            "mitigation": "Maintain a counter with UpdateItem.",
        },
    ]
    md = renderers.render_engineering_report_md(report)
    register = md.split("## Risk register", 1)[1].split("\n## ", 1)[0]
    assert register.count("Mitigation:") == 1
    assert "Mitigation: Maintain a counter with UpdateItem." in register
