"""The engineering report lists each item once across its sections (#1220).

#1410 and #1454 removed repeated paragraphs and sentences. What was left were items
one section lists and another lists again in its own words: a CRITICAL or HIGH
risk's mitigation in both the Risk register and Mitigation strategies, the close
calls' engine pairs in both the register and the Close calls table, an open tie in
both Pending engine choices and the appendix (three times inside its entry), the
reality check's reason in each consolidation step the appendix already points to
Migration trade-offs for, and the reality check's scores in an Aurora-by-table row
the appendix gives per query. Each is now stated where it belongs and referred to
elsewhere; the executive report and the UI read the same ``report.json`` and keep
their own text.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.synthesis_report import _clean_relational_reason
from src.report.engineering_report import (
    _close_call_body,
    _mitigation_strategies_md,
    _table_reason_cell,
)
from src.report.query_traces import _trace_md
from src.report.renderers import render_engineering_report_md
from src.shared.design_text import (
    close_call_description,
    severe_risk_strategy,
    split_close_call_description,
    split_severe_risk_strategy,
)
from tests.report_concision import cross_section_repeats, section

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture
def trailgear() -> dict[str, Any]:
    return _load("trailgear_1_41_report.json")


# --- the shared texts synthesis writes and the report reads back -------------------


def test_a_close_call_description_reads_back_into_its_parts() -> None:
    text = close_call_description(
        "Wave 3: 19 queries were decided by less than 5 points",
        [
            "Aurora MySQL keeps 14 close calls and 5 ties over DynamoDB",
            "DynamoDB takes 5 close calls over Aurora MySQL",
        ],
        "them",
    )
    assert text == (
        "Wave 3: 19 queries were decided by less than 5 points: Aurora MySQL keeps 14 close "
        "calls and 5 ties over DynamoDB; DynamoDB takes 5 close calls over Aurora MySQL. A "
        "small change in the scores moves them."
    )
    assert split_close_call_description(text) == (
        "Wave 3: 19 queries were decided by less than 5 points",
        "Aurora MySQL keeps 14 close calls and 5 ties over DynamoDB; DynamoDB takes 5 close "
        "calls over Aurora MySQL",
        "A small change in the scores moves them.",
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        # The wording before #1410 names no "keeps" or "takes": left as it is.
        "Wave 3: 21 queries were decided by less than 5 points: 13 go to DynamoDB over Aurora "
        "PostgreSQL and 8 stay on Aurora PostgreSQL over DynamoDB. A small change in the "
        "scores moves them.",
        "Wave 3: 52 reads run on a copy of 13 tables that CDC keeps in sync.",
    ],
)
def test_any_other_text_is_not_a_close_call_description(text: str) -> None:
    assert split_close_call_description(text) is None


def test_a_severe_risk_strategy_reads_back_into_its_parts() -> None:
    text = severe_risk_strategy("RISK-001", "HIGH, DynamoDB", "t.a, t.b and 1 more", "Do it.")
    assert text == "RISK-001 (HIGH, DynamoDB; t.a, t.b and 1 more): Do it."
    assert split_severe_risk_strategy(text) == (
        "RISK-001",
        "RISK-001 (HIGH, DynamoDB; t.a, t.b and 1 more)",
        "Do it.",
    )
    assert split_severe_risk_strategy("Before the Wave 3 gate, close RISK-002.") is None


# --- Mitigation strategies and the Risk register -------------------------------------


def test_a_high_risk_strategy_refers_to_its_register_entry(trailgear) -> None:
    md = render_engineering_report_md(trailgear)
    mitigation = "Cover the 1 remaining query in the DynamoDB schema design or route it"
    register, strategies = section(md, "Risk register"), section(md, "Mitigation strategies")
    assert mitigation in register
    assert mitigation not in strategies
    assert (
        "- RISK-001 \\(HIGH, DynamoDB; trailgear.UserSession\\): its mitigation is under its "
        "Risk register entry." in strategies.splitlines()
    )
    assert cross_section_repeats(md)["severe_mitigation"] == 0


def test_a_strategy_with_its_own_text_is_printed_whole(trailgear) -> None:
    """A severe risk with no mitigation recorded, or one whose mitigation differs from
    the strategy, keeps the strategy's text: the register does not say it."""
    risks = trailgear["risk_assessment"]["risks"]
    risk = next(r for r in risks if r["risk_id"] == "RISK-001")
    for mitigation in (None, "Something else entirely."):
        report = copy.deepcopy(trailgear)
        changed = next(r for r in report["risk_assessment"]["risks"] if r["risk_id"] == "RISK-001")
        changed["mitigation"] = mitigation
        lines = _mitigation_strategies_md(report)
        assert any(risk["mitigation"] in line for line in lines), mitigation
        assert not any("its mitigation is under" in line for line in lines)


