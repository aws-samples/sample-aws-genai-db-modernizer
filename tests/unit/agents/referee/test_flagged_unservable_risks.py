"""#1148: the risk register raises the flagged queries no other kept engine takes.

``flagged_unservable`` (#888, #1033) lists the queries a review flagged that
stay on their engine: no other kept engine can take them (kind ``flagged``), or
a customer pin on their table's writes holds them there (kind ``held``). The
``routing.flagged`` rule raises one risk per engine from the facts layer's own
rows (``report_facts.flagged_unservable_rows``), so its counts agree with
``facts.unservable``; a report written before the rule gets the same risk at
render time (``backfill_report_risks``).
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from src.agents.referee.migration_risks import RULES, build_migration_risks
from src.shared.capability_labels import flag_redesign_hints
from src.shared.migration_risk_rules import (
    HELD_ONLY_SEVERITY,
    backfill_report_risks,
    flagged_unservable_risks,
    wave_gate_strategies,
)
from src.shared.prose_style import find_style_hits
from src.shared.report_facts import compute_report_facts, unservable_query_facts

AURORA = "aurora_mysql"
DDB = "dynamodb"
WAVES = [
    {"wave": 1, "engines": [AURORA], "homogeneity": "homogeneous", "tables": ["db.a"]},
    {"wave": 3, "engines": [DDB], "moves_from": [AURORA], "tables": ["db.b"]},
]
TABLES = {"f1": {"db.b"}, "f2": {"db.c"}, "h1": {"db.b"}}


def _row(qid: str, engine: str = DDB, cause: str = "no_kept_engine", category: str = "full_scan"):
    return {"query_id": qid, "engine": engine, "cause": cause, "reason_category": category}


def _risks(rows: list[dict[str, Any]], traces: dict[str, Any] | None = None) -> list[dict]:
    return flagged_unservable_risks(
        {"flagged_unservable": rows, "query_traces": traces or {}},
        {AURORA: 1, DDB: 3},
        TABLES,
    )


def test_a_flagged_query_no_other_engine_takes_is_high_in_its_engines_wave() -> None:
    [risk] = _risks([_row("f1"), _row("f2", category="client_side_join")])
    assert risk["rule"] == "routing.flagged" and RULES["routing.flagged"].severity == "HIGH"
    assert risk["severity"] == "HIGH"
    assert risk["wave"] == 3
    assert risk["query_ids"] == ["f1", "f2"]
    assert risk["affected_tables"] == ["db.b", "db.c"]
    assert risk["description"] == (
        "[dynamodb] 2 queries stay on DynamoDB although a review found DynamoDB cannot serve "
        "them, and no other engine the plan keeps can take them. Review reasons: it would "
        "need a client-side join (1); it would need a full scan (1)."
    )
    # The reason category's redesign hint, once per category, in the mitigation.
    for category in ("full_scan", "client_side_join"):
        assert flag_redesign_hints(DDB, category)[0] in risk["mitigation"]
    assert risk["mitigation"].startswith("Before the Wave 3 gate, redesign these queries")
    assert "pin" not in risk["mitigation"]


def test_a_query_held_by_a_pin_is_medium_and_names_the_pin() -> None:
    [risk] = _risks([_row("h1", cause="held_by_pin")])
    assert risk["severity"] == HELD_ONLY_SEVERITY == "MEDIUM"
    assert risk["description"] == (
        "[dynamodb] 1 query stays on DynamoDB although a review found DynamoDB cannot serve "
        "it, and a pin on its table's writes holds it there. Review reason: it would need a "
        "full scan."
    )
    assert "To move the held query, change the pin in the assignment review." in (
        risk["mitigation"]
    )


def test_flagged_and_held_queries_on_one_engine_are_one_high_risk() -> None:
    [risk] = _risks([_row("f1"), _row("h1", cause="held_by_pin")])
    assert risk["severity"] == "HIGH"
    assert ": for 1, no other engine the plan keeps can take it, and 1 is held there" in (
        risk["description"]
    )
    assert "change the pin" in risk["mitigation"]


def test_one_risk_per_engine_with_each_engines_wave() -> None:
    risks = _risks([_row("f1"), _row("a1", engine="aurora-mysql", cause="held_by_pin")])
    assert [(r["description"][:14], r["wave"], r["severity"]) for r in risks] == [
        ("[aurora_mysql]", 1, "MEDIUM"),
        ("[dynamodb] 1 q", 3, "HIGH"),
    ]


def test_no_wave_reads_as_cutover() -> None:
    [risk] = flagged_unservable_risks({"flagged_unservable": [_row("f1")]}, {}, {})
    assert risk["wave"] is None and risk["mitigation"].startswith("Before the cutover, ")


def test_no_risk_without_flagged_queries() -> None:
    assert _risks([]) == []
    assert flagged_unservable_risks({}, {}, {}) == []


def test_a_query_the_trace_already_lists_stays_with_the_unservable_rule() -> None:
    """The facts count a query once: a flagged row for a query
    ``query_traces.unservable`` lists is that query's ``routing.unservable`` risk."""
    traces = {"unservable": [{"query_id": "f1", "engine": DDB, "kind": "fallback"}]}
    assert _risks([_row("f1")], traces) == []
    risks = build_migration_risks(
        migration_waves=WAVES,
        query_assignments=[],
        source_queries=[],
        query_traces=traces,
        flagged_unservable=[_row("f1"), _row("f2")],
    )
    assert [(r["rule"], r["query_ids"]) for r in risks] == [
        ("routing.unservable", ["f1"]),
        ("routing.flagged", ["f2"]),
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [_row("f1")],
        [_row("h1", cause="held_by_pin")],
        [_row("f1"), _row("f2", engine=AURORA), _row("h1", cause="held_by_pin")],
    ],
)
def test_the_counts_agree_with_the_unservable_facts(rows: list[dict[str, Any]]) -> None:
    report = {"flagged_unservable": rows, "query_traces": {"unservable": []}}
    facts = unservable_query_facts(report)
    assert facts is not None
    risks = _risks(rows)
    assert sum(len(r["query_ids"]) for r in risks) == (
        facts["flagged_query_count"] + facts["held_query_count"]
    )
    held = {r["query_id"] for r in rows if r["cause"] == "held_by_pin"}
    for r in risks:
        only_held = set(r["query_ids"]) <= held
        assert r["severity"] == ("MEDIUM" if only_held else "HIGH")


