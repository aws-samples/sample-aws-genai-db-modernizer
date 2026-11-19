# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2484: every deliverable says which objects an Aurora design leaves out, from one fact.

Synthesis lists them once in ``facts.design_left_out`` (name, kind of note, reason); the
engineering report lists each one, the executive report counts them in one sentence (it
names no table) and the results page shows them in an alert (Jest). A report written
before the fact gets it at read time from its design notes. A design that leaves nothing
out changes no deliverable.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.contracts.synthesis_output import ReportFacts
from src.report.engineering_report import render_engineering_report_md
from src.report.executive_report import build_story
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, design_left_out_facts

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"
REASON = (
    "its schema and name match another table of the design once case and quote marks are "
    "ignored, so neither is designed; rename one, or design them by hand."
)
NOTES = [
    {"feature": "colliding_table_name", "source_object": "Sales.orders",
     "recommendation": f"Table not created: {REASON}"},
    {"feature": "colliding_table_name", "source_object": "sales.orders",
     "recommendation": f"Table not created: {REASON}"},
    {"feature": "ambiguous_reference", "source_object": "sales.lines.fk_customer",
     "recommendation": "Foreign key not created: its referenced table customers is in more "
     "than one other schema (archive, crm) and not in this table's, so the design cannot "
     "tell which one it references; add it by hand once you know."},
]  # fmt: skip
BRIEF = (
    "The Aurora MySQL design leaves out 2 tables and 1 foreign key. The engineering report "
    "and the results page name each one and say why."
)


def _plain() -> dict:
    report: dict = json.loads(FIXTURE.read_text())
    assert report.get("facts") is None
    return report


def _with_notes() -> dict:
    report = _plain()
    design = report["schema_designs"]["aurora_mysql"]
    design["app_layer_notes"] = [*design["app_layer_notes"], *copy.deepcopy(NOTES)]
    return report


def _story_text(report: dict) -> str:
    return " ".join(getattr(f, "text", "") or "" for f in build_story(report))


def test_synthesis_lists_each_left_out_object_once() -> None:
    facts = compute_report_facts(_with_notes())
    assert facts["design_left_out"] == [
        {
            "engine": "aurora_mysql",
            "object_count": 3,
            "objects": [
                {"name": "Sales.orders", "feature": "colliding_table_name",
                 "label": "Colliding table name", "object": "table", "reason": REASON},
                {"name": "sales.orders", "feature": "colliding_table_name",
                 "label": "Colliding table name", "object": "table", "reason": REASON},
                {"name": "sales.lines.fk_customer", "feature": "ambiguous_reference",
                 "label": "Ambiguous reference", "object": "foreign key",
                 "reason": NOTES[2]["recommendation"].removeprefix("Foreign key not created: ")},
            ],  # fmt: skip
            "brief_line": BRIEF,
            "ddl_names_each": False,
        }
    ]
    ReportFacts.model_validate(facts)


def test_a_design_that_leaves_nothing_out_states_an_empty_list() -> None:
    assert design_left_out_facts(_plain()) == []
    assert compute_report_facts(_plain())["design_left_out"] == []


def test_a_block_written_before_2484_gains_the_fact_at_read_time() -> None:
    """The value decides, not the key: a block re-dumped through the contract reads the
    field back as None, and one with no key at all is older still."""
    report = _with_notes()
    stored = compute_report_facts(report)
    expected = stored.pop("design_left_out")
    stored["engine_count"] = 99  # a stored figure is never recomputed
    for block in (stored, {**stored, "design_left_out": None}):
        report["facts"] = block
        facts = report_facts(report)
        assert facts["design_left_out"] == expected
        assert facts["engine_count"] == 99
    # A stored empty list is kept as stored.
    report["facts"] = {**stored, "design_left_out": []}
    assert report_facts(report)["design_left_out"] == []
    dumped = ReportFacts.model_validate({**stored, "design_left_out": None}).model_dump(mode="json")
    report["facts"] = dumped
    assert report_facts(report)["design_left_out"] == expected


