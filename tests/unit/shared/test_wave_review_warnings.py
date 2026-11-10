# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A design review with only warnings does not hold a wave (maintainer decision on
#2051, #1992).

A review's change requests are read by the contract's ``ChangeSeverity`` (``blocker``
or not), never from its prose. A blocker holds the wave; a review with only warnings
leaves it suggested, with each warning listed for the customer to accept by approving
it, in every deliverable from one fact. A warning is never a redesign trigger.
"""

from __future__ import annotations

import copy
from typing import Any

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_handler import _engine_schema_summaries
from src.agents.schema_design.wave_redesign import fixable_requests
from src.contracts.schema_design_trace import DesignTrace
from src.contracts.synthesis_output import ReportFacts
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.executive_brief import decision_paragraph
from src.shared.report_facts import compute_report_facts
from src.shared.wave_readiness import open_review_count, review_change_summary, review_warnings
from src.tools.schema.base_schema_agent import SchemaDesignTrace
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text
from tests.unit.report.test_wave_readiness_deliverables import _report

WARNINGS = [
    {
        "category": "indexing",
        "target": "shop.a",
        "change": "Add a composite index on (status, created_at)",
    },
    {"category": "indexing", "target": "shop.b", "change": "Drop the unused index on b.note"},
]
# The Bedrock smoke's Aurora MySQL review on 4b080c13: "no blockers, but two warnings".
SMOKE_TRACE = {
    "trace": [
        {"type": "designer", "iteration": 0},
        {
            "type": "pe_review",
            "iteration": 0,
            "verdict": "CHANGES_REQUESTED",
            "change_requests": 2,
            "summary": "No blockers, but two warnings on indexes.",
            "blockers": 0,
            "warnings": WARNINGS,
        },
    ]
}


def _review(blockers: int, warnings: list[dict]) -> dict:
    return {
        "trace": [
            {
                "type": "pe_review",
                "iteration": 0,
                "verdict": "CHANGES_REQUESTED",
                "change_requests": blockers + len(warnings),
                "blockers": blockers,
                "warnings": warnings,
            }
        ]
    }


def _designed(trace: dict) -> dict[str, Any]:
    """The deliverables fixture with its Aurora MySQL design reviewed as ``trace``."""
    report = _report("MEDIUM")
    report["schema_designs"]["aurora_mysql"].update(
        open_review_count=open_review_count(trace),
        reviewed_count=1,
        review_warnings=review_warnings(trace),
    )
    report["facts"] = compute_report_facts(report)
    return report


def test_the_smokes_warning_only_review_leaves_the_wave_suggested_with_its_warnings() -> None:
    report = _designed(SMOKE_TRACE)
    one = report["facts"]["waves"][0]
    assert one["readiness"] == "suggested" and one["holds"] == []
    assert one["readiness_line"] == (
        "Suggested, with 2 design review warnings to accept: Aurora MySQL shop.a: Add a "
        "composite index on (status, created_at); Aurora MySQL shop.b: Drop the unused index "
        "on b.note. If you do not accept them: revise the Aurora MySQL schema design in your "
        "modernization session and review it again."
    )
    assert [w["target"] for w in one["review_warnings"]] == ["shop.a", "shop.b"]
    ReportFacts.model_validate(report["facts"])
    # Wave 1 is the decision, and approving it accepts the warnings.
    assert report["facts"]["executive_brief"]["decision_wave"] == 1
    decision = decision_paragraph(report["facts"]) or ""
    assert decision.startswith("Decision: approve wave 1.")
    assert "Approving wave 1 accepts its 2 design review warnings, listed in the roadmap." in (
        decision
    )


def test_every_deliverable_states_the_warnings_to_accept() -> None:
    report = _designed(SMOKE_TRACE)
    line = report["facts"]["waves"][0]["readiness_line"]
    assert f"- Readiness: {line}" in md_plain(render_engineering_report_md(report))
    # The executive report counts them and points to the engineering report (#2075).
    short = report["facts"]["waves"][0]["readiness_line_short"]
    assert short.startswith("Suggested, with 2 design review warnings to accept (listed in")
    assert f"Readiness: {short}" in " ".join(executive_text(report).split())
    chat = md_plain(
        render_final_summary(
            report, database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
        )
    )
    assert f"- Wave 1: {line}" in chat
    assert "accepts its 2 design review warnings" in chat
    assert report["facts"]["wave_readiness_notes"][0] == {
        "wave": 1,
        "readiness": "suggested",
        "line": line,
    }


def test_a_review_with_a_blocker_holds_the_wave() -> None:
    one = _designed(_review(1, []))["facts"]["waves"][0]
    assert one["readiness"] == "held"
    assert [h["check"] for h in one["holds"]] == ["open_review"]


def test_a_review_with_a_blocker_and_warnings_holds_and_lists_none() -> None:
    one = _designed(_review(1, WARNINGS))["facts"]["waves"][0]
    assert one["readiness"] == "held" and one["review_warnings"] == []
    assert "to accept" not in one["readiness_line"]


def test_a_warning_never_sends_the_design_back() -> None:
    report = _designed(SMOKE_TRACE)
    assert fixable_requests(report) == {}
    held = _designed(_review(1, []))
    assert list(fixable_requests(held)) == ["aurora_mysql"]


def test_blockers_are_read_from_the_contracts_severity_not_prose() -> None:
    summary = review_change_summary(
        [
            {"severity": "warning", "category": "indexing", "target": "t", "requested_change": "x"},
            {"severity": "blocker", "category": "keys", "target": "u", "requested_change": "y"},
            # DynamoDB's own scale: anything below blocker is a warning.
            {"severity": "major", "category": "gsi", "target": "v", "requested_change": "z"},
        ]
    )
    assert summary["blockers"] == 1 and [w["target"] for w in summary["warnings"]] == ["t", "v"]
    # The words "blocker" in the prose do not make one.
    prose = {"severity": "warning", "target": "t", "requested_change": "this is a blocker"}
    assert review_change_summary([prose])["blockers"] == 0


def test_the_bedrock_and_external_traces_record_blockers_and_warnings() -> None:
    from src.tools.schema.aurora_mysql_schema_agent import PEReviewResult

    review = PEReviewResult.model_validate(
        {
            "verdict": "CHANGES_REQUESTED",
            "change_requests": [
                {
                    "category": "indexing",
                    "severity": "warning",
                    "target": "shop.a",
                    "requested_change": "Add a composite index on (status, created_at)",
                    "rationale": "r",
                }
            ],
            "pe_notes": [],
            "strengths": [],
            "summary": "s",
        }
    )
    trace = SchemaDesignTrace()
    trace.log_pe_review(0, 1.0, review)
    entry = trace.entries[0]
    assert entry["blockers"] == 0 and entry["warnings"][0]["target"] == "shop.a"
    DesignTrace.model_validate({"trace": [entry]})
    # The synthesis summary reads them: no open review, one warning.
    data = SynthesisData(job_id="j", database_name="shop")
    data.engines["dynamodb"] = EngineArtifacts(
        engine="dynamodb",
        schema_design={"table_definitions": [{"table_name": "t", "source_tables": []}]},
        design_trace=DesignTrace.model_validate({"trace": [entry]}),
    )
    summary = _engine_schema_summaries(data)["dynamodb"]
    assert summary["open_review_count"] == 0 and len(summary["review_warnings"]) == 1
    # An external review recorded with its full answer: the same split.
    external = {"pe_review": {"source": "external", **review.model_dump(mode="json")}}
    assert open_review_count(external) == 0 and len(review_warnings(external)) == 1


def test_an_older_trace_with_no_severities_still_holds_when_not_approved() -> None:
    old = {"trace": [{"type": "pe_review", "iteration": 0, "verdict": "CHANGES_REQUESTED"}]}
    assert open_review_count(old) == 1 and review_warnings(old) == []
    report = copy.deepcopy(_designed(SMOKE_TRACE))
    for w in report["facts"]["waves"]:
        w.pop("review_warnings", None)
    assert all(w.review_warnings == [] for w in ReportFacts.model_validate(report["facts"]).waves)


# -- the executive report names no collected table (#2075) ---------------------------

# The WordPress shape the mirror e2e-llm run on PR #2070 printed: each warning names a
# collected table and its columns.
WP_WARNINGS = [
    {
        "category": "schema",
        "target": "wordpress.wp_posts",
        "change": "Drop the unused columns comment_count, comment_status, guid",
    },
    {
        "category": "indexing",
        "target": "wordpress.wp_postmeta",
        "change": "Add a composite index on (post_id, meta_key)",
    },
]
WP_COLLECTOR = {
    "database_schema": {
        "tables": [
            {
                "table_id": "wordpress.wp_posts",
                "table_name": "wp_posts",
                "schema_name": "wordpress",
                "columns": [
                    {"column_name": c} for c in ("comment_count", "comment_status", "guid")
                ],
            },
            {
                "table_id": "wordpress.wp_postmeta",
                "table_name": "wp_postmeta",
                "schema_name": "wordpress",
                "columns": [{"column_name": c} for c in ("post_id", "meta_key")],
            },
        ]
    }
}
WP_NAMES = {"wp_posts", "wordpress.wp_posts", "comment_count", "comment_status", "guid"}
WP_NAMES |= {"wp_postmeta", "wordpress.wp_postmeta", "post_id", "meta_key"}


def _executive_text(report: dict[str, Any]) -> str:
    from src.report.executive_report import collected_identifiers, render_executive_report_pdf
    from tests.executive_pdf import body_text

    pdf = render_executive_report_pdf(report, collected_identifiers(WP_COLLECTOR))
    return " ".join(body_text(pdf, "shop").split())


def test_the_executive_report_counts_the_warnings_and_names_no_table() -> None:
    from tests.executive_pdf import collected_table_names, table_name_hits

    report = _designed(_review(0, WP_WARNINGS))
    text = _executive_text(report)
    assert collected_table_names(WP_COLLECTOR) == WP_NAMES
    assert table_name_hits(text, WP_NAMES) == []
    one = report["facts"]["waves"][0]
    ReportFacts.model_validate(report["facts"])
    assert one["readiness_line_short"] == (
        "Suggested, with 2 design review warnings to accept (listed in the engineering "
        "report). If you do not accept them: revise the Aurora MySQL schema design in your "
        "modernization session and review it again."
    )
    assert f"Readiness: {one['readiness_line_short']}" in text
    # The engineering report, the chat summary and the facts keep the full list.
    assert "wordpress.wp_posts" in one["readiness_line"] and "guid" in one["readiness_line"]
    engineering = md_plain(render_engineering_report_md(report))
    assert f"- Readiness: {one['readiness_line']}" in engineering
    assert {"wordpress.wp_posts", "comment_count", "wordpress.wp_postmeta", "meta_key"} <= {
        n for n in WP_NAMES if n in engineering
    }
    chat = md_plain(
        render_final_summary(
            report, database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
        )
    )
    assert f"- Wave 1: {one['readiness_line']}" in chat


def test_a_failed_redesign_reason_stays_out_of_the_executive_report() -> None:
    """A held wave's note quotes a failed round's reason, free text that can name a
    table: the executive report states only that the round failed."""
    report = _report("HIGH")
    report["wave_redesign"] = {
        "rounds": [
            {
                "round": 1,
                "engines": ["dynamodb"],
                "outcome": {"dynamodb": "failed"},
                "failed": {
                    "dynamodb": "redesign round 1 failed: the designer stopped on "
                    "wordpress.wp_posts.guid"
                },
            }
        ]
    }
    report["facts"] = compute_report_facts(report)
    text = _executive_text(report)
    assert table_name_hits_none(text)
    two = report["facts"]["waves"][1]
    assert two["readiness"] == "held"
    assert "wordpress.wp_posts.guid" in two["readiness_line"]
    assert two["readiness_line_short"].startswith(
        "Not suggested yet (redesigned once, still held) (redesign round 1 failed; the "
        "earlier design is kept): an open high risk on DynamoDB"
    )
    assert f"Readiness: {two['readiness_line_short']}" in text


def table_name_hits_none(text: str) -> bool:
    from tests.executive_pdf import table_name_hits

    return table_name_hits(text, WP_NAMES) == []


def test_a_wave_with_nothing_to_shorten_reads_the_same_in_both_lines() -> None:
    report = _designed(_review(0, []))
    for w in report["facts"]["waves"]:
        assert w["readiness_line_short"] == w["readiness_line"]


def test_a_stored_block_from_before_the_short_line_gets_it_from_the_same_function() -> None:
    """A report.json written between #2051 and #2075 has no short line: the renderer
    reads it computed again from the same report, never the full warning text."""
    from src.report.facts import report_facts

    report = _designed(_review(0, WP_WARNINGS))
    expected = [w["readiness_line_short"] for w in report["facts"]["waves"]]
    for w in report["facts"]["waves"]:
        w.pop("readiness_line_short")
    assert [w["readiness_line_short"] for w in report_facts(report)["waves"]] == expected
    assert table_name_hits_none(_executive_text(report))


QID = "7ead756f" + "0" * 56


def _with_query(report: dict[str, Any]) -> dict[str, Any]:
    """The report holding ``QID`` as one of its query ids (a wave's table group)."""
    report["migration_waves"][1]["table_groups"] = [
        {"tables": ["shop.b"], "query_count": 1, "kind": "independent", "query_ids": [QID]}
    ]
    return report


def test_the_readiness_line_quotes_a_warnings_query_and_pattern_ids() -> None:
    """A warning's target is the review's own text: the engineering report quotes the
    query and access-pattern ids it names as the report does elsewhere (#2084); the
    fact stays plain."""
    from tests.report_concision import bare_ids, known_query_ids, raw_labels

    warning = {
        "category": "key_design",
        "target": "DDB-AP-15 / shop.b (query 7ead756f)",
        "change": "Add a sentinel item for the status lookup",
    }
    report = _with_query(_designed(_review(0, [])))
    report["schema_designs"]["dynamodb"].update(
        open_review_count=0, reviewed_count=1, review_warnings=[warning]
    )
    report["facts"] = compute_report_facts(report)
    two = report["facts"]["waves"][1]
    assert two["readiness"] == "suggested"
    assert "DynamoDB DDB-AP-15 / shop.b (query 7ead756f): Add a sentinel" in (two["readiness_line"])
    md = render_engineering_report_md(report)
    line = next(x for x in md.splitlines() if "Add a sentinel item" in x)
    assert "`DDB-AP-15`" in line and "`7ead756f`" in line
    assert bare_ids(md, known_query_ids(report)) == []
    assert raw_labels(md) == []
    assert line.count("`") == 4  # nothing else became a code span


def test_a_failed_redesign_reason_quotes_the_query_it_names() -> None:
    """A held wave's line quotes a failed round's reason, free text: a query id in it
    is quoted in the engineering report too (#2084)."""
    from tests.report_concision import bare_ids, known_query_ids

    report = _with_query(_report("HIGH"))
    report["wave_redesign"] = {
        "rounds": [
            {
                "round": 1,
                "engines": ["dynamodb"],
                "outcome": {"dynamodb": "failed"},
                "failed": {"dynamodb": f"redesign round 1 failed on query {QID[:8]}"},
            }
        ]
    }
    report["facts"] = compute_report_facts(report)
    assert f"query {QID[:8]}" in report["facts"]["waves"][1]["readiness_line"]
    md = render_engineering_report_md(report)
    assert f"query `{QID[:8]}`" in md
    assert bare_ids(md, known_query_ids(report)) == []