def test_synthesis_passes_the_rows_and_the_wording_is_plain() -> None:
    risks = build_migration_risks(
        migration_waves=WAVES,
        query_assignments=[],
        source_queries=[],
        flagged_unservable=[_row("f1"), _row("h1", cause="held_by_pin", category="other")],
        start_id=4,
    )
    [risk] = risks
    assert risk["risk_id"] == "RISK-005"
    for text in (risk["description"], risk["mitigation"], *wave_gate_strategies(risks)):
        body = re.sub(r"^\[[a-z_]+\] ", "", text)
        assert not find_style_hits(body), (text, find_style_hits(body))
    assert "flagged queries no other engine takes" in wave_gate_strategies(risks)[0]


# ---------------------------------------------------------------------------
# A report written before the rule (render time)
# ---------------------------------------------------------------------------


def _old_report() -> dict[str, Any]:
    """A 1.40 report: one analysis risk, one sync risk and its gate line, the
    post-wave check, and a flagged and a held query the register never raised."""
    report: dict[str, Any] = {
        "contract_version": "1.40",
        "migration_waves": WAVES,
        "query_groups": [
            {
                "source_queries": [
                    {"query_id": "f1", "tables_accessed": ["db.b"]},
                    {"query_id": "h1", "tables_accessed": ["db.b"]},
                ]
            }
        ],
        "flagged_unservable": [_row("f1"), _row("h1", engine=AURORA, cause="held_by_pin")],
        "query_traces": {"unservable": []},
        "risk_assessment": {
            "overall_risk_level": "MEDIUM",
            "risks": [
                {"risk_id": "RISK-001", "severity": "LOW", "description": "[dynamodb] x"},
                {
                    "risk_id": "RISK-002",
                    "severity": "MEDIUM",
                    "description": "Wave 3: sync",
                    "rule": "sync.wave",
                    "wave": 3,
                    "affected_tables": ["db.b"],
                    "query_ids": ["r1"],
                    "mitigation": "m",
                },
            ],
            "mitigation_strategies": [
                "Analysis line.",
                "Before the Wave 3 gate, close RISK-002 (tables kept in sync).",
                "After wave 3 (DynamoDB): compare the latency.",
            ],
        },
    }
    report["facts"] = compute_report_facts(report)
    return report


