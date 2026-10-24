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

from src.report.engine_choices import pending_choices_md
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


def test_pending_choices_with_the_same_tie_sentence_state_it_once(report: dict[str, Any]) -> None:
    tie = "DocumentDB and DynamoDB each score 78 at the whole-point resolution routing compares"
    a, b = (
        _choice("a_table", "0.48", "{engine} serves 10 queries, so choosing it adds one"),
        _choice("b_table", "0.05", "{engine} serves 20 queries, so choosing it adds one"),
    )
    a["why_tied"] = f"{tie}, so no rule picks one. Before rounding: DocumentDB 78.20."
    b["why_tied"] = f"{tie}, so no rule picks one. Before rounding: DynamoDB 78.95."
    report["pending_engine_choices"] = [a, b]
    md = "\n".join(pending_choices_md(report))
    assert repeated_statements(md) == []
    assert md.count(tie) == 1
    assert "The tie is the same as for `a_table`. Before rounding: DynamoDB 78.95." in md
    assert "Before rounding: DocumentDB 78.20." in md