def test_a_strategy_for_a_risk_not_in_the_register_is_printed_whole(trailgear) -> None:
    report = copy.deepcopy(trailgear)
    strategy = severe_risk_strategy("RISK-099", "HIGH, DynamoDB", "t.x", "Do the thing.")
    report["risk_assessment"]["mitigation_strategies"] = [strategy]
    assert (
        _mitigation_strategies_md(report)[2]
        == "- RISK-099 \\(HIGH, DynamoDB; t.x\\): Do the thing."
    )


# --- the close-call risk and the Close calls table ----------------------------------

_PAIRS = [
    "DynamoDB takes 12 close calls over Aurora PostgreSQL",
    "Aurora PostgreSQL keeps 9 close calls over DynamoDB",
]
_LEAD = "Wave 3: 21 queries were decided by less than 5 points"


def _with_current_close_call_text(report: dict[str, Any]) -> dict[str, Any]:
    report = copy.deepcopy(report)
    risk = next(r for r in report["risk_assessment"]["risks"] if r["rule"] == "routing.close_calls")
    risk["description"] = "[dynamodb] " + close_call_description(_LEAD, _PAIRS, "them")
    return report


def test_the_close_call_risk_names_the_table_instead_of_its_pairs(trailgear) -> None:
    md = render_engineering_report_md(_with_current_close_call_text(trailgear))
    register = section(md, "Risk register")
    assert (
        f"{_LEAD}, listed by engine pair under Close calls. A small change in the scores "
        "moves them." in register
    )
    for pair in _PAIRS:
        assert pair not in register
    # The table it names is printed, above the register.
    assert md.index("## Close calls") < md.index("## Risk register")
    assert cross_section_repeats(md)["close_call_pairs"] == 0


def test_without_a_close_calls_section_the_pairs_stay() -> None:
    risk = {"rule": "routing.close_calls"}
    body = close_call_description(_LEAD, _PAIRS, "them")
    assert _close_call_body(body, risk, listed=False) == body
    # Another rule's text is never cut, even one shaped the same way.
    assert _close_call_body(body, {"rule": "sync.wave"}, listed=True) == body


def test_a_report_with_the_old_wording_keeps_its_text(trailgear) -> None:
    risk = next(
        r for r in trailgear["risk_assessment"]["risks"] if r["rule"] == "routing.close_calls"
    )
    old = risk["description"].removeprefix("[dynamodb] ")
    assert old in section(render_engineering_report_md(trailgear), "Risk register")


# --- an open tie in the appendix and in Pending engine choices -----------------------

_TIE_SENTENCE = (
    "DynamoDB tied with DocumentDB at 78; the engine already serving more of the settled "
    "workload holds it until the customer chooses."
)


def _tie_trace() -> dict[str, Any]:
    first = {
        "step": 1,
        "version": 1,
        "rule": "tie held for the customer's choice",
        "reason_code": "tie_placeholder",
        "to_engine": "dynamodb",
        "to_engine_name": "DynamoDB",
        "candidates": [
            {"engine": "documentdb", "engine_name": "DocumentDB", "score": 78.0},
            {"engine": "dynamodb", "engine_name": "DynamoDB", "score": 78.0},
        ],
        "explanation": _TIE_SENTENCE,
    }
    return {
        "query_id": "q1",
        "first_assignment": first,
        "moves": [],
        "final_engine": "dynamodb",
        "final_engine_name": "DynamoDB",
        "final_version": 1,
        "final_score": 78.0,
        "tied_engines": ["documentdb", "dynamodb"],
        "tied_engine_names": ["DocumentDB", "DynamoDB"],
        "runner_up": {
            "engine": "documentdb",
            "score": 78.0,
            "final_score": 78.0,
            "gap": 0.0,
            "explanation": "DocumentDB tied with DynamoDB at 78; the engine already serving "
            "more of the settled workload holds it until the customer chooses.",
        },
        "source_tables": ["shop.email_logs"],
    }


