# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-item texts are stated once in the engineering report, on every sample (#1454).

#1461 removed repeated paragraphs; sentences remained wherever a per-item template
printed one catalog explanation or target-native approach for each item: an analysis
risk's explanation in the risk register and again under "Resolved by the assignment",
each pending choice's option text, and each carried write's approach. Each test builds
the case from a shared sentence and checks the sentence is stated once and the other
places refer to where it is.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.tie_break import DECIDED_BY_RULES, DECIDED_BY_UNROUNDED_SCORE
from src.report.engine_choices import pending_choices_md
from src.report.engineering_report import _SaidOnce
from src.report.escaping import md_text
from src.report.renderers import render_engineering_report_md
from src.shared.design_text import carried_write_detail, split_carried_write_detail
from tests.report_concision import repeated_statements, section

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_model_run_1_60_report.json"

EXPLANATION = (
    "Aurora serves single-table CRUD natively and uses none of its relational features "
    "for it (Amazon Aurora User Guide, What is Amazon Aurora)."
)
TTL = (
    "Use Time to Live: write an epoch-seconds expiry attribute only on the items the "
    "cut-off applies to, so DynamoDB deletes expired items in the background with no write cost"
)


@pytest.fixture
def report() -> dict[str, Any]:
    r: dict[str, Any] = json.loads(FIXTURE.read_text())
    return copy.deepcopy(r)


def _open(risk_id: str, text: str) -> dict[str, Any]:
    return {
        "severity": "MEDIUM",
        "description": f"[dynamodb] Flagged by the Aurora analysis for 3 queries: {text}",
        "rule": "analysis",
        "risk_id": risk_id,
        "risk_type": "PERFORMANCE_DEGRADATION",
        "affected_tables": ["wordpress.wp_posts"],
        "query_ids": ["a" * 64],
        "mitigation": "",
    }


def _resolved(text: str, to: str = "dynamodb") -> dict[str, Any]:
    return {
        "engine": "aurora_mysql",
        "severity": "MEDIUM",
        "description": f"[aurora_mysql] Moved to DynamoDB: {text}",
        "affected_tables": ["wordpress.wp_posts"],
        "query_ids": ["a" * 64],
        "resolved_on": to,
        "reason": "the assignment moved them",
    }


def test_a_resolved_risk_refers_to_the_open_risk_that_states_its_explanation(
    report: dict[str, Any],
) -> None:
    text = f"Table with no foreign keys: all queries are single-table CRUD. {EXPLANATION}"
    risk = report["risk_assessment"]
    risk["risks"].append(_open("RISK-901", text))
    risk["resolved_risks"].append(_resolved(text))
    md = render_engineering_report_md(report)
    assert repeated_statements(md) == []
    register = section(md, "Risk register")
    resolved = section(md, "Resolved by the assignment")
    assert "Aurora serves single-table CRUD natively" in register
    assert "Aurora serves single-table CRUD natively" not in resolved
    # What the resolved risk drops is still reachable: it names the risk that states it.
    assert "Stated under RISK-901." in resolved
    # The part that is its own stays.
    assert "Moved to DynamoDB: Table with no foreign keys" in resolved


def test_a_sentence_no_open_risk_states_stays_in_the_resolved_risk(report: dict[str, Any]) -> None:
    report["risk_assessment"]["resolved_risks"].append(_resolved(f"Table. {EXPLANATION}"))
    resolved = section(render_engineering_report_md(report), "Resolved by the assignment")
    assert "Aurora serves single-table CRUD natively" in resolved
    assert "Stated under" not in resolved


def test_two_resolved_risks_state_a_shared_sentence_once(report: dict[str, Any]) -> None:
    risk = report["risk_assessment"]
    risk["resolved_risks"] += [
        _resolved(f"Table one. {EXPLANATION}"),
        _resolved(f"Table two. {EXPLANATION}", "aurora_mysql"),
    ]
    resolved = section(render_engineering_report_md(report), "Resolved by the assignment")
    assert resolved.count("Aurora serves single-table CRUD natively") == 1
    assert "Stated above." in resolved


def test_open_risks_state_a_shared_sentence_once(report: dict[str, Any]) -> None:
    risk = report["risk_assessment"]
    risk["risks"] += [
        _open("RISK-901", f"First. {EXPLANATION}"),
        _open("RISK-902", f"Second. {EXPLANATION}"),
    ]
    register = section(render_engineering_report_md(report), "Risk register")
    assert register.count("Aurora serves single-table CRUD natively") == 1
    assert "Stated under RISK-901." in register


