# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The report states the customer's decision on a wave's design review warnings (#2067).

``/wave-edit`` records the decision under ``decisions/``, each warning by its number in
the list it showed and its own text; synthesis copies the assignment version's decisions
into ``report.json`` (``wave_warning_decisions``). One fact, each wave's readiness line
(and its short form for the executive report, #2075) with ``accepted_warnings`` and
``rejected_warnings``, repeats that decision while the review it was made on is the
wave's last. After a redesign kept a new design, the new review's warnings are a new
list for the customer to decide on: never matched to the old ones, never called fixed.
A report written before has no decisions and states the same line as before.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.contracts.synthesis_output import ReportFacts
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.executive_brief import decision_paragraph
from src.shared.report_facts import compute_report_facts
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text
from tests.unit.shared.test_wave_review_warnings import SMOKE_TRACE, WARNINGS, _designed, _review

A, B = ({"engine": "aurora_mysql", "n": n, **w} for n, w in enumerate(WARNINGS, 1))
NEXT = (
    "the next step is to revise the Aurora MySQL schema design in the modernization "
    "session and review it again"
)


def _decision(accepted: list[dict], rejected: list[dict], **extra: Any) -> dict[str, Any]:
    return {
        "wave": 1,
        "decision_version": 1,
        "assignment_version": 1,
        "accepted": accepted,
        "rejected": rejected,
        "auto": False,
        "recorded_at": "2026-11-09T10:00:00+00:00",
        **extra,
    }


def _decided(
    accepted: list[dict], rejected: list[dict], trace: dict = SMOKE_TRACE, **extra: Any
) -> dict[str, Any]:
    report = _designed(trace)
    report["wave_warning_decisions"] = [_decision(accepted, rejected, **extra)]
    report["facts"] = compute_report_facts(report)
    return report


def _redesigned(trace: dict, rejected: list[dict]) -> dict[str, Any]:
    """The decision, then a wave edit round that kept a new design reviewed as ``trace``."""
    report = _designed(trace)
    report["wave_warning_decisions"] = [_decision([], rejected)]
    report["wave_redesign"] = {
        "max_rounds": 2,
        "rounds": [
            {
                "round": 1,
                "assignment_version": 1,
                "engines": ["aurora_mysql"],
                "outcome": {"aurora_mysql": "improved"},
                "wave_edit": 1,
                "decision_version": 1,
            }
        ],
    }
    report["facts"] = compute_report_facts(report)
    return report


@pytest.mark.passes_on_main  # guards #1992's line: a report with no decision keeps it
def test_a_report_with_no_decision_states_the_same_line_as_before() -> None:
    before = _designed(SMOKE_TRACE)
    line = before["facts"]["waves"][0]["readiness_line"]
    assert line.startswith("Suggested, with 2 design review warnings to accept: ")
    old = copy.deepcopy(before)
    old.pop("wave_warning_decisions", None)
    assert compute_report_facts(old)["waves"][0]["readiness_line"] == line


def test_an_old_report_loads_and_its_waves_carry_no_decision() -> None:
    before = _designed(SMOKE_TRACE)
    row = before["facts"]["waves"][0]
    assert row["accepted_warnings"] == [] and row["rejected_warnings"] == []
    assert row["open_warning_count"] == 2 and row["rejected_warning_count"] == 0
    old = copy.deepcopy(before)
    for w in old["facts"]["waves"]:
        for key in (
            "accepted_warnings",
            "rejected_warnings",
            "accepted_warning_count",
            "rejected_warning_count",
            "open_warning_count",
        ):
            w.pop(key, None)
    ReportFacts.model_validate(old["facts"])


def test_every_warning_accepted_is_stated_as_accepted() -> None:
    report = _decided([A, B], [])
    row = report["facts"]["waves"][0]
    assert row["readiness"] == "suggested"
    assert row["readiness_line"] == (
        "Suggested, with 2 design review warnings. The customer accepted 2: Aurora MySQL "
        "shop.a: Add a composite index on (status, created_at); Aurora MySQL shop.b: Drop the "
        "unused index on b.note."
    )
    assert row["readiness_line_short"] == (
        "Suggested, with 2 design review warnings (listed in the engineering report): the "
        "customer accepted 2."
    )
    assert [(w["n"], w["target"]) for w in row["accepted_warnings"]] == [
        (1, "shop.a"),
        (2, "shop.b"),
    ]
    assert row["accepted_warning_count"] == 2 and row["open_warning_count"] == 0
    ReportFacts.model_validate(report["facts"])
    decision = decision_paragraph(report["facts"]) or ""
    assert "The customer accepted wave 1's 2 design review warnings, listed in the roadmap." in (
        decision
    )
    assert "Approving wave 1 accepts its" not in decision


def test_a_mixed_decision_states_each_part_in_one_voice() -> None:
    report = _decided([B], [A])
    row = report["facts"]["waves"][0]
    assert row["readiness_line"] == (
        "Suggested, with 2 design review warnings. The customer accepted 1: Aurora MySQL "
        "shop.b: Drop the unused index on b.note. The customer did not accept 1, still open: "
        f"Aurora MySQL shop.a: Add a composite index on (status, created_at); {NEXT}."
    )
    assert "you" not in row["readiness_line"].lower().split()
    assert row["readiness_line_short"] == (
        "Suggested, with 2 design review warnings (listed in the engineering report): the "
        "customer accepted 1 and did not accept 1, still open."
    )
    assert [w["target"] for w in row["rejected_warnings"]] == ["shop.a"]
    decision = decision_paragraph(report["facts"]) or ""
    assert (
        "The customer accepted 1 of wave 1's 2 design review warnings and did not accept 1, "
        "which are still open: approving wave 1 as it stands accepts them too."
    ) in decision


def test_a_decision_speaks_only_for_the_review_it_was_made_on() -> None:
    # The same two warnings in another order (or another text) are another review.
    swapped = {"trace": [{**SMOKE_TRACE["trace"][1], "warnings": WARNINGS[::-1]}]}
    report = _decided([A, B], [], trace=swapped)
    row = report["facts"]["waves"][0]
    assert row["readiness_line"].startswith("Suggested, with 2 design review warnings to accept")
    assert row["accepted_warnings"] == []


def test_a_kept_redesigns_new_review_is_a_new_list_never_matched() -> None:
    # The new review raises the rejected warning again in other words: not matched to
    # the old one, not "fixed", but a new warning to accept.
    reworded = {**WARNINGS[0], "change": "Index (status, created_at) together."}
    report = _redesigned(_review(0, [reworded]), [A])
    row = report["facts"]["waves"][0]
    assert row["readiness_line"] == (
        "Suggested: redesigned once after the customer did not accept 1 design review "
        "warning; the new review lists 1 design review warning to accept: Aurora MySQL "
        "shop.a: Index (status, created_at) together. If the customer does not accept them: "
        "revise the Aurora MySQL schema design in the modernization session and review it "
        "again."
    )
    assert "shop.a" not in row["readiness_line_short"]
    assert row["accepted_warnings"] == [] and row["rejected_warnings"] == []
    assert row["open_warning_count"] == 1
    assert "fixed" not in row["readiness_line"]
    decision = decision_paragraph(report["facts"]) or ""
    assert "Approving wave 1 accepts its 1 design review warning, listed in the roadmap." in (
        decision
    )


def test_a_kept_redesign_whose_review_lists_no_warning_says_so() -> None:
    report = _redesigned(_review(0, []), [A])
    row = report["facts"]["waves"][0]
    assert (
        row["readiness_line"]
        == row["readiness_line_short"]
        == (
            "Suggested: redesigned once after the customer did not accept 1 design review "
            "warning; the new review approved it with no warnings."
        )
    )


def test_a_decision_on_another_wave_changes_nothing_here() -> None:
    report = _decided([A, B], [], wave=2)
    row = report["facts"]["waves"][0]
    assert row["readiness_line"].startswith("Suggested, with 2 design review warnings to accept: ")
    assert row["accepted_warnings"] == [] and row["open_warning_count"] == 2


def test_every_deliverable_states_the_decision_and_the_executive_report_names_no_table() -> None:
    report = _decided([B], [A])
    row = report["facts"]["waves"][0]
    line, short = row["readiness_line"], row["readiness_line_short"]
    assert "The customer accepted 1: " in line and "the customer accepted 1" in short
    assert f"- Readiness: {line}" in md_plain(render_engineering_report_md(report))
    pdf = " ".join(executive_text(report).split())
    assert f"Readiness: {short}" in pdf
    assert "shop.a" not in pdf and "shop.b" not in pdf
    chat = md_plain(
        render_final_summary(
            report, database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
        )
    )
    assert f"- Wave 1: {line}" in chat