def test_an_old_report_gets_the_risk_its_facts_imply() -> None:
    old = _old_report()
    before = copy.deepcopy(old)
    new = backfill_report_risks(old)
    assert old == before, "the stored report is not changed in place"
    risks = new["risk_assessment"]["risks"]
    assert [r["risk_id"] for r in risks] == ["RISK-001", "RISK-002", "RISK-003", "RISK-004"]
    added = risks[2:]
    assert [(r["rule"], r["wave"], r["severity"]) for r in added] == [
        ("routing.flagged", 1, "MEDIUM"),
        ("routing.flagged", 3, "HIGH"),
    ]
    assert added[1]["affected_tables"] == ["db.b"]
    assert new["risk_assessment"]["overall_risk_level"] == "HIGH"
    assert new["risk_assessment"]["mitigation_strategies"] == [
        "Analysis line.",
        "Before the Wave 1 gate, close RISK-003 (flagged queries no other engine takes).",
        "Before the Wave 3 gate, close RISK-002 and RISK-004 (tables kept in sync and flagged "
        "queries no other engine takes).",
        "After wave 3 (DynamoDB): compare the latency.",
    ]
    # The facts block counts them, as a new synthesis would.
    facts = new["facts"]
    assert facts["open_risk_count"] == 4 and old["facts"]["open_risk_count"] == 2
    assert facts["open_risks_by_severity"] == {"HIGH": 1, "MEDIUM": 2, "LOW": 1}
    assert facts["critical_high_risks_by_engine"] == {DDB: 1}
    assert [w["critical_high_risks"] for w in facts["waves"]] == [0, 1]
    assert facts == compute_report_facts(new)


def test_a_report_that_carries_the_rule_or_raises_none_is_unchanged() -> None:
    new = backfill_report_risks(_old_report())
    assert backfill_report_risks(new) is new
    quiet = _old_report()
    quiet["flagged_unservable"] = []
    assert backfill_report_risks(quiet) is quiet
    assert backfill_report_risks({"flagged_unservable": [_row("f1")]}) == {
        "flagged_unservable": [_row("f1")]
    }


def test_a_report_from_before_the_wave_gate_lines_gets_them_before_the_wave_checks() -> None:
    old = _old_report()
    old["risk_assessment"]["risks"] = old["risk_assessment"]["risks"][:1]
    old["risk_assessment"]["mitigation_strategies"] = [
        "Analysis line.",
        "After wave 3 (DynamoDB): compare the latency.",
    ]
    old["risk_assessment"]["overall_risk_level"] = "LOW"
    new = backfill_report_risks(old)
    assert new["risk_assessment"]["mitigation_strategies"] == [
        "Analysis line.",
        "Before the Wave 1 gate, close RISK-002 (flagged queries no other engine takes).",
        "Before the Wave 3 gate, close RISK-003 (flagged queries no other engine takes).",
        "After wave 3 (DynamoDB): compare the latency.",
    ]
    assert new["risk_assessment"]["overall_risk_level"] == "HIGH"


def test_the_overall_level_never_falls() -> None:
    old = _old_report()
    old["flagged_unservable"] = [_row("h1", engine=AURORA, cause="held_by_pin")]
    old["risk_assessment"]["overall_risk_level"] = "CRITICAL"
    assert backfill_report_risks(old)["risk_assessment"]["overall_risk_level"] == "CRITICAL"


def test_a_rows_own_tables_win_over_the_fallback() -> None:
    row = {**_row("f1"), "source_tables": ["db.own"]}
    [risk] = _risks([row])
    assert risk["affected_tables"] == ["db.own"]
    [old] = _risks([_row("f1")])
    assert old["affected_tables"] == ["db.b"]