_PENDING = {"pending_engine_choices": [{"table": "shop.email_logs", "query_ids": ["q1"]}]}


def test_an_open_tie_names_its_pending_choice_once() -> None:
    lines = _trace_md(_tie_trace(), {"open_tie": True}, None, _PENDING)
    text = "\n".join(lines)
    assert text.count("holds it until the customer chooses") == 1  # the step only
    assert not any(line.startswith("- Runner-up:") for line in lines)
    assert (
        "- Unresolved tie: DocumentDB, DynamoDB at 78; the choice is under Pending engine "
        "choices (`email_logs`)." in lines
    )


def test_a_group_entry_names_every_table_its_queries_are_pending_under() -> None:
    pending = {
        "pending_engine_choices": [
            {"table": "shop.a", "query_ids": ["q1"]},
            {"table": "shop.b", "query_ids": ["q9"]},
            {"table": "shop.c", "query_ids": ["q2"]},
        ]
    }
    group = {"query_ids": ["q1", "q2"], "query_count": 2, "short_ids": ["q1", "q2"]}
    lines = _trace_md(_tie_trace(), {"open_tie": True}, group, pending)
    assert lines[-2].endswith("the choice is under Pending engine choices (`a`, `c`).")


def test_an_open_tie_no_pending_choice_holds_keeps_its_lines() -> None:
    """A report with no pending choices (written before #1003) or a tie the section does
    not list keeps the runner-up and the placeholder sentence: nothing else states it."""
    for report in ({}, {"pending_engine_choices": [{"table": "t", "query_ids": ["q7"]}]}):
        lines = _trace_md(_tie_trace(), {"open_tie": True}, None, report)
        assert any(line.startswith("- Runner-up: DocumentDB tied") for line in lines)
        assert lines[-2] == (
            "- Unresolved tie: DocumentDB, DynamoDB; the query is listed under the final "
            "engine pending the customer's choice."
        )


def test_a_runner_up_that_is_not_the_tie_partner_stays() -> None:
    trace = _tie_trace()
    trace["runner_up"] = {
        "engine": "aurora_mysql",
        "score": 70.0,
        "final_score": 78.0,
        "gap": 8.0,
        "explanation": "Aurora MySQL was next at 70, 8 points behind DynamoDB \\(78\\).",
    }
    lines = _trace_md(trace, {"open_tie": True}, None, _PENDING)
    assert any(line.startswith("- Runner-up: Aurora MySQL was next") for line in lines)


def test_a_settled_tie_is_not_pointed_at_pending_choices() -> None:
    trace = _tie_trace()
    trace["tied_engines"], trace["tied_engine_names"] = [], []
    lines = _trace_md(trace, {"open_tie": False, "tied": True}, None, _PENDING)
    assert not any("Pending engine choices" in line for line in lines)
    assert any(line.startswith("- Runner-up:") for line in lines)


# --- Queries on Aurora by table -----------------------------------------------------


def test_a_consolidation_row_names_the_move_without_its_scores() -> None:
    cell = _table_reason_cell(
        [
            "reality check: consolidated from DocumentDB → Aurora MySQL (no unique value: "
            "DocumentDB scores 80.0, Aurora MySQL scores 76.0)"
        ]
    )
    assert cell == "reality check: consolidated from DocumentDB → Aurora MySQL"


def test_another_single_reason_keeps_its_detail() -> None:
    reason = (
        "highest fit for Aurora MySQL (scores tie with DocumentDB; Aurora MySQL is the "
        "source-compatible relational engine)"
    )
    assert _table_reason_cell([reason]) == reason


@pytest.mark.parametrize(
    ("which", "shown"),
    [("source-compatible", "source-compatible"), ("chosen", "chosen relational")],
)
def test_the_utility_pin_reads_as_a_sentence(which: str, shown: str) -> None:
    reason = f"utility/metadata statement, kept on the {which} relational engine (Aurora MySQL)"
    assert _clean_relational_reason(reason) == (
        f"administration or metadata statement, kept on Aurora MySQL as the {shown} engine"
    )


def test_other_reasons_are_not_reworded() -> None:
    assert _clean_relational_reason("highest fit for Aurora MySQL") == (
        "highest fit for Aurora MySQL"
    )