def test_the_engineering_report_lists_each_object_from_the_fact() -> None:
    report = _with_notes()
    report["facts"] = compute_report_facts(report)
    md = render_engineering_report_md(report)
    assert "**Not created (3):** the design leaves these out" in md
    assert f"- Colliding table name on table `Sales.orders`: {REASON}" in md
    assert f"- Colliding table name on table `sales.orders`: {REASON}" in md
    assert "- Ambiguous reference on foreign key `sales.lines.fk_customer`: its referenced" in md
    # Listed once: not again among the application-layer notes.
    assert md.count("Sales.orders") == 1
    # The report prints the fact, not the notes: an edited fact shows.
    report["facts"]["design_left_out"][0]["objects"][0]["name"] = "From.facts"
    assert "`From.facts`" in render_engineering_report_md(report)


def test_the_executive_report_counts_them_and_names_no_table() -> None:
    report = _with_notes()
    report["facts"] = compute_report_facts(report)
    text = _story_text(report)
    assert BRIEF in text
    assert "Sales.orders" not in text and "fk_customer" not in text


def test_a_report_with_nothing_left_out_states_nothing() -> None:
    plain = _plain()
    md = render_engineering_report_md(plain)
    assert "Not created (" not in md
    assert "leaves out" not in _story_text(plain)


def test_a_report_with_nothing_left_out_renders_as_before() -> None:
    """The empty fact adds nothing: the engineering report of a design with no left-out
    note is the same with the fact as with a block that lacks it."""
    report = _plain()
    report["facts"] = compute_report_facts(report)
    with_fact = render_engineering_report_md(report)
    old = copy.deepcopy(report)
    del old["facts"]["design_left_out"]
    assert render_engineering_report_md(old) == with_fact


def test_the_fact_says_whether_the_script_carries_the_comments() -> None:
    """One flag, computed once from the stored script: true only when it ends with the
    comment block (a design saved before #2484 has none)."""
    report = _with_notes()
    design = report["schema_designs"]["aurora_mysql"]
    assert design_left_out_facts(report)[0]["ddl_names_each"] is False
    design["ddl_names_left_out"] = True
    assert design_left_out_facts(report)[0]["ddl_names_each"] is True


def test_the_engineering_report_names_the_ddl_only_when_it_carries_the_comments() -> None:
    clause = "the generated DDL names each one in a comment"
    report = _with_notes()
    report["facts"] = compute_report_facts(report)
    md = render_engineering_report_md(report)
    assert "**Not created (3):** the design leaves these out." in md
    assert clause not in md
    report["facts"]["design_left_out"][0]["ddl_names_each"] = True
    assert f"the design leaves these out, and {clause}." in render_engineering_report_md(report)


def test_the_summary_flags_a_script_that_ends_with_the_comments() -> None:
    from src.agents.referee.synthesis_handler import _build_aurora_schema_summary
    from src.shared.left_out_objects import with_not_created_comments

    notes = copy.deepcopy(NOTES)
    base = {"app_layer_notes": notes, "tables": [], "validation_passed": True}
    old = _build_aurora_schema_summary("aurora_mysql", {**base, "generated_ddl": "SELECT 1;"})
    assert old["ddl_names_left_out"] is False
    new = _build_aurora_schema_summary(
        "aurora_mysql", {**base, "generated_ddl": with_not_created_comments("SELECT 1;", notes)}
    )
    assert new["ddl_names_left_out"] is True


def test_synthesis_leaves_a_left_out_name_with_a_dash_as_the_merge_wrote_it() -> None:
    """The DDL comment keeps ``a -- b.orders``; the fact, the reports and the alert must
    name the same table, so the dash pass leaves a left-out note's name alone."""
    from src.agents.referee.synthesis_prose import clean_schema_design_prose

    left_out = {**NOTES[0], "source_object": "a -- b.orders"}
    other = {"feature": "trigger", "source_object": "a -- b", "recommendation": "Move it."}
    cleaned, _ = clean_schema_design_prose("aurora_mysql", {"app_layer_notes": [left_out, other]})
    assert cleaned is not None
    names = [n["source_object"] for n in cleaned["app_layer_notes"]]
    assert names[0] == "a -- b.orders"
    assert names[1] != "a -- b"  # any other label still loses its dash
