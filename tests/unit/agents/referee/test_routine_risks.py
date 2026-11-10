# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1938: the risk register raises the routines the pipeline could not carry automatically.

``facts.routines`` (``src.shared.routine_facts``) gives each routine behind a risk its
class. Per the maintainer's decisions on #1938 (2026-11-10):

- ``routines.migrate_yourself`` (HIGH): the routines the pipeline could not translate
  automatically are one to-do for the customer, worded from the facts ("we could not
  translate N routines automatically; migrate them yourself before wave N"). It holds no
  wave: the wave's line lists the to-do instead;
- ``routines.behaves_differently`` and ``routines.opted_out`` (MEDIUM);
- ``routines.carried`` (LOW): rewritten routines and routines with a check that could
  not run, one risk with the counts; the routines are listed in the facts only.

The texts count routines by kind and name none (the executive report prints them). A
report written with routine facts but before the rules gets the same risks at render
time.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from src.agents.referee.migration_risks import build_migration_risks
from src.shared.migration_risk_rules import (
    ROUTINE_RULES,
    RULES,
    backfill_report_risks,
    routine_risks,
    wave_gate_lines,
    wave_gate_strategies,
)
from src.shared.prose_style import find_style_hits
from src.shared.report_facts import compute_report_facts
from src.shared.routine_facts import ROUTINE_DETAILS_LISTED, ROUTINE_RISK_SEVERITY, routine_facts
from src.shared.wave_readiness import NON_HOLDING_RULES, wave_readiness
from tests.unit.shared.test_routine_facts import MIXED, _artifact, _fail, _row

MY = "aurora_mysql"
PG = "aurora_postgresql"
WAVES = [
    {"wave": 1, "engines": [PG], "moves_from": ["postgresql"], "tables": ["public.orders"]},
    {"wave": 2, "engines": ["dynamodb"], "moves_from": [PG], "tables": ["public.orders"]},
]
FACTS = routine_facts(MIXED, 1, WAVES)


def test_one_risk_per_class_with_its_severity_in_the_targets_wave() -> None:
    risks = routine_risks(FACTS)
    assert [(r["rule"], r["severity"], r["wave"]) for r in risks] == [
        ("routines.migrate_yourself", "HIGH", 1),
        ("routines.behaves_differently", "MEDIUM", 1),
        ("routines.opted_out", "MEDIUM", 1),
        ("routines.carried", "LOW", 1),
    ]
    assert [r["severity"] for r in risks] == list(ROUTINE_RISK_SEVERITY.values())
    assert set(ROUTINE_RULES.values()) <= set(RULES)
    assert all(r["description"].startswith(f"[{PG}] ") for r in risks)


def test_the_to_do_is_worded_from_the_facts_and_never_says_blocked() -> None:
    to_do = routine_risks(FACTS)[0]
    assert to_do["description"] == (
        f"[{PG}] We could not translate 5 routines automatically; migrate them yourself "
        "before wave 1. They are 4 functions, 1 trigger. This is a to-do and holds no wave. "
        "Why: a check failed (1); in a dependency cycle (2); names a routine that is not "
        "translated (2); its code could not be read (1); its code was rebuilt around a "
        "literal (1)."
    )
    assert to_do["to_do"] == "migrate 5 routines yourself"
    # The raw reason codes stay in the details, never the text.
    for code in ("dependency_cycle", "depends_on_needs_attention", "source_text_not_"):
        assert code not in to_do["description"]
        assert any(code in d for d in to_do["details"])
    assert to_do["mitigation"] == (
        "Migrate these routines yourself before wave 1, then check them again. See the Wave 1 "
        "gate in the roadmap."
    )
    for r in routine_risks(FACTS):
        assert not re.search(r"\bblock", r["description"] + r["mitigation"], re.I), r
        assert "cutover" not in r["description"] + r["mitigation"], r


def test_the_texts_count_by_kind_and_name_no_routine_or_table() -> None:
    names = {r.name for r in MIXED.routines} | {"orders", "public.orders"}
    for r in routine_risks(FACTS):
        for text in (r["description"], r["mitigation"]):
            for name in names:
                assert not re.search(rf"\b{re.escape(name)}\b", text), (name, text)
            body = re.sub(r"^\[[a-z_]+\] ", "", text)
            assert not find_style_hits(body), (text, find_style_hits(body))


def test_the_low_risk_rolls_up_with_counts_and_no_per_routine_detail() -> None:
    low = routine_risks(FACTS)[-1]
    assert low["description"] == (
        f"[{PG}] Wave 1: 2 routines are carried to Aurora PostgreSQL and need a check on the "
        "target: 1 has a safe header rewrite (the owner or DEFINER set to the target user, "
        "so it runs with that user's privileges); for 2, not every check could run."
    )
    assert low.get("details") is None
    assert "checked, not load-tested" not in low["description"]


def test_the_high_and_medium_details_name_each_routine_and_its_rules() -> None:
    to_do, differs, opted, _low = routine_risks(FACTS)
    assert to_do["details"] == [
        "function public.py_fn(): pg-language",
        "function public.f_a(): dependency_cycle, depends_on_needs_attention",
        "function public.f_b(): dependency_cycle, depends_on_needs_attention",
        "function public.secret(): not readable (source_text_not_collected)",
        "trigger public.orders.tg_audit: rebuilt around a literal (source_text_not_exact)",
        "pg-language: pg-language reason. Fix: pg-language fix.",
    ]
    assert to_do["affected_tables"] == ["public.orders"]
    assert differs["details"][0] == "function public.public_path(): pg-public-schema"
    assert opted["details"] == ["function public.opted_out(): source_text_not_collected"]


def test_a_long_list_of_routines_is_cut_and_counted() -> None:
    rows = [
        _row(f"public.f{i}()", "needs_attention", reasons=["pg-language"], checks=[_fail("pg-language")])
        for i in range(ROUTINE_DETAILS_LISTED + 3)
    ]  # fmt: skip
    (risk,) = routine_risks(routine_facts(_artifact(*rows), 1))
    listed = [d for d in risk["details"] if d.startswith("function ")]
    assert len(listed) == ROUTINE_DETAILS_LISTED
    assert risk["details"][ROUTINE_DETAILS_LISTED] == (
        "And 3 more: the engineering report lists every routine."
    )


def test_with_no_wave_the_texts_give_no_cutover_timing() -> None:
    """Maintainer rule: the only cutover sentence is "The cutover is your responsibility"."""
    facts = routine_facts(MIXED, 1, [{"wave": 1, "engines": [MY], "tables": []}])
    risks = routine_risks(facts)
    assert {r["wave"] for r in risks} == {None}
    assert risks[0]["mitigation"] == (
        "Migrate these routines yourself, then check them again; the cutover is your "
        "responsibility."
    )
    assert all(r["mitigation"].endswith("The cutover is your responsibility.") for r in risks[1:])
    for r in risks:
        text = re.sub(
            r"[Tt]he cutover is your responsibility", "", r["description"] + r["mitigation"]
        )
        assert "cutover" not in text, r
    numbered = [{**r, "risk_id": f"RISK-{i:03d}"} for i, r in enumerate(risks, start=1)]
    assert wave_gate_strategies(numbered) == [
        "Before cutover, close RISK-002, RISK-003 and RISK-004 (routines that behave "
        "differently, routines with no code collected and carried routines to check).",
        "To do: migrate 5 routines yourself (RISK-001); the cutover is your responsibility.",
    ]


def test_no_routine_facts_raise_none() -> None:
    assert routine_risks(None) == []
    assert routine_risks(routine_facts(_artifact(_row("public.plain()")), 1)) == []


def test_synthesis_raises_them_and_the_wave_gate_lists_them() -> None:
    risks = build_migration_risks(
        migration_waves=WAVES,
        query_assignments=[],
        source_queries=[],
        source_engine="postgresql",
        routines=FACTS,
    )
    assert [r["risk_id"] for r in risks if str(r["rule"]).startswith("routines.")] == [
        "RISK-001",
        "RISK-002",
        "RISK-003",
        "RISK-004",
    ]
    assert wave_gate_lines(risks)[1] == (
        "Close RISK-002, RISK-003 and RISK-004 before this gate (routines that behave "
        "differently, routines with no code collected and carried routines to check). "
        "To do before wave 1: migrate 5 routines yourself (RISK-001)."
    )


def test_the_gate_lists_the_to_do_as_a_to_do_and_other_risks_as_conditions() -> None:
    """The to-do is no condition of its wave's gate (review of #2093); every other rule
    keeps its "Close ... before this gate" text."""
    to_do = {**routine_risks(FACTS)[0], "risk_id": "RISK-007"}
    gap = {"risk_id": "RISK-008", "rule": "relational.gaps", "wave": 1}
    assert wave_gate_lines([to_do]) == {
        1: "To do before wave 1: migrate 5 routines yourself (RISK-007)."
    }
    assert wave_gate_lines([to_do, gap]) == {
        1: "Close RISK-008 before this gate (relational gaps). To do before wave 1: migrate "
        "5 routines yourself (RISK-007)."
    }
    assert "close" not in wave_gate_lines([to_do])[1].lower()


def test_the_to_do_holds_no_wave_but_another_high_risk_still_does() -> None:
    """The exemption is by rule id (``NON_HOLDING_RULES``), not by severity."""
    assert NON_HOLDING_RULES == {"routines.migrate_yourself"}
    wave = [{"wave": 1, "engines": [PG], "homogeneity": "homogeneous"}]
    to_do = {"risk_id": "RISK-001", "severity": "HIGH", "rule": "routines.migrate_yourself",
             "engine": PG, "wave": 1, "risk_type": "MIGRATION_COMPLEXITY"}  # fmt: skip
    (row,) = wave_readiness(
        wave,
        risks=[to_do],
        designed_engines=[PG],
        to_dos_by_wave={1: [str((FACTS or {})["migrate_yourself_line"])]},
    )
    assert (row["readiness"], row["holds"]) == ("suggested", [])
    assert row["to_dos"] == [
        "we could not translate 5 routines automatically; migrate them yourself before wave 1"
    ]
    assert row["readiness_line"] == (
        "Suggested: no check holds it. To do: we could not translate 5 routines "
        "automatically; migrate them yourself before wave 1."
    )
    assert row["readiness_line_short"] == row["readiness_line"]
    other = {**to_do, "risk_id": "RISK-002", "rule": "relational.gaps"}
    (held,) = wave_readiness(wave, risks=[to_do, other], designed_engines=[PG])
    assert held["readiness"] == "held"
    assert [h["risk_id"] for h in held["holds"]] == ["RISK-002"]


def _report(facts: Any = FACTS) -> dict[str, Any]:
    report: dict[str, Any] = {
        "contract_version": "1.90",
        "migration_waves": copy.deepcopy(WAVES),
        "source_table_count": 1,
        "risk_assessment": {
            "overall_risk_level": "LOW",
            "risks": [{"risk_id": "RISK-001", "severity": "LOW", "description": "[x] y"}],
            "mitigation_strategies": [],
        },
    }
    report["facts"] = compute_report_facts(report, routines=facts)
    return report


def test_the_facts_block_carries_the_routine_facts() -> None:
    assert _report()["facts"]["routines"] == FACTS
    assert _report(None)["facts"]["routines"] is None


def test_a_report_with_routine_facts_but_no_routine_risks_gets_them() -> None:
    old = _report()
    before = copy.deepcopy(old)
    new = backfill_report_risks(old)
    assert old == before
    risks = new["risk_assessment"]["risks"]
    assert [(r["risk_id"], r.get("rule")) for r in risks][:2] == [
        ("RISK-001", None),
        ("RISK-002", "routines.migrate_yourself"),
    ]
    assert len(risks) == 5 and new["risk_assessment"]["overall_risk_level"] == "HIGH"
    assert new["facts"]["open_risk_count"] == 5
    assert backfill_report_risks(new) is new


def test_a_report_written_before_the_routine_facts_is_unchanged() -> None:
    old = _report(None)
    del old["facts"]["routines"]
    assert backfill_report_risks(old) is old


def test_a_risk_with_no_to_do_dumps_as_it_did_before() -> None:
    """Every non-routine risk keeps the stored shape it had before ``Risk.to_do`` (1.91), so
    a report written before renders the same risks as a new synthesis."""
    from src.contracts.synthesis_output import Risk

    plain = {"severity": "LOW", "description": "[dynamodb] x", "rule": "design.trade_offs"}
    assert "to_do" not in Risk.model_validate(plain).model_dump(mode="json")
    to_do = Risk.model_validate({**plain, "to_do": "migrate 1 routine yourself"})
    assert to_do.model_dump(mode="json")["to_do"] == "migrate 1 routine yourself"
