# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DynamoDB merge-review decisions, in every deliverable (#1780).

When two design groups each gave a source table its own home, the DynamoDB group merge
keeps one as the authoritative home and the others as derived copies kept current from
its DynamoDB Streams (#333). Synthesis copies each decision into
``schema_designs.dynamodb.merge_reviews``; the facts layer states them once in
``facts.dynamodb_merge_reviews`` (ADR-032), each linked to its design-conflict risk. The
engineering report lists each decision, the executive report states the totals and the
review sentence without naming a table, and the results page lists the same lines
(``src/ui/src/components/__tests__/DynamoDBMergeReviewsCallout.test.js``, from the same
fixture).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.derived_copy_sync import derived_copy_sync_facts
from src.agents.referee.synthesis_handler import _build_standard_schema_summary
from src.agents.referee.synthesis_report import build_risk_assessment
from src.agents.schema_design.dynamodb_merge import (
    merge_overlaps,
    merge_review_decisions,
    overlap_home,
    recorded_home,
)
from src.agents.schema_design.group_merger import merge_group_drafts
from src.agents.schema_design.pattern_input import complete_pattern_members
from src.contracts.dynamodb_model_output import DynamoDBTradeOff
from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report import escaping
from src.report.facts import report_facts
from src.report.merge_reviews import MERGE_REVIEWS_PDF_NOTE, MERGE_REVIEWS_TITLE
from src.report.renderers import render_engineering_report_md
from src.shared.cutover_message import CUTOVER_MESSAGE
from src.shared.report_facts import (
    RECONSTRUCTED_NOTE,
    compute_report_facts,
    dynamodb_merge_review_facts,
    trade_off_topic_facts,
)
from tests.executive_pdf import executive_text
from tests.unit.agents.referee.test_synthesis_design_conflict_risk import _data
from tests.unit.agents.schema_design.test_dynamodb_merge_conflicts import _posts_drafts
from tests.unit.report.cache_role import with_cache_role_sentence

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "src/ui/src/components/__tests__/fixtures/dynamodbMergeReviews.json"
OLD_REPORT = (
    ROOT / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json"
)
EN = ROOT / "src/ui/src/locales/en.json"
MD_HEADING = f"## {MERGE_REVIEWS_TITLE}"

SAID = "cutover_responsibility"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _report(dynamodb: dict[str, Any] | None, risks: list[dict] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-11-03T00:00:00Z",
        "ranking": [{"target": "dynamodb", "confidence_score": 70, "workload_percent": 100.0}],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {"dynamodb": dynamodb} if dynamodb is not None else {},
        "risk_assessment": {
            "overall_risk_level": "MEDIUM" if risks else "LOW",
            "risks": risks or [],
            "mitigation_strategies": [],
        },
        "tied_queries": [],
    }
    report["facts"] = compute_report_facts(report)
    return report


def _fixture_report() -> dict[str, Any]:
    data = _fixture()
    return _report(data["schema_design"], data["risks"])


def _section(md: str) -> str:
    start = md.index(MD_HEADING)
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


TABLES = ("WpPosts", "wp_posts", "Comments", "CommentsByPost", "comment_meta", "wp_comment")


# --- the merge and synthesis --------------------------------------------------------


def test_the_merge_states_the_home_and_copies_it_chose() -> None:
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    [overlap] = merge_overlaps(merged)

    [decision] = merge_review_decisions(merged)

    assert decision["source_tables"] == overlap["source_tables"] == ["wordpress.wp_posts"]
    assert decision["authoritative_home"] == "WpPosts"
    assert decision["derived_copies"] == ["wp_posts"]
    assert decision["reconstructed"] is False
    # The merge recorded its choice on the trade-off, as structured fields.
    assert (overlap["authoritative_home"], overlap["derived_copies"]) == ("WpPosts", ["wp_posts"])
    assert "keeps WpPosts as the authoritative home" in overlap["description"]
    DynamoDBTradeOff.model_validate(overlap)


def _flipped(merged: dict[str, Any]) -> dict[str, Any]:
    """``merged`` after ``complete_pattern_members`` (#1602) adds five statements to the
    copy's read pattern: ranked again, the copy would now win the home."""
    [read] = [
        ap
        for ap in merged["access_patterns"]
        if ap["table_name"] == "wp_posts" and ap["operation"] in ("GetItem", "Query")
    ][:1]
    members = [read["query_ids"][0], *(f"q-added-{i}" for i in range(5))]
    completed, notes = complete_pattern_members(merged, [members])
    assert notes
    [overlap] = merge_overlaps(completed)
    unrecorded = {
        k: v for k, v in overlap.items() if k not in ("authoritative_home", "derived_copies")
    }
    assert overlap_home(unrecorded, completed["access_patterns"]) == ("wp_posts", ["WpPosts"])
    return completed


def test_the_recorded_home_holds_after_members_are_added_to_the_copy() -> None:
    """The review's case: the decision, the risk's mitigation (#1528) and the stream
    sync (#1527) all state the home the merge chose and its write notes point at."""
    completed = _flipped(merge_group_drafts(_posts_drafts(3, 1), "dynamodb"))

    [decision] = merge_review_decisions(completed)
    assert (decision["authoritative_home"], decision["derived_copies"]) == (
        "WpPosts",
        ["wp_posts"],
    )
    assert decision["reconstructed"] is False
    notes = [
        ap["key_condition"]
        for ap in completed["access_patterns"]
        if "write " in str(ap.get("key_condition"))
    ]
    assert notes and all("write WpPosts, the authoritative home" in n for n in notes)
    risks = build_risk_assessment(_data(completed))["risks"]
    [risk] = [r for r in risks if r["risk_type"] == "DATA_CONSISTENCY"]
    assert "current from WpPosts, the authoritative home" in risk["mitigation"]
    [sync] = derived_copy_sync_facts(completed, risks, {})["syncs"]
    assert sync["authoritative_home"] == "WpPosts"
    fact = _report(_build_standard_schema_summary(completed), risks)["facts"]
    [row] = fact["dynamodb_merge_reviews"]["decisions"]
    assert row["line"].startswith(
        "wordpress.wp_posts: the merge chose WpPosts as the authoritative home"
    )
    assert row["risk_id"] == risk["risk_id"] == sync["risk_id"]


def test_a_design_merged_before_the_record_says_its_decision_was_reconstructed() -> None:
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    [overlap] = merge_overlaps(merged)
    del overlap["authoritative_home"], overlap["derived_copies"]  # a pre-#1780 design

    summary = _build_standard_schema_summary(merged)
    risks = build_risk_assessment(_data(merged))["risks"]
    report = _report(summary, risks)

    [decision] = summary["merge_reviews"]
    assert decision["reconstructed"] is True and decision["authoritative_home"] == "WpPosts"
    fact = report["facts"]["dynamodb_merge_reviews"]
    assert fact["reconstructed_count"] == 1
    assert fact["decisions"][0]["line"].endswith(RECONSTRUCTED_NOTE)
    assert fact["summary_line"].endswith(
        "1 choice was reconstructed from the design's access patterns: the design was "
        "merged before the merge recorded it."
    )
    md = _section(render_engineering_report_md(report))
    assert escaping.md_text(RECONSTRUCTED_NOTE) in md
    assert "1 choice was reconstructed" in executive_text(report)


def test_a_record_that_does_not_split_the_targets_is_ranked_again() -> None:
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    [overlap] = merge_overlaps(merged)

    for home, copies in (("Elsewhere", ["wp_posts"]), ("WpPosts", []), ("WpPosts", "wp_posts")):
        overlap["authoritative_home"], overlap["derived_copies"] = home, copies
        assert recorded_home(overlap) is None
        [decision] = merge_review_decisions(merged)
        assert decision["reconstructed"] is True


def test_synthesis_copies_the_decisions_and_the_facts_link_their_risk() -> None:
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    summary = _build_standard_schema_summary(merged)
    risks = build_risk_assessment(_data(merged))["risks"]
    [risk] = [r for r in risks if r["risk_type"] == "DATA_CONSISTENCY"]

    assert summary["merge_reviews"] == merge_review_decisions(merged)
    fact = _report(summary, risks)["facts"]["dynamodb_merge_reviews"]
    assert fact["risk_ids"] == [risk["risk_id"]]
    assert fact["decisions"][0]["risk_id"] == risk["risk_id"]
    assert (fact["source_table_count"], fact["derived_copy_count"]) == (1, 1)


def test_an_overlap_naming_one_table_records_no_decision() -> None:
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    [overlap] = merge_overlaps(merged)
    overlap["target_tables"] = overlap["target_tables"][:1]

    assert merge_review_decisions(merged) == []
    assert "merge_reviews" not in _build_standard_schema_summary(merged)


@pytest.mark.passes_on_main  # a merge with no overlap already adds no review
def test_a_merge_with_no_overlap_adds_nothing_to_report_json() -> None:
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    merged["trade_offs"] = []

    summary = _build_standard_schema_summary(merged)

    assert "merge_reviews" not in summary


# --- the facts layer ----------------------------------------------------------------


def test_the_fixture_facts_are_the_facts_layer_output() -> None:
    data = _fixture()
    report = {"schema_designs": {"dynamodb": data["schema_design"]}}
    report["risk_assessment"] = {"risks": data["risks"]}

    facts = dynamodb_merge_review_facts(report)

    assert facts == data["facts"]
    ReportFacts.model_validate(_fixture_report()["facts"])


def test_the_summary_wording_for_one_decision() -> None:
    design = {
        "merge_reviews": [
            {
                "source_tables": ["a"],
                "authoritative_home": "A",
                "derived_copies": ["a2"],
            }
        ]
    }

    fact = dynamodb_merge_review_facts({"schema_designs": {"dynamodb": design}})

    assert fact is not None
    assert fact["summary_line"] == (
        "The DynamoDB group merge found 1 source table modelled independently by more than "
        "one design group. It chose 1 authoritative home, which takes every write to that "
        "source table, and kept 1 table as a derived copy updated from DynamoDB Streams."
    )
    # No risk in the register: the line names none, and none is listed.
    assert fact["risk_ids"] == [] and fact["decisions"][0]["risk_id"] is None
    assert fact["decisions"][0]["line"] == (
        "a: the merge chose A as the authoritative home, which takes every write to it; "
        "a2 is a derived copy updated from the DynamoDB Streams of A."
    )


def test_a_risk_of_another_overlap_or_type_is_not_linked() -> None:
    data = _fixture()
    other = copy.deepcopy(data["risks"])
    other[0]["risk_type"] = "MIGRATION_COMPLEXITY"
    other[1]["affected_tables"] = ["wp_comments"]  # another overlap's source tables
    report = {"schema_designs": {"dynamodb": data["schema_design"]}}
    report["risk_assessment"] = {"risks": other}

    fact = dynamodb_merge_review_facts(report)

    assert fact is not None
    assert [d["risk_id"] for d in fact["decisions"]] == [None, None]
    assert fact["risk_ids"] == []


def test_no_decisions_no_fact() -> None:
    assert dynamodb_merge_review_facts({"schema_designs": {}}) is None
    assert dynamodb_merge_review_facts({"schema_designs": {"dynamodb": {}}}) is None
    broken = {"merge_reviews": [{"source_tables": ["a"], "derived_copies": ["b"]}, "x"]}
    assert dynamodb_merge_review_facts({"schema_designs": {"dynamodb": broken}}) is None
    assert _report({"status": "completed"})["facts"]["dynamodb_merge_reviews"] is None


# --- the deliverables -----------------------------------------------------------------


def test_the_engineering_report_lists_every_decision() -> None:
    data = _fixture()
    section = _section(render_engineering_report_md(_fixture_report()))

    assert escaping.md_text(data["facts"]["summary_line"]) in section
    assert escaping.md_text(data["facts"]["review_line"]) in section
    printed = [line[2:] for line in section.splitlines() if line.startswith("- ")]
    assert printed == [escaping.md_text(d["line"]) for d in data["facts"]["decisions"]]


def test_the_executive_report_states_the_decisions_and_names_no_table() -> None:
    data = _fixture()
    text = executive_text(_fixture_report())

    assert MERGE_REVIEWS_TITLE in text
    assert data["facts"]["summary_line"] in text
    assert data["facts"]["review_line"] in text
    assert " ".join(MERGE_REVIEWS_PDF_NOTE.split()) in text
    assert "Data-consistency risks: RISK-004, RISK-005" in text
    decisions = text[text.index(MERGE_REVIEWS_TITLE) :]
    for name in TABLES:
        assert name not in decisions


def test_the_three_deliverables_state_the_same_decisions() -> None:
    """The engineering report and the executive report print the fact's own sentences;
    the results page prints the same fixture lines (DynamoDBMergeReviewsCallout.test.js)
    under the same title."""
    data = _fixture()
    report = _fixture_report()
    fact = report_facts(report)["dynamodb_merge_reviews"]

    assert fact == data["facts"]
    md = _section(render_engineering_report_md(report))
    text = executive_text(report)
    for sentence in (fact["summary_line"], fact["review_line"]):
        assert escaping.md_text(sentence) in md and sentence in text
    en = json.loads(EN.read_text(encoding="utf-8"))
    assert en["analysis-results.merge-reviews.header"] == MERGE_REVIEWS_TITLE == data["title"]


def test_the_renderers_print_the_stored_facts() -> None:
    doctored = _fixture_report()
    fact = doctored["facts"]["dynamodb_merge_reviews"]
    fact["summary_line"] = "Stored summary."
    fact["decisions"][0]["line"] = "Stored line."

    md = _section(render_engineering_report_md(doctored))

    assert "Stored summary." in md and "- Stored line." in md
    assert "Stored summary." in executive_text(doctored)


def test_customer_text_is_escaped() -> None:
    design = {
        "merge_reviews": [
            {
                "source_tables": ["a|b"],
                "authoritative_home": "<b>Home</b>*x*",
                "derived_copies": ["c_d"],
            }
        ]
    }
    report = _report(design)

    section = _section(render_engineering_report_md(report))

    line = report["facts"]["dynamodb_merge_reviews"]["decisions"][0]["line"]
    assert f"- {escaping.md_text(line)}" in section
    assert "<b>" not in section and "c_d" not in section
    # The PDF card prints the fact's table-free sentences and the risk ids, as text.
    doctored = copy.deepcopy(report)
    doctored["facts"]["dynamodb_merge_reviews"]["risk_ids"] = ["RISK-<b>1</b> & 2"]
    assert "Data-consistency risks: RISK-<b>1</b> & 2" in executive_text(doctored)


@pytest.mark.passes_on_main  # no decision, no section or card, as before
def test_nothing_is_shown_without_decisions() -> None:
    report = _report({"status": "completed"})

    assert MD_HEADING not in render_engineering_report_md(report)
    assert MERGE_REVIEWS_TITLE not in executive_text(report)


# --- a report written before #1780 ----------------------------------------------------


@pytest.mark.passes_on_main  # the old job already rendered without it
def test_an_old_job_loads_and_renders_unchanged() -> None:
    old = json.loads(OLD_REPORT.read_text(encoding="utf-8"))
    stored = copy.deepcopy(old["facts"])
    assert "dynamodb_merge_reviews" not in stored

    SynthesisOutputContract.model_validate(old)
    facts = report_facts(old)

    # Read back unchanged, but for the trade-off topic groups a block written before
    # #1221 gains from the same function.
    # #2107: and the cutover sentence, which a block written before it reads back with.
    assert facts["cutover_responsibility"] == CUTOVER_MESSAGE
    # The reader fills these for an older report (ADR-032 Decision 4; #1221, #2107, #1128).
    READ_TIME_FILLED = ("trade_off_topics", SAID, "slice_plan", "adoption")
    assert {k: v for k, v in facts.items() if k not in READ_TIME_FILLED} == (
        with_cache_role_sentence(stored)
    )
    assert facts["trade_off_topics"] == trade_off_topic_facts(
        old, stored["reality_check_trade_offs"]
    )
    assert MD_HEADING not in render_engineering_report_md(old)


def test_an_overlap_synthesized_before_1780_renders_as_before() -> None:
    """A real overlap synthesized before #1780: the DynamoDB summary has the merge's
    review trade-off and the register its design-conflict risk, but no ``merge_reviews``.
    No fact, no section, no card; its facts read back unchanged."""
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    [overlap] = merge_overlaps(merged)
    del overlap["authoritative_home"], overlap["derived_copies"]
    summary = _build_standard_schema_summary(merged)
    del summary["merge_reviews"]
    risks = build_risk_assessment(_data(merged))["risks"]
    report = _report(summary, risks)
    assert overlap["description"] in [t["description"] for t in summary["trade_offs"]]
    assert any(r["risk_type"] == "DATA_CONSISTENCY" for r in risks)
    assert report["facts"]["dynamodb_merge_reviews"] is None
    del report["facts"]["dynamodb_merge_reviews"]
    stored = copy.deepcopy(report["facts"])

    assert report_facts(report) == stored
    assert MD_HEADING not in render_engineering_report_md(report)
    assert MERGE_REVIEWS_TITLE not in executive_text(report)


def test_a_facts_block_written_before_1780_gains_the_fact_from_schema_designs() -> None:
    report = _fixture_report()
    expected = report["facts"]["dynamodb_merge_reviews"]
    del report["facts"]["dynamodb_merge_reviews"]

    assert report_facts(report)["dynamodb_merge_reviews"] == expected


def test_two_reviews_of_the_same_sources_get_their_own_risks() -> None:
    """The re-review's case: a review carried in from an earlier merge, with the same
    source tables and other targets, makes two overlaps and two risks with equal
    affected tables. Each risk goes to one overlap, in the merge's order, in the
    decisions and the stream sync alike, and ``risk_ids`` lists each once."""
    merged = merge_group_drafts(_posts_drafts(3, 1), "dynamodb")
    [overlap] = merge_overlaps(merged)
    stale = {
        **copy.deepcopy(overlap),
        "target_tables": ["WpPosts", "wp_posts_old"],
        "derived_copies": ["wp_posts_old"],
    }
    merged["trade_offs"].append(stale)
    assert len(merge_overlaps(merged)) == 2
    risks = build_risk_assessment(_data(merged))["risks"]
    conflict_ids = [r["risk_id"] for r in risks if r["risk_type"] == "DATA_CONSISTENCY"]
    assert len(conflict_ids) == 2

    fact = _report(_build_standard_schema_summary(merged), risks)["facts"]
    reviews = fact["dynamodb_merge_reviews"]
    syncs = derived_copy_sync_facts(merged, risks, {})["syncs"]

    assert [d["risk_id"] for d in reviews["decisions"]] == conflict_ids
    assert reviews["risk_ids"] == conflict_ids
    assert [s["risk_id"] for s in syncs] == conflict_ids
    assert [d["derived_copies"] for d in reviews["decisions"]] == [["wp_posts"], ["wp_posts_old"]]


def test_one_source_table_spelled_two_ways_counts_once() -> None:
    def review(source: str, home: str) -> dict[str, Any]:
        return {"source_tables": [source], "authoritative_home": home, "derived_copies": ["c"]}

    design = {"merge_reviews": [review("wordpress.wp_posts", "A"), review("wp_posts", "B")]}

    fact = dynamodb_merge_review_facts({"schema_designs": {"dynamodb": design}})

    assert fact is not None
    assert (fact["decision_count"], fact["source_table_count"]) == (2, 1)
    assert "found 1 source table modelled independently" in fact["summary_line"]
