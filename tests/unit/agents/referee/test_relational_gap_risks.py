"""#1149: the risk register raises the relational move's version and feature gaps.

``facts.relational_gaps`` (#471, #1079) lists each gap the relational move to
wave 1's Aurora engine carries, with its impact. The ``relational.gaps`` rule
raises one Wave 1 risk per impact: a gap that blocks the move HIGH, one that
needs a rewrite MEDIUM, one that behaves differently LOW, an informational one
none. It raises nothing when the gap check's target is not wave 1's engine (the
rule #1145 applies to wave 1's note). A report written before the rule gets the
same risks at render time (``backfill_report_risks``).
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from src.agents.referee.migration_risks import build_migration_risks
from src.shared.migration_risk_rules import (
    GAP_RISK_SEVERITY,
    LISTED_ITEMS,
    backfill_report_risks,
    relational_gap_risks,
    wave_gate_strategies,
)
from src.shared.prose_style import find_style_hits
from src.shared.report_facts import compute_report_facts

PG = "aurora_postgresql"
WAVE_1 = {
    "wave": 1,
    "engines": [PG],
    "homogeneity": "cross_engine",
    "moves_from": ["sqlserver"],
    "tables": ["db.a", "db.b", "db.c"],
}
WAVES = [WAVE_1, {"wave": 2, "engines": ["dynamodb"], "moves_from": [PG], "tables": ["db.a"]}]


def _gap(feature: str, impact: str, tables=("db.a",), queries=("q1",), objects=()) -> dict:
    return {
        "rule_id": feature.lower().replace(" ", "-"),
        "feature": feature,
        "impact": impact,
        "reason": f"{feature} changes on the target.",
        "docs": "Migration Playbook",
        "tables": list(tables),
        "queries": list(queries),
        "objects": list(objects),
    }


def _check(*gaps: dict, target: str = PG) -> dict[str, Any]:
    by_impact = {i: 0 for i in ("blocks_move", "needs_rewrite", "behaves_differently")}
    by_impact["informational"] = 0
    for g in gaps:
        by_impact[g["impact"]] += 1
    return {
        "source_engine": "sqlserver",
        "target_engine": target,
        "gaps": list(gaps),
        "gaps_by_impact": by_impact,
    }


ALL = _check(
    _gap("Linked servers", "blocks_move", tables=("db.b",), queries=()),
    _gap("TOP row limits", "needs_rewrite", queries=("q1", "q2")),
    _gap("T-SQL date and time functions", "needs_rewrite", tables=("db.c", "unknown")),
    _gap("Case-insensitive text comparison", "behaves_differently"),
    _gap("Babelfish for Aurora PostgreSQL", "informational"),
)


def test_one_wave_1_risk_per_impact_with_its_severity() -> None:
    risks = relational_gap_risks(ALL, WAVES)
    assert [(r["severity"], r["wave"], len(r["details"])) for r in risks] == [
        ("HIGH", 1, 1),
        ("MEDIUM", 1, 2),
        ("MEDIUM", 1, 1),
    ]
    # A gap that behaves differently changes results without an error: MEDIUM, like a
    # rewrite (maintainer decision on #1204).
    assert GAP_RISK_SEVERITY == {
        "blocks_move": "HIGH",
        "needs_rewrite": "MEDIUM",
        "behaves_differently": "MEDIUM",
    }
    assert all(r["rule"] == "relational.gaps" for r in risks)
    blocks, rewrite, differs = risks
    assert blocks["description"] == (
        "[aurora_postgresql] Wave 1: 1 relational gap blocks the move to Aurora PostgreSQL: "
        "Linked servers."
    )
    assert blocks["mitigation"].startswith("Before the Wave 1 gate, replace this feature ")
    assert rewrite["description"].startswith(
        "[aurora_postgresql] Wave 1: 2 relational gaps need a rewrite before cutover to Aurora "
        "PostgreSQL: TOP row limits; T-SQL date and time functions."
    )
    assert "test them on Aurora PostgreSQL" in rewrite["mitigation"]
    assert "compare their results with the source" in differs["mitigation"]
    # The evidence: the gaps' queries, and their tables wave 1 moves ("unknown" is not one).
    assert rewrite["query_ids"] == ["q1", "q2"]
    assert rewrite["affected_tables"] == ["db.a", "db.c"]
    assert blocks["affected_tables"] == ["db.b"] and blocks["query_ids"] == []


def test_the_details_carry_each_gaps_reason_documentation_and_objects() -> None:
    gap = _gap("T-SQL triggers", "needs_rewrite", objects=[{"kind": "trigger", "id": "trg_a"}])
    [risk] = relational_gap_risks(_check(gap), WAVES)
    assert risk["details"] == [
        "T-SQL triggers: T-SQL triggers changes on the target. Documentation: Migration "
        "Playbook. Found in trigger trg_a."
    ]
    assert "trg_a" not in risk["description"] and "Playbook" not in risk["description"]


def test_an_informational_gap_raises_no_risk() -> None:
    assert relational_gap_risks(_check(_gap("Babelfish", "informational")), WAVES) == []
    assert relational_gap_risks(_check(), WAVES) == []


@pytest.mark.parametrize(
    "waves",
    [
        [{**WAVE_1, "engines": ["aurora_mysql"]}],
        [{**WAVE_1, "wave": 2}],
        [{**WAVE_1, "engines": []}],
        [],
        None,
    ],
)
def test_a_gap_check_for_another_engine_than_wave_1s_raises_none(waves) -> None:
    """Same rule as wave 1's note (#1145): the gaps belong to wave 1's own engine."""
    assert relational_gap_risks(ALL, waves) == []


def test_no_gap_check_raises_none() -> None:
    assert relational_gap_risks(None, WAVES) == []
    assert relational_gap_risks({"target_engine": PG}, WAVES) == []


def test_a_long_gap_list_names_the_first_features_then_counts() -> None:
    gaps = [_gap(f"Feature {i}", "behaves_differently") for i in range(LISTED_ITEMS + 2)]
    [risk] = relational_gap_risks(_check(*gaps), WAVES)
    assert "Feature 4; and 2 more." in risk["description"]
    assert "Feature 5" not in risk["description"]
    assert len(risk["details"]) == LISTED_ITEMS + 2


def test_synthesis_raises_them_last_and_the_wording_is_plain() -> None:
    risks = build_migration_risks(
        migration_waves=WAVES,
        query_assignments=[],
        source_queries=[],
        source_engine="sqlserver",
        flagged_unservable=[
            {"query_id": "f1", "engine": PG, "cause": "no_kept_engine", "reason_category": "other"}
        ],
        relational_gaps=ALL,
    )
    assert [(r["rule"], r["risk_id"]) for r in risks] == [
        ("routing.flagged", "RISK-001"),
        ("relational.gaps", "RISK-002"),
        ("relational.gaps", "RISK-003"),
        ("relational.gaps", "RISK-004"),
    ]
    lines = wave_gate_strategies(risks)
    assert lines == [
        "Before the Wave 1 gate, close RISK-001, RISK-002, RISK-003 and RISK-004 (flagged "
        "queries no other engine takes and relational gaps)."
    ]
    for r in risks:
        if r["rule"] != "relational.gaps":
            continue
        for text in (r["description"], r["mitigation"]):
            body = re.sub(r"^\[[a-z_]+\] ", "", text)
            assert not find_style_hits(body), (text, find_style_hits(body))


# ---------------------------------------------------------------------------
# A report written before the rule (render time)
# ---------------------------------------------------------------------------


def _old_report() -> dict[str, Any]:
    report: dict[str, Any] = {
        "contract_version": "1.42",
        "migration_waves": WAVES,
        "source_table_count": 3,
        "risk_assessment": {
            "overall_risk_level": "LOW",
            "risks": [{"risk_id": "RISK-001", "severity": "LOW", "description": "[x] y"}],
            "mitigation_strategies": ["After wave 1 (Aurora PostgreSQL): compare the latency."],
        },
    }
    report["facts"] = compute_report_facts(report, relational_gaps=ALL)
    return report


def test_an_old_report_gets_the_gap_risks_its_facts_list() -> None:
    old = _old_report()
    before = copy.deepcopy(old)
    new = backfill_report_risks(old)
    assert old == before
    risks = new["risk_assessment"]["risks"]
    assert [(r["risk_id"], r.get("rule"), r["severity"]) for r in risks] == [
        ("RISK-001", None, "LOW"),
        ("RISK-002", "relational.gaps", "HIGH"),
        ("RISK-003", "relational.gaps", "MEDIUM"),
        ("RISK-004", "relational.gaps", "MEDIUM"),
    ]
    assert new["risk_assessment"]["overall_risk_level"] == "HIGH"
    assert new["risk_assessment"]["mitigation_strategies"] == [
        "Before the Wave 1 gate, close RISK-002, RISK-003 and RISK-004 (relational gaps).",
        "After wave 1 (Aurora PostgreSQL): compare the latency.",
    ]
    assert new["facts"]["open_risk_count"] == 4
    assert new["facts"]["open_risks_by_severity"] == {"HIGH": 1, "MEDIUM": 2, "LOW": 1}
    assert new["facts"]["waves"][0]["critical_high_risks"] == 1
    assert new["facts"] == compute_report_facts(new, relational_gaps=ALL)
    assert backfill_report_risks(new) is new


def test_a_report_with_no_gap_check_is_unchanged() -> None:
    old = _old_report()
    old["facts"]["relational_gaps"] = None
    assert backfill_report_risks(old) is old


def test_a_same_engine_version_gap_is_a_wave_1_risk_too() -> None:
    """MySQL 5.7 to Aurora MySQL 8.0: a homogeneous wave 1 moves the source onto Aurora,
    so its version gaps are Wave 1 risks (wave 1's note lists gaps only for a
    cross-engine wave 1, #1145; the register covers both)."""
    wave1 = {
        "wave": 1,
        "engines": ["aurora_mysql"],
        "homogeneity": "homogeneous",
        "moves_from": ["mysql"],
        "tables": ["db.a"],
    }
    check = {
        "source_engine": "mysql",
        "source_line": "5.7",
        "target_engine": "aurora_mysql",
        "target_lines": ["8.0", "8.4"],
        "version_checked": True,
        "gaps": [
            _gap("MySQL 5.7 major version", "blocks_move", tables=(), queries=()),
            _gap("MyISAM tables", "needs_rewrite", queries=()),
        ],
    }
    blocks, rewrite = relational_gap_risks(check, [wave1])
    assert (blocks["severity"], blocks["wave"], blocks["description"]) == (
        "HIGH",
        1,
        "[aurora_mysql] Wave 1: 1 relational gap blocks the move to Aurora MySQL: MySQL 5.7 "
        "major version.",
    )
    assert (rewrite["severity"], rewrite["affected_tables"]) == ("MEDIUM", ["db.a"])
