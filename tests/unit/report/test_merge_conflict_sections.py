# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The access patterns an unresolved DynamoDB merge leaves unserved, in every
deliverable (#802).

Synthesis copies the merged schema output's ``unresolved_merge_conflicts`` into
``schema_designs.dynamodb``; the facts layer states them once in
``facts.dynamodb_merge_conflicts`` (ADR-032). The engineering report lists each
failure with its access patterns and queries, the executive report states the
totals, and the results page lists them
(``src/ui/src/components/__tests__/DynamoDBMergeConflictsCallout.test.js``, from the
same fixture).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.synthesis_handler import _build_standard_schema_summary
from src.agents.schema_design.group_merger import merge_group_drafts
from src.contracts.synthesis_output import ReportFacts
from src.report import escaping
from src.report.facts import report_facts
from src.report.merge_conflicts import (
    MERGE_CONFLICTS_INTRO,
    MERGE_CONFLICTS_PDF_NOTE,
    MERGE_CONFLICTS_TITLE,
    NOT_RECORDED_TEXT,
)
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import (
    compute_report_facts,
    dynamodb_merge_conflict_facts,
    trade_off_topic_facts,
)
from tests.executive_pdf import executive_text
from tests.unit.agents.schema_design.valid_designs import (
    ddb_design,
    ddb_pattern,
    ddb_table,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "src/ui/src/components/__tests__/fixtures/dynamodbMergeConflicts.json"
OLD_REPORT = (
    ROOT / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json"
)
MD_HEADING = f"## {MERGE_CONFLICTS_TITLE}"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _report(dynamodb: dict[str, Any] | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-11-02T00:00:00Z",
        "ranking": [{"target": "dynamodb", "confidence_score": 70, "workload_percent": 100.0}],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {"dynamodb": dynamodb} if dynamodb is not None else {},
        "tied_queries": [],
    }
    report["facts"] = compute_report_facts(report)
    return report


def _section(md: str) -> str:
    start = md.index(MD_HEADING)
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


# --- synthesis copies the merge's list into report.json ---------------------------


def _merged(clean: bool) -> dict[str, Any]:
    users = ddb_design(
        [ddb_table("Main" if not clean else "Users", "users", pk="user_id")],
        [ddb_pattern("DDB-AP-1", "Main" if not clean else "Users", "users", "q-users")],
    )
    orders = ddb_design(
        [ddb_table("Main", "orders", pk="order_id")],
        [
            ddb_pattern("DDB-AP-1", "Main", "orders", "q-order"),
            ddb_pattern("DDB-AP-2", "Main", "orders", "q-lines"),
        ],
    )
    return merge_group_drafts([users, orders], "dynamodb")


def test_synthesis_copies_the_unserved_patterns_of_a_dropped_design() -> None:
    merged = _merged(clean=False)

    summary = _build_standard_schema_summary(merged)

    [conflict] = summary["unresolved_merge_conflicts"]
    assert conflict["query_ids"] == ["q-users"]
    assert conflict["failure"] in merged["validation_failures"]
    facts = compute_report_facts({"schema_designs": {"dynamodb": summary}})
    fact = facts["dynamodb_merge_conflicts"]
    assert fact["unserved_pattern_count"] == 1 and fact["unserved_query_count"] == 1
    assert fact["conflicts"][0]["pattern_ids"] == conflict["pattern_ids"]


@pytest.mark.passes_on_main  # a clean merge already adds no conflict
def test_a_clean_merge_adds_nothing_to_report_json() -> None:
    summary = _build_standard_schema_summary(_merged(clean=True))

    assert "unresolved_merge_conflicts" not in summary
    assert dynamodb_merge_conflict_facts({"schema_designs": {"dynamodb": summary}}) is None


# --- the facts layer ----------------------------------------------------------------


def test_the_fixture_facts_are_the_facts_layer_output() -> None:
    data = _fixture()

    facts = dynamodb_merge_conflict_facts({"schema_designs": {"dynamodb": data["schema_design"]}})

    assert facts == data["facts"]
    assert facts is not None
    assert (facts["conflict_count"], facts["unserved_pattern_count"]) == (4, 2)
    assert facts["unserved_query_count"] == 2  # distinct across conflicts
    assert (facts["at_risk_pattern_count"], facts["at_risk_query_count"]) == (1, 1)
    # DDB-AP-2 is listed by two conflicts: the second says so.
    assert [c["repeated_pattern_ids"] for c in facts["conflicts"]] == [[], ["DDB-AP-2"], [], []]
    legacy = data["legacy"]
    assert (
        dynamodb_merge_conflict_facts({"schema_designs": {"dynamodb": legacy["schema_design"]}})
        == legacy["facts"]
    )
    ReportFacts.model_validate({**_report(data["schema_design"])["facts"]})


def _summary(*conflicts: dict[str, Any]) -> str:
    design = {"unresolved_merge_conflicts": list(conflicts)}
    facts = dynamodb_merge_conflict_facts({"schema_designs": {"dynamodb": design}})
    assert facts is not None
    return str(facts["summary_line"])


def test_each_kind_has_its_summary_wording() -> None:
    f = "DynamoDB merge: x"
    unserved = {"failure": f, "status": "unserved", "pattern_ids": ["A"], "query_ids": ["q1"]}
    at_risk = {"failure": f, "status": "at_risk", "pattern_ids": ["B"], "query_ids": ["q2"]}
    none = {"failure": f, "status": "none_named"}
    unknown = {"failure": f, "status": "not_recorded"}
    left = "The DynamoDB group merge left"

    assert _summary(unserved) == (
        f"{left} 1 conflict unresolved: the merged design does not serve 1 access pattern "
        "(1 query)."
    )
    assert _summary(at_risk) == (
        f"{left} 1 conflict unresolved: 1 access pattern (1 query) is at risk until the "
        "conflicts are resolved."
    )
    assert _summary(none) == (
        f"{left} 1 conflict unresolved: the merge names no access pattern they leave unserved."
    )
    assert _summary(unknown) == (
        f"{left} 1 conflict unresolved: the unserved access patterns were not recorded for "
        "1 conflict."
    )
    # A pattern both unserved and at risk counts once, as unserved.
    both = {**at_risk, "pattern_ids": ["A", "B"], "query_ids": ["q1", "q2"]}
    assert _summary(unserved, both) == (
        f"{left} 2 conflicts unresolved: the merged design does not serve 1 access pattern "
        "(1 query); 1 more access pattern (1 query) is at risk until the conflicts are "
        "resolved."
    )
    assert "every access pattern" not in " ".join(
        _summary(c) for c in (unserved, at_risk, none, unknown)
    )


def test_a_legacy_failure_never_reads_as_served() -> None:
    """A design merged before #802 synthesized again: its patterns are unknown."""
    merged = _merged(clean=False)
    del merged["unresolved_merge_conflicts"]

    summary = _build_standard_schema_summary(merged)
    report = _report(summary)
    fact = report["facts"]["dynamodb_merge_conflicts"]

    assert fact["unrecorded_conflict_count"] == 1
    assert "were not recorded" in fact["summary_line"]
    md = _section(render_engineering_report_md(report))
    assert f"  - {NOT_RECORDED_TEXT}" in md
    assert "were not recorded" in executive_text(report)


def test_no_conflicts_no_fact() -> None:
    assert dynamodb_merge_conflict_facts({"schema_designs": {}}) is None
    assert dynamodb_merge_conflict_facts({"schema_designs": {"dynamodb": {}}}) is None
    assert _report({"status": "completed"})["facts"]["dynamodb_merge_conflicts"] is None


# --- the deliverables -----------------------------------------------------------------


def test_the_engineering_report_lists_every_conflict_and_what_it_leaves_unserved() -> None:
    data = _fixture()
    section = _section(render_engineering_report_md(_report(data["schema_design"])))

    assert escaping.md_text(data["facts"]["summary_line"]) in section
    assert escaping.md_text(MERGE_CONFLICTS_INTRO) in section
    lines = section.splitlines()
    for conflict in data["facts"]["conflicts"]:
        at = lines.index(f"- {escaping.md_text(conflict['failure'])}")
        detail = lines[at + 1]
        for value in [*conflict["pattern_ids"], *conflict["query_ids"]]:
            assert f"`{value}`" in detail
        if conflict["table_name"]:
            assert f"on `{conflict['table_name']}`" in detail
    assert "No access pattern is left unserved" not in section


def test_the_engineering_report_prints_the_lines_the_results_page_prints() -> None:
    """The fixture's lines are what DynamoDBMergeConflictsCallout.test.js expects too."""
    data = _fixture()
    section = _section(render_engineering_report_md(_report(data["schema_design"])))

    printed = [line[4:].replace("`", "") for line in section.splitlines() if line[:4] == "  - "]

    assert printed == data["not_served_lines"]
    legacy = _section(render_engineering_report_md(_report(data["legacy"]["schema_design"])))
    printed = [line[4:] for line in legacy.splitlines() if line[:4] == "  - "]
    assert printed == data["legacy"]["not_served_lines"]


def test_the_executive_report_states_the_totals_and_names_nothing() -> None:
    data = _fixture()
    text = executive_text(_report(data["schema_design"]))

    assert MERGE_CONFLICTS_TITLE in text
    assert data["facts"]["summary_line"] in text
    assert " ".join(MERGE_CONFLICTS_PDF_NOTE.split()) in text
    for conflict in data["facts"]["conflicts"]:
        for value in [*conflict["pattern_ids"], *conflict["query_ids"]]:
            assert value not in text
    assert "'Main'" not in text and "ByEmail" not in text


def test_the_renderers_print_the_stored_facts() -> None:
    report = _report(_fixture()["schema_design"])
    doctored = copy.deepcopy(report)
    doctored["facts"]["dynamodb_merge_conflicts"]["summary_line"] = "Stored line."
    doctored["facts"]["dynamodb_merge_conflicts"]["conflicts"][0]["pattern_ids"] = ["DDB-AP-9"]

    md = _section(render_engineering_report_md(doctored))

    assert "Stored line." in md and "`DDB-AP-9`" in md
    assert "Stored line." in executive_text(doctored)


def test_customer_text_is_escaped() -> None:
    design = {
        "unresolved_merge_conflicts": [
            {
                "failure": "DynamoDB merge: table 'A|B' <b>x</b> *y*",
                "table_name": "A|B",
                "pattern_ids": ["DDB-AP-1"],
                "query_ids": ["q`1"],
            }
        ]
    }
    report = _report(design)

    section = _section(render_engineering_report_md(report))

    assert f"- {escaping.md_text(design['unresolved_merge_conflicts'][0]['failure'])}" in section
    assert "<b>" not in section
    assert f"`{escaping.md_code('q`1')}`" in section
    executive_text(report)  # reportlab markup is escaped: renders without error


@pytest.mark.passes_on_main  # no conflict, no section, as before
def test_no_section_without_conflicts() -> None:
    report = _report({"status": "completed"})

    assert MD_HEADING not in render_engineering_report_md(report)
    assert MERGE_CONFLICTS_TITLE not in executive_text(report)


# --- a report written before #802 -----------------------------------------------------


@pytest.mark.passes_on_main  # the old job already rendered without it
def test_an_old_job_loads_and_renders_without_the_section() -> None:
    old = json.loads(OLD_REPORT.read_text(encoding="utf-8"))
    assert "dynamodb_merge_conflicts" not in old["facts"]

    facts = report_facts(old)

    assert facts.get("dynamodb_merge_conflicts") is None
    # Read back unchanged, but for the trade-off topic groups a block written before
    # #1221 gains from the same function.
    rest = {k: v for k, v in facts.items() if k != "trade_off_topics"}
    assert rest == old["facts"]
    assert facts["trade_off_topics"] == trade_off_topic_facts(
        old, old["facts"]["reality_check_trade_offs"]
    )
    assert MD_HEADING not in render_engineering_report_md(old)


def test_a_facts_block_written_before_802_gains_the_fact_from_schema_designs() -> None:
    report = _report(_fixture()["schema_design"])
    expected = report["facts"]["dynamodb_merge_conflicts"]
    del report["facts"]["dynamodb_merge_conflicts"]

    assert report_facts(report)["dynamodb_merge_conflicts"] == expected
