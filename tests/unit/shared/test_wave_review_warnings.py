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
    assert f"Readiness: {line}" in " ".join(executive_text(report).split())
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