def test_a_pointer_is_stated_once_where_two_sentences_point_at_one_place() -> None:
    """The discourse judge read "Stated in the X migration note. Stated in the X
    migration note." in one mitigation: two repeated sentences in a row that point at
    the same place are one pointer."""
    said = _SaidOnce()
    first = (
        "DynamoDB has no triggers, so the application writes the audit row itself on every "
        "insert. The application also checks the approval flag before it accepts every reply "
        "a user posts."
    )
    said.line(first, "in the x migration note")
    assert said.line(first, "under RISK-001") == "Stated in the x migration note."
    mixed = said.line(f"{first} A new sentence that nothing before has said at all.", "y")
    assert mixed == (
        "Stated in the x migration note. A new sentence that nothing before has said at all."
    )


def test_a_short_sentence_is_never_replaced(report: dict[str, Any]) -> None:
    risk = report["risk_assessment"]
    risk["risks"].append(_open("RISK-901", "Table is read often. Move it."))
    risk["resolved_risks"].append(_resolved("Table is read often. Move it."))
    md = render_engineering_report_md(report)
    assert "Stated under" not in section(md, "Resolved by the assignment")


def _choice(table: str, delta: str, first: str) -> dict[str, Any]:
    def option(engine: str, direction: str) -> dict[str, Any]:
        return {
            "engine": engine,
            "score": 78,
            "unrounded_score": 78.2,
            "trade_off": f"{first.format(engine=engine)}. It scores {delta} points {direction}.",
        }

    return {
        "table": f"discourse.{table}",
        "query_ids": ["q" * 64],
        "current_engine": "dynamodb",
        "decide_for_me_engine": "documentdb",
        "decide_for_me_reason": "DocumentDB scores higher",
        "why_tied": f"The tie on {table}.",
        "options": [option("documentdb", "higher"), option("dynamodb", "lower")],
    }


def test_pending_choices_state_an_options_first_sentence_once(report: dict[str, Any]) -> None:
    first = (
        "{engine} already serves 166 settled queries in this plan, so choosing it adds no engine"
    )
    report["pending_engine_choices"] = [
        _choice("a_table", "0.48", first),
        _choice("b_table", "0.05", first),
        _choice("c_table", "0.25", first),
    ]
    lines = pending_choices_md(report)
    md = "\n".join(lines)
    assert repeated_statements(md) == []
    # The first table states it; each later table refers to it and keeps its own delta.
    assert md.count("already serves 166 settled queries") == 2  # one per engine
    assert any(
        ln.startswith("- DocumentDB: as for `a_table`. It scores 0.05 points higher")
        for ln in lines
    )
    assert any(
        ln.startswith("- DynamoDB: as for `a_table`. It scores 0.25 points lower") for ln in lines
    )


def test_a_pending_choice_with_its_own_option_text_keeps_it(report: dict[str, Any]) -> None:
    report["pending_engine_choices"] = [
        _choice("a_table", "0.48", "{engine} serves 10 queries, so choosing it adds no engine now"),
        _choice("b_table", "0.05", "{engine} serves 20 queries, so choosing it adds no engine now"),
    ]
    md = "\n".join(pending_choices_md(report))
    assert "as for `" not in md
    assert md.count("serves 10 queries") == 2 and md.count("serves 20 queries") == 2


def _carried(report: dict[str, Any], details: list[str]) -> str:
    report["risk_assessment"]["risks"].append(
        {
            "severity": "MEDIUM",
            "description": "[dynamodb] DynamoDB serves 4 statements on 4 tables poorly.",
            "rule": "design.carried_writes",
            "risk_id": "RISK-903",
            "risk_type": "MIGRATION_COMPLEXITY",
            "affected_tables": ["wordpress.wp_posts"],
            "query_ids": ["a" * 64],
            "mitigation": "Design them.",
            "details": details,
        }
    )
    return section(render_engineering_report_md(report), "Risk register")


