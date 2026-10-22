"""#1328: every deliverable states the customer's engine scope, from facts.engine_scope.

The figure is computed once at synthesis (ADR-032); the engineering report and the
executive report only format it. A report written before the field, or for a job
with no scope, states nothing.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.engineering_report import render_engineering_report_md
from src.report.executive_report import build_story
from src.report.facts import report_facts
from src.shared.contract_versioning import load_contract
from src.shared.report_facts import compute_report_facts

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"
STATEMENT = "Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)"


def _old() -> dict:
    report: dict = json.loads(FIXTURE.read_text())
    assert "engine_scope" not in json.dumps(report)  # written before #1328
    return report


def _scoped() -> dict:
    report = copy.deepcopy(_old())
    report["facts"] = compute_report_facts(report, engine_scope=["dynamodb", "aurora_postgresql"])
    return report


def _story_text(report: dict) -> str:
    return " ".join(getattr(f, "text", "") or "" for f in build_story(report))


def test_a_report_written_before_loads_with_no_scope_and_states_none() -> None:
    old = _old()
    loaded = load_contract(SynthesisOutputContract, old, artifact="synthesis/v1/report.json")
    assert loaded.facts is None or loaded.facts.engine_scope is None
    # A facts block written before the field (no engine_scope key) loads as None too.
    with_facts = copy.deepcopy(old)
    with_facts["facts"] = compute_report_facts(with_facts)
    del with_facts["facts"]["engine_scope"]
    reloaded = load_contract(
        SynthesisOutputContract, with_facts, artifact="synthesis/v1/report.json"
    )
    assert reloaded.facts is not None and reloaded.facts.engine_scope is None
    assert report_facts(old).get("engine_scope") is None
    assert "Engines in scope" not in render_engineering_report_md(with_facts)
    assert "Engines in scope" not in render_engineering_report_md(old)
    assert "Engines in scope" not in _story_text(old)


def test_both_reports_state_the_scope_from_the_facts() -> None:
    scoped = _scoped()
    assert f"{STATEMENT}." in render_engineering_report_md(scoped)
    assert STATEMENT in _story_text(scoped)


def test_synthesis_computes_the_scope_once_in_display_order() -> None:
    old = _old()
    facts = compute_report_facts(old, engine_scope=["dynamodb", "aurora_postgresql"])
    assert facts["engine_scope"] == ["aurora_postgresql", "dynamodb"]
    assert compute_report_facts(old)["engine_scope"] is None


def test_the_engineering_line_escapes_a_name_from_a_loaded_report() -> None:
    scoped = _scoped()
    scoped["facts"]["engine_scope"] = ["dynamodb", "<b>*x*</b>"]
    text = render_engineering_report_md(scoped)
    line = next(x for x in text.splitlines() if x.startswith("Engines in scope:"))
    assert "<b>" not in line and "*x*" not in line
    assert line.startswith("Engines in scope: DynamoDB, ")
