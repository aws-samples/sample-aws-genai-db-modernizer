# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each report states the owner rule its own lines follow (#2166 review).

Since #2166 a table no statement writes stays on the plan's relational engine; a report
saved before it lists such a table under the engine that serves most of its queries
(``owner_basis: most_queries``). The rule sentence above the lines is the report's own:
``facts.table_owner_lines.unwritten_owner_rule``, computed once in the facts layer from
the lines (ADR-032) and filled in at read time for a block stored before the field. The
engineering report's Migration map, the executive report and the UI callout
(``TableOwnersCallout.test.js``) print the sentence that fact names, so an old report
never states the new rule above old-rule rows.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report.escaping import md_text
from src.report.executive_report import render_executive_report_pdf
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from src.report.table_owners import (
    OWNER_RULE,
    OWNER_RULE_MOST_QUERIES,
    OWNER_RULE_SHORT,
    OWNER_RULE_SHORT_MOST_QUERIES,
)
from src.shared.report_facts import compute_report_facts
from tests.executive_pdf import normalize, section_texts
from tests.report_concision import section

REPO = Path(__file__).resolve().parents[3]
REAL_RUN = REPO / "tests/fixtures/real_runs/wordpress-both/report.json"
MODEL_RUN = REPO / "tests/unit/report/fixtures/wordpress_model_run_1_60_report.json"
PRE_212 = REPO / "tests/unit/report/fixtures/wordpress_sample_run_0_3_report.json"


def _load(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(path.read_text())
    return report


def _lines(report: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    facts = report_facts(report)["table_owner_lines"]
    return {r["table"]: (r["owner"], r["owner_basis"]) for r in facts["table_lines"]}


def _migration_map(report: dict[str, Any]) -> str:
    return section(render_engineering_report_md(report), "Migration map")


def _recommendation(report: dict[str, Any]) -> str:
    pdf = render_executive_report_pdf(report)
    return section_texts(pdf, str(report["database_name"]))["The recommendation"]


@pytest.mark.parametrize("path", [REAL_RUN, MODEL_RUN])
def test_a_report_saved_before_2166_states_its_own_rule(path: Path) -> None:
    """Its stored lines name a reader as the owner of a table nothing writes."""
    report = _load(path)
    assert "unwritten_owner_rule" not in report["facts"]["table_owner_lines"]
    assert ("dynamodb", "most_queries") in _lines(report).values()
    assert report_facts(report)["table_owner_lines"]["unwritten_owner_rule"] == "most_queries"
    md = _migration_map(report)
    assert md_text(OWNER_RULE_MOST_QUERIES) in md
    assert md_text(OWNER_RULE) not in md
    recommendation = _recommendation(report)
    assert normalize(OWNER_RULE_SHORT_MOST_QUERIES) in recommendation
    assert normalize(OWNER_RULE_SHORT) not in recommendation


def test_a_report_written_now_states_the_relational_step_rule() -> None:
    """The same saved run with its lines on the #2166 rule (no reader owns a table
    nothing writes), its facts block computed now: the new sentence."""
    report = copy.deepcopy(_load(REAL_RUN))
    for row in report["table_mappings"]:
        if row.get("owner_basis") == "most_queries":
            row.update(owner="aurora_mysql", owner_basis="retained")
    report["facts"] = compute_report_facts(report)
    assert report["facts"]["table_owner_lines"]["unwritten_owner_rule"] == "relational_step"
    md = _migration_map(report)
    assert md_text(OWNER_RULE) in md
    assert md_text(OWNER_RULE_MOST_QUERIES) not in md
    recommendation = _recommendation(report)
    assert normalize(OWNER_RULE_SHORT) in recommendation
    assert normalize(OWNER_RULE_SHORT_MOST_QUERIES) not in recommendation


def test_a_report_written_before_212_keeps_its_rule_and_its_basis() -> None:
    """The v0.3 sample run has no owner lines; they are filled in at read time with the
    rule its own waves used: a table nothing writes is the main reader's, with
    ``most_queries`` (ADR-032's #212 amendment), whoever that reader is."""
    report = _load(PRE_212)
    assert "table_owner_lines" not in report["facts"]
    lines = _lines(report)
    assert lines["wordpress.wp_commentmeta"] == ("dynamodb", "most_queries")
    for table in (
        "wordpress.wp_actionscheduler_groups",
        "wordpress.wp_term_relationships",
        "wordpress.wp_term_taxonomy",
        "wordpress.wp_terms",
        "wordpress.wp_usermeta",
        "wordpress.wp_users",
        "wordpress.wp_wc_admin_notes",
    ):
        assert lines[table] == ("aurora_mysql", "most_queries"), table
    assert report_facts(report)["table_owner_lines"]["unwritten_owner_rule"] == "most_queries"
    assert md_text(OWNER_RULE_MOST_QUERIES) in _migration_map(report)