def test_carried_writes_with_one_approach_are_listed_under_it(report: dict[str, Any]) -> None:
    details = [
        carried_write_detail("delete", "ns.a", "Retention delete", 20, TTL),
        carried_write_detail("delete", "ns.b", "Retention delete", 19, TTL),
        carried_write_detail("update", "ns.c", "Consistency repair sweep", 14, "Write both copies"),
        carried_write_detail("delete", "ns.d", "Retention delete", 20, TTL),
    ]
    register = _carried(report, details)
    assert register.count("Use Time to Live") == 1
    assert repeated_statements(register) == []
    # Every statement is still named, with its score, and the other approach stays.
    for stmt in (
        "DELETE of a, retention delete (score 20)",
        "DELETE of b, retention delete (score 19)",
        "DELETE of d, retention delete (score 20)",
    ):
        assert md_text(stmt) in register
    update = "UPDATE of c, consistency repair sweep (score 14): Write both copies"
    assert f"  - {md_text(update)}" in register.splitlines()
    assert register.index("Statements: ") < register.index("Approach: Use Time to Live")


def test_carried_writes_with_different_approaches_keep_their_lines(report: dict[str, Any]) -> None:
    details = [
        carried_write_detail("delete", "ns.a", "Retention delete", 20, TTL),
        carried_write_detail("update", "ns.c", "Sweep", 14, "Write both copies"),
    ]
    register = _carried(report, details)
    lines = register.splitlines()
    assert f"  - {md_text('DELETE of a, retention delete (score 20): ' + TTL)}" in lines
    assert f"  - {md_text('UPDATE of c, sweep (score 14): Write both copies')}" in lines
    assert not [ln for ln in lines if "Statements: " in ln]


def test_the_detail_text_round_trips() -> None:
    text = carried_write_detail("delete", "ns.a", "Retention Delete", 20.5, "Do x; then y.")
    assert text == "DELETE of a, retention delete (score 20.5): Do x; then y"
    assert split_carried_write_detail(text) == (
        "DELETE of a, retention delete (score 20.5)",
        "Do x; then y",
    )
    assert split_carried_write_detail("A trade-off title, nothing else") is None


def test_pending_choices_state_the_scores_once_in_the_table(report: dict[str, Any]) -> None:
    """#1558: the table owns the scores; a subsection neither restates the tie nor
    repeats the score pair in its pick."""
    a = _choice("a_table", "0.48", "{engine} serves 10 queries, so choosing it adds one")
    a["why_tied"] = (
        "DocumentDB and DynamoDB each score 78 at the whole-point resolution routing "
        "compares, so no routing rule picks one. Before rounding: DocumentDB 78.20, "
        "DynamoDB 77.72."
    )
    a["decide_for_me_rule"] = "unrounded_score"
    a["decide_for_me_reason"] = "DocumentDB scores higher before rounding (78.20 against 77.72)."
    report["pending_engine_choices"] = [a]
    lines = pending_choices_md(report)
    md = "\n".join(lines)
    sub = md.split("\n### ", 1)[1]
    assert "78.20" in md.split("\n### ", 1)[0]  # the table row states it
    assert "78.20" not in sub and "77.72" not in sub
    assert "Before rounding:" not in md
    assert "no routing rule picks one" not in sub
    assert "- Decide for me: DocumentDB, the higher score before rounding." in lines
    # Each option keeps its own trade-off.
    assert any(ln.startswith("- DocumentDB: documentdb serves 10 queries") for ln in lines)


def test_the_unrounded_score_rule_is_the_routing_codes_constant() -> None:
    """The renderer cannot import agents (ADR-032), so it spells the rule; this pins
    the spelling to ``tie_break.DECIDED_BY_UNROUNDED_SCORE``."""
    from src.agents.referee.tie_break import DECIDED_BY_UNROUNDED_SCORE
    from src.report.engine_choices import _decide_line

    reason = "REASON"
    line = _decide_line(
        {
            "decide_for_me_engine": "documentdb",
            "decide_for_me_rule": DECIDED_BY_UNROUNDED_SCORE,
            "decide_for_me_reason": reason,
        }
    )
    assert line == "- Decide for me: DocumentDB, the higher score before rounding."


# Main prints the reason for every rule, so this guards behaviour it already has.
@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "rule",
    [r for r in (*DECIDED_BY_RULES, "", "a_rule_not_yet_known") if r != DECIDED_BY_UNROUNDED_SCORE],
)
def test_a_pick_keeps_its_reason_unless_the_unrounded_score_decided(
    report: dict[str, Any], rule: str
) -> None:
    a = _choice("a_table", "0.48", "{engine} serves 10 queries, so choosing it adds one")
    a["decide_for_me_rule"] = rule
    a["decide_for_me_reason"] = "The reason, with no pair."
    report["pending_engine_choices"] = [a]
    assert "- Decide for me: The reason, with no pair." in pending_choices_md(report)
